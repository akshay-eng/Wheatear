"""Agent Liftoff CLI entrypoint.

`agent_liftoff migrate` runs the full pipeline end to end. Per-stage debugging
commands (normalize/map/translate/validate as independently invocable steps
with IR persisted to disk between them) are a natural fast-follow once a
corridor is validated against real data -- not built speculatively now.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import click

from agent_liftoff.connectors.copilot_studio.importer import detect_format
from agent_liftoff.connectors.registry import load_exporter, load_importer
from agent_liftoff.corridors import SUPPORTED_CORRIDORS
from agent_liftoff.errors import LiftoffError
from agent_liftoff.eval.generate_cases import generate_cases
from agent_liftoff.llm.factory import build_provider
from agent_liftoff.pipeline.map import map_agent
from agent_liftoff.pipeline.translate import deterministic_instructions, translate_agent
from agent_liftoff.pipeline.validate import validate_agent


def _build_provider(provider_name: str, key_env: str):
    api_key = os.environ.get(key_env)
    if not api_key:
        raise click.ClickException(f"Environment variable {key_env} is not set.")
    try:
        return build_provider(provider_name, api_key)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context):
    """Agent Liftoff: migrate AI agents and workflows between orchestration platforms.

    Run with no command for the interactive wizard, or use a specific
    subcommand (e.g. `migrate`) for scripting/CI.
    """
    if ctx.invoked_subcommand is None:
        from agent_liftoff.wizard import run_wizard

        run_wizard()


@main.command()
def wizard():
    """Interactive guided migration: asks for the export, output path, and LLM config."""
    from agent_liftoff.wizard import run_wizard

    run_wizard()


def _register_foundry() -> None:
    """Attach `agent_liftoff foundry`.

    Imported here rather than at module scope so a plain `agent_liftoff migrate`
    never pays for the foundry's imports, and so a broken foundry can't stop
    the rest of the CLI from working.
    """
    from agent_liftoff.foundry.cli import register

    register(main)


_register_foundry()


@main.command()
@click.argument("clone_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
def extract(clone_dir: Path):
    """Validate that CLONE_DIR looks like a recognized Copilot Studio export."""
    fmt = detect_format(clone_dir)
    if fmt is None:
        raise click.ClickException(
            f"{clone_dir} doesn't look like a Copilot Studio export Agent Liftoff recognizes "
            "(expected either a `pac copilot clone` workspace with a *.mcs.yaml root file, "
            "or a solution export with solution.xml + a bots/ directory)."
        )
    click.echo(f"OK: recognized as a '{fmt}' export")


@main.command()
@click.option("--from", "source", required=True, help="Source platform, e.g. copilot-studio")
@click.option("--to", "target", required=True, help="Target platform, e.g. orchestrate")
@click.option("--llm-provider", default="anthropic", show_default=True, help="LLM provider for the (optional) Translate stage.")
@click.option("--llm-key-env", default="ANTHROPIC_API_KEY", show_default=True, help="Env var holding the LLM API key.")
@click.option("--no-llm", is_flag=True, help="Skip the LLM Translate stage; run the fully deterministic pipeline.")
@click.argument("clone_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("output_dir", type=click.Path(file_okay=False, path_type=Path))
def migrate(source: str, target: str, llm_provider: str, llm_key_env: str, no_llm: bool, clone_dir: Path, output_dir: Path):
    """Run extract -> normalize -> map -> translate -> validate -> export."""
    try:
        _run_migrate(source, target, llm_provider, llm_key_env, no_llm, clone_dir, output_dir)
    except LiftoffError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--from", "source", default="orchestrate", show_default=True, help="Source platform.")
@click.option("--to", "target", default="copilot-studio", show_default=True, help="Target platform.")
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.argument("output_dir", type=click.Path(file_okay=False, path_type=Path))
def convert(source: str, target: str, input_path: Path, output_dir: Path):
    """Deterministically convert an exported agent to the target format.

    No LLM, no AI -- pure field mapping (import -> map -> export). Instructions
    are carried over verbatim. Unlike `migrate`, INPUT_PATH may be a single
    exported file (e.g. an Orchestrate agent.yaml) or a directory.
    """
    try:
        # no_llm is forced True: this command never touches a model.
        _run_migrate(source, target, "", "", True, input_path, output_dir)
    except LiftoffError as exc:
        raise click.ClickException(str(exc)) from exc


def _fetch_marketplace(instance_url: str, api_key: str, session_cookie: str | None):
    """Read the global catalog of installable tools.

    The catalog is served by the console, not the instance API, and it
    authenticates with a console session rather than an IAM token. We try the
    API key first because that path needs no manual step; a copied browser
    cookie is the fallback when it doesn't.

    Returns the flattened pool plus an enrich hook -- the hook is what fetches
    parameter schemas for shortlisted candidates, and it has to stay on this
    side of the pipeline boundary because it does I/O.
    """
    from agent_liftoff.connectors.orchestrate.catalog_client import (
        OrchestrateCatalogClient,
        enrich_artifacts,
        to_artifacts,
    )
    from agent_liftoff.pipeline.resolve import build_marketplace_catalog

    auth = {"session_cookie": session_cookie} if session_cookie else {"api_key": api_key}
    client = OrchestrateCatalogClient(instance_url, **auth)
    pool = build_marketplace_catalog(to_artifacts(client.list_installable()))

    def enrich(artifacts: list) -> None:
        enrich_artifacts(client, artifacts)

    return pool, enrich


@main.command("match-tools")
@click.argument("export_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--bot", "bot_schema", default=None, help="Bot schema name, for a multi-agent solution export.")
@click.option("--url-env", default="IBMUrl", show_default=True, help="Env var holding the Orchestrate instance URL.")
@click.option("--key-env", default="IBMKey", show_default=True, help="Env var holding the Orchestrate API key.")
@click.option("--llm-provider", default=None, help="LLM provider for adjudication. Omit to use the saved wizard config.")
@click.option("--no-llm", is_flag=True, help="Show the deterministic shortlist only; make no model call.")
@click.option("--top", default=5, show_default=True, help="Shortlist entries to display per tool.")
@click.option("--cookie-env", default="WXO_CONSOLE_COOKIE", show_default=True,
              help="Env var holding a console session Cookie header, if the catalog rejects the API key.")
@click.option("--no-catalog", is_flag=True, help="Search only the instance's installed tools.")
def match_tools(export_dir, bot_schema, url_env, key_env, llm_provider, no_llm, top, cookie_env, no_catalog):
    """Show how source tools resolve against a live Orchestrate instance.

    An inspection window onto the Resolve stage: for each tool in a Copilot
    Studio export it prints the ranked candidates the deterministic shortlist
    produced, then the model's verdict. Read-only -- nothing is written, nothing
    is installed and no agent is deployed.

    Two pools are searched in order: the tools installed on the instance, then
    the global catalog of installable tools. With --no-llm it makes no model
    call at all, so the ranking can be judged on its own before any AI is
    involved.
    """
    from agent_liftoff import creds as _creds
    from agent_liftoff.config import load_config
    from agent_liftoff.connectors.copilot_studio import solution_importer
    from agent_liftoff.connectors.orchestrate.rest_client import OrchestrateRestClient
    from agent_liftoff.pipeline.resolve import build_catalog, resolve_agent_tools, shortlist_scored

    instance_url, api_key = os.environ.get(url_env), os.environ.get(key_env)
    if not instance_url or not api_key:
        raise click.ClickException(f"Set {url_env} and {key_env} to your Orchestrate instance URL and API key.")

    click.echo("Reading installed tools…")
    try:
        catalog = build_catalog(OrchestrateRestClient(api_key, instance_url).list_all_tools())
    except LiftoffError as exc:
        raise click.ClickException(str(exc)) from exc
    kinds = Counter(entry.kind for entry in catalog)
    click.echo(f"  {len(catalog)} tool(s) installed on this instance: {dict(kinds)}")

    marketplace, enrich = [], None
    if not no_catalog:
        click.echo("Reading the global catalog…")
        try:
            marketplace, enrich = _fetch_marketplace(
                instance_url, api_key, os.environ.get(cookie_env)
            )
        except LiftoffError as exc:
            # A missing catalog degrades the run to instance-only matching; it
            # doesn't invalidate it, so this is a warning, not a failure.
            click.echo(f"  catalog unavailable: {exc}")
        else:
            click.echo(f"  {len(marketplace)} tool(s) installable from the catalog")
    click.echo("")

    try:
        import_result = solution_importer.import_agent(export_dir, bot_schema=bot_schema)
    except ValueError as exc:  # multi-bot solution with no --bot
        raise click.ClickException(str(exc)) from exc
    agent = map_agent(import_result, target_platform="orchestrate")

    if not agent.tools:
        click.echo("No tools found on this agent -- nothing to match.")
        return

    provider = None
    if not no_llm:
        saved = load_config()
        name = llm_provider or (saved.llm_provider if saved else None)
        key = _creds.load_secret(_creds.llm_key_name(name)) if name else None
        if not key:
            raise click.ClickException(
                "No LLM key found. Run `agent_liftoff wizard` once to store one, or pass --no-llm."
            )
        provider = build_provider(name, key)

    click.echo(f"Agent: {agent.name}  ({len(agent.tools)} tool(s))\n")
    for tool in agent.tools:
        click.echo(f"── {tool.ref}  [op: {tool.operation_id or 'n/a'}]")
        if tool.description:
            click.echo(f"   {' '.join(tool.description.split())[:150]}")
        if tool.inputs:
            click.echo(f"   inputs: {', '.join(p.name for p in tool.inputs)}")
        for label, pool in (("installed", catalog), ("catalog", marketplace)):
            if not pool:
                continue
            click.echo(f"   shortlist over {label} tools (deterministic, before any model call):")
            for rank, (score, candidate) in enumerate(shortlist_scored(tool, pool)[:top], 1):
                detail = ", ".join(candidate.params) or "no params" if candidate.installed else (
                    candidate.display_name or ""
                )
                click.echo(f"     {rank}. {score:6.2f}  {candidate.ref}  ({detail})")
        click.echo("")

    if provider is None:
        click.echo("--no-llm: stopping before adjudication.")
        return

    click.echo("Adjudicating with the model…\n")
    resolve_agent_tools(agent, catalog, provider, marketplace=marketplace, enrich=enrich)
    for tool in agent.tools:
        status = "RESOLVED" if not tool.review_required else "NEEDS REVIEW"
        click.echo(f"── {tool.ref}   [{status}, confidence {tool.confidence:.2f}]")
        if tool.notes:
            click.echo(f"   {' '.join(tool.notes.split())}")
        click.echo("")


def _run_migrate(source, target, llm_provider, llm_key_env, no_llm, clone_dir, output_dir):
    if (source, target) not in SUPPORTED_CORRIDORS:
        supported = ", ".join(f"{s} -> {t}" for s, t in SUPPORTED_CORRIDORS)
        raise click.ClickException(f"Unsupported corridor '{source}' -> '{target}'. Supported: {supported}")

    importer_mod = load_importer(source)
    exporter_mod = load_exporter(target)  # fail early if the target has no exporter

    click.echo(f"Extract: reading {clone_dir}")
    import_result = importer_mod.import_agent(clone_dir)

    click.echo(f"Map: resolving references for {target}")
    agent = map_agent(import_result, target_platform=target)

    if no_llm or not os.environ.get(llm_key_env):
        if not no_llm:
            click.echo(f"Translate: {llm_key_env} not set; using deterministic fallback")
        else:
            click.echo("Translate: skipped (--no-llm); deterministic fallback")
        deterministic_instructions(agent)
    else:
        click.echo(f"Translate: synthesizing instructions via {llm_provider}")
        provider = _build_provider(llm_provider, llm_key_env)
        translate_agent(agent, provider)

    click.echo("Validate: checking the generated agent")
    validation = validate_agent(agent)
    for issue in validation.issues:
        click.echo(f"  [{issue.severity}] {issue.field}: {issue.message}")
    if not validation.is_valid:
        raise click.ClickException("Validation failed; fix the errors above before exporting.")

    cases = generate_cases(agent)
    click.echo(f"Generated {len(cases)} eval case(s) from the original topics.")

    click.echo(f"Export: writing {target} agent to {output_dir}")
    result = exporter_mod.export_agent(agent, output_dir)

    click.echo(f"Wrote {result.agent_path}")
    if result.needs_review:
        click.echo(f"Review needed: see {result.review_manifest_path}")


if __name__ == "__main__":
    main()

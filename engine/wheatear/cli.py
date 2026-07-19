"""Wheatear CLI entrypoint.

`wheatear migrate` runs the full pipeline end to end. Per-stage debugging
commands (normalize/map/translate/validate as independently invocable steps
with IR persisted to disk between them) are a natural fast-follow once a
corridor is validated against real data -- not built speculatively now.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from wheatear.connectors.copilot_studio.importer import detect_format
from wheatear.connectors.registry import load_exporter, load_importer
from wheatear.corridors import SUPPORTED_CORRIDORS
from wheatear.errors import WheatearError
from wheatear.eval.generate_cases import generate_cases
from wheatear.llm.factory import build_provider
from wheatear.pipeline.map import map_agent
from wheatear.pipeline.translate import deterministic_instructions, translate_agent
from wheatear.pipeline.validate import validate_agent


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
    """Wheatear: migrate AI agents and workflows between orchestration platforms.

    Run with no command for the interactive wizard, or use a specific
    subcommand (e.g. `migrate`) for scripting/CI.
    """
    if ctx.invoked_subcommand is None:
        from wheatear.wizard import run_wizard

        run_wizard()


@main.command()
def wizard():
    """Interactive guided migration: asks for the export, output path, and LLM config."""
    from wheatear.wizard import run_wizard

    run_wizard()


@main.command()
@click.argument("clone_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
def extract(clone_dir: Path):
    """Validate that CLONE_DIR looks like a recognized Copilot Studio export."""
    fmt = detect_format(clone_dir)
    if fmt is None:
        raise click.ClickException(
            f"{clone_dir} doesn't look like a Copilot Studio export Wheatear recognizes "
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
    except WheatearError as exc:
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
    except WheatearError as exc:
        raise click.ClickException(str(exc)) from exc


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

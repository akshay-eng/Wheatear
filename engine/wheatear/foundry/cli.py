"""`wheatear foundry` -- probe platforms, compile adapters, run them.

Registered as a subgroup rather than folded into `cli.py` so the foundry stays
one component with one entry point, and so nothing here is imported by a plain
`wheatear migrate`.

The commands map one-to-one onto the agents, which is deliberate: each stage's
output is written to the store as an inspectable artifact, so a corridor can be
built in steps and each step judged before the next one runs.

    foundry doctor                 can this machine run the sandbox?
    foundry probe <platform>       Inspector  -> a SchemaCorpus
    foundry corpora                what has been probed, and when
    foundry build <platform>       Translator + Engineer -> adapters
    foundry corridor <src> <dst>   both halves at once
    foundry adapters               what is cached
    foundry show <platform>        the spec and the generated code
    foundry run <platform>         apply a cached adapter to a file of records
    foundry ship                   strip a built corridor into engine/assets/
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from wheatear.errors import WheatearError
from wheatear.foundry import inspector, runtime
from wheatear.foundry.orchestrator import Orchestrator, entity_kinds_in
from wheatear.foundry.probes.base import ProbeContext
from wheatear.foundry.sandbox import DockerSandbox, SubprocessSandbox
from wheatear.foundry.store import FoundryStore
from wheatear.foundry.types import Direction, EntityKind

KIND_CHOICES = [kind.value for kind in EntityKind]
DIRECTION_CHOICES = [d.value for d in Direction]


def _store(root: str | None) -> FoundryStore:
    return FoundryStore(Path(root) if root else None)


def _provider(no_llm: bool, name: str | None, model: str | None = None):
    """The configured LLM provider, or None when running deterministically.

    Deliberately not fatal when absent. The deterministic alignment pass alone
    produces a real mapping for the unambiguous fields, so --no-llm is a
    supported way to work, not a degraded one.
    """
    if no_llm:
        return None
    from wheatear import creds
    from wheatear.config import load_config
    from wheatear.llm.factory import build_provider

    saved = load_config()
    provider_name = name or (saved.llm_provider if saved else None)
    if not provider_name:
        return None
    key = creds.load_secret(creds.llm_key_name(provider_name)) or os.environ.get(
        saved.llm_key_env if saved else "ANTHROPIC_API_KEY"
    )
    if not key:
        return None
    try:
        return build_provider(provider_name, key, model)
    except ValueError:
        return None


def _sandbox(unsandboxed: bool, timeout: int):
    """The runner for this build, checked before anything is spent on it.

    Checked here rather than discovered per adapter because the failure is
    always the same one twice over: a machine that cannot reach a container
    daemon cannot reach it for adapter one or adapter eight, and finding out
    at the end costs a full build of model calls to produce eight adapters
    that all failed verification and none of which will be allowed to run.
    """
    if unsandboxed:
        return SubprocessSandbox(timeout_s=timeout)
    container = DockerSandbox(timeout_s=timeout)
    ready, detail = container.available()
    if not ready:
        raise click.ClickException(
            f"Generated code has nowhere isolated to run: {detail}\n"
            "Fix the container runtime, or pass --unsandboxed to run the tests in a "
            "resource-limited subprocess instead -- which has a real network stack, so "
            "only the static guard stands between generated code and a socket."
        )
    if not container.image_present():
        raise click.ClickException(
            f"The sandbox image {container.image} is not present. "
            f"Run `{container.runtime} pull {container.image}` once."
        )
    return container


def _context(platform: str, export: Path | None, url_env: str, key_env: str,
             cookie_env: str, offline: bool) -> ProbeContext:
    return ProbeContext(
        platform=platform,
        export_path=export,
        instance_url=os.environ.get(url_env),
        api_key=os.environ.get(key_env),
        session_cookie=os.environ.get(cookie_env),
        extra={
            key: value
            for key, value in (("dataverse_token", os.environ.get("WHEATEAR_DATAVERSE_TOKEN")),)
            if value
        },
        allow_network=not offline,
    )


@click.group("foundry")
def foundry():
    """Probe platforms, compile deterministic field mappings, and reuse them.

    The foundry infers a corridor's field mapping once, compiles it into tested
    Python, and caches it against the schema it was built for. Migrating ten
    thousand agents then costs one cache lookup rather than ten thousand model
    calls.

    Entity kinds are built in dependency order: tool, knowledge, connection,
    topic, agent -- so an agent's references resolve against adapters that
    already exist.
    """


# ----------------------------------------------------------------------


@foundry.command()
@click.option("--unsandboxed", is_flag=True, help="Also report the fallback runner.")
def doctor(unsandboxed: bool):
    """Check whether this machine can run generated code in isolation."""
    container = DockerSandbox()
    ready, detail = container.available()
    click.echo(f"container runtime : {'OK ' if ready else 'no '} {detail}")
    if ready:
        present = container.image_present()
        click.echo(
            f"sandbox image     : {'OK  ' + container.image if present else 'missing ' + container.image}"
        )
        if not present:
            click.echo(f"                    run `{container.runtime} pull {container.image}` once")
    if unsandboxed or not ready:
        fallback = SubprocessSandbox()
        _, why = fallback.available()
        click.echo(f"fallback runner   : {why}")
        click.echo(
            "                    --unsandboxed runs generated tests in a resource-limited\n"
            "                    subprocess. It has a real network stack; only the static\n"
            "                    guard stands between generated code and a socket."
        )


@foundry.command()
@click.argument("platform")
@click.argument("export", required=False, type=click.Path(exists=True, path_type=Path))
@click.option("--url-env", default="IBMUrl", show_default=True, help="Env var holding the instance URL.")
@click.option("--key-env", default="IBMKey", show_default=True, help="Env var holding the API key.")
@click.option("--cookie-env", default="WXO_CONSOLE_COOKIE", show_default=True,
              help="Env var holding a console session Cookie header.")
@click.option("--offline", is_flag=True, help="Structural pass only; make no network call.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
@click.option("--json", "as_json", is_flag=True, help="Print the corpus as JSON.")
def probe(platform, export, url_env, key_env, cookie_env, offline, store_root, as_json):
    """Probe PLATFORM and store what its records look like.

    Two passes: the export archive first (offline, authoritative about
    structure), then the live APIs (which fill in what the export strips).
    Whatever neither pass reached is reported as a gap, with what would close
    it -- nothing is guessed.

    Credentials are read from the environment and never stored. Sample records
    are redacted before they touch the disk.
    """
    context = _context(platform, export, url_env, key_env, cookie_env, offline)
    corpus = inspector.inspect(context)
    path = _store(store_root).put_corpus(corpus)

    if as_json:
        click.echo(corpus.model_dump_json(indent=2))
        return

    click.echo(f"{corpus.platform}  fingerprint {corpus.fingerprint()[:16]}")
    click.echo(f"stored at {path}\n")
    for entity in corpus.entities:
        leaves = len(entity.leaves())
        click.echo(
            f"  {entity.kind.value:<11} {entity.name:<24} "
            f"{entity.sample_count:>4} record(s)  {leaves:>3} field(s)  [{entity.origin.value}]"
        )
    if corpus.gaps:
        click.echo("\ngaps -- what the probe could not see:")
        for gap in corpus.gaps:
            click.echo(f"  [{gap.reason.value}] {gap.what}")
            if gap.detail:
                click.echo(f"      {' '.join(gap.detail.split())}")
            if gap.remedy:
                click.echo(f"      fix: {' '.join(gap.remedy.split())}")
    for note in corpus.notes:
        click.echo(f"\n{note}")


@foundry.command()
@click.option("--platform", default=None, help="Only this platform.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def corpora(platform, store_root):
    """List what has been probed, and when."""
    records = _store(store_root).list_corpora(platform)
    if not records:
        click.echo("Nothing probed yet. Run `wheatear foundry probe <platform> <export>`.")
        return
    for record in records:
        age = record.age()
        days = age.days
        when = f"{days}d ago" if days else f"{age.seconds // 3600}h ago"
        click.echo(
            f"{record.platform:<16} {record.fingerprint[:16]}  {when:<10} "
            f"{', '.join(record.entity_kinds)}"
        )


@foundry.command()
@click.argument("platform")
@click.option("--direction", type=click.Choice(DIRECTION_CHOICES), default="import",
              show_default=True, help="import = platform -> IR; export = IR -> platform.")
@click.option("--entity", "entities", multiple=True, type=click.Choice(KIND_CHOICES),
              help="Entity kinds to build. Repeatable; defaults to everything probed.")
@click.option("--fingerprint", default=None, help="Build against a specific stored corpus.")
@click.option("--rebuild", is_flag=True, help="Ignore the cache and recompile.")
@click.option("--no-llm", is_flag=True, help="Deterministic alignment only; make no model call.")
@click.option("--llm-provider", default=None, help="Override the saved provider.")
@click.option("--llm-model", default=None, help="Override the provider's default model.")
@click.option("--unsandboxed", is_flag=True,
              help="Run generated tests in a subprocess instead of a container (weaker).")
@click.option("--timeout", default=180, show_default=True, help="Seconds per sandbox run.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def build(platform, direction, entities, fingerprint, rebuild, no_llm, llm_provider, llm_model,
          unsandboxed, timeout, store_root):
    """Compile adapters for PLATFORM from its last probe.

    Reads the stored corpus, correlates it against the IR, generates the
    mapping code, and runs the generated tests in a sandbox with no network.
    Adapters that already exist for the same schema are reused unless
    --rebuild is given.
    """
    store = _store(store_root)
    corpus = (
        store.get_corpus(platform, fingerprint) if fingerprint else store.latest_corpus(platform)
    )
    if corpus is None:
        raise click.ClickException(
            f"No stored corpus for '{platform}'. Run `wheatear foundry probe {platform} <export>` first."
        )

    orchestrator = Orchestrator(
        store=store,
        sandbox=_sandbox(unsandboxed, timeout),
        provider=_provider(no_llm, llm_provider, llm_model),
    )
    if orchestrator.provider is None and not no_llm:
        click.echo("No LLM configured; running the deterministic alignment only.\n")

    kinds = [EntityKind(value) for value in entities] if entities else entity_kinds_in(corpus)
    if not kinds:
        raise click.ClickException(f"The stored corpus for '{platform}' has no entities to map.")

    failures = 0
    for kind in kinds:
        click.echo(f"── {platform} {direction} {kind.value}")
        try:
            result = orchestrator.ensure_adapter(
                corpus, Direction(direction), kind, rebuild=rebuild
            )
        except WheatearError as exc:
            raise click.ClickException(str(exc)) from exc

        if result.artifact is None:
            click.echo(f"   skipped: {result.reason}\n")
            continue

        artifact = result.artifact
        status = "cached" if result.from_cache else f"{result.origin} in {artifact.attempts} attempt(s)"
        click.echo(f"   {status}: {artifact.report.summary()}")
        click.echo(
            f"   {len(artifact.spec.mappings)} field(s) mapped, "
            f"{len(artifact.spec.flags)} flagged for review"
        )
        if not artifact.verified:
            failures += 1
            click.echo("   NOT VERIFIED -- this adapter will not be run unattended:")
            for line in artifact.report.feedback(4).splitlines():
                click.echo(f"     {line}")
        for flag in artifact.spec.blocking_flags()[:5]:
            click.echo(f"   [block] {flag.path}: {' '.join(flag.detail.split())}")
        click.echo(f"   {store.adapter_dir(artifact.key)}\n")

    if failures:
        raise click.ClickException(
            f"{failures} adapter(s) did not pass their tests. They are stored for inspection "
            "but will not run; see the failures above."
        )


@foundry.command()
@click.argument("source")
@click.argument("target")
@click.option("--rebuild", is_flag=True, help="Ignore the cache and recompile both halves.")
@click.option("--no-llm", is_flag=True, help="Deterministic alignment only.")
@click.option("--unsandboxed", is_flag=True, help="Subprocess runner instead of a container.")
@click.option("--timeout", default=180, show_default=True, help="Seconds per sandbox run.")
@click.option("--llm-model", default=None, help="Override the provider's default model.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def corridor(source, target, rebuild, no_llm, unsandboxed, timeout, llm_model, store_root):
    """Build both halves of the SOURCE -> TARGET corridor.

    A corridor is an import adapter for SOURCE and an export adapter for
    TARGET, meeting at the IR. They are built and cached separately, so the
    next corridor into TARGET reuses its export half untouched.
    """
    store = _store(store_root)
    corpora_ = {}
    for platform in (source, target):
        corpus = store.latest_corpus(platform)
        if corpus is None:
            raise click.ClickException(
                f"No stored corpus for '{platform}'. Probe it first."
            )
        corpora_[platform] = corpus

    orchestrator = Orchestrator(
        store=store,
        sandbox=_sandbox(unsandboxed, timeout),
        provider=_provider(no_llm, None, llm_model),
        on_progress=lambda message: click.echo(f"  {message}", err=True),
    )
    click.echo(f"{source} -> {target}\n", err=True)
    result = orchestrator.corridor(corpora_[source], corpora_[target], rebuild=rebuild)

    click.echo(f"\n{source} -> {target}\n")
    for label, group in (("import", result.imports), ("export", result.exports)):
        for kind, adapter in group.items():
            click.echo(f"  {label:<7} {kind.value:<11} {adapter.describe()}")
    click.echo("")
    for note in result.notes:
        click.echo(f"  {note}")
    blocking = result.blocking_flags()
    if blocking:
        click.echo(f"\n  {len(blocking)} blocking flag(s) -- fields with no counterpart:")
        for family, detail in blocking[:10]:
            click.echo(f"    {family}: {detail}")


@foundry.command()
@click.option("--platform", default=None, help="Only this platform.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def adapters(platform, store_root):
    """List compiled adapters in the cache."""
    found = _store(store_root).list_adapters(platform)
    if not found:
        click.echo("No adapters compiled yet. Run `wheatear foundry build <platform>`.")
        return
    for artifact in found:
        mark = "OK " if artifact.verified else "!! "
        click.echo(
            f"{mark}{artifact.key.family():<40} {artifact.key.schema_fingerprint[:12]}  "
            f"{len(artifact.spec.mappings):>3} mapping(s)  {artifact.report.summary()}"
        )


@foundry.command()
@click.argument("platform")
@click.option("--direction", type=click.Choice(DIRECTION_CHOICES), default="import", show_default=True)
@click.option("--entity", type=click.Choice(KIND_CHOICES), default="agent", show_default=True)
@click.option("--code", is_flag=True, help="Print the generated adapter source.")
@click.option("--tests", "show_tests", is_flag=True, help="Print the generated test module.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def show(platform, direction, entity, code, show_tests, store_root):
    """Show the mapping spec (and optionally the code) for one adapter."""
    found = _store(store_root).list_adapters(platform, Direction(direction), EntityKind(entity))
    if not found:
        raise click.ClickException(f"No adapter for {platform}/{direction}/{entity}.")
    artifact = found[0]

    if code:
        click.echo(artifact.code)
        return
    if show_tests:
        click.echo(artifact.tests)
        return

    click.echo(f"{artifact.key.family()}  schema {artifact.key.schema_fingerprint[:16]}")
    click.echo(f"built {artifact.created_at:%Y-%m-%d %H:%M} by {artifact.generator}, "
               f"{artifact.attempts} attempt(s)")
    click.echo(f"tests: {artifact.report.summary()}\n")
    click.echo("field mappings:")
    for mapping in artifact.spec.mappings:
        sources = ", ".join(mapping.source_paths) or "(constant)"
        click.echo(
            f"  {mapping.target_path:<34} <- {sources:<40} "
            f"[{mapping.transform.value}, {mapping.confidence:.2f}]"
        )
        if mapping.rationale:
            click.echo(f"      {' '.join(mapping.rationale.split())[:150]}")
    if artifact.spec.flags:
        click.echo("\nflags:")
        for flag in artifact.spec.flags:
            click.echo(f"  [{flag.severity:<5}] {flag.path:<34} {flag.reason.value}")
    if artifact.spec.notes:
        click.echo("\nnotes:")
        for note in artifact.spec.notes:
            click.echo(f"  {' '.join(note.split())}")


@foundry.command("run")
@click.argument("platform")
@click.argument("records_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--direction", type=click.Choice(DIRECTION_CHOICES), default="import", show_default=True)
@click.option("--entity", type=click.Choice(KIND_CHOICES), default="agent", show_default=True)
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the converted records here as JSON.")
@click.option("--validate-ir", is_flag=True, help="Check each result against the IR model.")
@click.option("--store-root", default=None, help="Override the foundry store location.")
def run_adapter(platform, records_file, direction, entity, out, validate_ir, store_root):
    """Apply a cached adapter to a JSON file of records.

    The fast path: no probe, no model, no container. RECORDS_FILE is a JSON
    array of source records, or a single record object.
    """
    try:
        payload = json.loads(records_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Could not read {records_file}: {exc}") from exc
    records = payload if isinstance(payload, list) else [payload]

    found = _store(store_root).list_adapters(platform, Direction(direction), EntityKind(entity))
    if not found:
        raise click.ClickException(
            f"No adapter for {platform}/{direction}/{entity}. Build one first."
        )
    try:
        adapter = runtime.load(found[0])
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    converted, report = runtime.convert_all(adapter, records)
    click.echo(f"{report.summary()}  [{adapter.key_slug}]")
    for reason, count in sorted(report.flag_counts.items()):
        click.echo(f"  {count} record(s) flagged: {reason}")
    for failure in report.failures[:5]:
        click.echo(f"  record {failure.index} failed: {failure.error}")

    if validate_ir and Direction(direction) is Direction.IMPORT:
        invalid = 0
        for index, record in enumerate(converted):
            result = runtime.to_ir(EntityKind(entity), record)
            if not result.ok:
                invalid += 1
                if invalid <= 5:
                    click.echo(f"  record {index} is not valid IR: {'; '.join(result.errors[:3])}")
        click.echo(f"  {len(converted) - invalid}/{len(converted)} validate as IR "
                   f"{EntityKind(entity).value} records")

    if out:
        out.write_text(json.dumps(converted, indent=2, ensure_ascii=False), encoding="utf-8")
        click.echo(f"wrote {out}")


@foundry.command()
@click.argument("platform")
@click.option("--direction", type=click.Choice(DIRECTION_CHOICES), required=True)
@click.option("--entity", type=click.Choice(KIND_CHOICES), required=True)
@click.option("--store-root", default=None, help="Override the foundry store location.")
@click.confirmation_option(prompt="Delete this compiled adapter from the cache?")
def forget(platform, direction, entity, store_root):
    """Remove a compiled adapter, forcing a rebuild next time."""
    store = _store(store_root)
    found = store.list_adapters(platform, Direction(direction), EntityKind(entity))
    if not found:
        click.echo("Nothing to forget.")
        return
    for artifact in found:
        store.forget(artifact.key)
        click.echo(f"forgot {artifact.key.slug()}")


def register(main: click.Group) -> None:
    """Attach the foundry commands to the top-level CLI."""
    main.add_command(foundry)


@foundry.command()
@click.option("--store-root", default=None, help="Read the build from here instead of the default store.")
@click.option("--to", "destination", default=None, help="Write to here instead of engine/assets/.")
@click.option("--platform", "platforms", multiple=True, help="Only these platforms (repeatable).")
def ship(store_root, destination, platforms):
    """Publish a locally-built corridor as shippable assets.

    Strips what a probe saw from a tenant -- sample records, observed values,
    single-value "enums", generated test bodies -- and keeps the field layout
    and the compiled adapter, which belong to the vendor rather than to
    whoever ran the probe.

    Adapters are keyed on the platform versions they were built against, so
    anyone on those versions loads them and calls no model at all.
    """
    from wheatear.assets import ASSETS
    from wheatear.foundry.shipping import ship as ship_assets

    target = Path(destination) if destination else ASSETS
    report = ship_assets(_store(store_root), target, list(platforms) or None)

    click.echo(f"shipped to {target}\n")
    for name in report.corpora:
        click.echo(f"  corpus   {name}")
    for name in sorted(report.adapters):
        click.echo(f"  adapter  {name}")
    click.echo(f"\n  {report.summary()}")
    if report.skipped_unverified:
        click.echo(
            "\n  Unverified adapters are not shipped -- they are worth reading and "
            "finishing by hand, and not worth publishing to people who will run them "
            "unattended:"
        )
        for name in sorted(set(report.skipped_unverified)):
            click.echo(f"    {name}")

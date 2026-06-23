"""Interactive guided migration.

The questionary/rich calls here are thin and intentionally not unit tested
(driving a real interactive prompt isn't worth the harness complexity for a
one-person CLI feature) -- but every piece of actual logic (env var
resolution, config diffing, default path suggestion) is split into small
functions that are tested without touching a terminal at all. See
tests/test_wizard_logic.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel

from wheatear.banner import print_banner
from wheatear.config import WheatearConfig, load_config, save_config
from wheatear.connectors.copilot_studio.importer import detect_format, import_agent
from wheatear.connectors.orchestrate.exporter import export_agent
from wheatear.corridors import SOURCE_PLATFORMS, SUPPORTED_CORRIDORS, TARGET_PLATFORMS
from wheatear.eval.generate_cases import generate_cases
from wheatear.llm.factory import PROVIDER_KEY_ENV_DEFAULTS, build_provider
from wheatear.pipeline.map import map_agent
from wheatear.pipeline.translate import translate_agent
from wheatear.pipeline.validate import validate_agent
from wheatear.source_fetch import SourceFetchError, resolve_export_source

console = Console()


def suggest_output_path(export_path: Path) -> Path:
    return export_path.parent / f"{export_path.name}-orchestrate"


def resolve_key_env_for_provider(provider: str, existing: WheatearConfig | None) -> str:
    """The env var name to use for a chosen provider: keep the saved one if
    it was saved for this same provider, otherwise fall back to that
    provider's conventional default.
    """
    if existing and existing.llm_provider == provider:
        return existing.llm_key_env
    return PROVIDER_KEY_ENV_DEFAULTS[provider]


def config_changed(new: WheatearConfig, old: WheatearConfig | None) -> bool:
    return old is None or new != old


def _cancelled(value) -> bool:
    """questionary returns None on Ctrl-C / Ctrl-D; centralize the check."""
    return value is None


def _platform_choices(platforms: list[tuple[str, str, bool]]) -> list[questionary.Choice]:
    return [
        questionary.Choice(name, value=key)
        if implemented
        else questionary.Choice(f"{name} (coming soon)", value=key, disabled="not yet implemented")
        for name, key, implemented in platforms
    ]


def ask_source_platform() -> str:
    result = questionary.select(
        "Which platform are you migrating from?",
        choices=_platform_choices(SOURCE_PLATFORMS),
    ).ask()
    if _cancelled(result):
        raise SystemExit(1)
    return result


def ask_target_platform() -> str:
    result = questionary.select(
        "Which platform are you migrating to?",
        choices=_platform_choices(TARGET_PLATFORMS),
    ).ask()
    if _cancelled(result):
        raise SystemExit(1)
    return result


def validate_corridor(source: str, target: str) -> None:
    if (source, target) not in SUPPORTED_CORRIDORS:
        supported = ", ".join(f"{s} -> {t}" for s, t in SUPPORTED_CORRIDORS)
        console.print(f"[bold red]Unsupported corridor[/bold red] '{source}' -> '{target}'. Supported: {supported}")
        raise SystemExit(1)


def ask_export_path() -> Path:
    while True:
        raw = questionary.text("GitHub repo URL or local path to the export:").ask()
        if _cancelled(raw):
            raise SystemExit(1)

        try:
            path = resolve_export_source(raw)
        except SourceFetchError as exc:
            console.print(f"[red]{exc}[/red] Try again.")
            continue

        fmt = detect_format(path)
        if fmt is None:
            console.print(f"[red]{path} doesn't look like a recognized Copilot Studio export.[/red] Try again.")
            continue
        console.print(f"[green]Recognized as a '{fmt}' export.[/green]")
        return path


def ask_output_path(export_path: Path) -> Path:
    raw = questionary.text(
        "Where should the watsonx Orchestrate output go?",
        default=str(suggest_output_path(export_path)),
    ).ask()
    if _cancelled(raw):
        raise SystemExit(1)
    return Path(raw).expanduser()


def ask_llm_settings(existing: WheatearConfig | None) -> WheatearConfig:
    provider = questionary.select(
        "Which LLM provider should run the Translate stage?",
        choices=[
            questionary.Choice("anthropic (Claude)", value="anthropic"),
            questionary.Choice("openai", value="openai", disabled="not yet implemented"),
            questionary.Choice("watsonx.ai", value="watsonx", disabled="not yet implemented"),
        ],
        default="anthropic",
    ).ask()
    if _cancelled(provider):
        raise SystemExit(1)

    key_env = resolve_key_env_for_provider(provider, existing)
    return WheatearConfig(llm_provider=provider, llm_key_env=key_env)


def resolve_api_key(config: WheatearConfig) -> str:
    existing = os.environ.get(config.llm_key_env)
    if existing:
        console.print(f"Using {config.llm_key_env} from your environment.")
        return existing

    key = questionary.password(f"{config.llm_key_env} isn't set. Enter your API key:").ask()
    if _cancelled(key) or not key:
        raise SystemExit(1)

    # Session-only: never written to disk. The config file remembers which
    # provider/env-var to use next time, not the secret itself.
    os.environ[config.llm_key_env] = key
    console.print(
        f"[yellow]Using this key for this session only.[/yellow] To avoid re-entering it next time, run:\n"
        f"  export {config.llm_key_env}=...\n"
    )
    return key


def run_wizard() -> None:
    print_banner(console)

    source = ask_source_platform()
    target = ask_target_platform()
    validate_corridor(source, target)

    export_path = ask_export_path()
    output_path = ask_output_path(export_path)

    # Extract and Map are fully deterministic -- no LLM involved -- so they
    # run, and can fail, before ever bothering the user for an API key.
    try:
        agent = _run_deterministic_stages(export_path)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- see matching comment below
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    # Only now, with a confirmed-readable export in hand, ask for the one
    # thing Translate actually needs.
    saved_config = load_config()
    llm_config = ask_llm_settings(saved_config)
    if config_changed(llm_config, saved_config):
        save_config(llm_config)
    api_key = resolve_api_key(llm_config)
    provider = build_provider(llm_config.llm_provider, api_key)

    try:
        _run_ai_and_export_stages(agent, output_path, target, llm_config, provider)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 -- last-resort boundary so a vendor SDK
        # error (auth failure, network issue, bad response shape) shows as a
        # clean message instead of a raw traceback the user can't act on.
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc


def _run_deterministic_stages(export_path: Path):
    """Extract + Map: no LLM provider in scope at all, by construction."""
    with console.status("[bold]Extract: reading export..."):
        import_result = import_agent(export_path)
    console.print(f"[green]Extract[/green]    {import_result.agent.name}")

    with console.status("[bold]Map: resolving tool/knowledge/connection references..."):
        agent = map_agent(import_result)
    console.print(
        f"[green]Map[/green]        {len(agent.tools)} tool(s), "
        f"{len(agent.knowledge)} knowledge ref(s), {len(agent.connections)} connection(s)"
    )
    return agent


def _run_ai_and_export_stages(agent, output_path: Path, target: str, llm_config: WheatearConfig, provider) -> None:
    with console.status(f"[bold]Translate: synthesizing instructions via {llm_config.llm_provider}..."):
        translate_agent(agent, provider)
    console.print(f"[green]Translate[/green]  confidence {agent.translation_confidence:.2f}")

    validation = validate_agent(agent)
    for issue in validation.issues:
        color = "red" if issue.severity == "error" else "yellow"
        console.print(f"  [{color}][{issue.severity}][/{color}] {issue.field}: {issue.message}")
    if not validation.is_valid:
        console.print("[bold red]Validation failed.[/bold red] Fix the errors above before exporting.")
        raise SystemExit(1)

    cases = generate_cases(agent)
    console.print(f"[green]Validate[/green]   {len(cases)} eval case(s) generated from the source agent")

    export_result = export_agent(agent, output_path)
    console.print(Panel.fit(f"Wrote {target} agent to {export_result.agent_path}", style="bold green"))

    if export_result.needs_review:
        console.print(f"[bold yellow]Review needed:[/bold yellow] see {export_result.review_manifest_path}")

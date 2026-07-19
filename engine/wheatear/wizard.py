"""Interactive guided migration.

Two modes:

  Manual  -- User provides a path or GitHub URL to an existing Copilot Studio
             export. Wheatear transforms it and writes Orchestrate YAML files.
             The user then runs `orchestrate agents import -f agent.yaml`
             themselves (or with the import hint shown at the end).

  Auto    -- User provides credentials for the source platform (Copilot
             Studio / Power Platform) and the target platform (Orchestrate).
             Wheatear discovers all agents in the environment, the user picks
             which ones to migrate, and Wheatear transforms AND deploys them
             end-to-end without any manual file handling.

The questionary/rich calls here are thin and intentionally not unit tested
(driving a real interactive prompt isn't worth the harness complexity) -- but
every pure-logic helper (env var resolution, config diffing, path suggestion)
is in small functions tested in tests/test_wizard_logic.py.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from wheatear.banner import print_banner
from wheatear.config import WheatearConfig, load_config, save_config
from wheatear.connectors.copilot_studio.importer import detect_format
from wheatear.connectors.copilot_studio.importer import import_agent as copilot_import_agent
from wheatear.connectors.registry import load_exporter
from wheatear.corridors import SOURCE_PLATFORMS, SUPPORTED_CORRIDORS, TARGET_PLATFORMS
from wheatear.eval.generate_cases import generate_cases
from wheatear.llm.factory import PROVIDER_KEY_ENV_DEFAULTS
from wheatear.pipeline.map import map_agent
from wheatear.pipeline.translate import deterministic_instructions, translate_agent
from wheatear.pipeline.validate import validate_agent
from wheatear.source_fetch import SourceFetchError, resolve_export_source

console = Console()

_SLATE = "#7C92A6"
_AMBER = "#E2924B"


# ---------------------------------------------------------------------------
# Credential helpers — keychain-backed, session-cached
# ---------------------------------------------------------------------------

def _prompt_api_key(label: str, keychain_key: str, env_var: str) -> str:
    """Prompt for an API key with keychain save/load and env-var shortcut.

    Priority order:
      1. Already set in os.environ  →  use silently (no prompt)
      2. Saved in OS keychain       →  show masked value, confirm or replace
      3. Neither                    →  ask, then save to keychain + environ
    """
    from wheatear.creds import load_secret, save_secret

    if os.environ.get(env_var):
        console.print(f"  Using [bold]{env_var}[/bold] from environment.")
        return os.environ[env_var]

    saved = load_secret(keychain_key)
    if saved:
        tail = f"***{saved[-4:]}"
        console.print(f"  Saved {label} key found  [dim]({tail})[/dim]")
        choice = questionary.select(
            f"Use saved {label} key?",
            choices=[
                questionary.Choice("Yes — use saved key", value="use"),
                questionary.Choice("No — enter a new key (replaces saved)", value="new"),
            ],
        ).ask()
        if _cancelled(choice):
            raise SystemExit(1)
        if choice == "use":
            os.environ[env_var] = saved
            return saved

    key = questionary.password(f"Enter {label} API key:").ask()
    if _cancelled(key) or not key:
        raise SystemExit(1)
    os.environ[env_var] = key
    if save_secret(keychain_key, key):
        console.print("  [dim]Key saved to OS keychain for future sessions.[/dim]")
    else:
        console.print("  [yellow]Key held in memory for this session only.[/yellow]")
    return key


def _step_header(n: int, total: int, label: str) -> None:
    """Print a numbered step divider so the user always knows where they are."""
    console.rule(
        f"[bold cyan]Step {n}/{total}[/bold cyan]  [bold]{label}[/bold]",
        style="dim",
    )


# ---------------------------------------------------------------------------
# Pure-logic helpers (unit-tested in test_wizard_logic.py)
# ---------------------------------------------------------------------------

def suggest_output_path(export_path: Path) -> Path:
    return export_path.parent / f"{export_path.name}-orchestrate"


def resolve_key_env_for_provider(provider: str, existing: WheatearConfig | None) -> str:
    """The env var name to use for a chosen provider: keep the saved one if
    it was saved for this same provider, otherwise fall back to the default."""
    if existing and existing.llm_provider == provider:
        return existing.llm_key_env
    # Deterministic mode needs no key; keep any prior env name for round-tripping.
    if provider == "none":
        return existing.llm_key_env if existing else ""
    return PROVIDER_KEY_ENV_DEFAULTS[provider]


def config_changed(new: WheatearConfig, old: WheatearConfig | None) -> bool:
    return old is None or new != old


# ---------------------------------------------------------------------------
# Shared TUI primitives
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Platform / corridor questions (shared by both modes)
# ---------------------------------------------------------------------------

def ask_source_platform() -> str:
    result = questionary.select(
        "Which platform are you migrating from?",
        choices=_platform_choices(SOURCE_PLATFORMS),
    ).ask()
    if _cancelled(result):
        raise SystemExit(1)
    return result


def ask_target_platform(exclude_source_key: str | None = None) -> str:
    choices = [p for p in TARGET_PLATFORMS if p[1] != exclude_source_key]
    result = questionary.select(
        "Which platform are you migrating to?",
        choices=_platform_choices(choices),
    ).ask()
    if _cancelled(result):
        raise SystemExit(1)
    return result


def validate_corridor(source: str, target: str) -> None:
    if (source, target) not in SUPPORTED_CORRIDORS:
        supported = ", ".join(f"{s} -> {t}" for s, t in SUPPORTED_CORRIDORS)
        console.print(
            f"[bold red]Unsupported corridor[/bold red] '{source}' -> '{target}'. "
            f"Supported: {supported}"
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class OrchestrateCredentials:
    instance_url: str
    api_key_env: str  # env var name, never the value itself


@dataclass
class ScannedSolution:
    """Holds the result of export+unpack for one Power Platform solution."""
    solution_name: str          # unique name (used by pac)
    solution_label: str         # friendly / display name
    sol_dir: Path               # path to the unpacked directory
    bots: list[tuple[str, str]] = field(default_factory=list)  # [(schema, display_name)]
    error: str | None = None    # set when export/unpack failed


def ask_orchestrate_credentials(existing: WheatearConfig | None) -> OrchestrateCredentials:
    """Prompt for watsonx Orchestrate deployment credentials.

    The API key value is set in os.environ for the current session only --
    consistent with how LLM keys are handled. The instance URL is returned
    so it can be saved to the config file (it's a URL, not a secret).
    """
    console.print(
        Panel(
            "[bold]How to find your credentials:[/bold]\n\n"
            "  1. Sign in to [bold]cloud.ibm.com[/bold]\n"
            "  2. Open [bold]Resource List[/bold] from the top-left menu\n"
            "  3. Under [bold]AI / Machine Learning[/bold], click your watsonx Orchestrate instance\n"
            "  4. Click [bold]Launch[/bold] to open the watsonx Orchestrate UI\n"
            "  5. Go to [bold]Settings[/bold] (gear icon, bottom-left)\n"
            "  6. Copy the [bold]Service Instance URL[/bold] and generate or copy an [bold]API Key[/bold]",
            title="[bold]watsonx Orchestrate — where to find credentials[/bold]",
            border_style=_SLATE,
        )
    )

    from wheatear.creds import KEY_TGT_ORCHESTRATE

    saved_url = existing.orchestrate_instance_url if existing else None
    url = questionary.text("Service Instance URL:", default=saved_url or "").ask()
    if _cancelled(url) or not url.strip():
        raise SystemExit(1)

    api_key_env = (existing.orchestrate_api_key_env if existing else None) or "ORCHESTRATE_API_KEY"
    _prompt_api_key("Target Orchestrate", KEY_TGT_ORCHESTRATE, api_key_env)

    return OrchestrateCredentials(instance_url=url.strip(), api_key_env=api_key_env)




# ---------------------------------------------------------------------------
# LLM settings
# ---------------------------------------------------------------------------

def ask_llm_settings(existing: WheatearConfig | None) -> WheatearConfig:
    console.print(
        "  [dim]Note: the transform currently runs deterministically. Your LLM key is "
        "saved for later (when AI-assisted translation is enabled) but is not used now.[/dim]"
    )
    provider = questionary.select(
        "Which LLM provider's key should Wheatear save (for later use)?",
        choices=[
            questionary.Choice("anthropic (Claude)", value="anthropic"),
            questionary.Choice("google (Gemini)", value="google"),
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
    from wheatear.creds import llm_key_name
    return _prompt_api_key(
        config.llm_provider,
        llm_key_name(config.llm_provider),
        config.llm_key_env,
    )


# ---------------------------------------------------------------------------
# Manual mode path input
# ---------------------------------------------------------------------------

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
            console.print(
                f"[red]{path} doesn't look like a recognized Copilot Studio export.[/red] Try again."
            )
            continue
        console.print(f"[green]Recognized as a [bold]{fmt}[/bold] export.[/green]")
        return path


def ask_output_path(export_path: Path) -> Path:
    raw = questionary.text(
        "Where should the watsonx Orchestrate output go?",
        default=str(suggest_output_path(export_path)),
    ).ask()
    if _cancelled(raw):
        raise SystemExit(1)
    return Path(raw).expanduser()


# ---------------------------------------------------------------------------
# Auto mode — auto-discover path input
# ---------------------------------------------------------------------------

def ask_auto_output_base() -> Path:
    raw = questionary.text(
        "Output directory for all migrated agents:",
        default="./orchestrate-migration",
    ).ask()
    if _cancelled(raw):
        raise SystemExit(1)
    return Path(raw).expanduser()


# ---------------------------------------------------------------------------
# Hints shown at the end of successful runs
# ---------------------------------------------------------------------------

def _show_connection_panel(pac_version: str, pac_account: str, orchestrate_creds: OrchestrateCredentials | None) -> None:
    """Print a tidy summary panel after PAC auth confirms we're connected."""
    lines = [
        f"  [green]✓[/green]  PAC CLI     [bold]{pac_version}[/bold]",
        f"  [green]✓[/green]  Signed in   [bold]{pac_account}[/bold]",
    ]
    if orchestrate_creds:
        lines.append(
            f"  [green]✓[/green]  Orchestrate [dim]{orchestrate_creds.instance_url}[/dim]"
        )
    console.print(
        Panel("\n".join(lines), title="[bold]Connection[/bold]", border_style=_SLATE, expand=False)
    )


def _show_solutions_table(solutions: list) -> None:
    """Print a rich table of available solutions before asking the user to pick."""
    table = Table(
        border_style=_SLATE,
        show_header=True,
        header_style="bold dim",
        padding=(0, 2),
    )
    table.add_column("Unique Name", style="bold", min_width=22)
    table.add_column("Friendly Name", min_width=22)
    table.add_column("Version", style="dim", width=10)
    for s in solutions:
        table.add_row(s.unique_name, s.friendly_name, s.version)
    console.print(table)


def _show_migration_plan(
    agent_names: list[str],
    solution_names: list[str],
    config: WheatearConfig,
    output_base: Path,
    orchestrate_creds: OrchestrateCredentials | None,
) -> None:
    """Print a summary panel of what will be migrated before processing starts."""
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(style="dim", min_width=18)
    table.add_column(style="bold")
    table.add_row("Agents", ",  ".join(agent_names))
    table.add_row("Solutions", ",  ".join(solution_names))
    table.add_row("LLM provider", config.llm_provider)
    table.add_row("Output", str(output_base.resolve()))
    if orchestrate_creds:
        table.add_row("Deploy to", orchestrate_creds.instance_url)
    console.print(
        Panel(table, title="[bold]Migration Plan[/bold]", border_style=_SLATE, expand=False)
    )


def _show_export_error(sol_label: str, exc: Exception | None) -> None:
    """Print a human-friendly panel for PAC solution export failures."""
    raw = str(exc) if exc else ""
    # Extract the key "Error: ..." line from PAC's verbose output
    key_line = raw
    for line in reversed(raw.splitlines()):
        stripped = line.strip()
        if stripped.startswith("Error:") or stripped.startswith("error:"):
            key_line = stripped
            break

    is_permission = any(x in raw.lower() for x in ("readaccess", "access right", "permission"))
    tip = (
        "  • Check you are an [bold]Environment Maker[/bold] or [bold]Admin[/bold] in this environment.\n"
        "  • The solution may be owned by another account — try [cyan]pac auth select[/cyan] to switch."
        if is_permission
        else "  • Run [cyan]pac solution list[/cyan] to confirm the solution name is correct.\n"
             "  • Check your network connection and Power Platform service health."
    )
    console.print(
        Panel(
            f"[bold]{key_line}[/bold]\n\n{tip}",
            title=f"[bold red]Export failed · {sol_label}[/bold red]",
            border_style="red",
            expand=False,
        )
    )


def _print_orchestrate_import_hint(agent_path: Path, creds: OrchestrateCredentials) -> None:
    console.print(
        Panel(
            f"[bold]Import the generated agent:[/bold]\n\n"
            f"  [cyan]orchestrate agents import -f {agent_path}[/cyan]\n\n"
            f"[dim]Instance:[/dim] {creds.instance_url}\n"
            f"[dim]Auth env var:[/dim] {creds.api_key_env}",
            title="Next: import into watsonx Orchestrate",
            border_style=_SLATE,
        )
    )


def _print_auto_summary(
    results: list[tuple[str, bool, str]],
    orchestrate_creds: OrchestrateCredentials,
) -> None:
    """Rich summary table at the end of an auto migration run."""
    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - ok_count

    title_color = "green" if fail_count == 0 else ("yellow" if ok_count > 0 else "red")
    title = (
        f"[bold {title_color}]Migration complete — "
        f"{ok_count}/{len(results)} agent(s) deployed[/bold {title_color}]"
    )

    table = Table(
        title=title,
        border_style=_SLATE,
        show_header=True,
        header_style="bold",
        show_lines=True,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Agent", style="bold", min_width=20)
    table.add_column("Status", width=10)
    table.add_column("Detail")

    for i, (name, success, detail) in enumerate(results, 1):
        status = "[green]deployed ✓[/green]" if success else "[red]failed ✗[/red]"
        table.add_row(str(i), name, status, detail[:80])

    console.print(table)
    console.print(
        Panel(
            f"[dim]Instance:[/dim]  {orchestrate_creds.instance_url}\n"
            f"[dim]Auth var:[/dim]  [cyan]{orchestrate_creds.api_key_env}[/cyan]",
            title="Orchestrate target",
            border_style=_SLATE,
            expand=False,
        )
    )


# ---------------------------------------------------------------------------
# Shared pipeline helpers
# ---------------------------------------------------------------------------

def _translate_stage(agent, provider) -> None:
    """Run the LLM Translate stage, or the deterministic carry-over when no
    provider is in scope (provider is None). Keeps every pipeline path able to
    run without an LLM -- the AI is the last mile, not a hard dependency.
    """
    if provider is None:
        deterministic_instructions(agent)
    else:
        translate_agent(agent, provider)


def _export_for_target(agent, target: str, output_dir: Path):
    """Export via the platform registry so the correct exporter runs for the
    chosen target (Orchestrate *or* Copilot Studio). This is what makes the
    wizard bidirectional rather than Orchestrate-only.
    """
    return load_exporter(target).export_agent(agent, output_dir)


def _provider_for(config: WheatearConfig, validate: bool = True):
    """Prompt for (and persist) the LLM API key, but DO NOT use it yet.

    LLM-assisted translation is deferred: the wizard runs a fully deterministic
    transform for now. We still ask for the key so it's captured for when
    translation is switched on. Returns None so the pipeline uses the
    deterministic carry-over. `validate` is accepted for call-site
    compatibility but ignored -- validating would make a live API call, and the
    key is intentionally not used yet.

    When AI translation is ready, replace the body with the real provider build.
    """
    if config.llm_provider == "none":
        return None
    resolve_api_key(config)  # prompt or load-from-keychain; kept in env + keychain
    console.print(
        "  [green]✓[/green]  LLM key captured [dim](saved for later — not used; "
        "transform runs deterministically for now)[/dim]"
    )
    return None


def _run_deterministic_stages(export_path: Path, target: str = "orchestrate"):
    """Extract + Map: no LLM provider in scope at all, by construction."""
    with console.status("[bold]Extract: reading export..."):
        import_result = copilot_import_agent(export_path)
    console.print(f"[green]Extract[/green]    {import_result.agent.name}")

    with console.status("[bold]Map: resolving tool/knowledge/connection references..."):
        agent = map_agent(import_result, target_platform=target)
    console.print(
        f"[green]Map[/green]        {len(agent.tools)} tool(s), "
        f"{len(agent.knowledge)} knowledge ref(s), {len(agent.connections)} connection(s)"
    )
    return agent


def _run_ai_and_export_stages(
    agent, output_path: Path, target: str, llm_config: WheatearConfig, provider
) -> Path:
    label = "carrying instructions over (deterministic)" if provider is None else (
        f"synthesizing instructions via {llm_config.llm_provider}"
    )
    with console.status(f"[bold]Translate: {label}..."):
        _translate_stage(agent, provider)
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

    export_result = _export_for_target(agent, target, output_path)
    console.print(Panel.fit(f"Wrote {target} agent to {export_result.agent_path}", style="bold green"))

    if export_result.needs_review:
        console.print(f"[bold yellow]Review needed:[/bold yellow] see {export_result.review_manifest_path}")

    return export_result.agent_path


def _build_final_config(
    llm_config: WheatearConfig,
    orchestrate_creds: OrchestrateCredentials | None,
    saved_config: WheatearConfig | None,
    src_creds: "OrchestrateSrcCredentials | None" = None,
) -> WheatearConfig:
    """Merge wizard-collected settings into a single config object to save."""
    return WheatearConfig(
        llm_provider=llm_config.llm_provider,
        llm_key_env=llm_config.llm_key_env,
        orchestrate_instance_url=(
            orchestrate_creds.instance_url if orchestrate_creds
            else (saved_config.orchestrate_instance_url if saved_config else None)
        ),
        orchestrate_api_key_env=(
            orchestrate_creds.api_key_env if orchestrate_creds
            else (saved_config.orchestrate_api_key_env if saved_config else "ORCHESTRATE_API_KEY")
        ),
        source_orchestrate_url=(
            src_creds.instance_url if src_creds
            else (saved_config.source_orchestrate_url if saved_config else None)
        ),
        source_orchestrate_workspace_id=(
            src_creds.workspace_id if src_creds
            else (getattr(saved_config, "source_orchestrate_workspace_id", None) if saved_config else None)
        ),
        source_env_url=saved_config.source_env_url if saved_config else None,
        source_tenant_id=saved_config.source_tenant_id if saved_config else None,
    )


# ---------------------------------------------------------------------------
# Manual wizard
# ---------------------------------------------------------------------------

def _manual_wizard() -> None:
    source = ask_source_platform()
    target = ask_target_platform(exclude_source_key=source)
    validate_corridor(source, target)

    saved_config = load_config()
    orchestrate_creds: OrchestrateCredentials | None = None
    if target == "orchestrate":
        orchestrate_creds = ask_orchestrate_credentials(saved_config)

    export_path = ask_export_path()
    output_path = ask_output_path(export_path)

    try:
        agent = _run_deterministic_stages(export_path, target=target)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    llm_config = ask_llm_settings(saved_config)
    final_config = _build_final_config(llm_config, orchestrate_creds, saved_config)
    if config_changed(final_config, saved_config):
        save_config(final_config)

    provider = _provider_for(final_config, validate=False)

    try:
        agent_path = _run_ai_and_export_stages(agent, output_path, target, final_config, provider)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    if orchestrate_creds and agent_path:
        _print_orchestrate_import_hint(agent_path, orchestrate_creds)


# ---------------------------------------------------------------------------
# Orchestrate source credentials
# ---------------------------------------------------------------------------

@dataclass
class OrchestrateSrcCredentials:
    """Credentials for the source Orchestrate instance (the one we export FROM)."""
    instance_url: str
    api_key: str          # held in memory only — never written to disk
    workspace_id: str = "00000000-0000-0000-0000-000000000001"


def ask_orchestrate_source_credentials(existing: WheatearConfig | None = None) -> OrchestrateSrcCredentials:
    """Prompt for source Orchestrate instance credentials, pre-filling from saved config."""
    from wheatear.creds import KEY_SRC_ORCHESTRATE

    console.print(
        Panel(
            "[bold]How to find your watsonx Orchestrate credentials:[/bold]\n\n"
            "  1. Sign in to [bold]cloud.ibm.com[/bold]\n"
            "  2. Open [bold]Resource List[/bold] → "
            "[bold]AI / Machine Learning[/bold] → your Orchestrate instance\n"
            "  3. Click [bold]Launch[/bold], then go to [bold]Settings[/bold] "
            "(gear icon, bottom-left)\n"
            "  4. Copy the [bold]Service Instance URL[/bold] and generate an [bold]API Key[/bold]",
            title="[bold]Source Orchestrate — where to find credentials[/bold]",
            border_style=_SLATE,
        )
    )

    saved_url = existing.source_orchestrate_url if existing else None
    url = questionary.text(
        "Source Orchestrate — Service Instance URL:",
        default=saved_url or "",
    ).ask()
    if _cancelled(url) or not url.strip():
        raise SystemExit(1)

    api_key = _prompt_api_key(
        "Source Orchestrate",
        KEY_SRC_ORCHESTRATE,
        "ORCHESTRATE_SOURCE_API_KEY",
    )

    from wheatear.config import DEFAULT_WORKSPACE_ID
    saved_ws = (
        getattr(existing, "source_orchestrate_workspace_id", None)
        if existing else None
    ) or DEFAULT_WORKSPACE_ID
    workspace_id = questionary.text(
        "Workspace ID:",
        default=saved_ws,
    ).ask()
    if _cancelled(workspace_id):
        raise SystemExit(1)

    return OrchestrateSrcCredentials(
        instance_url=url.strip(),
        api_key=api_key,
        workspace_id=workspace_id.strip() or DEFAULT_WORKSPACE_ID,
    )


# ---------------------------------------------------------------------------
# ADK helpers
# ---------------------------------------------------------------------------

def _ensure_adk(adk) -> str:
    """Check ADK is installed; offer to install it if not. Returns version."""
    found, version = adk.check()
    if found:
        return version

    console.print(
        Panel(
            "[bold]The IBM watsonx Orchestrate ADK CLI is required for auto mode.[/bold]\n\n"
            "Install it with:\n\n"
            f"  [cyan]{adk.install_guide()}[/cyan]\n\n"
            "Wheatear can install it for you now.",
            title="[bold yellow]Orchestrate ADK not found[/bold yellow]",
            border_style="yellow",
        )
    )
    do_install = questionary.confirm("Install the Orchestrate ADK now?", default=True).ask()
    if _cancelled(do_install) or not do_install:
        raise SystemExit(1)

    try:
        with console.status("  Running pip install --upgrade ibm-watsonx-orchestrate…"):
            adk.install()
    except Exception as exc:
        console.print(
            Panel(
                f"[bold]{exc}[/bold]\n\n"
                f"Try running manually:\n  [cyan]{adk.install_guide()}[/cyan]",
                title="[bold red]Install failed[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc

    found, version = adk.check()
    if not found:
        console.print(
            Panel(
                "The install succeeded but the `orchestrate` command is still not on PATH.\n"
                "You may need to restart your shell or activate the virtual environment.",
                title="[bold red]orchestrate not on PATH[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1)

    console.print(f"  [green]✓[/green]  Installed {version}")
    return version


_TABLE_PREVIEW_MAX = 25  # agents shown in the preview table before truncating


def _show_agents_table(agents: list, toolkits: list) -> None:
    """Print a compact preview table of discovered agents (and toolkits if any)."""
    preview = agents[:_TABLE_PREVIEW_MAX]
    table = Table(
        border_style=_SLATE,
        show_header=True,
        header_style="bold dim",
        padding=(0, 1),
        expand=False,
        show_lines=True,
    )
    table.add_column("#", style="dim", width=3, no_wrap=True)
    table.add_column("Agent ID", style="bold cyan", min_width=18, max_width=38, no_wrap=True, overflow="ellipsis")
    table.add_column("Display Name", style="bold", min_width=14, max_width=28, no_wrap=True, overflow="ellipsis")
    table.add_column("Description", min_width=20, max_width=44, no_wrap=True, overflow="ellipsis")
    table.add_column("Model", style="dim", min_width=10, max_width=28, no_wrap=True, overflow="ellipsis")
    for i, a in enumerate(preview, 1):
        table.add_row(
            str(i),
            a.name,
            a.display_name or "—",
            a.description or "—",
            a.llm or "—",
        )
    console.print(table)
    if len(agents) > _TABLE_PREVIEW_MAX:
        console.print(
            f"  [dim]… and {len(agents) - _TABLE_PREVIEW_MAX} more — "
            "all will appear in the selection list below.[/dim]"
        )

    if toolkits:
        tk_table = Table(
            border_style=_SLATE,
            show_header=True,
            header_style="bold dim",
            padding=(0, 1),
            expand=False,
            title="[dim]Available toolkits[/dim]",
        )
        tk_table.add_column("Toolkit Name", style="bold", min_width=20, max_width=42, no_wrap=True, overflow="ellipsis")
        tk_table.add_column("Type", style="dim", min_width=12, max_width=24, no_wrap=True)
        for tk in toolkits:
            tk_table.add_row(tk.name, tk.kind or "—")
        console.print(tk_table)


# ---------------------------------------------------------------------------
# Auto wizard — Orchestrate source path (Orchestrate → Orchestrate or other)
# ---------------------------------------------------------------------------

def _expand_agent_graph(selected, all_agents, src_creds, adk, orch_import, export_base):
    """Discover the transitive collaborator closure of the selected agents.

    Exports+imports each reachable agent once (cached), resolving each agent's
    collaborators against the discovered instance agents. Returns the agents in
    leaf-first migration order plus the IR cache, so the main loop can migrate
    the whole multi-agent graph -- collaborators before the agents that use them.
    """
    from wheatear.workflow import assemble_workflow, reachable_ids

    by_name = {a.name: a for a in all_agents}
    by_id = {a.agent_id: a for a in all_agents if a.agent_id}
    ir_cache: dict = {}

    def _fetch(info):
        if info.name in ir_cache:
            return ir_cache[info.name]
        yaml_path = export_base / f"{_safe_dirname(info.name)}.yaml"
        adk.export_agent(
            agent_id=info.agent_id, dest=yaml_path,
            api_key=src_creds.api_key, instance_url=src_creds.instance_url,
            workspace_id=src_creds.workspace_id, agent_name=info.name,
        )
        ir_cache[info.name] = orch_import(yaml_path)
        return ir_cache[info.name]

    def neighbors(name):
        info = by_name.get(name)
        if info is None:
            return []
        try:
            ir = _fetch(info)
        except Exception:
            return []  # unreachable agent: skip, surfaced as a dropped agent below
        out = []
        for collab in ir.agent.collaborators:
            target = by_name.get(collab.ref) or by_id.get(collab.ref)
            if target is not None:
                out.append(target.name)
        return out

    all_names = reachable_ids([a.name for a in selected], neighbors)
    agents_ir = [ir_cache[n].agent for n in all_names if n in ir_cache]
    workflow = assemble_workflow(agents_ir, source_platform="orchestrate")
    ordered_infos = [by_name[a.name] for a in workflow.migration_order() if a.name in by_name]

    selected_names = {a.name for a in selected}
    pulled_in = [i.name for i in ordered_infos if i.name not in selected_names]
    return ordered_infos, ir_cache, pulled_in


def _maybe_ai_repair_and_retry(name, solution_dir, failed_result, config, deployer) -> bool:
    """On a push failure, tell the user there's an issue and -- only with their
    consent -- use the saved LLM key to attempt a fix, then retry the push.
    Returns True if the retry succeeded.
    """
    key = os.environ.get(config.llm_key_env, "")
    if config.llm_provider in ("", "none") or not key:
        console.print(
            "  [yellow]No LLM key available to auto-fix.[/yellow] The transformed files are saved; "
            "fix and import them manually."
        )
        return False

    console.print(
        f"  [yellow]There's an issue with the generated solution files for [bold]{name}[/bold].[/yellow]"
    )
    proceed = questionary.confirm(
        f"Use {config.llm_provider} to attempt a fix and retry the push?", default=True
    ).ask()
    if _cancelled(proceed) or not proceed:
        console.print("  [dim]Skipped AI repair — files left as-is for manual import.[/dim]")
        return False

    from wheatear.llm.factory import build_provider
    from wheatear.repair import repair_solution

    try:
        provider = build_provider(config.llm_provider, key)
        with console.status(f"  Asking {config.llm_provider} to fix the solution…"):
            rep = repair_solution(solution_dir, failed_result.output, provider)
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [red]Repair attempt failed:[/red] {exc}")
        return False

    if not rep.changed:
        console.print(f"  [yellow]No applicable fixes proposed.[/yellow] [dim]{rep.explanation}[/dim]")
        return False

    console.print(
        f"  [green]Applied {len(rep.changed)} fix(es):[/green] {', '.join(rep.changed)}  "
        f"[dim]{rep.explanation}[/dim]"
    )
    with console.status("  Retrying push…"):
        retry = deployer.deploy_solution(solution_dir)
    if retry.success:
        console.print(f"  [green]✓[/green]  {name} pushed to Copilot Studio after repair")
        return True
    console.print(
        Panel(
            f"[bold]{retry.output[:500]}[/bold]",
            title=f"[red]Still failing after repair · {name}[/red]",
            border_style="red",
        )
    )
    return False


def _push_solutions_to_copilot(solutions, config) -> list[tuple[str, bool, str]]:
    """Pack + import each transformed solution into Copilot Studio (PAC is
    already authenticated). Returns (name, success, detail) per agent.
    """
    from wheatear.connectors.copilot_studio import deployer

    console.rule("[bold cyan]Push to Copilot Studio[/bold cyan]", style="dim")
    outcomes: list[tuple[str, bool, str]] = []
    for name, solution_dir in solutions:
        with console.status(f"  Packing + importing [bold]{name}[/bold]…"):
            result = deployer.deploy_solution(solution_dir)
        if result.success:
            console.print(f"  [green]✓[/green]  {name} pushed to Copilot Studio")
            outcomes.append((name, True, "pushed"))
            continue
        console.print(
            Panel(
                f"[bold]{result.output[:500]}[/bold]",
                title=f"[red]Push failed ({result.stage}) · {name}[/red]",
                border_style="red",
            )
        )
        fixed = _maybe_ai_repair_and_retry(name, solution_dir, result, config, deployer)
        outcomes.append((name, fixed, "pushed after repair" if fixed else f"import {result.stage} failed"))
    return outcomes


def _orchestrate_source_wizard() -> None:
    """Auto-discover and migrate agents starting from a watsonx Orchestrate instance.

    Discovery-first flow:
      1. Source Orchestrate credentials (URL + API key + workspace ID)
      2. Connect to source instance (IAM token exchange + REST probe)
      3. Discover agents + toolkits
      4. User selects agents to export
      5. Configure LLM + target credentials
      6. Expand collaborator graph → migrate leaf-first (Import→Map→Translate→Validate→Export) → deploy/save
    """
    from wheatear.connectors.orchestrate import adk_client as adk
    from wheatear.connectors.orchestrate.importer import import_agent as orch_import_agent

    saved_config = load_config()
    TOTAL_STEPS = 6

    # ── Step 1: Source Orchestrate credentials ────────────────────────────────
    _step_header(1, TOTAL_STEPS, "Source Orchestrate credentials")
    src_creds = ask_orchestrate_source_credentials(saved_config)

    # ── Step 2: Connect to source instance ───────────────────────────────────
    _step_header(2, TOTAL_STEPS, "Connect to Orchestrate")

    ok = False
    err = ""
    for _attempt in range(3):
        with console.status("  Authenticating with IBM IAM…"):
            ok, err = adk.probe_connection(
                api_key=src_creds.api_key,
                instance_url=src_creds.instance_url,
                workspace_id=src_creds.workspace_id,
            )
        if ok:
            break
        is_timeout = "timed out" in err.lower() or "timeout" in err.lower()
        console.print(
            Panel(
                f"[bold]{err[:300]}[/bold]\n\n"
                + ("IBM Cloud APIs can be slow — retrying automatically…" if is_timeout else
                   "Check your Service Instance URL and API key."),
                title="[bold red]Connection failed[/bold red]",
                border_style="red",
            )
        )
        if not is_timeout:
            break
        retry = questionary.confirm("Retry connection?", default=True).ask()
        if not retry:
            break
    if not ok:
        raise SystemExit(1)

    console.print(
        Panel(
            f"  [green]✓[/green]  Connected to [bold]{src_creds.instance_url}[/bold]",
            title="[bold]Orchestrate Source Connection[/bold]",
            border_style=_SLATE,
            expand=False,
        )
    )

    # ── Step 3: Discover agents + toolkits ───────────────────────────────────
    _step_header(3, TOTAL_STEPS, "Discover agents & toolkits")
    try:
        with console.status("  Fetching agents via REST API…"):
            agents = adk.list_agents(
                api_key=src_creds.api_key,
                instance_url=src_creds.instance_url,
                workspace_id=src_creds.workspace_id,
            )
    except Exception as exc:
        console.print(
            Panel(
                f"[bold]{exc}[/bold]\n\n"
                "Check that the API key has read access to this instance.",
                title="[bold red]Could not list agents[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc

    toolkits = []
    try:
        with console.status("  Fetching toolkits…"):
            toolkits = adk.list_toolkits(
                api_key=src_creds.api_key,
                instance_url=src_creds.instance_url,
                workspace_id=src_creds.workspace_id,
            )
    except Exception:
        pass

    if not agents:
        console.print(
            Panel(
                "No agents were found in this Orchestrate environment.\n\n"
                "Make sure you are connected to the correct instance and that\n"
                "agents have been created or imported there.",
                title="[yellow]No agents found[/yellow]",
                border_style="yellow",
            )
        )
        raise SystemExit(0)

    console.print(
        f"\n  [dim]{len(agents)} agent(s){f' · {len(toolkits)} toolkit(s)' if toolkits else ''}"
        f" found:[/dim]\n"
    )
    _show_agents_table(agents, toolkits)
    console.print()

    # ── Step 4: Select agents to export ──────────────────────────────────────
    _step_header(4, TOTAL_STEPS, "Select agents to migrate")
    selected_agents = questionary.checkbox(
        "Select agent(s) to migrate:",
        choices=[
            questionary.Choice(
                (a.display_name or a.name)
                + (f"  [{a.name}]" if a.display_name else "")
                + (f"  —  {a.description[:50]}" if a.description else ""),
                value=a,
                checked=False,
            )
            for a in agents
        ],
    ).ask()
    if _cancelled(selected_agents) or not selected_agents:
        console.print("[yellow]No agents selected.[/yellow]")
        raise SystemExit(0)

    # ── Step 5: Configure translation & target ────────────────────────────────
    _step_header(5, TOTAL_STEPS, "Configure translation & target")

    # Show all target platforms except the source. Coming-soon ones are visible
    # (so the user can see the roadmap) but disabled — only implemented targets
    # are selectable.
    _target_choices = [
        questionary.Choice("Export raw YAML to folder (no migration)", value="export-only"),
    ] + [
        questionary.Choice(name, value=key)
        if impl
        else questionary.Choice(f"{name} (coming soon)", value=key, disabled="not yet implemented")
        for name, key, impl in TARGET_PLATFORMS
        if key != "orchestrate"  # exclude same platform as source
    ]
    target = questionary.select(
        "Migrate agents to which platform?",
        choices=_target_choices,
    ).ask()
    if _cancelled(target):
        raise SystemExit(1)

    # ── Export-only shortcut (no pipeline, no LLM, no target credentials) ────
    if target == "export-only":
        _run_export_only(src_creds, selected_agents, adk)
        return

    # ── Target credentials ────────────────────────────────────────────────────
    orchestrate_creds: OrchestrateCredentials | None = None
    pac_account: str | None = None

    if target == "orchestrate":
        orchestrate_creds = ask_orchestrate_credentials(saved_config)

    elif target == "copilot-studio":
        from wheatear.connectors.copilot_studio import pac_client as pac
        pac_version = _ensure_pac(pac)
        pac_account = _ensure_pac_auth(pac)
        console.print(
            Panel(
                f"  [green]✓[/green]  PAC CLI  {pac_version}\n"
                f"  [green]✓[/green]  Signed in as [bold]{pac_account}[/bold]",
                title="[bold]Copilot Studio — PAC connection[/bold]",
                border_style=_SLATE,
                expand=False,
            )
        )

    llm_config = ask_llm_settings(saved_config)
    final_config = _build_final_config(llm_config, orchestrate_creds, saved_config, src_creds)
    if config_changed(final_config, saved_config):
        save_config(final_config)
    provider = _provider_for(final_config)

    output_base = Path(f"./{target}-migration")
    deploy = target == "orchestrate" and orchestrate_creds is not None

    export_base = Path(tempfile.mkdtemp(prefix="wheatear-orch-"))
    try:
        # ── Discover collaborator graph: pull in connected agents, order leaf-first ──
        with console.status("  Discovering connected agents…"):
            ordered_infos, ir_cache, pulled_in = _expand_agent_graph(
                selected_agents, agents, src_creds, adk, orch_import_agent, export_base
            )
        if pulled_in:
            console.print(
                f"  [green]+ {len(pulled_in)} connected agent(s)[/green] pulled in automatically: "
                f"[dim]{', '.join(pulled_in)}[/dim]"
            )
            console.print("  [dim]Migrating leaf-first so collaborators exist before their callers.[/dim]")

        # Show plan (expanded set, in migration order)
        _show_migration_plan(
            [a.name for a in ordered_infos],
            [src_creds.instance_url],
            final_config,
            output_base,
            orchestrate_creds,
        )

        # ── Step 6: Export → pipeline → deploy ───────────────────────────────────
        _step_header(6, TOTAL_STEPS, f"Export & migrate → {target}")
        results: list[tuple[str, bool, str]] = []
        copilot_solutions: list[tuple[str, Path]] = []  # (name, solution_dir) to push via PAC
        _BOT_STAGES = 6 + (1 if deploy else 0)

        def _make_progress() -> Progress:
            return Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            )

        for idx, agent_info in enumerate(ordered_infos, 1):
            console.print(
                f"\n  [bold cyan]Agent {idx}/{len(ordered_infos)}:[/bold cyan]  "
                f"[bold]{agent_info.name}[/bold]"
            )
            yaml_path = export_base / f"{_safe_dirname(agent_info.name)}.yaml"
            _confidence: float | None = None
            _cases: list = []
            _export_result = None
            _validation = None

            with _make_progress() as prog:
                t = prog.add_task("  Starting…", total=_BOT_STAGES)
                try:
                    # Stage 1: Export from Orchestrate to YAML (cached from discovery)
                    prog.update(
                        t,
                        description=f"  [dim][1/{_BOT_STAGES}][/dim]  Export  fetching YAML from Orchestrate",
                    )
                    if agent_info.name not in ir_cache:
                        adk.export_agent(
                            agent_id=agent_info.agent_id,
                            dest=yaml_path,
                            api_key=src_creds.api_key,
                            instance_url=src_creds.instance_url,
                            workspace_id=src_creds.workspace_id,
                            agent_name=agent_info.name,
                        )
                        ir_cache[agent_info.name] = orch_import_agent(yaml_path)
                    prog.advance(t)

                    # Stage 2: Import (Orchestrate YAML → IR; reuse discovery cache)
                    prog.update(
                        t,
                        description=f"  [dim][2/{_BOT_STAGES}][/dim]  Import  reading Orchestrate export",
                    )
                    import_result = ir_cache[agent_info.name]
                    prog.advance(t)

                    # Stage 3: Map (resolved for the chosen target platform)
                    prog.update(
                        t,
                        description=f"  [dim][3/{_BOT_STAGES}][/dim]  Map  resolving tools & knowledge",
                    )
                    ir_agent = map_agent(import_result, target_platform=target)
                    prog.advance(t)

                    # Stage 4: Translate (LLM, or deterministic carry-over)
                    _tlabel = "carrying prompt over" if provider is None else f"{final_config.llm_provider} AI adapting instructions"
                    prog.update(
                        t,
                        description=f"  [dim][4/{_BOT_STAGES}][/dim]  Translate  {_tlabel}",
                    )
                    _translate_stage(ir_agent, provider)
                    _confidence = getattr(ir_agent, "translation_confidence", None)
                    prog.advance(t)

                    # Stage 5: Validate
                    prog.update(
                        t,
                        description=f"  [dim][5/{_BOT_STAGES}][/dim]  Validate  schema check + eval cases",
                    )
                    _validation = validate_agent(ir_agent)
                    _cases = generate_cases(ir_agent)
                    if not _validation.is_valid:
                        errs = "; ".join(
                            f"{i.field}: {i.message}"
                            for i in _validation.issues if i.severity == "error"
                        )
                        raise RuntimeError(errs)
                    prog.advance(t)

                    # Stage 6: Export to the target platform (registry-dispatched)
                    prog.update(
                        t,
                        description=f"  [dim][6/{_BOT_STAGES}][/dim]  Export  writing {target} output",
                    )
                    agent_output_dir = output_base / _safe_dirname(agent_info.name)
                    _export_result = _export_for_target(ir_agent, target, agent_output_dir)
                    # Keep the raw Orchestrate export (full toolkits + every tool
                    # name) alongside the migrated output, outside the importable
                    # solution so it doesn't interfere with a target import.
                    if yaml_path.exists():
                        raw_dest = output_base / "_source-exports" / f"{_safe_dirname(agent_info.name)}.orchestrate.yaml"
                        raw_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(yaml_path, raw_dest)
                    if target == "copilot-studio":
                        copilot_solutions.append((agent_info.name, _export_result.agent_path))
                    prog.advance(t)

                    # Stage 7 (optional): Deploy to target Orchestrate
                    if deploy and orchestrate_creds:
                        prog.update(
                            t,
                            description=f"  [dim][7/{_BOT_STAGES}][/dim]  Deploy  → watsonx Orchestrate",
                        )
                        from wheatear.connectors.orchestrate.deployer import deploy_agent
                        deploy_result = deploy_agent(
                            _export_result.agent_path,
                            orchestrate_creds.instance_url,
                            orchestrate_creds.api_key_env,
                        )
                        prog.advance(t)
                        if deploy_result.success:
                            prog.update(t, description="  [green]✓  Deployed to Orchestrate[/green]")
                            results.append((agent_info.name, True, "Deployed"))
                        else:
                            prog.update(t, description="  [yellow]⚠  Deploy returned non-zero[/yellow]")
                            results.append((agent_info.name, False, deploy_result.output[:80]))
                    else:
                        prog.update(t, description="  [green]✓  Done[/green]")
                        results.append((agent_info.name, True, str(_export_result.agent_path)))

                except Exception as exc:
                    prog.update(t, description=f"  [red]✗  {exc}[/red]")
                    results.append((agent_info.name, False, str(exc)[:80]))

            if _validation:
                for issue in _validation.issues:
                    c = "red" if issue.severity == "error" else "yellow"
                    console.print(f"    [{c}][{issue.severity}][/{c}] {issue.field}: {issue.message}")
            if _confidence is not None:
                extras = f"  ·  {len(_cases)} eval case(s)" if _cases else ""
                console.print(f"    [dim]Translate confidence: {_confidence:.2f}{extras}[/dim]")
            if _export_result and _export_result.needs_review:
                console.print(
                    f"    [yellow]Review manifest:[/yellow] {_export_result.review_manifest_path}"
                )

    finally:
        shutil.rmtree(export_base, ignore_errors=True)

    # ── Push transformed solutions into Copilot Studio (PAC already authed) ──
    if copilot_solutions:
        push_outcomes = _push_solutions_to_copilot(copilot_solutions, final_config)
        # Reflect push results in the per-agent results for the final summary.
        pushed = {n: (ok, detail) for n, ok, detail in push_outcomes}
        results = [
            (n, pushed.get(n, (ok, detail))[0], pushed.get(n, (ok, detail))[1])
            for (n, ok, detail) in results
        ]

    # ── Final summary ──────────────────────────────────────────────────────────
    console.print()
    if orchestrate_creds:
        _print_auto_summary(results, orchestrate_creds)
    else:
        for name, ok, detail in results:
            mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
            console.print(f"  {mark} {name} — {detail}")


# ---------------------------------------------------------------------------
# Auto wizard — PAC CLI path (Copilot Studio → Orchestrate)
# ---------------------------------------------------------------------------

def _scan_solutions(pac, solutions: list, base_dir: Path) -> list[ScannedSolution]:
    """Export + unpack each solution and scan for bots. Returns ScannedSolution for each."""
    results: list[ScannedSolution] = []

    def _progress() -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

    for sol in solutions:
        zip_path = base_dir / f"{sol.unique_name}.zip"
        sol_dir = base_dir / f"{sol.unique_name}-unpacked"
        sol_label = sol.friendly_name or sol.unique_name

        with _progress() as prog:
            t = prog.add_task(f"  [bold]{sol_label}[/bold]  Exporting…", total=2)
            try:
                pac.export_solution(sol.unique_name, zip_path)
                prog.advance(t)
                prog.update(t, description=f"  [bold]{sol_label}[/bold]  Unpacking…")
                pac.unpack_solution(zip_path, sol_dir)
                prog.advance(t)

                bots = pac.list_bots_in_solution(sol_dir)
                if bots:
                    prog.update(
                        t,
                        description=(
                            f"  [bold]{sol_label}[/bold]  "
                            f"[green]{len(bots)} agent(s) found ✓[/green]"
                        ),
                    )
                else:
                    top = pac.list_solution_top_dirs(sol_dir)
                    prog.update(
                        t,
                        description=(
                            f"  [bold]{sol_label}[/bold]  "
                            f"[yellow]no agents found[/yellow]  [dim]{top}[/dim]"
                        ),
                    )
                results.append(
                    ScannedSolution(
                        solution_name=sol.unique_name,
                        solution_label=sol_label,
                        sol_dir=sol_dir,
                        bots=bots,
                    )
                )
            except Exception as exc:
                prog.update(t, description=f"  [bold]{sol_label}[/bold]  [red]Failed[/red]")
                _show_export_error(sol_label, exc)
                results.append(
                    ScannedSolution(
                        solution_name=sol.unique_name,
                        solution_label=sol_label,
                        sol_dir=sol_dir,
                        bots=[],
                        error=str(exc)[:120],
                    )
                )

    return results


def _build_agent_choices(scanned: list[ScannedSolution]) -> list:
    """Build a questionary choice list grouped by solution with Separators."""
    choices: list = []
    for scan in scanned:
        if not scan.bots:
            continue
        choices.append(questionary.Separator(f"  ── {scan.solution_label} ──"))
        for schema, bot_name in scan.bots:
            choices.append(
                questionary.Choice(f"  {bot_name}", value=(scan, schema, bot_name), checked=True)
            )
    return choices


def _auto_wizard() -> None:
    """Auto-discover and migrate agents.

    Dispatches to the correct source-platform sub-wizard based on user choice.
    """
    source = ask_source_platform()

    if source == "orchestrate":
        _orchestrate_source_wizard()
        return

    _copilot_studio_auto_wizard(source)


def _copilot_studio_auto_wizard(source: str) -> None:
    """Auto-discover and migrate agents using the PAC CLI.

    Discovery-first flow:
      1. Target credentials (Orchestrate)
      2. Connect to Power Platform (PAC check + auth)
      3. Browse solutions → user picks which to scan → scan (export+unpack) each
      4. From scan results: user picks specific agents grouped by solution
      5. Configure LLM for translation
      6. Pipeline: Slice → Extract → Map → Translate → Validate → Export → Deploy
    """
    from wheatear.connectors.copilot_studio import pac_client as pac

    target = ask_target_platform(exclude_source_key=source)
    validate_corridor(source, target)

    saved_config = load_config()
    deploy = target == "orchestrate"

    # ── Step 1: Target credentials ────────────────────────────────────────────
    orchestrate_creds: OrchestrateCredentials | None = None
    if deploy:
        _step_header(1, 6, "Target credentials — watsonx Orchestrate")
        orchestrate_creds = ask_orchestrate_credentials(saved_config)

    # ── Step 2: Connect to Power Platform ────────────────────────────────────
    _step_header(2, 6, "Connect to Power Platform")
    pac_version = _ensure_pac(pac)
    pac_account = _ensure_pac_auth(pac)
    _show_connection_panel(pac_version, pac_account, orchestrate_creds)

    # ── Step 3: Browse solutions → select → scan ──────────────────────────────
    _step_header(3, 6, "Browse solutions")
    try:
        with console.status("  Running [cyan]pac solution list[/cyan]…"):
            solutions = pac.list_solutions(unmanaged_only=True)
    except Exception as exc:
        console.print(
            Panel(str(exc), title="[bold red]Could not list solutions[/bold red]", border_style="red")
        )
        raise SystemExit(1) from exc

    if not solutions:
        console.print(
            Panel(
                "No unmanaged solutions found in this environment.\n\n"
                "Agents must be part of a [bold]custom[/bold] (unmanaged) solution before\n"
                "they can be exported. Create one in the Power Platform maker portal\n"
                "and add your agent to it.",
                title="[yellow]No solutions found[/yellow]",
                border_style="yellow",
            )
        )
        raise SystemExit(0)

    console.print(f"\n  [dim]{len(solutions)} unmanaged solution(s) available:[/dim]\n")
    _show_solutions_table(solutions)
    console.print()

    selected_solutions = questionary.checkbox(
        "Select solution(s) to scan for agents (you can pick multiple):",
        choices=[
            questionary.Choice(
                f"{s.unique_name}  [dim]({s.friendly_name}  v{s.version})[/dim]",
                value=s,
                checked=False,
            )
            for s in solutions
        ],
    ).ask()
    if _cancelled(selected_solutions) or not selected_solutions:
        console.print("[yellow]No solutions selected.[/yellow]")
        raise SystemExit(0)

    # All unpacked dirs live in a single temp base that persists through step 6
    scan_base = Path(tempfile.mkdtemp(prefix="wheatear-scan-"))
    try:
        console.print()
        scanned = _scan_solutions(pac, selected_solutions, scan_base)

        # ── Step 4: Select agents from scan results ───────────────────────────
        _step_header(4, 6, "Select agents to migrate")
        all_choices = _build_agent_choices(scanned)

        if not all_choices:
            diag_lines = []
            for s in scanned:
                top = pac.list_solution_top_dirs(s.sol_dir) if s.sol_dir.is_dir() else []
                if s.error:
                    diag_lines.append(f"  [red]✗[/red]  {s.solution_label}: {s.error[:80]}")
                else:
                    diag_lines.append(
                        f"  [yellow]○[/yellow]  {s.solution_label}: no bots/ dir  "
                        f"[dim]layout: {top}[/dim]"
                    )
            console.print(
                Panel(
                    "No agents were found in any of the scanned solutions.\n\n"
                    + "\n".join(diag_lines)
                    + "\n\n[dim]Newer Copilot Studio (generative AI) agents may use a different\n"
                    "directory layout than classic PVA bots. Share the layout above\n"
                    "to help improve detection.[/dim]",
                    title="[yellow]No agents found[/yellow]",
                    border_style="yellow",
                )
            )
            raise SystemExit(0)

        total_found = sum(len(s.bots) for s in scanned if not s.error)
        console.print(
            f"  [green]Found {total_found} agent(s)[/green] across "
            f"{len([s for s in scanned if s.bots])} solution(s):\n"
        )

        selected_items: list[tuple[ScannedSolution, str, str]] = questionary.checkbox(
            "Select the agent(s) to migrate:",
            choices=all_choices,
        ).ask()
        if _cancelled(selected_items) or not selected_items:
            console.print("[yellow]No agents selected.[/yellow]")
            raise SystemExit(0)

        # ── Step 5: Configure translation ─────────────────────────────────────
        _step_header(5, 6, "Configure translation")
        llm_config = ask_llm_settings(saved_config)
        final_config = _build_final_config(llm_config, orchestrate_creds, saved_config)
        if config_changed(final_config, saved_config):
            save_config(final_config)
        provider = _provider_for(final_config)

        output_base = Path("./orchestrate-migration")

        # ── Step 6: Migrate & deploy ───────────────────────────────────────────
        _step_header(6, 6, "Migrate & deploy")
        agent_names = [item[2] for item in selected_items]
        sol_names = list(dict.fromkeys(item[0].solution_name for item in selected_items))
        _show_migration_plan(agent_names, sol_names, final_config, output_base, orchestrate_creds)

        results: list[tuple[str, bool, str]] = []
        _BOT_STAGES = 6 + (1 if deploy and orchestrate_creds else 0)

        def _make_progress() -> Progress:
            return Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=False,
            )

        for idx, (scan, bot_schema, bot_name) in enumerate(selected_items, 1):
            console.print(
                f"\n  [bold cyan]Agent {idx}/{len(selected_items)}:[/bold cyan]  "
                f"[bold]{bot_name}[/bold]  [dim]({scan.solution_label})[/dim]"
            )
            bot_slice_dir = scan_base / f"slice-{bot_schema}"
            _confidence: float | None = None
            _cases: list = []
            _export_result = None
            _validation = None

            with _make_progress() as prog:
                t = prog.add_task("  Starting…", total=_BOT_STAGES)
                try:
                    prog.update(t, description=f"  [dim][1/{_BOT_STAGES}][/dim]  Slice  isolating bot files")
                    pac.create_bot_slice(scan.sol_dir, bot_schema, bot_slice_dir)
                    prog.advance(t)

                    prog.update(t, description=f"  [dim][2/{_BOT_STAGES}][/dim]  Extract  reading Copilot Studio export")
                    import_result = import_agent(bot_slice_dir)
                    prog.advance(t)

                    prog.update(t, description=f"  [dim][3/{_BOT_STAGES}][/dim]  Map  resolving tools, connections & knowledge")
                    agent = map_agent(import_result, target_platform=target)
                    prog.advance(t)

                    _tlabel = "carrying prompt over" if provider is None else f"{final_config.llm_provider} AI  (may take ~10 s)"
                    prog.update(t, description=f"  [dim][4/{_BOT_STAGES}][/dim]  Translate  {_tlabel}")
                    _translate_stage(agent, provider)
                    _confidence = getattr(agent, "translation_confidence", None)
                    prog.advance(t)

                    prog.update(t, description=f"  [dim][5/{_BOT_STAGES}][/dim]  Validate  schema check + eval cases")
                    _validation = validate_agent(agent)
                    _cases = generate_cases(agent)
                    if not _validation.is_valid:
                        errs = "; ".join(
                            f"{i.field}: {i.message}"
                            for i in _validation.issues if i.severity == "error"
                        )
                        raise RuntimeError(errs)
                    prog.advance(t)

                    prog.update(t, description=f"  [dim][6/{_BOT_STAGES}][/dim]  Export  writing {target} output")
                    agent_output_dir = output_base / _safe_dirname(bot_name)
                    _export_result = _export_for_target(agent, target, agent_output_dir)
                    prog.advance(t)

                    if deploy and orchestrate_creds:
                        prog.update(t, description=f"  [dim][7/{_BOT_STAGES}][/dim]  Deploy  → watsonx Orchestrate")
                        from wheatear.connectors.orchestrate.deployer import deploy_agent
                        deploy_result = deploy_agent(
                            _export_result.agent_path,
                            orchestrate_creds.instance_url,
                            orchestrate_creds.api_key_env,
                        )
                        prog.advance(t)
                        if deploy_result.success:
                            prog.update(t, description="  [green]✓  Deployed to Orchestrate[/green]")
                            results.append((bot_name, True, "Deployed"))
                        else:
                            prog.update(t, description="  [yellow]⚠  Deploy returned non-zero[/yellow]")
                            results.append((bot_name, False, deploy_result.output[:80]))
                    else:
                        prog.update(t, description="  [green]✓  Done[/green]")
                        results.append((bot_name, True, str(_export_result.agent_path)))

                except Exception as exc:
                    prog.update(t, description=f"  [red]✗  {exc}[/red]")
                    results.append((bot_name, False, str(exc)[:80]))

            if _validation:
                for issue in _validation.issues:
                    c = "red" if issue.severity == "error" else "yellow"
                    console.print(f"    [{c}][{issue.severity}][/{c}] {issue.field}: {issue.message}")
            if _confidence is not None:
                extras = f"  ·  {len(_cases)} eval case(s)" if _cases else ""
                console.print(f"    [dim]Translate confidence: {_confidence:.2f}{extras}[/dim]")
            if _export_result and _export_result.needs_review:
                console.print(f"    [yellow]Review manifest:[/yellow] {_export_result.review_manifest_path}")

        # ── Final summary ──────────────────────────────────────────────────────
        console.print()
        if orchestrate_creds:
            _print_auto_summary(results, orchestrate_creds)
        else:
            for name, ok, detail in results:
                mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
                console.print(f"  {mark} {name} — {detail}")

    finally:
        shutil.rmtree(scan_base, ignore_errors=True)


def _ensure_pac(pac) -> str:
    """Check PAC CLI is installed; offer to install if missing. Returns version string."""
    found, version = pac.check()
    if found:
        return version

    console.print(
        Panel(
            "[bold]Microsoft Power Platform CLI (pac) is required.[/bold]\n\n"
            "Wheatear can install it now with:\n\n"
            f"  [cyan]{pac.install_guide()}[/cyan]\n\n"
            "[dim]Requires the .NET SDK — download from https://dot.net/download if missing.[/dim]",
            title="[bold yellow]PAC CLI not found[/bold yellow]",
            border_style="yellow",
        )
    )
    do_install = questionary.confirm("Install the PAC CLI now?", default=True).ask()
    if _cancelled(do_install) or not do_install:
        raise SystemExit(1)

    try:
        with console.status(f"  Running {pac.install_guide()}…"):
            pac.install()
    except Exception as exc:
        console.print(
            Panel(
                f"[bold]{exc}[/bold]\n\n"
                f"Try running manually:\n  [cyan]{pac.install_guide()}[/cyan]",
                title="[bold red]Install failed[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc

    found, version = pac.check()
    if not found:
        tools_path = pac.dotnet_tools_path()
        console.print(
            Panel(
                f"The install succeeded but [bold]pac[/bold] is still not found.\n\n"
                f"Add the dotnet tools directory to your shell's PATH and re-run:\n\n"
                f"  [cyan]export PATH=\"{tools_path}:$PATH\"[/cyan]",
                title="[bold red]pac not on PATH[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1)

    console.print(f"  [green]✓[/green]  PAC CLI {version} installed")
    return version


def _ensure_pac_auth(pac) -> str:
    """Check PAC auth; run device code flow in TUI if needed. Returns account name."""
    authed, account = pac.auth_status()
    if authed:
        return account

    console.print("[bold]Not authenticated — starting device code sign-in…[/bold]")

    def _show_code(msg: str) -> None:
        console.print(
            Panel(msg, title="[bold]Sign in to Microsoft[/bold]", border_style=_AMBER)
        )
        console.print("[dim]Waiting for you to complete sign-in in your browser…[/dim]")

    try:
        return pac.do_device_auth(_show_code)
    except Exception as exc:
        console.print(
            Panel(
                f"[bold]{exc}[/bold]\n\n"
                "Run [cyan]pac auth create --deviceCode[/cyan] manually to diagnose.",
                title="[bold red]Authentication failed[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc


def _validate_llm_key(config: WheatearConfig) -> None:
    """Lightweight auth check against the LLM provider (lists models, no tokens used)."""
    from wheatear.llm.factory import validate_api_key
    key = os.environ.get(config.llm_key_env, "")
    try:
        with console.status(f"  Validating {config.llm_provider} API key…"):
            validate_api_key(config.llm_provider, key)
        console.print(f"  [green]✓[/green]  {config.llm_provider} API key accepted")
    except ValueError as exc:
        console.print(
            Panel(
                f"[bold]{exc}[/bold]\n\n"
                f"The key was read from [cyan]{config.llm_key_env}[/cyan].\n"
                "Check you pasted it correctly and that the account has API access.",
                title="[bold red]API key rejected[/bold red]",
                border_style="red",
            )
        )
        raise SystemExit(1) from exc
    except Exception as exc:
        # Network hiccup / rate-limit / SDK issue — warn but don't block
        console.print(
            f"  [yellow]⚠[/yellow]  Could not reach {config.llm_provider} to validate key "
            f"[dim]({exc})[/dim] — continuing anyway"
        )


def _build_solution_choices(
    solutions: list, selected_copilot_names: set[str]
) -> list[questionary.Choice]:
    """Return questionary choices for the solution list.

    Pre-checks solutions whose unique_name or friendly_name (case-insensitive)
    contains any selected copilot name — most likely candidates to export.
    Puts pre-checked solutions first so they appear at the top of the list.
    """
    checked, unchecked = [], []
    for s in solutions:
        name_lower = s.unique_name.lower()
        friendly_lower = s.friendly_name.lower()
        pre_check = any(
            cn in name_lower or cn in friendly_lower
            for cn in selected_copilot_names
        )
        label = f"{s.unique_name}  ({s.friendly_name})"
        choice = questionary.Choice(label, value=s, checked=pre_check)
        (checked if pre_check else unchecked).append(choice)
    return checked + unchecked


def _safe_dirname(name: str) -> str:
    """Convert an agent display name to a safe directory name."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")


def _run_export_only(src_creds: "OrchestrateSrcCredentials", selected_agents: list, adk) -> None:
    """Dump raw REST YAML for each selected agent to ./orchestrate-exports/<name>/."""
    from pathlib import Path

    output_base = Path("./orchestrate-exports")
    output_base.mkdir(parents=True, exist_ok=True)

    console.print(
        f"\n  Saving [bold]{len(selected_agents)}[/bold] agent(s) to "
        f"[bold cyan]{output_base.resolve()}[/bold cyan]\n"
    )

    ok = 0
    fail = 0
    for agent_info in selected_agents:
        agent_dir = output_base / _safe_dirname(agent_info.name)
        agent_dir.mkdir(parents=True, exist_ok=True)
        dest = agent_dir / "agent.yaml"

        with console.status(f"  Exporting [bold]{agent_info.name}[/bold]…"):
            try:
                adk.export_agent(
                    agent_id=agent_info.agent_id,
                    dest=dest,
                    api_key=src_creds.api_key,
                    instance_url=src_creds.instance_url,
                    workspace_id=src_creds.workspace_id,
                    agent_name=agent_info.name,
                )
                console.print(f"  [green]✓[/green]  {agent_info.name}  →  {dest}")
                ok += 1
            except Exception as exc:
                console.print(f"  [red]✗[/red]  {agent_info.name}: {exc}")
                fail += 1

    console.print()
    if fail == 0:
        console.print(f"  [bold green]All {ok} agent(s) exported.[/bold green]")
    else:
        console.print(f"  [bold yellow]{ok} exported, {fail} failed.[/bold yellow]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    print_banner(console)

    mode = questionary.select(
        "How do you want to migrate?",
        choices=[
            questionary.Choice(
                "Auto — connect to source platform, discover all agents, migrate & deploy",
                value="auto",
            ),
            questionary.Choice(
                "Manual — provide a local path or GitHub URL to an existing export",
                value="manual",
            ),
        ],
    ).ask()
    if _cancelled(mode):
        raise SystemExit(1)

    if mode == "auto":
        _auto_wizard()
    else:
        _manual_wizard()

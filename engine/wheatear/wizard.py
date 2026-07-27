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

from wheatear.banner import print_banner, print_compact_header
from wheatear.config import WheatearConfig, load_config, save_config
from wheatear.onboarding import ensure_corridor_tools, needs_onboarding, run_onboarding
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
from wheatear.tui import flush_input

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


BACK = object()  # sentinel: a step function returns this to mean "go back"


def _clear_step(step_n: int, total: int, label: str) -> None:
    """Clear the screen and redraw a compact header + step marker, so each
    step replaces the last one on screen instead of stacking underneath it.

    Uses the one-line compact header rather than the full splash logo --
    the full header is ~13 lines by itself, and repeating it on every step
    easily pushes step content past a standard terminal's height, which
    looks identical to "didn't clear" even when it genuinely did.
    """
    flush_input()
    console.clear()
    print_compact_header(console)
    _step_header(step_n, total, label)


def _clear_section(label: str) -> None:
    """Same clear-and-redraw as _clear_step, for screens that sit outside a
    numbered sequence (choosing the corridor before a flow's steps begin) --
    numbering them "1/2" only to restart at "1/7" a moment later would read
    as the wizard having reset itself."""
    flush_input()
    console.clear()
    print_compact_header(console)
    console.rule(f"[bold cyan]{label}[/bold cyan]", style="dim")


def _with_back_choice(
    choices: list[questionary.Choice], allow_back: bool, label: str = "◀ Back to previous step"
) -> list[questionary.Choice]:
    """Append a selectable '◀ Back' choice to a select/checkbox prompt's
    choices when this step isn't the first one in its flow."""
    if not allow_back:
        return choices
    return [*choices, questionary.Separator(), questionary.Choice(label, value=BACK)]


def _back_hint(allow_back: bool) -> None:
    """Shown above a free-text prompt that supports going back -- there's no
    choice list to append '◀ Back' to, so a typed sentinel does the job."""
    if allow_back:
        console.print("  [dim]Type :back and press Enter to return to the previous step.[/dim]")


def _is_back(raw: str) -> bool:
    """True if the user typed the :back sentinel.

    Checked as a *suffix*, not an exact match: questionary.text pre-fills a
    default value with the cursor at the end, so typing ":back" over a
    remembered/suggested value appends rather than replaces -- an exact
    match would silently treat "some/path:back" as a literal path instead
    of recognizing the sentinel.
    """
    stripped = raw.strip().lower()
    return stripped.endswith(":back") or stripped.endswith(":b")


# Selected rows get a formatted-text title (a [(style, text)] list, which
# questionary passes straight through to prompt_toolkit) so a picked row is
# green and boxed rather than differing by one faint glyph. Unselected rows
# stay plain strings on purpose: questionary only applies its own
# "class:highlighted" style to string titles, so leaving them plain is what
# keeps the cursor row visibly highlighted as you move through the list.
_PICKED_STYLE = "fg:ansigreen bold"

# Everything the selection screen draws around the item rows: the caller's
# header + rule (2), the summary block (3), the prompt line (1), and the
# menu's own fixed rows -- search, clear-filter, select-all, two separators,
# both pager rows, confirm, back (9). Deliberately exact: if the choice list
# outgrows the terminal, prompt_toolkit scrolls it, and the first thing to
# scroll out of sight is the Confirm row at the bottom.
_MENU_CHROME_ROWS = 16


def _match_filter(haystack: str, terms: list[str]) -> bool:
    return all(t in haystack for t in terms)


def _multiselect_menu(
    prompt: str,
    items: list,
    label_fn,
    back_label: str = "◀ Back to previous step",
    key_fn=None,
    preselected: set | None = None,
    redraw=None,
    noun: str = "item",
    context: str = "",
    page_size: int | None = None,
):
    """Enter-driven multi-select: press Enter on a row to toggle it (not
    Space, which isn't discoverable and is inconsistent with every other
    single-key-per-choice menu in this wizard). A distinct 'Confirm' choice
    is the only thing that actually finishes the selection.

    Built to stay usable against a real enterprise inventory rather than a
    demo list of five:

      * a type-to-filter row, so finding one agent among thousands doesn't
        mean scrolling to it;
      * fixed-size pages, so the menu never grows taller than the terminal
        (prompt_toolkit would scroll it, but a scrolled list hides the
        Confirm row -- the one thing the user is looking for);
      * "Select all" scoped to the current filter, which is what makes
        "migrate everything matching X" a two-keystroke operation;
      * a running summary of what's picked, printed above the menu.

    `redraw` is called before each render to repaint the screen underneath
    (see _clear_step): each toggle is a fresh questionary prompt, and without
    it every keystroke would leave another answered "? Select agent(s) ..."
    line stacking down the screen.

    `preselected` (compared via `key_fn`, identity by default) lets a caller
    re-show this menu with a prior answer already checked -- e.g. after the
    user went back past this step and returned, rather than starting blank.

    Returns the chosen subset of `items`, or BACK.
    """
    key_fn = key_fn or (lambda item: item)
    keys = [key_fn(item) for item in items]
    labels = [label_fn(item) for item in items]
    haystacks = [label.lower() for label in labels]
    selected: set = {k for k in (preselected or set()) if k in set(keys)}

    if page_size is None:
        page_size = max(5, min(15, console.height - _MENU_CHROME_ROWS))

    query = ""
    page = 0
    # Must start on a choice that's selectable on the very first render --
    # Confirm is disabled until at least one item is picked, so it can't be
    # the initial default (questionary rejects a default pointing at a
    # disabled choice).
    last_value: object = "__search__"

    while True:
        terms = query.lower().split()
        matches = [i for i in range(len(items)) if _match_filter(haystacks[i], terms)]
        total_pages = max(1, -(-len(matches) // page_size))
        page = min(page, total_pages - 1)
        window = matches[page * page_size : (page + 1) * page_size]

        matching_keys = {keys[i] for i in matches}
        all_matching_picked = bool(matches) and matching_keys <= selected

        if redraw:
            redraw()
        _print_selection_summary(
            labels, keys, selected, matches, items, query, noun, context, page, total_pages
        )

        choices: list = []
        if query:
            choices.append(
                questionary.Choice(f'🔎 Filter: "{query}"  —  edit', value="__search__")
            )
            choices.append(questionary.Choice("✕ Clear filter", value="__clear_search__"))
        else:
            choices.append(
                questionary.Choice(f"🔎 Search / filter {noun}s by name…", value="__search__")
            )
        if matches:
            scope = f"{len(matches)} matching" if query else str(len(items))
            choices.append(
                questionary.Choice(
                    f"{'✓ Deselect' if all_matching_picked else '○ Select'} all {scope}",
                    value="__toggle_all__",
                )
            )
        choices.append(questionary.Separator())

        for i in window:
            if keys[i] in selected:
                choices.append(
                    questionary.Choice([(_PICKED_STYLE, f"[✓] {labels[i]}")], value=i)
                )
            else:
                choices.append(questionary.Choice(f"[ ] {labels[i]}", value=i))
        if not matches:
            choices.append(
                questionary.Choice(
                    f"(no {noun} matches this filter)", value="__none__", disabled="no matches"
                )
            )

        choices.append(questionary.Separator())
        if total_pages > 1:
            if page > 0:
                choices.append(questionary.Choice("▲ Previous page", value="__prev__"))
            if page < total_pages - 1:
                choices.append(questionary.Choice("▼ Next page", value="__next__"))

        count = len(selected)
        if count:
            choices.append(
                questionary.Choice(
                    [(_PICKED_STYLE, f"✅ Confirm — migrate {count} {noun}{'s' if count != 1 else ''}")],
                    value="__confirm__",
                )
            )
        else:
            choices.append(
                questionary.Choice(
                    "✅ Confirm",
                    value="__confirm__",
                    disabled=f"select at least one {noun} first",
                )
            )
        choices.append(questionary.Choice(back_label, value=BACK))

        # Keeping the cursor where it was only works while that row still
        # exists -- paging or filtering can retire it, and questionary raises
        # on a default that isn't a selectable value.
        selectable = {
            c.value for c in choices if isinstance(c, questionary.Choice) and not c.disabled
        }
        if last_value not in selectable:
            last_value = "__search__"

        flush_input()
        pick = questionary.select(prompt, choices=choices, default=last_value).ask()
        if _cancelled(pick):
            raise SystemExit(1)
        last_value = pick

        if pick is BACK:
            return BACK
        if pick == "__confirm__":
            return [item for item, k in zip(items, keys) if k in selected]
        if pick == "__search__":
            flush_input()
            typed = questionary.text(
                f"Filter {noun}s (substring match, blank shows all):", default=query
            ).ask()
            query = "" if _cancelled(typed) else typed.strip()
            page = 0
            last_value = "__search__"
            continue
        if pick == "__clear_search__":
            query = ""
            page = 0
            last_value = "__search__"
            continue
        if pick == "__prev__":
            page -= 1
            last_value = "__search__"
            continue
        if pick == "__next__":
            page += 1
            last_value = "__search__"
            continue
        if pick == "__toggle_all__":
            # Scoped to what's on screen: with a filter active, "select all"
            # meaning "all 100k" would be a destructive surprise.
            if all_matching_picked:
                selected -= matching_keys
            else:
                selected |= matching_keys
            continue
        selected.symmetric_difference_update({keys[pick]})


def _print_selection_summary(
    labels: list[str],
    keys: list,
    selected: set,
    matches: list[int],
    items: list,
    query: str,
    noun: str,
    context: str,
    page: int,
    total_pages: int,
) -> None:
    """The at-a-glance answer to "what have I actually picked?" -- row markers
    alone are easy to lose track of once the list is filtered and paged, and
    a selection made three pages ago is otherwise invisible.

    Exactly three lines tall (blank + counts + picks), because _MENU_CHROME_ROWS
    budgets for it when sizing a page.
    """
    picked_labels = [labels[i] for i in range(len(items)) if keys[i] in selected]
    shown = ", ".join(label.split("  ")[0] for label in picked_labels[:4])
    if len(picked_labels) > 4:
        shown += f", +{len(picked_labels) - 4} more"

    line = (
        f"  [bold green]{len(picked_labels)} selected[/bold green]"
        f"  [dim]of {len(items)} {noun}s[/dim]"
    )
    if context:
        line += f"  [dim]· {context}[/dim]"
    if query:
        line += f'  [dim]·[/dim]  [bold]{len(matches)}[/bold] [dim]matching "{query}"[/dim]'
    if total_pages > 1:
        line += f"  [dim]· page {page + 1}/{total_pages}[/dim]"

    console.print()
    console.print(line)
    console.print(f"  [green]{shown}[/green]" if shown else "  [dim]nothing selected yet[/dim]")


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

def ask_source_platform(
    allow_back: bool = False,
    back_label: str = "◀ Back to previous step",
    default: str | None = None,
) -> str:
    result = questionary.select(
        "Which platform are you migrating from?",
        choices=_with_back_choice(_platform_choices(SOURCE_PLATFORMS), allow_back, back_label),
        default=default,
    ).ask()
    if _cancelled(result):
        raise SystemExit(1)
    return result


def ask_target_platform(
    exclude_source_key: str | None = None,
    allow_back: bool = False,
    back_label: str = "◀ Back to previous step",
    default: str | None = None,
    extra_choices: list | None = None,
) -> str:
    """`extra_choices` are prepended non-platform destinations (currently the
    Orchestrate flow's "export to folder, don't migrate" escape hatch)."""
    choices = [p for p in TARGET_PLATFORMS if p[1] != exclude_source_key]
    result = questionary.select(
        "Which platform are you migrating to?",
        choices=_with_back_choice(
            (extra_choices or []) + _platform_choices(choices), allow_back, back_label
        ),
        # A remembered choice only applies if it's still a valid option here
        # (e.g. not the platform just picked as the source).
        default=default if default != exclude_source_key else None,
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


def ask_orchestrate_credentials(
    existing: WheatearConfig | None,
    allow_back: bool = False,
    default_url: str | None = None,
) -> OrchestrateCredentials:
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

    _back_hint(allow_back)
    saved_url = existing.orchestrate_instance_url if existing else None
    # Prefer what the user already typed this session (if they're revisiting
    # this step after going back) over whatever's persisted from a prior run.
    url = questionary.text("Service Instance URL:", default=default_url or saved_url or "").ask()
    if _cancelled(url):
        raise SystemExit(1)
    if allow_back and _is_back(url):
        return BACK
    if not url.strip():
        raise SystemExit(1)

    api_key_env = (existing.orchestrate_api_key_env if existing else None) or "ORCHESTRATE_API_KEY"
    _prompt_api_key("Target Orchestrate", KEY_TGT_ORCHESTRATE, api_key_env)

    return OrchestrateCredentials(instance_url=url.strip(), api_key_env=api_key_env)




# ---------------------------------------------------------------------------
# LLM settings
# ---------------------------------------------------------------------------

def ask_llm_settings(
    existing: WheatearConfig | None, allow_back: bool = False, default: str | None = None
) -> WheatearConfig:
    console.print(
        "  [dim]Note: the transform currently runs deterministically. Your LLM key is "
        "saved for later (when AI-assisted translation is enabled) but is not used now.[/dim]"
    )
    provider = questionary.select(
        "Which LLM provider's key should Wheatear save (for later use)?",
        choices=_with_back_choice(
            [
                questionary.Choice("anthropic (Claude)", value="anthropic"),
                questionary.Choice("google (Gemini)", value="google"),
                questionary.Choice("openai", value="openai", disabled="not yet implemented"),
                questionary.Choice("watsonx.ai", value="watsonx", disabled="not yet implemented"),
            ],
            allow_back,
        ),
        default=default or "anthropic",
    ).ask()
    if _cancelled(provider):
        raise SystemExit(1)
    if provider is BACK:
        return BACK

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

def ask_export_path(allow_back: bool = False, default: str | None = None) -> Path:
    _back_hint(allow_back)
    while True:
        raw = questionary.text(
            "GitHub repo URL or local path to the export:", default=default or ""
        ).ask()
        if _cancelled(raw):
            raise SystemExit(1)
        if allow_back and _is_back(raw):
            return BACK
        default = raw  # a failed attempt below shouldn't erase what they just typed

        try:
            # A GitHub URL means a clone here -- easily several seconds.
            with console.status("  Fetching the export…", spinner="dots"):
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


def ask_output_path(export_path: Path, allow_back: bool = False, default: str | None = None) -> Path:
    _back_hint(allow_back)
    raw = questionary.text(
        "Where should the watsonx Orchestrate output go?",
        default=default or str(suggest_output_path(export_path)),
    ).ask()
    if _cancelled(raw):
        raise SystemExit(1)
    if allow_back and _is_back(raw):
        return BACK
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


def _export_for_target(agent, target: str, output_dir: Path, llm: str | None = None):
    """Export via the platform registry so the correct exporter runs for the
    chosen target (Orchestrate *or* Copilot Studio). This is what makes the
    wizard bidirectional rather than Orchestrate-only.

    `llm` (optional) is the explicit target model chosen upstream (e.g. via the
    model matrix); only the Orchestrate exporter accepts it. When None, the
    exporter falls back to its own static model resolution.
    """
    exporter = load_exporter(target)
    if target == "orchestrate" and llm is not None:
        return exporter.export_agent(agent, output_dir, llm=llm)
    return exporter.export_agent(agent, output_dir)


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
    agent, output_path: Path, target: str, llm_config: WheatearConfig, provider, llm: str | None = None
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

    export_result = _export_for_target(agent, target, output_path, llm=llm)
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
        onboarding_completed=saved_config.onboarding_completed if saved_config else False,
    )


# ---------------------------------------------------------------------------
# Manual wizard
# ---------------------------------------------------------------------------

def _manual_wizard() -> bool:
    """Gather every answer via a back-navigable step sequence (each step
    clears and redraws in place; every step -- including the first -- offers
    a way back), then run the pipeline once, uninterrupted, against the
    collected answers.

    Returns True if the user backed out of step 1 entirely (the caller
    should return to migration-mode selection instead of exiting), False
    once the pipeline has run.
    """
    saved_config = load_config()
    TOTAL = 6
    answers: dict = {}
    step = 1

    while True:
        if step == 1:
            _clear_step(1, TOTAL, "Source platform")
            source = ask_source_platform(
                allow_back=True, back_label="◀ Back to migration mode",
                default=answers.get("source"),
            )
            if source is BACK:
                return True
            answers["source"] = source
            step = 2

        elif step == 2:
            _clear_step(2, TOTAL, "Target platform")
            target = ask_target_platform(
                exclude_source_key=answers["source"], allow_back=True,
                default=answers.get("target"),
            )
            if target is BACK:
                step = 1
                continue
            try:
                validate_corridor(answers["source"], target)
            except SystemExit:
                questionary.press_any_key_to_continue("Press any key to try again...").ask()
                continue  # redraw step 2 and re-ask
            answers["target"] = target
            if target == "orchestrate":
                step = 3
            else:
                answers["orchestrate_creds"] = None
                step = 4

        elif step == 3:  # only reached when target == "orchestrate"
            _clear_step(3, TOTAL, "Target credentials")
            existing_creds = answers.get("orchestrate_creds")
            creds = ask_orchestrate_credentials(
                saved_config, allow_back=True,
                default_url=existing_creds.instance_url if existing_creds else None,
            )
            if creds is BACK:
                step = 2
                continue
            answers["orchestrate_creds"] = creds
            step = 4

        elif step == 4:
            _clear_step(4, TOTAL, "Export location")
            existing_path = answers.get("export_path")
            export_path = ask_export_path(
                allow_back=True, default=str(existing_path) if existing_path else None
            )
            if export_path is BACK:
                step = 3 if answers["target"] == "orchestrate" else 2
                continue
            answers["export_path"] = export_path
            step = 5

        elif step == 5:
            _clear_step(5, TOTAL, "Output location")
            existing_output = answers.get("output_path")
            output_path = ask_output_path(
                answers["export_path"], allow_back=True,
                default=str(existing_output) if existing_output else None,
            )
            if output_path is BACK:
                step = 4
                continue
            answers["output_path"] = output_path
            step = 6

        else:  # step == 6
            _clear_step(6, TOTAL, "LLM settings (for later use)")
            existing_llm = answers.get("llm_config")
            llm_config = ask_llm_settings(
                saved_config, allow_back=True,
                default=existing_llm.llm_provider if existing_llm else None,
            )
            if llm_config is BACK:
                step = 5
                continue
            answers["llm_config"] = llm_config
            break

    target = answers["target"]
    orchestrate_creds: OrchestrateCredentials | None = answers["orchestrate_creds"]

    try:
        agent = _run_deterministic_stages(answers["export_path"], target=target)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    final_config = _build_final_config(answers["llm_config"], orchestrate_creds, saved_config)
    if config_changed(final_config, saved_config):
        save_config(final_config)

    provider = _provider_for(final_config, validate=False)

    try:
        agent_path = _run_ai_and_export_stages(
            agent, answers["output_path"], target, final_config, provider
        )
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise SystemExit(1) from exc

    if orchestrate_creds and agent_path:
        _print_orchestrate_import_hint(agent_path, orchestrate_creds)

    return False


# ---------------------------------------------------------------------------
# Orchestrate source credentials
# ---------------------------------------------------------------------------

@dataclass
class OrchestrateSrcCredentials:
    """Credentials for the source Orchestrate instance (the one we export FROM)."""
    instance_url: str
    api_key: str          # held in memory only — never written to disk
    workspace_id: str = "00000000-0000-0000-0000-000000000001"


def ask_orchestrate_source_credentials(
    existing: WheatearConfig | None = None,
    allow_back: bool = False,
    back_label: str = "◀ Back to previous step",
    default_url: str | None = None,
    default_workspace: str | None = None,
):
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

    if allow_back:
        console.print(f"  [dim]Type :back and press Enter to {back_label[2:].lower()}.[/dim]")
    saved_url = existing.source_orchestrate_url if existing else None
    url = questionary.text(
        "Source Orchestrate — Service Instance URL:",
        default=default_url or saved_url or "",
    ).ask()
    if _cancelled(url):
        raise SystemExit(1)
    if allow_back and _is_back(url):
        return BACK
    if not url.strip():
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
        default=default_workspace or saved_ws,
    ).ask()
    if _cancelled(workspace_id):
        raise SystemExit(1)

    return OrchestrateSrcCredentials(
        instance_url=url.strip(),
        api_key=api_key,
        workspace_id=workspace_id.strip() or DEFAULT_WORKSPACE_ID,
    )


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


def _orchestrate_source_wizard(target: str) -> bool:
    """Auto-discover and migrate agents starting from a watsonx Orchestrate instance.

    Discovery-first flow (steps 1-6 are a back-navigable question sequence;
    step 7 is the actual migration run, which -- like the manual wizard's
    pipeline -- is not back-navigable once started):
      1. Source Orchestrate credentials (URL + API key + workspace ID)
      2. Connect to source instance (IAM token exchange + REST probe)
      3. Discover agents + toolkits
      4. User selects agents to export
      5. Target credentials / Copilot Studio (PAC) connection
      6. LLM settings
      7. Expand collaborator graph → migrate leaf-first (Import→Map→Translate→Validate→Export) → deploy/save

    `target` is already chosen by _auto_wizard (along with the source), so
    this flow never re-asks where the agents are going. Returns True if the
    user backed out of step 1 entirely (the caller should return to corridor
    selection instead of exiting).
    """
    from wheatear.connectors.orchestrate import adk_client as adk
    from wheatear.connectors.orchestrate.importer import import_agent as orch_import_agent

    saved_config = load_config()
    # Export-only stops after picking agents -- promising steps that will
    # never be shown just makes the progress marker lie.
    TOTAL = 4 if target == "export-only" else 7
    answers: dict = {"target": target}
    step = 1

    def _retry_or_back_or_exit() -> str:
        """Shown after a network step fails. Returns 'back' or exits."""
        action = questionary.select(
            "What next?",
            choices=[
                questionary.Choice("◀ Try different credentials", value="back"),
                questionary.Choice("Exit", value="exit"),
            ],
        ).ask()
        if _cancelled(action) or action == "exit":
            raise SystemExit(1)
        return "back"

    while True:
        if step == 1:
            _clear_step(1, TOTAL, "Source Orchestrate credentials")
            existing_src_creds = answers.get("src_creds")
            src_creds = ask_orchestrate_source_credentials(
                saved_config, allow_back=True, back_label="◀ Back to target platform",
                default_url=existing_src_creds.instance_url if existing_src_creds else None,
                default_workspace=existing_src_creds.workspace_id if existing_src_creds else None,
            )
            if src_creds is BACK:
                return True
            answers["src_creds"] = src_creds
            step = 2

        elif step == 2:
            _clear_step(2, TOTAL, "Connect to Orchestrate")
            src_creds = answers["src_creds"]
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
                _retry_or_back_or_exit()
                step = 1
                continue

            console.print(
                Panel(
                    f"  [green]✓[/green]  Connected to [bold]{src_creds.instance_url}[/bold]",
                    title="[bold]Orchestrate Source Connection[/bold]",
                    border_style=_SLATE,
                    expand=False,
                )
            )
            questionary.press_any_key_to_continue("Press any key to continue...").ask()
            step = 3

        elif step == 3:
            _clear_step(3, TOTAL, "Discover agents & toolkits")
            src_creds = answers["src_creds"]
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
                _retry_or_back_or_exit()
                step = 1
                continue

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
                _retry_or_back_or_exit()
                step = 1
                continue

            answers["agents"] = agents
            answers["toolkits"] = toolkits
            step = 4

        elif step == 4:
            agents = answers["agents"]
            toolkits = answers["toolkits"]

            def _agent_label(a) -> str:
                return (
                    (a.display_name or a.name)
                    + (f"  [{a.name}]" if a.display_name else "")
                    + (f"  —  {a.description[:50]}" if a.description else "")
                )

            existing_selection = answers.get("selected_agents")
            selected_agents = _multiselect_menu(
                "Select agent(s) to migrate  (Enter toggles a row; choose Confirm when done):",
                agents,
                _agent_label,
                back_label="◀ Back to credentials",
                key_fn=lambda a: a.name,
                preselected={a.name for a in existing_selection} if existing_selection else None,
                redraw=lambda: _clear_step(4, TOTAL, "Select agents to migrate"),
                noun="agent",
                context=f"{len(toolkits)} toolkit(s)" if toolkits else "",
            )
            if selected_agents is BACK:
                step = 1
                continue
            answers["selected_agents"] = selected_agents

            # ── Export-only shortcut (no pipeline, no LLM, no target creds) ──
            if target == "export-only":
                _run_export_only(answers["src_creds"], selected_agents, adk)
                return False
            step = 5

        elif step == 5:
            if target == "orchestrate":
                _clear_step(5, TOTAL, "Target credentials")
                existing_target_creds = answers.get("orchestrate_creds")
                creds = ask_orchestrate_credentials(
                    saved_config, allow_back=True,
                    default_url=existing_target_creds.instance_url if existing_target_creds else None,
                )
                if creds is BACK:
                    step = 4
                    continue
                answers["orchestrate_creds"] = creds

            elif target == "copilot-studio":
                _clear_step(5, TOTAL, "Copilot Studio connection")
                from wheatear.connectors.copilot_studio import pac_client as pac
                pac_account = _ensure_pac_auth(pac)
                with console.status("  Reading PAC CLI version…", spinner="dots"):
                    _, pac_version = pac.check()
                console.print(
                    Panel(
                        f"  [green]✓[/green]  PAC CLI  {pac_version}\n"
                        f"  [green]✓[/green]  Signed in as [bold]{pac_account}[/bold]",
                        title="[bold]Copilot Studio — PAC connection[/bold]",
                        border_style=_SLATE,
                        expand=False,
                    )
                )
                flush_input()
                questionary.press_any_key_to_continue("Press any key to continue...").ask()
                answers["orchestrate_creds"] = None
            else:
                answers["orchestrate_creds"] = None
            step = 6

        else:  # step == 6
            _clear_step(6, TOTAL, "LLM settings (for later use)")
            existing_llm = answers.get("llm_config")
            llm_config = ask_llm_settings(
                saved_config, allow_back=True,
                default=existing_llm.llm_provider if existing_llm else None,
            )
            if llm_config is BACK:
                step = 5
                continue
            answers["llm_config"] = llm_config
            break

    src_creds = answers["src_creds"]
    agents = answers["agents"]
    selected_agents = answers["selected_agents"]
    orchestrate_creds: OrchestrateCredentials | None = answers["orchestrate_creds"]
    llm_config = answers["llm_config"]

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

        # ── Step 8: Export → pipeline → deploy ───────────────────────────────────
        _step_header(8, TOTAL, f"Export & migrate → {target}")
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

    return False


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


def _auto_wizard() -> bool:
    """Pick the corridor -- source platform, then target platform -- check
    the tooling that corridor needs, then hand off to the sub-wizard for
    that source.

    Both halves of the corridor are asked here, back to back, because they
    are one decision: "migrate from X to Y". Asking for the target again
    partway through a sub-wizard made the user re-answer something they'd
    already settled, several screens after the fact.

    Returns True if the user backed all the way out to migration-mode
    selection.
    """
    source = None
    target = None
    step = 1

    while True:
        if step == 1:
            _clear_section("Migrating from")
            source = ask_source_platform(
                allow_back=True, back_label="◀ Back to migration mode", default=source
            )
            if source is BACK:
                return True
            step = 2

        elif step == 2:
            _clear_section("Migrating to")
            # Orchestrate-sourced runs can skip migration entirely and just
            # pull the raw YAML down; that's a destination choice, so it
            # belongs in this list rather than buried mid-flow.
            extra = (
                [questionary.Choice("Export raw YAML to folder (no migration)", value="export-only")]
                if source == "orchestrate"
                else []
            )
            target = ask_target_platform(
                exclude_source_key=source,
                allow_back=True,
                back_label="◀ Back to source platform",
                default=target,
                extra_choices=extra,
            )
            if target is BACK:
                step = 1
                continue
            if target != "export-only":
                validate_corridor(source, target)
            step = 3

        else:
            # Corridor tooling (PAC + its .NET prerequisite, the Orchestrate
            # CLI for deploys) is knowable the moment both ends are chosen,
            # so check it before any credential prompt or discovery call --
            # a missing dependency surfaces as a checklist rather than a raw
            # exception deep inside the pipeline.
            if not ensure_corridor_tools(
                source,
                target,
                deploy_to_orchestrate=(target == "orchestrate"),
                back_label="◀ Back to target platform",
            ):
                step = 2
                continue

            if source == "orchestrate":
                went_back = _orchestrate_source_wizard(target)
            elif source == "n8n":
                went_back = _n8n_source_wizard(target)
            else:
                went_back = _copilot_studio_auto_wizard(source, target)
            if went_back:
                step = 2
                continue
            return False


@dataclass
class N8nSourceCredentials:
    base_url: str
    api_key: str  # held in memory for the session only


def ask_n8n_source_credentials(existing: WheatearConfig | None) -> N8nSourceCredentials:
    """Prompt for the source n8n instance base URL + API key.

    The URL is a non-secret (saved to config); the API key goes to the OS
    keychain + session env, never to disk in the clear -- same handling as
    every other source credential.
    """
    from wheatear.creds import KEY_N8N_API_KEY

    console.print(
        Panel(
            "[bold]How to find your n8n API key:[/bold]\n\n"
            "  1. Open your n8n instance in a browser\n"
            "  2. Go to [bold]Settings → n8n API[/bold]\n"
            "  3. Click [bold]Create an API key[/bold] and copy it\n\n"
            "The base URL is your n8n root, e.g. [bold]http://localhost:5678[/bold].",
            title="[bold]n8n — where to find credentials[/bold]",
            border_style=_SLATE,
        )
    )
    saved_url = existing.n8n_base_url if existing else None
    url = questionary.text("n8n base URL:", default=saved_url or "http://localhost:5678").ask()
    if _cancelled(url) or not url.strip():
        raise SystemExit(1)
    env_var = (existing.n8n_api_key_env if existing else None) or "N8N_API_KEY"
    api_key = _prompt_api_key("n8n", KEY_N8N_API_KEY, env_var)
    return N8nSourceCredentials(base_url=url.strip(), api_key=api_key)


def _activate_orchestrate_env(instance_url: str, api_key: str) -> tuple[bool, str]:
    """Register + activate an Orchestrate ADK env from the target creds so both
    `orchestrate models list` (model matrix) and `orchestrate agents import`
    (deploy) work against the right instance. Returns (ok, detail). Best-effort
    -- a failure just means the model matrix falls back to the static resolver.
    """
    import shutil
    import subprocess

    if not api_key:
        return False, "no target API key in the session environment"
    if shutil.which("orchestrate") is None:
        return False, "the 'orchestrate' CLI is not on PATH"
    env_name = "wheatear-target"
    try:
        # `env add` prompts "(Y/n)" when the env already exists; feed 'y' and a
        # closed stdin so it never blocks waiting on the terminal.
        subprocess.run(
            ["orchestrate", "env", "add", "-n", env_name, "-u", instance_url],
            input="y\n", capture_output=True, text=True, timeout=60,
        )
        result = subprocess.run(
            ["orchestrate", "env", "activate", env_name, "--api-key", api_key],
            input="\n", capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "activation failed").strip().splitlines()[-1][:160]
    except subprocess.TimeoutExpired:
        return False, "activation timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:160]


def _recommend_target_model(model_hint: str | None) -> tuple[str | None, str]:
    """Best-effort target-model resolution via the model matrix against the live
    Orchestrate model list. Returns (chosen_llm_or_None, message). On any
    failure (CLI not active, no live list), returns (None, ...) so the exporter
    falls back to its own static resolver -- the migration never blocks on this.
    """
    try:
        from wheatear.model_matrix import recommend
        from wheatear.model_matrix.target_sources import OrchestrateModelSource

        rec = recommend(model_hint, OrchestrateModelSource())
        best = rec.best
        if best is None:
            return None, "no target models available; using the exporter's default."
        return best.raw_id, f"matrix picked '{best.raw_id}' for source '{model_hint}'."
    except Exception as exc:  # noqa: BLE001
        return None, f"model matrix unavailable ({str(exc)[:60]}); using the exporter's default."


def _n8n_source_wizard(target: str) -> bool:
    """Auto-discover workflows from a live n8n instance (REST API) and migrate
    the selected ones. Mirrors the Orchestrate-source flow: connect -> discover
    -> select -> pipeline -> (deploy). A local workflow-JSON directory is also
    accepted instead of a live instance."""
    from wheatear.connectors.n8n import importer as n8n_importer
    from wheatear.connectors.n8n import n8n_client
    from wheatear.connectors.orchestrate.catalog import connector_resolver

    existing = load_config()

    # --- source: live instance or local files -------------------------------
    mode = questionary.select(
        "n8n source:",
        choices=[
            questionary.Choice("Connect to a live n8n instance (REST API)", value="live"),
            questionary.Choice("Import from a local folder of workflow .json files", value="local"),
        ],
    ).ask()
    if _cancelled(mode):
        return False

    raw_workflows: list[dict] = []
    selected_names: set[str] | None = None  # None = export everything imported
    if mode == "live":
        creds = ask_n8n_source_credentials(existing)
        with console.status("[bold]Connecting to n8n..."):
            ok, msg = n8n_client.probe_connection(creds.base_url, creds.api_key)
        if not ok:
            console.print(f"[bold red]Could not connect:[/bold red] {msg}")
            return False
        console.print(f"[green]✓[/green] {msg}")
        with console.status("[bold]Discovering workflows..."):
            workflows = n8n_client.list_workflows(creds.base_url, creds.api_key)
        if not workflows:
            console.print("[yellow]No workflows found in this n8n instance.[/yellow]")
            return False
        selected = _multiselect_menu(
            "Select the workflows to migrate (their sub-workflow collaborators are pulled in automatically):",
            workflows,
            label_fn=lambda w: f"{w.name}  [dim]({'active' if w.active else 'inactive'})[/dim]",
            key_fn=lambda w: w.workflow_id,
            noun="workflow",
        )
        if not selected:
            return False
        selected_names = {w.name for w in selected}
        # Fetch the FULL set so cross-workflow toolWorkflow (collaborator)
        # references resolve, even to workflows the user didn't explicitly pick.
        with console.status("[bold]Fetching workflow definitions..."):
            all_ids = [w.workflow_id for w in workflows]
            raw_workflows = n8n_client.fetch_all_workflows(creds.base_url, creds.api_key, all_ids)
        # persist the base URL (non-secret)
        cfg = existing or WheatearConfig()
        cfg.n8n_base_url = creds.base_url
        save_config(cfg)
    else:
        path_str = questionary.path("Folder containing n8n workflow .json files:").ask()
        if _cancelled(path_str) or not path_str:
            return False
        path = Path(path_str).expanduser()
        if n8n_importer.detect_format(path) is None:
            console.print(f"[bold red]{path} has no recognizable n8n workflow JSON.[/bold red]")
            return False
        raw_workflows = n8n_importer._load_json_files(path)

    # --- import (two-pass bundle) -------------------------------------------
    with console.status("[bold]Extract: parsing n8n workflows..."):
        bundle = n8n_importer.import_workflows(raw_workflows)
    console.print(
        f"[green]Extract[/green]    {len(bundle.results)} agent(s): "
        + ", ".join(a.name for a in bundle.workflow.agents)
    )

    # Narrow to the user's selection + its collaborator closure (a chosen
    # supervisor pulls in the agents it delegates to). None = export all.
    export_names: set[str] | None = None
    if selected_names is not None:
        from wheatear.workflow import reachable_ids

        def _collabs(name: str) -> list[str]:
            a = bundle.workflow.by_name(name)
            return [c.ref for c in a.collaborators] if a else []

        present = {a.name for a in bundle.workflow.agents}
        export_names = set(reachable_ids(selected_names & present, _collabs))

    # --- target creds + LLM settings ----------------------------------------
    orchestrate_creds = None
    if target == "orchestrate":
        orchestrate_creds = ask_orchestrate_credentials(existing)
        # Activate the Orchestrate ADK env from these creds so the model matrix
        # can read the tenant's allowed models and deploy targets the right
        # instance. Without this the model matrix sees no models and the static
        # fallback may pick a model the tenant doesn't allow.
        key_val = os.environ.get(orchestrate_creds.api_key_env, "")
        with console.status("[bold]Activating Orchestrate environment..."):
            activated, activate_detail = _activate_orchestrate_env(orchestrate_creds.instance_url, key_val)
        if activated:
            console.print("  [green]✓[/green] Orchestrate environment active.")
        else:
            console.print(f"  [yellow]⚠[/yellow] Could not activate Orchestrate env ({activate_detail}); model matrix may fall back to a default.")
    provider = _provider_for(existing or WheatearConfig())
    resolver = connector_resolver()

    # --- map + translate + export each agent (yaml artifact + review manifest)
    output_root = Path.cwd() / "n8n-migration"
    ordered = bundle.workflow.migration_order()
    if export_names is not None:
        ordered = [a for a in ordered if a.name in export_names]
    by_name = {r.agent.name: r for r in bundle.results}

    llm = None
    if target == "orchestrate":
        # one model pick for the whole workflow (same source model family)
        hint = next((a.model_hint for a in ordered if a.model_hint), None)
        llm, model_msg = _recommend_target_model(hint)
        if model_msg:
            console.print(f"  [dim]model:[/dim] {model_msg}")

    for agent in ordered:
        res = by_name[agent.name]
        try:
            map_agent(res, target_platform=target, connector_resolver=resolver)
            out_dir = output_root / agent.name.replace(" ", "_")
            _run_ai_and_export_stages(agent, out_dir, target, existing or WheatearConfig(), provider, llm=llm)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]✗ {agent.name}: {str(exc)[:100]}[/red]")

    # --- provision + deploy to Orchestrate (tools, KB, descriptions, wiring) --
    if target != "orchestrate" or orchestrate_creds is None:
        console.print(f"[green]Export complete[/green] — see {output_root}")
        return False

    console.print("\n[bold]Provisioning & deploying to Orchestrate[/bold] (MCP tools, knowledge bases, collaborators)…")
    from wheatear.connectors.orchestrate.provisioner import provision_and_deploy
    from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient

    # A real LLM provider for AI-generated agent descriptions (n8n has none).
    desc_provider = None
    try:
        from wheatear.llm.factory import build_provider
        cfg = existing or WheatearConfig()
        if cfg.llm_provider in ("google", "anthropic"):
            desc_provider = build_provider(cfg.llm_provider, resolve_api_key(cfg))
    except Exception:  # noqa: BLE001
        desc_provider = None

    client = OrchestrateRestClient(
        os.environ.get(orchestrate_creds.api_key_env, ""),
        orchestrate_creds.instance_url,
    )
    selected_results = {a.name: by_name[a.name] for a in ordered}
    from rich.markup import escape as _esc

    def _prov_log(m: str) -> None:
        # Messages carry literal "[Agent Name]" prefixes -> escape so Rich
        # doesn't try to parse them as markup tags.
        if m.startswith("──"):
            console.print(f"[bold]{_esc(m)}[/bold]")
        else:
            console.print(f"  [dim]{_esc(m)}[/dim]")

    reports = provision_and_deploy(
        client, bundle.workflow, selected_results, llm or "groq/openai/gpt-oss-120b",
        provider=desc_provider, on_progress=_prov_log,
    )

    table = Table(title="n8n → orchestrate (deployed)")
    table.add_column("Agent")
    table.add_column("OK")
    table.add_column("Tools")
    table.add_column("KB")
    table.add_column("Collaborators")
    table.add_column("Validation")
    for rp in reports:
        table.add_row(
            rp.name,
            "[green]✓[/green]" if rp.ok else "[red]✗[/red]",
            str(len(rp.tools)),
            str(len(rp.knowledge)),
            ", ".join(rp.collaborators) or "—",
            rp.error or rp.validation,
        )
    console.print(table)
    return False


def _copilot_studio_auto_wizard(source: str, target: str) -> bool:
    """Auto-discover and migrate agents using the PAC CLI.

    Discovery-first flow:
      1. Target credentials (Orchestrate)
      2. Connect to Power Platform (PAC check + auth)
      3. Browse solutions → user picks which to scan → scan (export+unpack) each
      4. From scan results: user picks specific agents grouped by solution
      5. Configure LLM for translation
      6. Pipeline: Slice → Extract → Map → Translate → Validate → Export → Deploy

    `source`/`target` are already chosen (and their tooling already checked)
    by _auto_wizard. Returns True if the user backed out to the corridor
    selection instead of proceeding.
    """
    from wheatear.connectors.copilot_studio import pac_client as pac

    saved_config = load_config()
    deploy = target == "orchestrate"

    # ── Step 1: Target credentials ────────────────────────────────────────────
    orchestrate_creds: OrchestrateCredentials | None = None
    if deploy:
        _step_header(1, 6, "Target credentials — watsonx Orchestrate")
        orchestrate_creds = ask_orchestrate_credentials(saved_config)

    # ── Step 2: Connect to Power Platform ────────────────────────────────────
    _step_header(2, 6, "Connect to Power Platform")
    pac_account = _ensure_pac_auth(pac)
    with console.status("  Reading PAC CLI version…", spinner="dots"):
        _, pac_version = pac.check()
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
                    import_result = copilot_import_agent(bot_slice_dir)
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

    return False


def _ensure_pac_auth(pac) -> str:
    """Check PAC auth; run device code flow in TUI if needed. Returns account name."""
    # `pac auth list` is a .NET cold start -- seconds of nothing without this.
    with console.status("  Checking your Power Platform sign-in…", spinner="dots"):
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
    saved_config = load_config()
    if needs_onboarding(saved_config):
        run_onboarding(saved_config)
    else:
        print_banner(console)

    while True:
        flush_input()
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
                questionary.Separator(),
                questionary.Choice("Quit", value="quit"),
            ],
        ).ask()
        if _cancelled(mode):
            raise SystemExit(1)
        if mode == "quit":
            # Chosen deliberately, so this is a successful run -- unlike the
            # Ctrl-C path above, which keeps its non-zero status.
            console.print("  [dim]Bye.[/dim]")
            raise SystemExit(0)

        went_back = _auto_wizard() if mode == "auto" else _manual_wizard()
        if went_back:
            console.clear()
            print_banner(console)
            continue
        return

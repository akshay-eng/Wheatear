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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.markup import escape
from rich.text import Text

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
    verb: str = "migrate",
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
                    [(_PICKED_STYLE, f"✅ Confirm — {verb} {count} {noun}{'s' if count != 1 else ''}")],
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

def _show_connection_panel(
    pac_version: str,
    pac_account: str,
    orchestrate_creds: OrchestrateCredentials | None,
    pac_environment: str = "",
) -> None:
    """Print a tidy summary panel after PAC auth confirms we're connected."""
    lines = [
        f"  [green]✓[/green]  PAC CLI     [bold]{pac_version}[/bold]",
        f"  [green]✓[/green]  Signed in   [bold]{pac_account}[/bold]",
    ]
    if pac_environment:
        # Named on the panel because it decides which solutions are listed, and
        # a run against the wrong environment looks like an empty tenant.
        lines.append(f"  [green]✓[/green]  Environment [bold]{escape(pac_environment)}[/bold]")
    if orchestrate_creds:
        lines.append(
            f"  [green]✓[/green]  Orchestrate [dim]{orchestrate_creds.instance_url}[/dim]"
        )
    console.print(
        Panel("\n".join(lines), title="[bold]Connection[/bold]", border_style=_SLATE, expand=False)
    )


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
        foundry_store_root=saved_config.foundry_store_root if saved_config else None,
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
                # Going this way the environment decides where the migrated
                # solutions are *written*, which is the more expensive mistake
                # of the two: pushing a tenant's agents into Prod by accident.
                pac_environment = _ensure_pac_environment(pac)
                with console.status("  Reading PAC CLI version…", spinner="dots"):
                    _, pac_version = pac.check()
                console.print(
                    Panel(
                        f"  [green]✓[/green]  PAC CLI  {pac_version}\n"
                        f"  [green]✓[/green]  Signed in as [bold]{pac_account}[/bold]\n"
                        f"  [green]✓[/green]  Writing to [bold]{escape(pac_environment)}[/bold]",
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
      2. Connect to Power Platform (PAC check + which account to read as)
      3. Browse solutions → user picks which to scan → scan (export+unpack) each
      4. From scan results: user picks specific agents grouped by solution
      5. Configure the LLM that adjudicates tool matches
      6. Run the compiled corridor, once per source solution

    Step 6 is `pipeline/solution_migration.py`, the same code `scripts/
    migrate_solution.py` runs. It works on a whole solution rather than one
    sliced bot at a time, because an agent's tools, knowledge and delegation
    edges arrive as separate records and only become an agent when they are
    joined -- and the delegation graph only survives if the agents that
    reference each other are named in the same run.

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
    # After the account is settled, never before: which environments exist at
    # all depends on who is signed in, so asking first would offer a list
    # belonging to the previous tenant.
    pac_environment = _ensure_pac_environment(pac)
    with console.status("  Reading PAC CLI version…", spinner="dots"):
        _, pac_version = pac.check()
    _show_connection_panel(pac_version, pac_account, orchestrate_creds, pac_environment)

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

    # All unpacked dirs live in a single temp base that persists through step 6
    scan_base = Path(tempfile.mkdtemp(prefix="wheatear-scan-"))
    # Exporting and unpacking a solution is slow, so a solution scanned once is
    # not scanned again when the user steps back to change the selection.
    scan_cache: dict[str, ScannedSolution] = {}
    try:
        while True:
            # ── Step 3: pick solutions ────────────────────────────────────────
            def _redraw_solutions() -> None:
                _clear_step(3, 6, "Browse solutions")
                console.print(
                    f"  [dim]{len(solutions)} unmanaged solution(s) in this environment.[/dim]"
                )

            selected_solutions = _multiselect_menu(
                "Select solution(s) to scan for agents  "
                "(Enter toggles a row; choose Confirm when done):",
                solutions,
                lambda s: f"{s.unique_name}  ({s.friendly_name}  v{s.version})",
                back_label="◀ Back to target platform",
                key_fn=lambda s: s.unique_name,
                redraw=_redraw_solutions,
                noun="solution",
                verb="scan",
            )
            if selected_solutions is BACK:
                return True

            console.print()
            fresh = [s for s in selected_solutions if s.unique_name not in scan_cache]
            for scan in _scan_solutions(pac, fresh, scan_base):
                scan_cache[scan.solution_name] = scan
            scanned = [scan_cache[s.unique_name] for s in selected_solutions]

            # ── Step 4: Select agents from scan results ───────────────────────
            _step_header(4, 6, "Select agents to migrate")
            found = [
                (scan, schema, bot_name)
                for scan in scanned
                for schema, bot_name in scan.bots
            ]

            if not found:
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
                # Back to the solution picker rather than out of the wizard:
                # the likeliest cause is having scanned the wrong solutions,
                # and that is now a recoverable mistake.
                flush_input()
                questionary.press_any_key_to_continue(
                    "Press any key to choose different solutions..."
                ).ask()
                continue

            solutions_with_bots = len({scan.solution_name for scan, _, _ in found})

            def _redraw_agents() -> None:
                _clear_step(4, 6, "Select agents to migrate")
                console.print(
                    f"  [green]Found {len(found)} agent(s)[/green] across "
                    f"{solutions_with_bots} solution(s)."
                )

            selected_items = _multiselect_menu(
                "Select the agent(s) to migrate  "
                "(Enter toggles a row; choose Confirm when done):",
                found,
                lambda row: f"{row[2]}  ({row[0].solution_label})",
                back_label="◀ Back to solutions",
                key_fn=lambda row: (row[0].solution_name, row[1]),
                redraw=_redraw_agents,
                noun="agent",
                context=f"{solutions_with_bots} solution(s)",
            )
            if selected_items is BACK:
                continue
            break

        # ── Step 5: Configure translation ─────────────────────────────────────
        _step_header(5, 6, "Configure translation")
        llm_config = ask_llm_settings(saved_config)
        final_config = _build_final_config(llm_config, orchestrate_creds, saved_config)
        if config_changed(final_config, saved_config):
            save_config(final_config)
        if final_config.llm_provider != "none":
            resolve_api_key(final_config)  # prompt or load from keychain; lands in os.environ
        provider = _corridor_provider(final_config)

        output_base = Path("./orchestrate-migration")

        # ── Step 6: Migrate & deploy ───────────────────────────────────────────
        _step_header(6, 6, "Migrate & deploy")
        agent_names = [item[2] for item in selected_items]
        sol_names = list(dict.fromkeys(item[0].solution_name for item in selected_items))
        _show_migration_plan(agent_names, sol_names, final_config, output_base, orchestrate_creds)

        # The corridor's compiled adapters are what read the export. Checked
        # here, before anything is spent on it: a missing adapter stops the
        # first stage outright, and finding that out after the user has picked
        # an environment, a solution and four agents is a worse way to learn it.
        from wheatear.pipeline.solution_migration import adapters_ready

        store = _foundry_store(saved_config)
        while True:
            ready, detail = adapters_ready(store)
            if ready:
                console.print(f"  [green]✓[/green]  {detail}  [dim]({store.root})[/dim]")
                break
            store = _ask_store_root(store, detail)
            if store is None:
                return False
            # Remember a store the user had to point us at, so the next
            # migration finds it without asking again.
            final_config.foundry_store_root = str(store.root)
            save_config(final_config)

        on_conflict = ask_conflict_policy() if orchestrate_creds else "rename"

        flush_input()
        report = _run_foundry_migration(
            selected_items, store, orchestrate_creds, provider, output_base, on_conflict
        )
        if report is None:
            console.print(
                "  [dim]Stopped before migrating. Fix the credentials and run again.[/dim]"
            )
            return False

        # ── Final summary ──────────────────────────────────────────────────────
        console.print()
        _show_foundry_summary(report, orchestrate_creds)

        # Agents are on the target either way. If some of their tools are not,
        # stay open and finish the job when they arrive rather than sending the
        # operator away with a command to remember.
        # Connections first: a tool that landed but cannot authenticate is a
        # capability the agent appears to have and does not.
        if orchestrate_creds and report.agents:
            client, _why = _orchestrate_client(orchestrate_creds)
            if client is not None:
                console.print()
                _setup_connections(
                    report,
                    client,
                    orchestrate_creds.instance_url,
                    client._session.headers["Authorization"].split()[1],
                )

        if report is not None and report.pending and orchestrate_creds:
            client, _why = _orchestrate_client(orchestrate_creds)
            if client is not None:
                console.print()
                report = _watch_for_installs(
                    report,
                    client,
                    orchestrate_creds.instance_url,
                    # Forced to "update" whatever was chosen for the first
                    # pass: renaming here would attach the newly-installed tool
                    # to a fresh `ITSM_Agent_2` and leave the agent people are
                    # actually using without it.
                    lambda: _run_foundry_migration(
                        selected_items, store, orchestrate_creds, provider, output_base, "update"
                    ),
                )
                console.print()
                _show_foundry_summary(report, orchestrate_creds)

        console.print()
        _show_model_usage()
        console.print()
        _show_manual_steps(report)

    finally:
        shutil.rmtree(scan_base, ignore_errors=True)

    return False


def _pac_device_auth(pac) -> str:
    """Run PAC's device code flow, showing the code as PAC prints it."""
    console.print("[bold]Starting device code sign-in…[/bold]")

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


# Above this many environments, scrolling to the one you want stops being
# reasonable and a filter row is offered instead. Enterprise tenants routinely
# have one environment per maker.
_ENV_FILTER_THRESHOLD = 10


def _ensure_pac_environment(pac) -> str:
    """Choose which Power Platform environment this migration reads.

    One tenant holds many Dataverse environments and an agent lives in exactly
    one of them. `pac solution list` reports the *selected* environment's
    solutions and never says which that is -- so without this step a run
    against the wrong environment shows an empty solution list, or worse, a
    plausible one belonging to Dev when the user meant Prod.

    Returns a description of the environment in use, for the connection panel.
    Degrades to whatever PAC already has selected when the list can't be read:
    losing the ability to *choose* an environment should not cost the ability
    to migrate from the one already chosen.
    """
    with console.status("  Reading your Power Platform environments…", spinner="dots"):
        environments = pac.list_environments()
        current = pac.current_environment()

    if not environments:
        if current:
            console.print(f"  [dim]Using the environment PAC has selected:[/dim] {current}")
            return current
        console.print(
            "  [yellow]Could not list environments.[/yellow] Continuing with whatever "
            "environment PAC has selected — check it with [cyan]pac env who[/cyan] if the "
            "solution list looks wrong."
        )
        return "unknown"

    def is_current(env) -> bool:
        return env.active or (bool(current) and (current in (env.url, env.environment_id, env.display_name)))

    active = next((e for e in environments if is_current(e)), None)

    if len(environments) == 1:
        only = environments[0]
        console.print(f"  [dim]One environment in this tenant:[/dim] {only.label()}")
        return only.label()

    chosen = _pick_environment(environments, active)
    if chosen is None:
        return active.label() if active else (current or "unknown")

    if active is not None and chosen.selector == active.selector:
        return chosen.label()

    try:
        with console.status(f"  Switching to {chosen.display_name}…", spinner="dots"):
            pac.select_environment(chosen.selector)
            # Read back rather than trust: if the switch silently did not take,
            # every later call would export from the previous environment and
            # nothing on screen would say so.
            now = pac.current_environment()
    except Exception as exc:  # noqa: BLE001 - reported; the old environment still works
        console.print(f"  [red]Could not switch environment:[/red] {exc}")
        return active.label() if active else (current or "unknown")

    if now and chosen.url and now.rstrip("/") != chosen.url.rstrip("/"):
        console.print(
            f"  [yellow]PAC reports it is still on[/yellow] {now}[yellow], not "
            f"{chosen.url}.[/yellow] Solutions will be read from {now}."
        )
        return now
    console.print(f"  [green]✓[/green]  Switched to [bold]{chosen.display_name}[/bold]")
    return chosen.label()


def _pick_environment(environments: list, active):
    """Single-select over the environment list, with a filter when it is long."""
    query = ""
    while True:
        terms = query.lower().split()
        matches = [e for e in environments if all(t in e.label().lower() for t in terms)]

        choices = []
        if len(environments) > _ENV_FILTER_THRESHOLD or query:
            label = f'🔎 Filter: "{query}"  —  edit' if query else "🔎 Search environments by name…"
            choices.append(questionary.Choice(label, value="__search__"))
            if query:
                choices.append(questionary.Choice("✕ Clear filter", value="__clear__"))
            choices.append(questionary.Separator())

        for env in matches:
            mark = "● " if active is not None and env.selector == active.selector else "  "
            choices.append(questionary.Choice(f"{mark}{env.label()}", value=env))
        if not matches:
            choices.append(
                questionary.Choice(
                    "(nothing matches this filter)", value="__none__", disabled="no matches"
                )
            )

        console.print(
            f"\n  [dim]{len(environments)} environment(s) in this tenant"
            + (f'; {len(matches)} matching "{query}"' if query else "")
            + ".  ● is the one currently selected.[/dim]"
        )
        flush_input()
        picked = questionary.select(
            "Which environment holds the agents you want to migrate?", choices=choices
        ).ask()
        if _cancelled(picked):
            raise SystemExit(1)

        if picked == "__search__":
            flush_input()
            typed = questionary.text(
                "Filter environments (substring match, blank shows all):", default=query
            ).ask()
            query = "" if _cancelled(typed) else typed.strip()
            continue
        if picked == "__clear__":
            query = ""
            continue
        return picked


def _ensure_pac_auth(pac) -> str:
    """Confirm which Power Platform account this migration reads, and offer to change it.

    Not merely a check. Which account is active decides which tenant's
    solutions are listed and which environment every later `pac` call reaches,
    and PAC keeps several signed in at once -- so a migration that silently
    used whichever happened to be active is one environment away from exporting
    the wrong agents. The account is shown, and switching it is one keystroke
    from here rather than a documented shell command in another window.

    Returns the account name every later step will be reading as.
    """
    while True:
        # `pac auth list` is a .NET cold start -- seconds of nothing without this.
        with console.status("  Checking your Power Platform sign-in…", spinner="dots"):
            profiles = pac.list_auth_profiles()

        active = next((p for p in profiles if p.active), None)
        if not profiles:
            console.print("[bold]No Power Platform account is signed in.[/bold]")
            _pac_device_auth(pac)
            continue

        others = [p for p in profiles if p is not active]
        console.print(
            Panel(
                f"  [green]✓[/green]  Signed in as [bold]{active.label() if active else 'unknown'}[/bold]"
                + (
                    f"\n  [dim]{len(others)} other account(s) also signed in on this machine.[/dim]"
                    if others
                    else ""
                ),
                title="[bold]Copilot Studio account[/bold]",
                border_style=_SLATE,
                expand=False,
            )
        )

        choices = [
            questionary.Choice(
                f"Continue as {active.user or active.name}" if active else "Continue",
                value="continue",
            )
        ]
        for profile in others:
            choices.append(
                questionary.Choice(f"Switch to {profile.label()}", value=("select", profile.index))
            )
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice("Sign in to another account…", value="add")
        )
        choices.append(
            questionary.Choice(
                "Sign out of all accounts and start over", value="logout"
            )
        )

        flush_input()
        action = questionary.select("Which account should this migration use?", choices=choices).ask()
        if _cancelled(action):
            raise SystemExit(1)

        if action == "continue":
            return (active.user or active.name) if active else "unknown"

        if action == "add":
            _pac_device_auth(pac)
            continue

        if action == "logout":
            confirm = questionary.confirm(
                "Sign out of every Power Platform account on this machine?", default=False
            ).ask()
            if _cancelled(confirm) or not confirm:
                continue
            try:
                with console.status("  Signing out…", spinner="dots"):
                    pac.clear_auth()
            except Exception as exc:  # noqa: BLE001 - reported, then re-listed
                console.print(f"  [red]Could not sign out:[/red] {exc}")
                continue
            console.print("  [dim]Signed out. Sign in with the account you want to migrate from.[/dim]")
            _pac_device_auth(pac)
            continue

        kind, index = action
        if kind == "select":
            try:
                with console.status("  Switching account…", spinner="dots"):
                    pac.select_auth_profile(index)
            except Exception as exc:  # noqa: BLE001 - reported, then re-listed
                console.print(f"  [red]Could not switch:[/red] {exc}")
            continue


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


# ---------------------------------------------------------------------------
# Foundry corridor: the compiled Copilot Studio -> Orchestrate migration
# ---------------------------------------------------------------------------

_STAGE_TITLES = {
    "scan": "Scan — reading the solution export",
    "ir": "Convert — records into the IR, through the cached adapters",
    "compose": "Compose — assembling whole agents",
    "lookup": "Tool lookup — your instance first, then the catalog",
    "mcp": "MCP servers & carried context",
    "logins": "Sign-in",
    "describe": "Descriptions",
    "export": "Write — watsonx Orchestrate YAML",
    "import": "Import — into watsonx Orchestrate",
}

_LEVEL_STYLE = {"warn": "yellow", "error": "red", "ok": "green", "info": "dim"}

# Model activity gets its own colour so it reads as a distinct kind of event --
# the one that is billed -- rather than as more pipeline chatter.
_MODEL_COLOUR = "magenta"


def _corridor_provider(config: WheatearConfig):
    """A real LLM provider for the corridor, or None if there is no key.

    Distinct from `_provider_for`, which deliberately returns None because the
    legacy pipeline's Translate stage is deferred. The corridor is not the same
    situation: its tool-lookup stage is model-adjudicated by design -- the model
    picks between a fixed, deterministically-ranked shortlist -- and without one
    every source tool is merely *suggested* candidates and nothing is carried.
    A migration that quietly dropped every tool because no model was built would
    look like a resolver that found nothing.
    """
    if config.llm_provider in ("", "none"):
        console.print(
            "  [yellow]No LLM provider configured.[/yellow] Tool matching will list "
            "candidates but choose none, so no tools will be carried."
        )
        return None

    from wheatear.creds import llm_key_name, load_secret
    from wheatear.llm.factory import build_provider

    key = os.environ.get(config.llm_key_env) or load_secret(llm_key_name(config.llm_provider))
    if not key:
        console.print(
            f"  [yellow]No {config.llm_provider} key found.[/yellow] Tool matching will "
            "list candidates but choose none."
        )
        return None
    try:
        provider = build_provider(config.llm_provider, key)
    except ValueError as exc:
        console.print(f"  [yellow]{exc}[/yellow] Tool matching will choose nothing.")
        return None
    console.print(f"  [green]✓[/green]  {config.llm_provider} will adjudicate tool matches")
    return _observed(provider)


# Totals for the whole run, so the closing summary can say what the migration
# actually cost rather than leaving it to a billing page a month later.
_METER = None


def _observed(provider):
    """Wrap a provider so every call is shown as it happens.

    Wrapped rather than instrumented at the call sites: `resolve.py` should
    only ever know it has an `LLMProvider`, and anything added later is covered
    without being touched.
    """
    global _METER
    from wheatear.llm.usage import ObservedProvider, UsageMeter

    _METER = UsageMeter()

    def show(call) -> None:
        # Deliberately not dim. This is the one part of a migration that costs
        # money as it runs, and burying it in the same grey as everything else
        # is how nobody notices a resolver looping.
        style = "bold red" if call.failed else _MODEL_COLOUR
        console.print(Text(f"      · {call.summary()}", style=style))

    return ObservedProvider(provider, meter=_METER, on_call=show)


def _show_model_usage() -> None:
    """What the model was asked to do, and what it cost."""
    if _METER is None or not _METER.calls:
        return
    grouped = _METER.by_activity()
    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim", justify="right", no_wrap=True)
    body.add_column()
    for activity, (calls, tokens) in sorted(grouped.items(), key=lambda kv: -kv[1][0]):
        body.add_row(
            str(calls),
            f"{escape(activity)}  [{_MODEL_COLOUR}]{tokens:,} tokens[/{_MODEL_COLOUR}]",
        )
    console.print(
        Panel(
            Group(body, Text(""), Text("  " + _METER.summary(), style=f"bold {_MODEL_COLOUR}")),
            title=f"[bold {_MODEL_COLOUR}]What the model did[/bold {_MODEL_COLOUR}]",
            border_style=_MODEL_COLOUR,
            expand=False,
        )
    )


def _foundry_store(saved_config: WheatearConfig | None):
    """The store the compiled adapters live in.

    A saved root wins over the default because a machine that built its
    corridor under a different root would otherwise be told it has no adapters
    while they sit on disk a directory away.
    """
    from wheatear.foundry.store import FoundryStore

    root = saved_config.foundry_store_root if saved_config else None
    return FoundryStore(Path(root).expanduser() if root else None)


def _ask_store_root(store, reason: str):
    """Explain that the corridor isn't compiled here, and offer a way forward.

    Returns a new store to try, or None if the user gave up. Building the
    corridor is deliberately not offered from inside a migration: it probes
    both platforms, runs generated code in a sandbox and takes minutes of model
    calls, and burying that behind a wizard step would make it look like part
    of moving one solution rather than the once-per-platform job it is.
    """
    console.print(
        Panel(
            f"[bold]{reason}[/bold]\n\n"
            f"Looked in: [cyan]{store.root}[/cyan]\n\n"
            "The Copilot Studio → Orchestrate corridor is compiled once and cached; every\n"
            "migration after that reuses it and calls no model to read your export.\n\n"
            "Building it is three commands -- `corridor` needs both platforms probed\n"
            "first and stops if they are not:\n\n"
            "  [cyan]wheatear foundry probe copilot-studio --export <unpacked-solution> --offline[/cyan]\n"
            "  [cyan]wheatear foundry probe orchestrate[/cyan]\n"
            "  [cyan]wheatear foundry corridor copilot-studio orchestrate[/cyan]\n\n"
            "[dim]The probes are quick. The corridor build is the slow one: a model call per\n"
            "entity kind, then the generated code compiled and tested in a sandbox.[/dim]\n\n"
            "  Already built elsewhere? Point Wheatear at that directory below.",
            title="[bold yellow]Adapters not compiled on this machine[/bold yellow]",
            border_style="yellow",
        )
    )
    flush_input()
    action = questionary.select(
        "What next?",
        choices=[
            questionary.Choice("Point at an existing foundry store…", value="path"),
            questionary.Choice("◀ Back", value="back"),
        ],
    ).ask()
    if _cancelled(action) or action == "back":
        return None

    raw = questionary.text("Path to the foundry store (the directory holding corpora/ and adapters/):").ask()
    if _cancelled(raw) or not raw.strip():
        return None

    from wheatear.foundry.store import FoundryStore

    return FoundryStore(Path(raw.strip()).expanduser())


def _migration_reporter():
    """A `solution_migration.Reporter` that draws stage rules as they arrive.

    Event text is printed as a styled `Text`, never as markup. The pipeline
    writes things like `HR Agent [source]: ...` and `unclassified: ['a.xml']`,
    and rich reads a bracketed word as a style tag -- so as markup the origin
    label silently vanished, and a stray `[/x]` would raise out of the middle
    of a migration.
    """
    seen: set[str] = set()

    def emit(event) -> None:
        title = _STAGE_TITLES.get(event.stage, event.stage)
        if title not in seen:
            seen.add(title)
            console.rule(f"[bold cyan]{title}[/bold cyan]", style="dim")
        console.print(Text(f"    {event.text}", style=_LEVEL_STYLE.get(event.level, "dim")))

    return emit


def _show_manual_steps(report) -> None:
    """The things Wheatear cannot do and a person must, printed to be acted on.

    Deliberately the last and loudest thing on screen. A tool that has to be
    installed by hand is the single fact most likely to decide whether a
    migrated agent actually works, and it is worthless buried in a review file
    nobody opens.
    """
    steps = list(report.manual_steps)
    # An agent that failed to import is also something left to do. The version
    # that only saw the step list announced "Target is complete" over a failed
    # migration -- the worst thing a tool like this can print.
    failed = [a for a in report.agents if a.deployed is False]

    if not steps and not failed:
        console.print(
            Panel(
                "Nothing left to do by hand — every tool this solution used was already "
                "available on your instance.",
                title="[bold green]Target is complete[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        return

    required = [s for s in steps if s.blocking]
    optional = [s for s in steps if not s.blocking]

    # A grid rather than joined lines: a step's detail is a sentence or three
    # and rich wraps it at the panel edge, which puts the continuation back at
    # column zero and makes the next step's title indistinguishable from the
    # previous step's overflow. A label column keeps the wrap inside it.
    body = Table.grid(padding=(0, 1))
    body.add_column(style="dim", justify="right", width=10, no_wrap=True)
    body.add_column(overflow="fold")

    # Failed imports lead. An agent that is not on the target outranks every
    # tool that is missing from one that is.
    n = 0
    for outcome in failed:
        n += 1
        body.add_row(
            f"[bold]{n}.[/bold]",
            f"[bold]{escape(outcome.name)} did not import[/bold]   [bold red]REQUIRED[/bold red]",
        )
        body.add_row("why", escape(outcome.detail or "see the import log above"))
        if outcome.agent_path:
            body.add_row("spec", f"[dim]{escape(str(outcome.agent_path))}[/dim]")
        body.add_row("", "")

    for step in required + optional:
        n += 1
        flag = "[bold red]REQUIRED[/bold red]" if step.blocking else "[yellow]optional[/yellow]"
        # Every field below is data -- a tool name, a console path, a command --
        # and rich would read a bracketed fragment in any of them as a style tag.
        body.add_row(f"[bold]{n}.[/bold]", f"[bold]{escape(step.title)}[/bold]   {flag}")
        if step.agents:
            body.add_row("needed by", escape(", ".join(step.agents)))
        body.add_row("where", escape(step.where))
        body.add_row("", escape(step.detail))
        if step.command:
            body.add_row("run", f"[cyan]{escape(step.command)}[/cyan]")
        body.add_row("", "")

    must_do = len(required) + len(failed)
    heading = (
        f"[bold red]{must_do} required[/bold red]" if must_do else "[green]nothing required[/green]"
    )
    if optional:
        heading += f", {len(optional)} optional"

    console.print(
        Panel(
            body,
            title=f"[bold]Still to do on watsonx Orchestrate — {heading}[/bold]",
            border_style=_AMBER if must_do else _SLATE,
        )
    )


def _show_foundry_summary(report, orchestrate_creds: OrchestrateCredentials | None) -> None:
    """Per-agent outcome table for a foundry-corridor run."""
    table = Table(
        title=f"[bold]Migration complete — {report.summary()}[/bold]",
        border_style=_SLATE,
        show_header=True,
        header_style="bold",
        show_lines=True,
    )
    # Four columns, not six. What the agent carries is three short lists that
    # are usually empty, and giving each its own column costs the width that
    # the agent's name and model need -- on an 80-column terminal the last
    # column was simply cut off, which loses the delegation graph entirely.
    table.add_column("Agent", style="bold", min_width=14)
    table.add_column("Status", width=11, no_wrap=True)
    table.add_column("Model", style="dim", overflow="fold")
    table.add_column("Carries", overflow="fold")

    for outcome in report.agents:
        if outcome.deployed is None:
            status = "[dim]written[/dim]"
        elif outcome.deployed:
            status = "[green]deployed ✓[/green]"
        else:
            status = "[red]failed ✗[/red]"

        carries: list[str] = []
        if outcome.tools:
            carries.append("tools: " + escape(", ".join(outcome.tools)))
        if outcome.dropped:
            carries.append(f"[yellow]{len(outcome.dropped)} tool(s) dropped[/yellow]")
        if outcome.knowledge:
            carries.append("knowledge: " + escape(", ".join(outcome.knowledge)))
        if outcome.collaborators:
            carries.append("delegates to: " + escape(", ".join(outcome.collaborators)))
        table.add_row(
            escape(outcome.name),
            status,
            escape(outcome.llm),
            "\n".join(carries) or "[dim]instructions only[/dim]",
        )
    console.print(table)

    if report.output_dir:
        console.print(f"  [dim]Files written to[/dim] {escape(str(report.output_dir))}")
    for outcome in report.agents:
        if outcome.review_path:
            console.print(
                f"  [dim]Review notes for {escape(outcome.name)}:[/dim] "
                f"{escape(str(outcome.review_path))}"
            )
    for note in report.notes:
        console.print(f"  [yellow]{escape(note)}[/yellow]")
    if orchestrate_creds:
        console.print(f"  [dim]Instance:[/dim] {orchestrate_creds.instance_url}")


# How often to ask the instance whether the tools have appeared. Installing one
# is a few clicks in a browser, so a slow poll would have the operator staring
# at a spinner well after they finished, and a fast one is a request per second
# against a tenant for no benefit.
_POLL_SECONDS = 10


def _console_origin(instance_url: str) -> str:
    try:
        from wheatear.connectors.orchestrate.catalog_client import console_origin

        return console_origin(instance_url)
    except Exception:  # noqa: BLE001 - a missing link is not worth failing over
        return ""


def _show_install_checklist(report, instance_url: str) -> None:
    """What is left to install, who is waiting for it, and where each one lives.

    Printed after the agents have already landed. The migration does not wait
    for these -- an agent that arrives doing four of its five jobs is one
    somebody can finish -- so this is a punch list, not an error.
    """
    origin = _console_origin(instance_url)
    by_agent = {a.name: a for a in report.agents}

    body = Table.grid(padding=(0, 1))
    body.add_column(style="dim", justify="right", width=11, no_wrap=True)
    body.add_column(overflow="fold")

    for n, item in enumerate(report.pending, 1):
        body.add_row(f"[bold]{n}.[/bold]", f"[bold]Install '{escape(item.title)}'[/bold]")
        body.add_row("search for", escape(item.title))
        body.add_row("installs as", f"[cyan]{escape(item.install_ref)}[/cyan]")
        if item.connections:
            body.add_row("then set", "connection " + escape(", ".join(item.connections)))
        for name in item.agents:
            outcome = by_agent.get(name)
            link = (
                f"{instance_url.rstrip('/')}/v1/orchestrate/agents/{outcome.agent_id}"
                if outcome is not None and outcome.agent_id
                else "(id unknown)"
            )
            # Labelled `api` deliberately: this is the only per-agent URL that
            # can be shown truthfully. The console is a single-page app that
            # answers 200 with the same shell for every path, so a deep link to
            # an agent page cannot be verified and a guessed one is worse than
            # the console root in the header above.
            body.add_row("waiting", f"[bold]{escape(name)}[/bold]")
            body.add_row("api", f"[dim]{escape(link)}[/dim]")
        body.add_row("", "")

    header = (
        f"Open [bold]{escape(origin)}[/bold] and go to "
        "[bold]Manage → Tools → Add tool → Catalog[/bold].\n"
        if origin
        else "Open your watsonx Orchestrate console: "
        "[bold]Manage → Tools → Add tool → Catalog[/bold].\n"
    )
    console.print(
        Panel(
            Group(header, body),
            title=f"[bold]{len(report.pending)} tool(s) to install — your agents are already migrated[/bold]",
            border_style=_AMBER,
        )
    )


def _connection_key(app_id: str, field: str) -> str:
    """Keychain entry name for one credential field of one connection."""
    return f"target_connection_{app_id}_{field}"


def _ask_credentials(request) -> dict[str, str] | None:
    """Collect the secret a connection needs, from the person at the keyboard.

    Nothing here is carried from the source platform -- a solution export
    contains no credentials and never will. This is the target's credential,
    typed by whoever is doing the migration, handed to the target, and offered
    to their own OS keychain so the next run does not ask again. It is not
    written to a Wheatear file, not put in a review manifest, not logged, and
    not passed on a command line where `ps` could read it.

    Returns None if they would rather do it in the console.
    """
    from wheatear.creds import load_secret, save_secret

    fields = request.fields
    if not fields:
        console.print(
            f"  [yellow]`{request.app_id}` uses {request.kind}, which takes free-form "
            "key/value pairs.[/yellow] Configure it in the console."
        )
        return None

    console.print(
        Panel(
            f"[bold]{escape(request.app_id)}[/bold] needs credentials before "
            + escape(", ".join(f"`{t}`" for t in request.tools) or "its tools")
            + " will work.\n\n"
            "[dim]Typed here, sent straight to watsonx Orchestrate, and saved only to "
            "your own OS keychain if you say so. Wheatear stores nothing itself and "
            "carries nothing from the source platform.[/dim]",
            title="[bold]Credentials for the target[/bold]",
            border_style=_AMBER,
        )
    )

    flush_input()
    if not questionary.confirm(f"Enter credentials for {request.app_id} now?", default=True).ask():
        return None

    secrets: dict[str, str] = {}
    for name, prompt, is_secret in fields:
        saved = load_secret(_connection_key(request.app_id, name)) if is_secret else None
        if saved:
            flush_input()
            if questionary.confirm(f"Use the saved {prompt.lower()}?", default=True).ask():
                secrets[name] = saved
                continue
        flush_input()
        ask = questionary.password if is_secret else questionary.text
        value = ask(f"{prompt}:").ask()
        if _cancelled(value) or not value:
            console.print("  [dim]Skipped — configure this connection in the console.[/dim]")
            return None
        secrets[name] = value

    flush_input()
    if questionary.confirm("Save these to your OS keychain for next time?", default=False).ask():
        for name, _prompt, is_secret in fields:
            if is_secret and name in secrets:
                save_secret(_connection_key(request.app_id, name), secrets[name])
        console.print("  [dim]Saved to your keychain. Nothing left Wheatear's process.[/dim]")
    return secrets


def _ask_auth_kind(app_id: str, offered: tuple[str, ...]) -> str | None:
    """Which auth kind to configure, from the ones the application declares.

    Offered rather than chosen. Configuring a ServiceNow connection as
    `bearer_token` when the tenant uses OAuth saves cleanly and fails on the
    first call, with an error about the response body rather than the auth.
    """
    from wheatear.connectors.orchestrate.provisioning import CREDENTIAL_FIELDS

    usable = [k for k in offered if k in CREDENTIAL_FIELDS] or list(CREDENTIAL_FIELDS)
    if len(usable) == 1:
        return usable[0]
    flush_input()
    kind = questionary.select(
        f"How does `{app_id}` authenticate?",
        choices=[
            questionary.Choice(
                k.replace("_", " ")
                + (
                    "  (asks for " + ", ".join(f[1].lower() for f in CREDENTIAL_FIELDS[k]) + ")"
                    if CREDENTIAL_FIELDS[k]
                    else ""
                ),
                value=k,
            )
            for k in usable
        ],
    ).ask()
    return None if _cancelled(kind) else kind


def _ask_server_url(request) -> bool:
    """Where the connection points. Kept if the tenant already knows.

    Asked because nothing in a Copilot export says it -- a solution carries a
    connection *reference*, not an endpoint -- so a migration that did not ask
    would leave the connection pointed at nothing and the tool failing on its
    first call. Never blanked: a value already configured by hand is left alone
    unless the operator types a replacement.
    """
    from wheatear.connectors.orchestrate.provisioning import existing_server_url, looks_like_a_url

    known = existing_server_url(request.app_id, request.environment)
    if known:
        console.print(f"  [dim]{escape(request.app_id)} already points at[/dim] {escape(known)}")
        request.server_url = None  # preserved by `configure`
        return True

    while True:
        flush_input()
        url = questionary.text(
            f"Server URL for {request.app_id} (e.g. https://yourorg.service-now.com):"
        ).ask()
        if _cancelled(url) or not url.strip():
            console.print(
                "  [yellow]No server URL given.[/yellow] The connection will be created but "
                "the tool cannot reach anything until one is set."
            )
            return True
        if looks_like_a_url(url):
            request.server_url = url.strip()
            return True
        # Deliberately does not echo what was typed: the likeliest wrong answer
        # here is the credential from the prompt beside it.
        console.print(
            "  [yellow]That does not look like a server URL.[/yellow] Expected something "
            "like https://yourorg.service-now.com — not a token or a key."
        )


def _configure_connection(request, offered: tuple[str, ...] = ()) -> bool:
    """Create, configure and (if it takes one) credential a connection."""
    from wheatear.connectors.orchestrate import provisioning

    kind = _ask_auth_kind(request.app_id, offered)
    if kind is None:
        return False
    request.kind = kind

    if not _ask_server_url(request):
        return False

    preference = "member"
    if request.fields:
        flush_input()
        preference = questionary.select(
            f"How should `{request.app_id}` authenticate?",
            choices=[
                questionary.Choice(
                    "Each user signs in themselves — the agent prompts them the first "
                    "time (recommended if the source enforced per-user permissions)",
                    value="member",
                ),
                questionary.Choice(
                    "One shared credential — you supply it now, nobody is prompted later",
                    value="team",
                ),
            ],
            default="member",
        ).ask()
        if _cancelled(preference):
            return False
    request.preference = preference

    secrets = None
    if preference == "team":
        secrets = _ask_credentials(request)
        if secrets is None:
            return False

    try:
        with console.status(f"  Configuring {request.app_id}…", spinner="dots"):
            done = provisioning.provision(request, secrets)
    except Exception as exc:  # noqa: BLE001 - reported; the migration continues
        console.print(f"  [red]Could not configure `{escape(request.app_id)}`:[/red] {exc}")
        return False
    for line in done:
        console.print(f"  [green]✓[/green]  {escape(line)}")
    if preference == "member":
        console.print(
            "  [dim]Each user will be asked to sign in the first time the agent uses it.[/dim]"
        )
    return True


def _ask_console_cookie(instance_url: str) -> str | None:
    """Ask for a console session, which is the only thing that can install a tool.

    Not the instance API key: installing a catalog tool goes through the
    console, which authenticates with a browser session and a CSRF token
    derived from it. The API key gets nowhere. The cookie is used in memory for
    this run and never stored -- it is a live session, and anybody holding it is
    the signed-in user.
    """
    origin = _console_origin(instance_url)
    console.print(
        Panel(
            "Wheatear can install these for you, but the catalog only accepts a browser\n"
            "session — your API key cannot reach it.\n\n"
            f"  1. Open [bold]{escape(origin)}[/bold] while signed in\n"
            "  2. DevTools → Network → click any request → Copy → [bold]Copy as cURL[/bold]\n"
            "  3. Paste just the [bold]Cookie:[/bold] header value below\n\n"
            "[dim]Used for this run only. Never written to disk, never logged. Sign out or "
            "close the browser session afterwards if you like.[/dim]",
            title="[bold]Install automatically?[/bold]",
            border_style=_SLATE,
        )
    )
    flush_input()
    cookie = questionary.password("Console Cookie header (blank to skip):").ask()
    if _cancelled(cookie) or not cookie.strip():
        return None
    return cookie.strip()


def _auto_install(pending: list, instance_url: str, cookie: str) -> list[str]:
    """Install each pending catalog tool through the console. Returns what landed."""
    from wheatear.connectors.orchestrate.catalog_install import ConsoleSession, install_artifact

    try:
        session = ConsoleSession(instance_url, cookie)
    except Exception as exc:  # noqa: BLE001 - a bad cookie is a message, not a crash
        console.print(f"  [red]{escape(str(exc))}[/red]")
        return []

    landed: list[str] = []
    for item in pending:
        if not item.artifact_id:
            console.print(
                f"  [yellow]—[/yellow]  {escape(item.title)}: the catalog snapshot has no id "
                "for this one, so it has to be installed by hand."
            )
            continue
        try:
            with console.status(f"  Installing {item.title}…", spinner="dots"):
                installed = install_artifact(session, item.artifact_id)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            console.print(f"  [red]✗[/red]  {escape(item.title)}: {escape(str(exc))}")
            continue
        console.print(f"  [green]✓[/green]  installed {escape(installed.summary())}")
        landed.append(item.install_ref)

        for app_id in installed.app_ids:
            from wheatear.connectors.orchestrate.provisioning import CredentialRequest

            _configure_connection(
                CredentialRequest(app_id=app_id, kind="bearer_token", tools=[installed.name]),
                offered=installed.security_schemes,
            )
    return landed


def _setup_connections(report, client, instance_url: str, token: str) -> None:
    """Offer to configure the connections the migrated tools authenticate through.

    Runs as part of the migration rather than being left to the first chat.
    With a member connection the platform asks whoever is talking to the agent
    to sign in, which is correct but is also the first time anybody finds out a
    credential was needed -- so the choice is put here, where the person doing
    the migration can settle it once.

    Which connection a tool uses is read from the tool's own binding, never
    guessed from its name: `SNOWMCPALL:get_record` shares every token with
    `servicenow_oauth2_...` and binds to neither.
    """
    from wheatear.connectors.orchestrate.connections import bound_connection, list_applications
    from wheatear.connectors.orchestrate.provisioning import CredentialRequest

    carried = {t for outcome in report.agents for t in outcome.tools}
    if not carried:
        return
    try:
        records = {str(t.get("name")): t for t in client.list_all_tools()}
        state = {}
        for connection in list_applications(instance_url, token):
            # Draft is what a migrated agent runs against first, and a
            # connection can be ready in one environment and broken in the
            # other -- so they are not collapsed.
            if connection.environment == "draft" or connection.app_id not in state:
                state[connection.app_id] = connection
    except Exception as exc:  # noqa: BLE001 - unknown state is a skipped offer, not a failure
        console.print(f"  [dim]Could not read connection state ({type(exc).__name__}).[/dim]")
        return

    wanted: dict[str, list[str]] = {}
    for name in sorted(carried):
        record = records.get(name)
        if record is None:
            continue
        for app_id in bound_connection(record).app_ids:
            wanted.setdefault(app_id, []).append(name)

    if not wanted:
        console.print("  [dim]No migrated tool declares a connection.[/dim]")
        return

    console.rule(
        "[bold cyan]Connections — so the tools work before anyone asks[/bold cyan]", style="dim"
    )

    broken = [a for a in wanted if not getattr(state.get(a), "ready", False)]
    console.print(
        f"  {len(wanted)} connection(s) back the tools that were migrated"
        + (f"; [bold]{len(broken)}[/bold] cannot work as configured." if broken else ", all configured.")
    )
    # Always asked, even when everything looks fine and even for a single
    # connection. "Here is what I have -- anything else in mind?" is a much
    # better question than silence, and silence is what let a connection with
    # an empty server URL through as working.
    flush_input()
    pace = questionary.select(
        "How do you want to handle them?",
        choices=[
            questionary.Choice(
                "Show me each one — I'll confirm or change it", value="each"
            ),
            questionary.Choice("Only ask about the ones that cannot work", value="broken"),
            questionary.Choice(
                "Accept what is already there for all of them — faster, and I accept "
                "some may need fixing by hand afterwards",
                value="defaults",
            ),
        ],
        default="each",
    ).ask()
    if _cancelled(pace):
        return

    if pace == "defaults":
        console.print(
            Panel(
                "Left exactly as they are. Anything not already configured will fail on the\n"
                "agent's first call — with an error naming a missing credential field rather\n"
                "than the connection — so check these in the console before relying on them:\n\n"
                + "\n".join(
                    f"  • {escape(a)}  [dim]({escape(getattr(state.get(a), 'summary', lambda: 'not configured')())})[/dim]"
                    for a in (broken or wanted)
                ),
                title="[bold yellow]Connections left untouched[/bold yellow]",
                border_style="yellow",
            )
        )
        for app_id in broken:
            report.manual_steps.append(
                _connection_manual_step(app_id, wanted[app_id], state.get(app_id))
            )
        return

    for app_id, tools in wanted.items():
        current = state.get(app_id)
        ready = getattr(current, "ready", False)
        if ready and pace == "broken":
            continue

        # Shown whether or not it is broken. "Here is what I have -- anything
        # else in mind?" is a much better question than silence, and silence is
        # what let a connection with no server URL through as working.
        console.print()
        console.print(
            f"  [bold]{escape(app_id)}[/bold]  [dim]needed by[/dim] {escape(', '.join(tools))}"
        )
        console.print(
            f"    {'[green]✓[/green]' if ready else '[yellow]![/yellow]'} "
            + escape(current.summary() if current is not None else "not configured on this tenant")
        )
        flush_input()
        if not questionary.confirm(
            "Change it?" if ready else f"Set up `{app_id}` now?", default=not ready
        ).ask():
            if not ready:
                report.manual_steps.append(_connection_manual_step(app_id, tools, current))
                console.print("  [dim]Left for the console.[/dim]")
            continue
        _configure_connection(CredentialRequest(app_id=app_id, kind="basic_auth", tools=tools))


def _connection_manual_step(app_id: str, tools: list[str], current) -> object:
    """Record a connection the operator chose not to fix, so the closing
    checklist still names it rather than letting it disappear."""
    from wheatear.pipeline.solution_migration import CONNECTIONS_ROUTE, ManualStep

    detail = current.summary() if current is not None else "It is not configured on this tenant."
    return ManualStep(
        kind="configure-connection",
        title=f"Finish configuring `{app_id}`",
        detail=(
            f"{detail} Until it is, {', '.join(tools)} will fail on the first call -- with an "
            "error naming a missing credential field rather than the connection."
        ),
        where=CONNECTIONS_ROUTE,
        agents=[],
        blocking=True,
    )


def _watch_for_installs(report, client, instance_url: str, rerun):
    """Stay open until the missing tools appear, then finish the job.

    The alternative was telling somebody to install a tool and re-run the whole
    migration by hand, which is two context switches and a command they have to
    remember. Here the agents are already on the target; the moment the tool
    shows up on the instance the migration is run again against the same
    selection, which re-resolves the tool and updates the agents in place.

    Re-running everything rather than patching the affected agents is
    deliberate: it is one code path, already exercised, and it also picks up
    anything else that changed on the tenant while the operator was in the
    console.
    """
    from wheatear.pipeline.solution_migration import installed_tool_names, still_missing

    offered_install = False
    while report.pending:
        _show_install_checklist(report, instance_url)

        # Offer to do it rather than only to wait. Asked once per run: if they
        # decline, or the cookie does not work, nagging on every loop is worse
        # than letting them get on with the console.
        if not offered_install and any(p.artifact_id for p in report.pending):
            offered_install = True
            flush_input()
            choice = questionary.select(
                "How do you want to handle these?",
                choices=[
                    questionary.Choice(
                        "Install them for me — I'll paste a console session", value="auto"
                    ),
                    questionary.Choice(
                        "I'll install them in the console — wait for me", value="wait"
                    ),
                ],
            ).ask()
            if _cancelled(choice):
                raise SystemExit(1)
            if choice == "auto":
                cookie = _ask_console_cookie(instance_url)
                if cookie:
                    _auto_install(report.pending, instance_url, cookie)
                    # Fall through to the poll, which is what confirms the
                    # install landed. Trusting a 201 over the tool list would
                    # be believing the write instead of checking it.

        missing = report.pending
        try:
            with console.status("", spinner="dots") as status:
                while missing:
                    names = ", ".join(m.install_ref for m in missing)
                    status.update(
                        f"  Waiting for [bold]{names}[/bold] — checking every "
                        f"{_POLL_SECONDS}s.  [dim]Ctrl-C to stop waiting.[/dim]"
                    )
                    time.sleep(_POLL_SECONDS)
                    found_now = still_missing(missing, installed_tool_names(client))
                    for landed in [m for m in missing if m not in found_now]:
                        console.print(
                            f"  [green]✓[/green]  [bold]{escape(landed.install_ref)}[/bold] "
                            "is now installed"
                        )
                    missing = found_now
        except KeyboardInterrupt:
            console.print()
            if not _ask_keep_waiting(missing):
                report.notes.append(
                    f"Stopped waiting with {len(missing)} tool(s) still to install: "
                    + ", ".join(m.install_ref for m in missing)
                )
                return report
            continue

        console.print(
            "\n  [green]Everything is installed.[/green] Re-running the migration so the "
            "tools attach to your agents…\n"
        )
        report = rerun()
        if report.pending:
            continue
        if any(a.deployed is False for a in report.agents):
            console.print(
                "\n  [yellow]The tools are installed, but the re-run did not land every "
                "agent.[/yellow] See the import log above."
            )
        else:
            console.print(
                "\n  [bold green]Done — every tool the source used is now on its agent.[/bold green]"
            )
    return report


def _ask_keep_waiting(missing) -> bool:
    """Ctrl-C during the watch: keep waiting, or finish without the tools."""
    names = ", ".join(m.install_ref for m in missing)
    flush_input()
    choice = questionary.select(
        f"Still waiting for {names}.",
        choices=[
            questionary.Choice("Keep waiting — check again now", value="wait"),
            questionary.Choice(
                "Stop waiting — leave the agents without these tools", value="stop"
            ),
        ],
    ).ask()
    if _cancelled(choice):
        return False
    return choice == "wait"


def ask_conflict_policy() -> str:
    """What to do about an agent of the same name already on the target.

    Asked rather than defaulted because the right answer flips depending on
    what the user is doing, and getting it wrong is quietly expensive either
    way. Re-running after installing a missing tool is the common case and
    wants *update* -- with the safe default the tool attaches itself to a brand
    new `ITSM_Agent_2` and the agent people actually use is untouched. But a
    first migration into a shared tenant wants the opposite, because silently
    overwriting an agent somebody else built is not a migration outcome anyone
    asked for.
    """
    flush_input()
    choice = questionary.select(
        "If an agent of the same name is already on the target:",
        choices=[
            questionary.Choice(
                "Update it — re-running after installing a tool attaches the tool "
                "to the agent people already use",
                value="update",
            ),
            questionary.Choice(
                "Import alongside it as _2 — never touches an existing agent", value="rename"
            ),
            questionary.Choice("Skip it — leave the existing agent completely alone", value="skip"),
        ],
        default="update",
    ).ask()
    if _cancelled(choice):
        raise SystemExit(1)
    return choice


def _orchestrate_client(creds: OrchestrateCredentials | None) -> tuple[Any, str]:
    """A REST client for the target instance, and why not if there isn't one.

    One place builds it, because the migration and the install watcher both
    need one and two constructions is two sets of credentials to keep in step.

    The reason is returned rather than swallowed. Without it a stale key in the
    keychain silently turns a migration into a dry run -- files written, an
    agent list that looks plausible, nothing on the tenant -- and the operator
    is left to work out from "imported nothing" that their credentials were the
    problem.
    """
    if creds is None:
        return None, "no target credentials were given"
    key = os.environ.get(creds.api_key_env, "")
    if not key:
        return None, f"{creds.api_key_env} is not set in this session"
    try:
        from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient

        return OrchestrateRestClient(key, creds.instance_url), ""
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which decides
        return None, f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"


def _confirm_dry_run(reason: str) -> bool:
    """The target is unreachable. Write the files anyway, or stop and fix it?

    Asked rather than assumed. Continuing produces YAML on disk and nothing on
    the tenant, which is a legitimate thing to want and a terrible thing to
    discover you got by accident.
    """
    console.print(
        Panel(
            f"[bold]{escape(reason)}[/bold]\n\n"
            "Nothing can be imported without a working connection to the instance.\n"
            "The migration can still run and write the Orchestrate YAML to disk for you\n"
            "to import yourself — but no agent will appear on the tenant.\n\n"
            "[dim]A stale key saved in your OS keychain is the usual cause. Quit and "
            "re-run to enter a new one.[/dim]",
            title="[bold red]Cannot reach watsonx Orchestrate[/bold red]",
            border_style="red",
        )
    )
    flush_input()
    choice = questionary.select(
        "What now?",
        choices=[
            questionary.Choice("Stop — I'll fix the credentials and re-run", value="stop"),
            questionary.Choice("Carry on and just write the files (dry run)", value="dry"),
        ],
    ).ask()
    if _cancelled(choice) or choice == "stop":
        return False
    return True


def _run_foundry_migration(
    scanned_selection: list,
    store,
    orchestrate_creds: OrchestrateCredentials | None,
    provider,
    output_base: Path,
    on_conflict: str = "update",
):
    """Migrate the selected agents, one call per source solution.

    Per solution rather than per agent because the corridor composes a whole
    solution at once: an agent's tools, knowledge and delegation edges are
    separate records that only become one agent when they are joined, and the
    delegation graph only survives if the agents that reference each other are
    named in the same run.
    """
    from wheatear.pipeline.solution_migration import MigrationReport, migrate_solution

    instance_url = orchestrate_creds.instance_url if orchestrate_creds else None
    client, why = _orchestrate_client(orchestrate_creds)
    token = None
    if client is not None:
        token = client._session.headers["Authorization"].split()[1]
    elif orchestrate_creds is not None and not _confirm_dry_run(why):
        return None

    # Group the picks by the solution they came from.
    by_solution: dict[str, tuple] = {}
    for scan, bot_schema, _bot_name in scanned_selection:
        entry = by_solution.setdefault(scan.solution_name, (scan, set()))
        entry[1].add(bot_schema)

    combined = MigrationReport(output_dir=output_base)
    for scan, wanted in by_solution.values():
        console.print(f"\n  [bold cyan]Solution:[/bold cyan]  [bold]{scan.solution_label}[/bold]")
        result = migrate_solution(
            scan.sol_dir,
            output_base / _safe_dirname(scan.solution_name),
            store=store,
            client=client,
            provider=provider,
            instance_url=instance_url,
            token=token,
            api_key=os.environ.get(orchestrate_creds.api_key_env, "") if orchestrate_creds else None,
            on_conflict=on_conflict,
            dry_run=client is None,
            only=wanted,
            report=_migration_reporter(),
        )
        combined.agents.extend(result.agents)
        combined.manual_steps.extend(result.manual_steps)
        combined.knowledge_bases.extend(result.knowledge_bases)
        combined.notes.extend(result.notes)
        combined.installed_pool = max(combined.installed_pool, result.installed_pool)
        combined.catalog_pool = max(combined.catalog_pool, result.catalog_pool)
    return combined


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

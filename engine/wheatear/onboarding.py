"""Pre-flight dependency screens: shared header + a checklist + an
install/refresh/continue loop, used in two places:

  run_onboarding()        -- the base checks (wheatear.syscheck.check_all),
                              shown once before the main wizard menu (and
                              again on a later run if never completed).

  ensure_corridor_tools()  -- corridor-specific tooling (the PAC CLI + the
                              .NET SDK it needs, the watsonx Orchestrate
                              CLI), run right after source/target platform
                              selection so a missing dependency surfaces as
                              a clear checklist instead of a raw exception
                              deep inside discovery or deploy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from wheatear.banner import AMBER, SLATE, print_compact_header, print_header, print_notes
from wheatear.config import WheatearConfig, save_config
from wheatear.syscheck import (
    DependencyStatus,
    check_all,
    check_corridor,
    corridor_needs_checks,
)
from wheatear.tui import flush_input

console = Console()


def _cancelled(value) -> bool:
    """questionary returns None on Ctrl-C / Ctrl-D; centralize the check."""
    return value is None


def needs_onboarding(config: WheatearConfig | None) -> bool:
    """First run (no config, or onboarding was never completed) triggers
    the onboarding screen. Once completed it won't reappear just because an
    optional dependency (git, a keyring backend) is still missing -- those
    degrade gracefully elsewhere in the app and re-nagging every launch
    would work against being user friendly, not for it.
    """
    return config is None or not config.onboarding_completed


def _render_chrome(title: str, intro_text: str, full_header: bool) -> None:
    """Everything above the results table -- drawn before the probes run as
    well as after, so the screen has content on it while checks are still
    going instead of sitting blank."""
    console.clear()
    if full_header:
        print_header(console)
    else:
        print_compact_header(console)
    console.print(
        Panel(intro_text, title=f"[bold]{title}[/bold]", border_style=SLATE, expand=False)
    )


def _probe_statuses(
    get_statuses: Callable[..., list[DependencyStatus]],
    title: str,
    intro_text: str,
    full_header: bool,
) -> list[DependencyStatus]:
    """Run the checks behind a spinner that names the tool being probed.

    These are genuinely slow -- a Copilot Studio corridor check spends the
    better part of ten seconds in `dotnet --version` and `pac help` cold
    starts. Run silently, that pause is indistinguishable from a crash, and
    a user who assumes it hung starts hammering keys at a screen that is
    about to put a menu under their cursor.
    """
    _render_chrome(title, intro_text, full_header)
    console.print()
    with console.status("  Checking your environment…", spinner="dots") as status:
        return get_statuses(
            lambda name: status.update(f"  Checking [bold]{name}[/bold]… [dim](this can take a moment)[/dim]")
        )


def _render_screen(
    statuses: list[DependencyStatus],
    restart_pending: dict[str, str],
    title: str,
    intro_text: str,
    full_header: bool,
) -> None:
    """Redraw the whole checklist screen in place. Called at the top of
    every loop iteration so re-checking/installing updates one live screen
    instead of stacking a fresh header+table block on top of the last one.

    The full splash header belongs to the *entrance* screen only (see
    _run_checklist_loop's `splash`). It's ~13 lines on its own, so repeating
    it per loop iteration easily exceeds a standard terminal's height once
    the table and menu are added -- and re-showing the logo mid-wizard reads
    as "something crashed and dumped me back at the start screen".
    """
    _render_chrome(title, intro_text, full_header)

    table = Table(border_style=SLATE, show_header=True, header_style="bold dim", padding=(0, 2))
    table.add_column("Dependency", style="bold", min_width=28)
    table.add_column("Status", width=18)
    table.add_column("Detail")
    for s in statuses:
        if s.name in restart_pending:
            # Fixed for real this session, but a re-check can't see it until
            # Wheatear restarts -- show that distinctly, never as "missing"
            # again, or a successful fix looks like it silently failed.
            table.add_row(s.name, "[cyan]✓ fixed (restart needed)[/cyan]", restart_pending[s.name])
        elif s.installed:
            table.add_row(s.name, "[green]✓ installed[/green]", s.detail)
        elif s.required:
            table.add_row(s.name, "[red]✗ missing[/red]", s.detail)
        else:
            table.add_row(s.name, "[yellow]○ missing[/yellow]", s.detail)
    console.print()
    console.print(table)


def _install_manually(statuses: list[DependencyStatus], restart_pending: dict[str, str]) -> None:
    """Show every applicable OS/package-manager suggestion (not just the one
    the checker would auto-pick) and drop the user into their own shell to
    run whatever they choose. Re-checking happens naturally on the next loop
    iteration once they exit back out.
    """
    missing = [
        s for s in statuses if not s.installed and s.manual_hint and s.name not in restart_pending
    ]
    if not missing:
        console.print("  [dim]Nothing missing that has a manual suggestion.[/dim]")
        return

    body = "\n\n".join(f"[bold]{s.name}[/bold]\n{s.manual_hint}" for s in missing)
    console.print(
        Panel(
            body,
            title="[bold]Suggested commands for your system[/bold]",
            border_style=AMBER,
            expand=False,
        )
    )

    flush_input()
    proceed = questionary.confirm(
        "Open a shell here so you can run these yourself? (type 'exit' to come back)",
        default=True,
    ).ask()
    if _cancelled(proceed) or not proceed:
        return

    shell = os.environ.get("SHELL", "/bin/bash")
    console.print(f"  [dim]Opening {shell} -- type 'exit' or Ctrl-D to return to Wheatear.[/dim]")
    subprocess.run([shell])
    console.print("  [dim]Back in Wheatear.[/dim]")
    flush_input()
    questionary.press_any_key_to_continue("Press any key to refresh the checklist...").ask()


def _install_automatically(statuses: list[DependencyStatus], restart_pending: dict[str, str]) -> None:
    """Detects the OS/package manager live (via each DependencyStatus.fix,
    built fresh on every call) and runs the fix -- only after an explicit
    per-item confirmation showing the exact command first.
    """
    fixable = [
        s for s in statuses if not s.installed and s.fix and s.name not in restart_pending
    ]
    if not fixable:
        console.print(
            "  [dim]Nothing here can be auto-installed -- try 'Install manually' instead.[/dim]"
        )
        return

    for s in fixable:
        console.print(
            Panel(
                f"[bold]{s.fix_command}[/bold]",
                title=f"Install: {s.name}",
                border_style=AMBER,
                expand=False,
            )
        )
        flush_input()
        proceed = questionary.confirm(f"Run this now to install {s.name}?", default=True).ask()
        if _cancelled(proceed) or not proceed:
            console.print(f"  [dim]Skipped {s.name}.[/dim]")
            continue

        with console.status(f"  Installing {s.name}..."):
            outcome = s.fix()

        if outcome.success:
            console.print(f"  [green]✓[/green]  {s.name}: {outcome.detail}")
            if outcome.restart_required:
                restart_pending[s.name] = outcome.detail
        else:
            console.print(f"  [red]✗[/red]  {s.name} install failed: {outcome.detail}")

    flush_input()
    questionary.press_any_key_to_continue("Press any key to refresh the checklist...").ask()


def _run_checklist_loop(
    get_statuses: Callable[..., list[DependencyStatus]],
    title: str,
    intro_text: str,
    allow_back: bool,
    back_label: str,
    hard_gate: bool = False,
    splash: bool = False,
) -> tuple[bool, dict[str, str]]:
    """Shared clear-redraw checklist + Install/Refresh/Continue(/Back)/Exit
    loop. Returns (went_back, restart_pending); went_back is always False
    when allow_back is False.

    hard_gate=True removes the "Continue anyway" bypass while a required
    item is still missing -- for corridor tooling (dotnet, PAC, the
    Orchestrate CLI) there's no graceful degradation like there is for the
    base checks (git, keyring): without them, discovery/deploy just fails.

    splash=True draws the full logo on the first render. Only the launch
    screen should ask for it; a checklist that appears mid-wizard must not,
    or the logo resurfacing looks like the wizard restarted from scratch.
    """
    restart_pending: dict[str, str] = {}
    first_render = True

    while True:
        full_header = splash and first_render
        statuses = _probe_statuses(get_statuses, title, intro_text, full_header)
        _render_screen(statuses, restart_pending, title, intro_text, full_header)
        first_render = False

        has_required_missing = any(not s.installed and s.required for s in statuses)
        has_manual = any(
            not s.installed and s.manual_hint and s.name not in restart_pending for s in statuses
        )
        has_fixable = any(
            not s.installed and s.fix and s.name not in restart_pending for s in statuses
        )

        choices = []
        if has_manual:
            choices.append(questionary.Choice("Install manually (opens a shell)", value="manual"))
        if has_fixable:
            choices.append(
                questionary.Choice(
                    "Install automatically (detects your OS & package manager)", value="auto"
                )
            )
        choices.append(
            questionary.Choice("Refresh list (installed something manually?)", value="refresh")
        )
        if not (hard_gate and has_required_missing):
            choices.append(
                questionary.Choice(
                    "Continue anyway" if has_required_missing else "Continue",
                    value="continue",
                )
            )
        if allow_back:
            choices.append(questionary.Choice(back_label, value="back"))
        choices.append(questionary.Choice("Exit", value="exit"))

        # get_statuses() above shells out to `dotnet --version`, `pac
        # --version` etc. and blocks for seconds; anything typed during that
        # would otherwise be applied to this menu the instant it appears.
        flush_input()
        action = questionary.select("What would you like to do?", choices=choices).ask()
        if _cancelled(action) or action == "exit":
            raise SystemExit(1)
        if action == "back":
            return True, restart_pending
        if action == "manual":
            console.print()
            _install_manually(statuses, restart_pending)
            continue
        if action == "auto":
            console.print()
            _install_automatically(statuses, restart_pending)
            continue
        if action == "refresh":
            continue
        return False, restart_pending  # action == "continue"


def _show_restart_pending_if_any(restart_pending: dict[str, str]) -> None:
    if not restart_pending:
        return
    console.print()
    console.print(
        Panel(
            "\n".join(f"[bold]{name}[/bold] -- {detail}" for name, detail in restart_pending.items()),
            title="[bold cyan]Fixed -- restart Wheatear to activate[/bold cyan]",
            border_style=AMBER,
            expand=False,
        )
    )


def run_onboarding(config: WheatearConfig | None) -> WheatearConfig:
    """Render the onboarding screen and loop until the user chooses to
    continue or exit. Returns the config to use for the rest of the run
    (with onboarding_completed set), already saved to disk.
    """
    _, restart_pending = _run_checklist_loop(
        check_all,
        title="Onboarding",
        intro_text=(
            "First run -- checking your environment before showing the main menu.\n"
            "None of this blocks you: anything optional can be skipped and fixed later."
        ),
        allow_back=False,
        back_label="",
        splash=True,
    )

    final_config = config or WheatearConfig()
    final_config.onboarding_completed = True
    save_config(final_config)

    _show_restart_pending_if_any(restart_pending)
    print_notes(console)
    return final_config


def ensure_corridor_tools(
    source: str,
    target: str,
    deploy_to_orchestrate: bool = False,
    back_label: str = "◀ Back",
) -> bool:
    """Check the tooling this specific corridor needs (the PAC CLI + its
    .NET prerequisite for Copilot Studio, the Orchestrate CLI for deploy)
    and walk the user through fixing anything missing before the wizard
    would otherwise hit it deep inside discovery or deploy as a raw
    exception. Call right after source/target platform selection.

    Returns True if OK to proceed, False if the user chose to go back
    instead (e.g. to pick a different target platform).
    """
    if not corridor_needs_checks(source, target, deploy_to_orchestrate):
        return True  # nothing corridor-specific needed for this pair

    went_back, restart_pending = _run_checklist_loop(
        lambda on_check=None: check_corridor(
            source, target, deploy_to_orchestrate, on_check=on_check
        ),
        title="Corridor check",
        intro_text=(
            f"Checking the extra tools this migration needs ({source} → {target}) "
            "before continuing."
        ),
        allow_back=True,
        back_label=back_label,
        hard_gate=True,
    )
    _show_restart_pending_if_any(restart_pending)
    if restart_pending:
        flush_input()
        questionary.press_any_key_to_continue("Press any key to continue...").ask()
    return not went_back

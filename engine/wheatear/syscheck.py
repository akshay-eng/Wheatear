"""Pre-flight dependency checks.

Two tiers:

  check_all()      -- base checks Wheatear needs regardless of which
                       migration corridor gets picked: `git` (used by
                       source_fetch.py for GitHub-URL exports) and a working
                       `keyring` secret backend (creds.py).

  check_corridor()  -- checks that only apply once a source/target platform
                       pair is actually chosen: the Power Platform `pac` CLI
                       (+ the .NET SDK it requires) whenever Copilot Studio
                       is involved, and the watsonx Orchestrate CLI whenever
                       deploying *into* Orchestrate. Run by
                       onboarding.ensure_corridor_tools() right after
                       platform selection, before any discovery/deploy work
                       that would otherwise fail deep inside a pipeline run
                       with a raw, unhelpful exception.

Nothing in this module ever runs a fix without the caller (the onboarding
screen) getting explicit per-item user confirmation first -- the check_*
functions only inspect the system, they never install anything themselves.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class InstallOutcome:
    success: bool
    detail: str
    # True when the fix genuinely worked but can't be verified by re-running
    # check_all() in *this* process (e.g. keyring caches its backend for the
    # process lifetime) -- the caller must not treat a still-"missing" status
    # after this as the fix having failed.
    restart_required: bool = False


@dataclass
class DependencyStatus:
    name: str
    installed: bool
    detail: str
    required: bool
    fix_command: str | None = None          # shown to the user before running
    fix: Callable[[], InstallOutcome] | None = None  # None => no safe auto-fix exists
    manual_hint: str | None = None          # per-OS/per-manager suggestions for a manual install


def _pip_install(packages: list[str], upgrade: bool = False) -> InstallOutcome:
    """Install into the *current* interpreter's environment -- no sudo, no
    system package manager, so this is always safe to run without asking
    the user for anything beyond the one confirmation already shown."""
    cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.extend(packages)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return InstallOutcome(False, "pip install timed out after 300s")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return InstallOutcome(False, detail[:400] or f"pip exited {result.returncode}")
    return InstallOutcome(True, f"installed {', '.join(packages)}")


def _run_system_install(cmd: list[str]) -> InstallOutcome:
    """Run a system package-manager command with inherited stdio.

    Deliberately does NOT capture stdout/stderr: if the command needs sudo,
    the password prompt must appear in the user's own terminal rather than
    being silently swallowed (or the process hanging forever waiting on a
    prompt no one can see).
    """
    try:
        result = subprocess.run(cmd, timeout=300)
    except subprocess.TimeoutExpired:
        return InstallOutcome(False, "timed out after 300s")
    except FileNotFoundError as exc:
        return InstallOutcome(False, str(exc))
    if result.returncode != 0:
        return InstallOutcome(False, f"exited with code {result.returncode}")
    return InstallOutcome(True, "installed")


# ---------------------------------------------------------------------------
# Python interpreter
# ---------------------------------------------------------------------------

_MIN_PYTHON = (3, 10)


def _check_python_version() -> DependencyStatus:
    ok = sys.version_info >= _MIN_PYTHON
    return DependencyStatus(
        name=f"Python >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}",
        installed=ok,
        detail=f"found {platform.python_version()}",
        required=True,
        # No safe auto-fix: upgrading the interpreter Wheatear itself is
        # running under isn't something to do from inside that interpreter.
    )


# ---------------------------------------------------------------------------
# git (used by source_fetch.py for GitHub-URL exports)
# ---------------------------------------------------------------------------

def _git_install_command() -> list[str] | None:
    """Best-effort install command for the current OS. None if no known
    package manager is available -- caller falls back to a manual hint."""
    system = platform.system()

    if system == "Darwin":
        return ["brew", "install", "git"] if shutil.which("brew") else None

    if system == "Windows":
        if shutil.which("winget"):
            return ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"]
        if shutil.which("choco"):
            return ["choco", "install", "git", "-y"]
        return None

    # Linux: try known package managers in order of prevalence.
    # apt-get needs its package index refreshed first -- on a fresh/minimal
    # image (or after `apt-get clean`) a bare `install` fails with "Unable
    # to locate package" even though the package genuinely exists.
    managers = [
        ("apt-get", ["sudo", "sh", "-c", "apt-get update && apt-get install -y git"]),
        ("dnf", ["sudo", "dnf", "install", "-y", "git"]),
        ("yum", ["sudo", "yum", "install", "-y", "git"]),
        ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "git"]),
        ("zypper", ["sudo", "zypper", "install", "-y", "git"]),
        ("apk", ["sudo", "apk", "add", "git"]),
    ]
    for binary, cmd in managers:
        if shutil.which(binary):
            return cmd
    return None


def _git_manual_hint() -> str:
    """Suggestions for the user to run themselves -- shown for every option
    on their OS (not just the one auto-detected), so a manual install isn't
    limited to whatever package manager check_all() happened to pick."""
    system = platform.system()

    if system == "Darwin":
        return "brew install git\n(or: xcode-select --install)"

    if system == "Windows":
        return "winget install --id Git.Git -e --source winget\n(or: choco install git -y)"

    options = [
        ("apt", "sudo apt-get update && sudo apt-get install -y git", "apt-get"),
        ("dnf", "sudo dnf install -y git", "dnf"),
        ("pacman", "sudo pacman -S --noconfirm git", "pacman"),
    ]
    lines = []
    for label, cmd, binary in options:
        tag = "   <- detected on this system" if shutil.which(binary) else ""
        lines.append(f"{label}:  {cmd}{tag}")
    return "\n".join(lines)


def _check_git() -> DependencyStatus:
    if shutil.which("git") is not None:
        try:
            version = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            version = "found"
        return DependencyStatus("git", True, version, required=False)

    cmd = _git_install_command()
    return DependencyStatus(
        name="git",
        installed=False,
        detail="not on PATH (only needed for GitHub-URL exports in manual mode)",
        required=False,
        fix_command=shlex.join(cmd) if cmd else None,
        fix=(lambda: _run_system_install(cmd)) if cmd else None,
        manual_hint=_git_manual_hint(),
    )


# ---------------------------------------------------------------------------
# keyring secret backend (used by creds.py to persist API keys)
# ---------------------------------------------------------------------------

def _fix_keyring_backend() -> InstallOutcome:
    """Install keyrings.alt. keyring caches its backend-discovery result for
    the life of the process, so this genuinely takes effect immediately in a
    *new* process (confirmed: a fresh interpreter picks it up right away) --
    but re-running the check inside the *same* Wheatear session will still
    report missing. Say so, rather than let a "Refresh" that still shows
    missing look like the install silently failed.
    """
    outcome = _pip_install(["keyrings.alt"])
    if outcome.success:
        return InstallOutcome(True, outcome.detail, restart_required=True)
    return outcome


def _keyring_manual_hint() -> str:
    system = platform.system()
    note = {
        "Darwin": "macOS Keychain normally works out of the box -- this is unusual.",
        "Windows": "Windows Credential Manager normally works out of the box -- this is unusual.",
    }.get(system, "No Secret Service daemon (e.g. gnome-keyring, kwallet) is reachable.")
    return (
        f"{note}\n"
        f"Fallback (no system daemon needed): {sys.executable} -m pip install keyrings.alt\n"
        f"(takes effect on Wheatear's *next* restart, not this session)"
    )


def _check_keyring_backend() -> DependencyStatus:
    from wheatear.creds import SERVICE

    probe_key = "onboarding-selftest"
    try:
        import keyring

        keyring.set_password(SERVICE, probe_key, "ok")
        value = keyring.get_password(SERVICE, probe_key)
        keyring.delete_password(SERVICE, probe_key)
        if value != "ok":
            raise RuntimeError("round-trip mismatch")
        backend = keyring.get_keyring().__class__.__name__
        return DependencyStatus("Credential storage (keyring)", True, f"backend: {backend}", required=False)
    except Exception as exc:
        fix = None
        cmd_str = None
        # keyrings.alt ships a pure-Python encrypted-file backend that needs
        # no system Secret Service / D-Bus daemon -- a safe, sudo-free fallback.
        if platform.system() == "Linux":
            cmd_str = f"{sys.executable} -m pip install keyrings.alt"
            fix = lambda: _fix_keyring_backend()  # noqa: E731
        return DependencyStatus(
            name="Credential storage (keyring)",
            installed=False,
            detail=f"no working secret backend ({exc}); API keys won't persist between sessions",
            required=False,
            fix_command=cmd_str,
            fix=fix,
            manual_hint=_keyring_manual_hint(),
        )


def _probe(on_check: Callable[[str], None] | None, label: str, fn):
    """Announce a check before running it. Individual probes shell out to
    tools that are slow to even start (`pac help` alone is seconds), so the
    caller needs to be able to say *which* one it's waiting on -- a silent
    pause reads as a hang."""
    if on_check:
        on_check(label)
    return fn()


def check_all(on_check: Callable[[str], None] | None = None) -> list[DependencyStatus]:
    """Run every base pre-flight check. Read-only -- never installs anything."""
    return [
        _probe(on_check, "Python", _check_python_version),
        _probe(on_check, "git", _check_git),
        _probe(on_check, "credential store", _check_keyring_backend),
    ]


# ---------------------------------------------------------------------------
# .NET SDK (the PAC CLI is a dotnet global tool -- it can't install without it)
# ---------------------------------------------------------------------------

def _dotnet_install_command() -> list[str] | None:
    system = platform.system()

    if system == "Windows":
        return ["winget", "install", "-e", "--id", "Microsoft.DotNet.SDK.8"] if shutil.which("winget") else None

    # macOS + Linux: Microsoft's own install script. Installs to ~/.dotnet
    # for the current user -- no sudo, and no dependency on whatever (or
    # whether any) dotnet package happens to exist in a given distro's repos.
    if not shutil.which("bash"):
        return None
    if shutil.which("curl"):
        fetch = "curl -sSL https://dot.net/v1/dotnet-install.sh"
    elif shutil.which("wget"):
        fetch = "wget -qO- https://dot.net/v1/dotnet-install.sh"
    else:
        return None
    return ["bash", "-c", f"{fetch} | bash /dev/stdin --channel LTS"]


def _dotnet_manual_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return "winget install -e --id Microsoft.DotNet.SDK.8\n(or download from https://dotnet.microsoft.com/download)"
    return (
        "curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin --channel LTS\n"
        "(no curl? wget -qO- https://dot.net/v1/dotnet-install.sh | bash /dev/stdin --channel LTS)\n"
        "(installs to ~/.dotnet for your user only -- no sudo needed;\n"
        "add ~/.dotnet to PATH afterward, or restart Wheatear to pick it up)"
    )


def _fix_dotnet() -> InstallOutcome:
    cmd = _dotnet_install_command()
    if cmd is None:
        return InstallOutcome(False, "no supported installer found for this OS")
    outcome = _run_system_install(cmd)
    if outcome.success:
        # The install script drops the SDK in ~/.dotnet -- make it visible to
        # the current process immediately rather than requiring a restart.
        dotnet_dir = str(Path.home() / ".dotnet")
        current = os.environ.get("PATH", "")
        if dotnet_dir not in current.split(os.pathsep):
            os.environ["PATH"] = dotnet_dir + os.pathsep + current
    return outcome


def _check_dotnet() -> DependencyStatus:
    if shutil.which("dotnet") is not None:
        try:
            version = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            version = "found"
        return DependencyStatus(".NET SDK", True, version, required=True)

    cmd = _dotnet_install_command()
    return DependencyStatus(
        name=".NET SDK",
        installed=False,
        detail="not on PATH (required by the PAC CLI for Copilot Studio)",
        required=True,
        fix_command=shlex.join(cmd) if cmd else None,
        fix=_fix_dotnet if cmd else None,
        manual_hint=_dotnet_manual_hint(),
    )


# ---------------------------------------------------------------------------
# PAC CLI (Copilot Studio discovery/export/import)
# ---------------------------------------------------------------------------

def _install_pac() -> InstallOutcome:
    from wheatear.connectors.copilot_studio import pac_client as pac

    if shutil.which("dotnet") is None:
        return InstallOutcome(False, "dotnet is required first -- install that, then retry")
    try:
        pac.install()
    except Exception as exc:  # noqa: BLE001 -- surfacing to the UI, not re-raising
        return InstallOutcome(False, str(exc)[:400])
    found, version = pac.check()
    if found:
        return InstallOutcome(True, f"installed {version}")
    return InstallOutcome(False, "install completed but 'pac' is still not on PATH")


def _check_pac() -> DependencyStatus:
    from wheatear.connectors.copilot_studio import pac_client as pac

    found, version = pac.check()
    if found:
        return DependencyStatus("PAC CLI (Copilot Studio)", True, version, required=True)

    if shutil.which("dotnet") is None:
        # Don't offer a fix that's guaranteed to fail -- dotnet must go first
        # (it's checked as its own separate item in the same screen).
        return DependencyStatus(
            name="PAC CLI (Copilot Studio)",
            installed=False,
            detail="not installed -- install the .NET SDK above first",
            required=True,
            manual_hint=f"Once dotnet is installed:\n{pac.install_guide()}",
        )

    return DependencyStatus(
        name="PAC CLI (Copilot Studio)",
        installed=False,
        detail="not on PATH",
        required=True,
        fix_command=pac.install_guide(),
        fix=_install_pac,
        manual_hint=pac.install_guide(),
    )


# ---------------------------------------------------------------------------
# watsonx Orchestrate CLI (only needed to deploy INTO Orchestrate)
# ---------------------------------------------------------------------------

def _fix_orchestrate_cli() -> InstallOutcome:
    return _pip_install(["ibm-watsonx-orchestrate"], upgrade=True)


def _check_orchestrate_cli() -> DependencyStatus:
    if shutil.which("orchestrate") is not None:
        return DependencyStatus("Orchestrate CLI (deploy)", True, "found", required=True)

    cmd_str = f"{sys.executable} -m pip install --upgrade ibm-watsonx-orchestrate"
    return DependencyStatus(
        name="Orchestrate CLI (deploy)",
        installed=False,
        detail="not on PATH (required to deploy into watsonx Orchestrate)",
        required=True,
        fix_command=cmd_str,
        fix=_fix_orchestrate_cli,
        manual_hint=cmd_str,
    )


def corridor_needs_checks(source: str, target: str, deploy_to_orchestrate: bool = False) -> bool:
    """Whether this corridor has any extra tooling to verify at all.

    Answers from the platform names alone, without running a single probe:
    check_corridor() takes several seconds (`pac help` is a .NET app cold
    start), so asking it "is there anything to check?" and then calling it
    again for the answers would double an already long wait.
    """
    return "copilot-studio" in (source, target) or deploy_to_orchestrate


def check_corridor(
    source: str,
    target: str,
    deploy_to_orchestrate: bool = False,
    on_check: Callable[[str], None] | None = None,
) -> list[DependencyStatus]:
    """Checks that depend on which platforms are actually in play for this
    migration -- run once, right after source/target selection, so a
    missing dotnet/PAC/Orchestrate-CLI surfaces as a clear checklist instead
    of a raw exception deep inside discovery or deploy.
    """
    checks: list[DependencyStatus] = []
    if "copilot-studio" in (source, target):
        checks.append(_probe(on_check, ".NET SDK", _check_dotnet))
        checks.append(_probe(on_check, "PAC CLI", _check_pac))
    if deploy_to_orchestrate:
        checks.append(_probe(on_check, "watsonx Orchestrate CLI", _check_orchestrate_cli))
    return checks
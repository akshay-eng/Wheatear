"""PAC CLI (Microsoft Power Platform CLI) wrapper for Copilot Studio auto-discovery.

Replaces the REST-API + MSAL approach for Copilot Studio sources: the PAC CLI
already handles authentication, environment selection, and solution export, so
we drive it as a subprocess rather than reimplementing that layer ourselves.

Minimum PAC version: 1.x  (confirmed against 1.52.1)
Install:  dotnet tool install --global Microsoft.PowerApps.CLI.Tool --version 1.52.1
Auth:     pac auth create --deviceCode   (device code flow, no app registration needed)

Public surface used by the wizard:
  check()                 -- is pac installed? what version?
  auth_status()           -- is there an active auth profile?
  do_device_auth(cb)      -- run auth, stream device code message to cb
  list_copilots()         -- parse 'pac copilot list'
  list_solutions()        -- parse 'pac solution list'
  export_solution()       -- run 'pac solution export'
  extract_solution()      -- unzip
  list_bots_in_solution() -- scan extracted bots/ dir for bot display names
  create_bot_slice()      -- filter an extracted solution dir to one bot
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Regex to find UUIDs in pac output (copilot ID, solution ID, etc.)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# pac solution list row: UniqueName  Friendly Name  Version  Managed
# UniqueName has no spaces, Version is x.y or x.y.z.w, Managed is True|False
_SOLUTION_ROW_RE = re.compile(r"^(\S+)\s+(.+?)\s+([\d.]+)\s+(True|False)\s*$")

# Recommended version to install if pac is missing
PAC_INSTALL_VERSION = "1.52.1"


class PacError(Exception):
    pass


@dataclass
class CopilotInfo:
    name: str
    copilot_id: str
    solution_id: str


@dataclass
class SolutionInfo:
    unique_name: str
    friendly_name: str
    version: str
    managed: bool


# ---------------------------------------------------------------------------
# Installation check
# ---------------------------------------------------------------------------

def check() -> tuple[bool, str]:
    """Return (found, version_string). version_string is empty if not found."""
    if shutil.which("pac") is None:
        return False, ""
    # pac doesn't have a clean --version flag but any call prints the version
    # in the error header (confirmed against 1.52.1).
    result = subprocess.run(
        ["pac", "help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    combined = result.stdout + result.stderr
    m = re.search(r"Version:\s*([\d.]+)", combined)
    return True, (m.group(1) if m else "unknown")


def install_guide() -> str:
    """Return the command to install the recommended PAC CLI version."""
    return (
        f"dotnet tool install --global Microsoft.PowerApps.CLI.Tool "
        f"--version {PAC_INSTALL_VERSION}"
    )


def dotnet_tools_path() -> str:
    """Return the dotnet global tools directory for the current user."""
    return str(Path.home() / ".dotnet" / "tools")


def install() -> None:
    """Run the dotnet tool install command and add the tools dir to PATH.

    Raises PacError on failure.  After a successful install the current
    process's PATH includes ~/.dotnet/tools so subsequent shutil.which("pac")
    calls find the binary without requiring a new shell.
    """
    result = subprocess.run(
        [
            "dotnet", "tool", "install",
            "--global", "Microsoft.PowerApps.CLI.Tool",
            "--version", PAC_INSTALL_VERSION,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).strip()
        # dotnet exits non-zero if the tool is already installed at this version —
        # treat that as success.
        if "already installed" in combined.lower():
            pass  # fall through to PATH injection below
        else:
            raise PacError(f"dotnet tool install failed:\n{combined[:400]}")

    # Inject ~/.dotnet/tools into the current process PATH so shutil.which
    # finds pac immediately without requiring a shell restart.
    tools = dotnet_tools_path()
    current = os.environ.get("PATH", "")
    if tools not in current.split(os.pathsep):
        os.environ["PATH"] = tools + os.pathsep + current


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def auth_status() -> tuple[bool, str]:
    """Return (authenticated, account_name).

    Runs 'pac auth list' and checks whether any account email appears in the
    output. If not authenticated, returns (False, "").
    """
    result = subprocess.run(
        ["pac", "auth", "list"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    combined = result.stdout + result.stderr
    # Look for an email address in the output
    m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", combined)
    if m:
        return True, m.group(0)
    return False, ""


def do_device_auth(on_code: Callable[[str], None]) -> str:
    """Run 'pac auth create --deviceCode', call on_code with the browser URL
    + one-time code message when it appears in stdout, then block until the
    user completes auth in their browser.

    Returns the authenticated account name, or "unknown" if it can't be parsed.
    """
    proc = subprocess.Popen(
        ["pac", "auth", "create", "--deviceCode"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    code_shown = False
    all_lines: list[str] = []

    for line in proc.stdout:  # type: ignore[union-attr]
        all_lines.append(line)
        stripped = line.strip()
        # Detect the device code message by key phrases PAC always includes
        if not code_shown and (
            "microsoft.com" in stripped.lower()
            or "enter the code" in stripped.lower()
            or "devicelogin" in stripped.lower()
        ):
            on_code(stripped)
            code_shown = True

    proc.wait()
    combined = "".join(all_lines)

    if proc.returncode != 0 and "authenticated successfully" not in combined.lower():
        raise PacError(f"pac auth create failed:\n{combined[:500]}")

    # Extract account name from "'user@domain.com' authenticated successfully."
    m = re.search(r"'([\w.+-]+@[\w.-]+\.\w+)'", combined)
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def list_copilots() -> list[CopilotInfo]:
    """Run 'pac copilot list' and parse the result.

    Uses UUID anchors in the output to extract copilot ID + solution ID
    robustly, regardless of column widths or multi-word copilot names.
    """
    result = subprocess.run(
        ["pac", "copilot", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise PacError(f"pac copilot list failed:\n{result.stderr.strip() or result.stdout.strip()}")

    copilots: list[CopilotInfo] = []
    for line in result.stdout.splitlines():
        uuids = _UUID_RE.findall(line)
        if len(uuids) < 2:
            continue  # header line or connection message — no UUIDs
        copilot_id = uuids[0]
        solution_id = uuids[1]
        # Name is everything before the copilot UUID, trimmed
        idx = line.lower().index(copilot_id.lower())
        name = line[:idx].strip()
        if name:
            copilots.append(CopilotInfo(name=name, copilot_id=copilot_id, solution_id=solution_id))

    return copilots


def list_solutions(unmanaged_only: bool = True) -> list[SolutionInfo]:
    """Run 'pac solution list' and parse the result.

    Set unmanaged_only=True (default) to exclude Microsoft-managed platform
    solutions from the list, which dramatically reduces the list size.
    """
    result = subprocess.run(
        ["pac", "solution", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise PacError(f"pac solution list failed:\n{result.stderr.strip() or result.stdout.strip()}")

    solutions: list[SolutionInfo] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SOLUTION_ROW_RE.match(line)
        if not m:
            continue
        unique_name = m.group(1)
        # Skip header line itself
        if unique_name.lower() in ("unique", "unique_name"):
            continue
        managed = m.group(4) == "True"
        if unmanaged_only and managed:
            continue
        solutions.append(
            SolutionInfo(
                unique_name=unique_name,
                friendly_name=m.group(2).strip(),
                version=m.group(3),
                managed=managed,
            )
        )

    return solutions


# ---------------------------------------------------------------------------
# Export and extraction
# ---------------------------------------------------------------------------

def export_solution(unique_name: str, dest_zip: Path) -> None:
    """Run 'pac solution export --name <unique_name> --path <dest_zip> --managed false'."""
    result = subprocess.run(
        [
            "pac", "solution", "export",
            "--name", unique_name,
            "--path", str(dest_zip),
            "--managed", "false",
        ],
        capture_output=True,
        text=True,
        timeout=180,  # large solutions may take a while
    )
    if result.returncode != 0:
        combined = result.stderr.strip() or result.stdout.strip()
        raise PacError(f"pac solution export --name {unique_name} failed:\n{combined[:500]}")


def extract_solution(zip_path: Path, dest_dir: Path) -> Path:
    """Extract a Dataverse solution ZIP to dest_dir, return the dest_dir path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir


def unpack_solution(zip_path: Path, dest_dir: Path) -> Path:
    """Run 'pac solution unpack' to convert a packed solution ZIP into the
    source-control directory layout (bots/, botcomponents/, etc.) that
    Wheatear's importer expects.

    'pac solution export' produces a packed ZIP — the raw XML is a single
    customizations.xml blob, not the per-bot directory tree. 'pac solution unpack'
    is the mandatory second step that expands it into the Wheatear-readable layout.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "pac", "solution", "unpack",
            "--zipfile", str(zip_path),
            "--folder", str(dest_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        combined = result.stderr.strip() or result.stdout.strip()
        raise PacError(f"pac solution unpack failed:\n{combined[:500]}")
    return dest_dir


# ---------------------------------------------------------------------------
# Bot slicing (filter multi-bot solutions to one bot at a time)
# ---------------------------------------------------------------------------

def list_bots_in_solution(sol_dir: Path) -> list[tuple[str, str]]:
    """Return (schemaname, display_name) for every bot found in the solution.

    PAC CLI versions and OS filesystems vary in capitalisation; we try all
    known directory name forms so unpack on macOS (case-insensitive) and Linux
    (case-sensitive) both work.
    """
    # Try both capitalisation forms that different PAC CLI / OS combos produce
    for dir_name in ("bots", "Bots"):
        bots_dir = sol_dir / dir_name
        if not bots_dir.is_dir():
            continue
        bots: list[tuple[str, str]] = []
        for bot_xml in sorted(bots_dir.glob("*/bot.xml")):
            schema = bot_xml.parent.name
            try:
                root = ET.parse(bot_xml).getroot()
                name_el = root.find("name")
                display = (name_el.text or "").strip() if name_el is not None else ""
            except ET.ParseError:
                display = ""
            bots.append((schema, display or schema))
        if bots:
            return bots
    return []


def list_solution_top_dirs(sol_dir: Path) -> list[str]:
    """Return names of top-level entries in an unpacked solution dir.
    Used for diagnostic output when bot detection fails.
    """
    if not sol_dir.is_dir():
        return []
    return sorted(p.name for p in sol_dir.iterdir())


def create_bot_slice(sol_dir: Path, bot_schema: str, dest: Path) -> Path:
    """Create a filtered solution directory that contains only the specified bot.

    Copies:
      solution.xml                 (shared metadata)
      bots/<bot_schema>/           (this bot only)
      botcomponents/<bot_schema>.* (only components belonging to this bot)

    Botcomponents belonging to a bot have schemanames that start with the bot's
    schemaname followed by a dot (e.g. bot "ai_HelperBee" owns components named
    "ai_HelperBee.gpt.default", "ai_HelperBee.topic.ConversationStart", etc.).
    """
    dest.mkdir(parents=True, exist_ok=True)

    sol_xml = sol_dir / "solution.xml"
    if sol_xml.exists():
        shutil.copy2(sol_xml, dest / "solution.xml")

    bot_src = sol_dir / "bots" / bot_schema
    if bot_src.is_dir():
        shutil.copytree(bot_src, dest / "bots" / bot_schema)

    prefix = bot_schema + "."
    comp_dir = sol_dir / "botcomponents"
    if comp_dir.is_dir():
        for comp in comp_dir.iterdir():
            if comp.is_dir() and (comp.name == bot_schema or comp.name.startswith(prefix)):
                shutil.copytree(comp, dest / "botcomponents" / comp.name)

    return dest


# ---------------------------------------------------------------------------
# Convenience: match selected copilot names to bots found in a solution
# ---------------------------------------------------------------------------

def match_bots(
    sol_bots: list[tuple[str, str]],
    copilot_names: set[str],
) -> list[tuple[str, str]]:
    """Return the subset of (schema, display_name) pairs from sol_bots whose
    display_name matches any name in copilot_names (case-insensitive)."""
    lower = {n.lower() for n in copilot_names}
    return [(s, d) for s, d in sol_bots if d.lower() in lower or s.lower() in lower]

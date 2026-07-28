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

def find_pac() -> str | None:
    """The PAC CLI, on PATH or in the dotnet global tools directory.

    The second half is what makes this work at all. `dotnet tool install
    --global` puts `pac` in `~/.dotnet/tools`, and that directory is only on
    PATH if the dotnet installer edited the user's shell profile -- which it
    does not always do, and which never affects an already-running shell.

    Looking only at PATH means an installed `pac` reads as missing, so the
    wizard offers to install it, `dotnet tool install` reports it is already
    installed, the process patches its own PATH, and the *next* launch asks
    again. Forever. Observed on this machine with pac 1.52.1 present and
    working the whole time.
    """
    found = shutil.which("pac")
    if found:
        return found
    for name in ("pac", "pac.exe"):
        candidate = Path(dotnet_tools_path()) / name
        if candidate.is_file():
            return str(candidate)
    return None


def _pac() -> str:
    """The PAC executable to invoke, or the bare name so the error names it."""
    return find_pac() or "pac"


def check() -> tuple[bool, str]:
    """Return (found, version_string). version_string is empty if not found."""
    executable = find_pac()
    if executable is None:
        return False, ""
    # pac doesn't have a clean --version flag but any call prints the version
    # in the error header (confirmed against 1.52.1).
    result = subprocess.run(
        [executable, "help"],
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

    The account reported is the *active* profile where one is marked, not
    simply the first email in the output. Somebody with two tenants signed in
    has two emails listed and only one of them is the account every later `pac`
    call will actually use; naming the wrong one tells them the migration is
    reading an environment it is not.
    """
    profiles = list_auth_profiles()
    active = next((p for p in profiles if p.active), None)
    if active is not None:
        return True, active.user or active.name or "unknown"
    if profiles:
        return True, profiles[0].user or profiles[0].name or "unknown"
    return False, ""


@dataclass
class AuthProfile:
    """One signed-in Power Platform account, as `pac auth list` reports it."""

    index: int
    active: bool
    user: str
    name: str = ""
    kind: str = ""
    environment: str = ""

    def label(self) -> str:
        parts = [self.user or self.name or f"profile {self.index}"]
        if self.environment:
            parts.append(self.environment)
        return "  ·  ".join(parts)


# `pac auth list` prints a table whose columns have moved between releases
# (Index/Active/Kind/Name/User/Cloud/Type on 1.5x). Two anchors are stable
# across all of them and are the only things parsed: the `[N]` index and the
# account's email. Everything else is best-effort decoration, on the same
# principle as `auth_status` before it -- a layout change should cost a column,
# not the ability to switch accounts.
_AUTH_INDEX_RE = re.compile(r"^\s*\[?(\d+)\]?\s*(\*?)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_URL_RE = re.compile(r"https?://\S+")


def parse_auth_list(output: str) -> list[AuthProfile]:
    """Parse `pac auth list` output into profiles.

    Split out from the subprocess call so it can be tested against real
    captured output rather than only against a live, signed-in machine.
    """
    profiles: list[AuthProfile] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        index_match = _AUTH_INDEX_RE.match(line)
        if not index_match:
            continue
        email = _EMAIL_RE.search(line)
        url = _URL_RE.search(line)
        # An asterisk marks the active profile. PAC has printed it both
        # immediately after the index and in its own column, so the whole line
        # is checked rather than one position.
        active = bool(index_match.group(2)) or " * " in line or line.rstrip().endswith("*")
        profiles.append(
            AuthProfile(
                index=int(index_match.group(1)),
                active=active,
                user=email.group(0) if email else "",
                environment=url.group(0) if url else "",
            )
        )
    # Exactly one profile and no marker at all still means that profile is the
    # one in use -- PAC omits the asterisk when there is nothing to choose
    # between.
    if len(profiles) == 1 and not profiles[0].active:
        profiles[0].active = True
    return profiles


def list_auth_profiles() -> list[AuthProfile]:
    """Every Power Platform account currently signed in on this machine."""
    try:
        result = subprocess.run(
            [_pac(), "auth", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_auth_list(result.stdout + result.stderr)


def select_auth_profile(index: int) -> None:
    """Make an already-signed-in profile the active one.

    Switching rather than re-authenticating: a user with two tenants signed in
    should not have to run a device-code flow to move between them.
    """
    result = subprocess.run(
        [_pac(), "auth", "select", "--index", str(index)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout).strip()
        raise PacError(f"pac auth select --index {index} failed:\n{combined[:400]}")


def clear_auth() -> None:
    """Sign out of every Power Platform account on this machine.

    `pac auth clear` deletes the stored profiles; it does not revoke anything
    in Entra. The next `pac` call has to authenticate from scratch, which is
    the point -- it is how somebody moves to an account that is not currently
    signed in.
    """
    result = subprocess.run(
        [_pac(), "auth", "clear"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout).strip()
        raise PacError(f"pac auth clear failed:\n{combined[:400]}")


def delete_auth_profile(index: int) -> None:
    """Sign out of one account, leaving the others alone."""
    result = subprocess.run(
        [_pac(), "auth", "delete", "--index", str(index)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout).strip()
        raise PacError(f"pac auth delete --index {index} failed:\n{combined[:400]}")


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------
#
# One tenant holds many Dataverse environments -- Dev, Test, Prod, a personal
# one per maker -- and an agent lives in exactly one of them. `pac solution
# list` reports the *selected* environment's solutions and says nothing about
# which that is, so a migration run against the wrong one finds no agents, or
# worse, finds last quarter's.


@dataclass
class EnvironmentInfo:
    """One Power Platform environment the signed-in account can reach."""

    index: int
    display_name: str
    environment_id: str = ""
    url: str = ""
    env_type: str = ""
    active: bool = False
    # `pac env list`'s last column: `orgexample01`, `unq2a7f8790...`. Not used
    # for selection -- the URL is better -- but it is what a Dataverse admin
    # recognises, so it is kept rather than discarded.
    unique_name: str = ""

    @property
    def selector(self) -> str:
        """What to hand `pac env select`, which takes an ID, URL, unique name
        or partial name (verified against 1.52.1).

        The URL is the least ambiguous of them: display names repeat across
        tenants, a partial name can match two environments, and an ID is
        unreadable in an error message.
        """
        return self.url or self.environment_id or self.unique_name or self.display_name

    def label(self) -> str:
        parts = [self.display_name or self.environment_id or f"environment {self.index}"]
        if self.env_type:
            parts.append(self.env_type)
        if self.url:
            parts.append(self.url)
        return "  ·  ".join(parts)


_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Environment kinds PAC prints in its own column. Recognised so a type is not
# mistaken for part of a display name, and so an unknown kind is simply left
# out rather than corrupting the name.
_ENV_TYPES = ("Default", "Sandbox", "Production", "Trial", "Developer", "Teams")


# The leading columns of an environment row, both optional. `pac env list` on
# 1.52.1 prints no index at all -- just an Active column holding `*` or
# nothing -- while other commands and other releases bracket an index. Both
# are accepted, and neither is required.
_ENV_LEAD_RE = re.compile(r"^\s*(?:\[(\d+)\]\s*)?(\*?)\s*")


def parse_env_list(output: str) -> list[EnvironmentInfo]:
    """Parse `pac env list` into environments.

    Anchored on the GUID and the URL, because those are the only two fields
    every release prints and they cannot be confused with anything else on the
    line. Everything before them is the display name; everything after is the
    unique name.

    Requiring an index column was the original bug: 1.52.1 prints

        Active Display Name     Environment ID  Environment URL  Unique Name
               copilotstudio    f53c0cc5-...    https://...      unq2a7f...
        *      Contoso Main  258ac4e4-...    https://...      orgexample01

    -- no index anywhere -- so every row failed to match, the list came back
    empty, and the wizard reported that it could not read the environments of a
    tenant that has three.

    Rows with neither a GUID nor a URL are skipped, which is what discards the
    `Connected as ...` preamble and the column header without having to
    recognise either.
    """
    environments: list[EnvironmentInfo] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        guid = _GUID_RE.search(line)
        url = _URL_RE.search(line)
        if guid is None and url is None:
            continue

        lead = _ENV_LEAD_RE.match(line)
        start = lead.end() if lead else 0
        cut = min(m.start() for m in (guid, url) if m is not None)
        name = line[start:cut].strip()
        tail = line[url.end() :].strip() if url is not None else ""

        env_type = ""
        for candidate in _ENV_TYPES:
            if re.search(rf"\b{candidate}\b", tail):
                env_type = candidate
                break
        # A release that prints the type before the ids would otherwise glue it
        # onto the name. The leading space is required so a one-word name that
        # happens to *be* a type word is not truncated to nothing.
        for candidate in _ENV_TYPES:
            if env_type:
                break
            if name.endswith(" " + candidate):
                env_type = candidate
                name = name[: -len(candidate)].strip()

        environments.append(
            EnvironmentInfo(
                index=int(lead.group(1)) if lead and lead.group(1) else len(environments) + 1,
                display_name=name,
                environment_id=guid.group(0) if guid else "",
                url=url.group(0).rstrip(",") if url else "",
                unique_name=tail.split()[0] if tail and env_type != tail else (tail or ""),
                env_type=env_type,
                active=bool(lead and lead.group(2)),
            )
        )
    return environments


def list_environments() -> list[EnvironmentInfo]:
    """Every environment the active account can reach, or [] if PAC can't say.

    Empty rather than an exception: not being able to enumerate environments
    costs the ability to *choose* one, and the migration can still run against
    whichever PAC already has selected. Refusing to migrate because a listing
    call failed would trade a working run for none.
    """
    try:
        result = subprocess.run(
            [_pac(), "env", "list"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_env_list(result.stdout + result.stderr)


def current_environment() -> str:
    """The environment `pac` is pointed at now, as a URL or a name.

    Read back rather than remembered. This is what confirms a switch actually
    happened -- see `select_environment`.
    """
    try:
        result = subprocess.run(
            [_pac(), "env", "who"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    combined = result.stdout + result.stderr
    url = _URL_RE.search(combined)
    if url:
        return url.group(0).rstrip(",")
    for line in combined.splitlines():
        if "environment" in line.lower() and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def select_environment(selector: str) -> None:
    """Point `pac` at one environment for every later call."""
    result = subprocess.run(
        [_pac(), "env", "select", "--environment", selector],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        combined = (result.stderr or result.stdout).strip()
        raise PacError(f"pac env select --environment {selector} failed:\n{combined[:400]}")


def do_device_auth(on_code: Callable[[str], None]) -> str:
    """Run 'pac auth create --deviceCode', call on_code with the browser URL
    + one-time code message when it appears in stdout, then block until the
    user completes auth in their browser.

    Returns the authenticated account name, or "unknown" if it can't be parsed.
    """
    proc = subprocess.Popen(
        [_pac(), "auth", "create", "--deviceCode"],
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
        [_pac(), "copilot", "list"],
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
        [_pac(), "solution", "list"],
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
            _pac(), "solution", "export",
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
    Agent Liftoff's importer expects.

    'pac solution export' produces a packed ZIP — the raw XML is a single
    customizations.xml blob, not the per-bot directory tree. 'pac solution unpack'
    is the mandatory second step that expands it into the Agent Liftoff-readable layout.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            _pac(), "solution", "unpack",
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
      customizations.xml           (connection reference -> connector id map)
      bots/<bot_schema>/           (this bot only)
      botcomponents/<bot_schema>.* (only components belonging to this bot)

    Botcomponents belonging to a bot have schemanames that start with the bot's
    schemaname followed by a dot (e.g. bot "ai_HelperBee" owns components named
    "ai_HelperBee.gpt.default", "ai_HelperBee.topic.ConversationStart", etc.).

    customizations.xml is solution-wide rather than per-bot, but it holds the
    only mapping from a tool's connection reference to the Power Platform
    connector behind it. Leaving it out cost every sliced import its connector
    identity -- the difference between "connector shared_service-now" and an
    unresolvable GUID -- so it comes along whole.
    """
    dest.mkdir(parents=True, exist_ok=True)

    for shared in ("solution.xml", "customizations.xml"):
        src = sol_dir / shared
        if src.exists():
            shutil.copy2(src, dest / shared)

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

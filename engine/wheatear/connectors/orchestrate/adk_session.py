"""Keep the ADK's session alive, so a migration does not die halfway through.

The ADK CLI holds its own short-lived token, separate from the API key. It
expires on its own schedule, and when it does every `orchestrate ... import`
fails with:

    The token found for environment 'X' is missing or expired.
    Use `orchestrate env activate X` to fetch a new one

Which is a fine message for a person typing commands and a terrible one to hit
in the middle of a migration: the tool lookup has already run, the YAML is
already written, and the agents simply do not arrive. Observed three times in
one afternoon, including on a run where every other stage worked.

Wheatear already holds the API key -- the same key it used to read the tool
catalogue over REST -- so there is no reason to ask a person to go and refresh
a session we can refresh ourselves. This module does exactly that and nothing
else: find the environment pointing at this instance (or make one), activate
it with the key we were given, and confirm the token works before anything
depends on it.

What it deliberately does not do is store the key or edit the user's shell.
The key lives in the process it was given to, and the ADK's own environment
file is the ADK's business.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from wheatear.errors import WheatearError

# `orchestrate env list` prints the URL truncated with an ellipsis once it is
# long enough, which every real instance URL is:
#
#   wheatear-migration  https://api.us-south.watson-orchestrate.cloud.i…  (active)
#
# so a full-string comparison never matches and the prefix is all there is.
ELLIPSIS = "…"
_ROW = re.compile(r"^\s*(\S+)\s+(\S+?)\s*(\(active\))?\s*$")


@dataclass(frozen=True)
class AdkEnvironment:
    name: str
    url: str
    active: bool = False

    @property
    def truncated(self) -> bool:
        return self.url.endswith(ELLIPSIS)

    def matches(self, instance_url: str) -> bool:
        """Whether this row refers to `instance_url`.

        Prefix comparison when the row is truncated, which is the normal case.
        Not fuzzy: the visible prefix of a watsonx instance URL already
        includes the region and most of the host, so two different instances in
        one account still differ inside it.
        """
        mine = (instance_url or "").rstrip("/")
        theirs = self.url.rstrip("/")
        if self.truncated:
            return mine.startswith(theirs[: -len(ELLIPSIS)])
        return mine == theirs


def parse_env_list(output: str) -> list[AdkEnvironment]:
    """Parse `orchestrate env list` into environments."""
    environments: list[AdkEnvironment] = []
    for line in output.splitlines():
        if not line.strip() or "://" not in line:
            continue
        match = _ROW.match(line)
        if not match:
            continue
        environments.append(
            AdkEnvironment(
                name=match.group(1), url=match.group(2), active=bool(match.group(3))
            )
        )
    return environments


def _run(orchestrate: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [orchestrate, *args], capture_output=True, text=True, timeout=timeout
    )


def list_environments(orchestrate: str) -> list[AdkEnvironment]:
    try:
        result = _run(orchestrate, "env", "list", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_env_list(result.stdout + result.stderr)


def session_is_live(orchestrate: str) -> bool:
    """Whether the active environment's token still works.

    Asked by making a call that needs one. There is no "is my token valid"
    command, and the expiry is not written anywhere we can read -- so the only
    honest test is to use it.
    """
    try:
        result = _run(orchestrate, "models", "list", timeout=90)
    except (OSError, subprocess.SubprocessError):
        return False
    combined = result.stdout + result.stderr
    return result.returncode == 0 and "missing or expired" not in combined


def ensure_session(
    instance_url: str, api_key: str, orchestrate: str, name_hint: str = "wheatear"
) -> str:
    """Make sure the ADK can talk to `instance_url`, and say which env it used.

    Idempotent and cheap when the session is already good: one `models list`
    that the migration wants the answer to anyway.

    Raises rather than returning a flag. A migration that cannot import is not
    a degraded migration -- it is one that writes files and puts nothing on the
    tenant -- and the caller is better placed to decide what to do about that
    than a boolean is to describe it.
    """
    if not api_key:
        raise WheatearError("No API key to activate the ADK session with.")

    environments = list_environments(orchestrate)
    match = next((e for e in environments if e.matches(instance_url)), None)

    if match is None:
        # Named after the instance so two instances in one account do not
        # collide, and so a person reading `orchestrate env list` later can
        # tell where the environment came from.
        suffix = re.sub(r"[^A-Za-z0-9]+", "-", instance_url.rstrip("/").split("/")[-1])[:12]
        name = f"{name_hint}-{suffix}".strip("-")
        added = _run(orchestrate, "env", "add", "--name", name, "--url", instance_url)
        if added.returncode != 0 and "already exists" not in (added.stdout + added.stderr):
            raise WheatearError(
                f"Could not register an ADK environment for {instance_url}: "
                f"{' '.join((added.stderr or added.stdout).split())[:200]}"
            )
    else:
        name = match.name

    activated = _run(orchestrate, "env", "activate", name, "--api-key", api_key)
    combined = activated.stdout + activated.stderr
    if activated.returncode != 0:
        raise WheatearError(
            f"Could not activate ADK environment '{name}': "
            f"{' '.join(combined.split())[:250]}"
        )
    if not session_is_live(orchestrate):
        raise WheatearError(
            f"ADK environment '{name}' activated but its token still does not work. "
            "Check that the API key belongs to this instance."
        )
    return name

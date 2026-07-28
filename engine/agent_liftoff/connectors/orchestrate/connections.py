"""What the target knows about logging in to the systems a tool talks to.

A migrated tool is only half a migration. `SNOWMCPALL:search_records` arrives
on the agent and then fails at runtime unless something on the target can
authenticate to ServiceNow -- and Agent Liftoff never carries credentials, so that
something has to already exist or be set up by hand.

Orchestrate models this as an *application connection*, and the field that
matters most is `preference`:

  **member** -- each user supplies their own credentials. The agent prompts
  whoever is chatting to sign in, and calls ServiceNow as them. This is what
  you want for a migrated agent whose source platform authenticated per user,
  and it is the only setting under which "the agent asks the user to log in"
  is true.

  **team** -- one shared credential for everybody. Simpler, and wrong whenever
  the source enforced per-user permissions, because every user inherits
  whatever the shared account can see.

`is_configured` says an admin has defined the connection; `credentials_entered`
says somebody has actually put a secret in it. Both can be false in ways that
look fine until the first call, which is why they are read and reported rather
than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from agent_liftoff.errors import RemoteAPIError

APPLICATIONS_PATH = "/v1/orchestrate/connections/applications"


@dataclass(frozen=True)
class AppConnection:
    """One application connection, in one environment."""

    app_id: str
    connection_id: str
    environment: str  # "draft" | "live"
    security_scheme: str | None
    preference: str | None  # "member" | "team"
    server_url: str | None
    is_configured: bool
    credentials_entered: bool
    sso: bool = False

    @property
    def prompts_the_user(self) -> bool:
        """Whether a user chatting to the agent is asked to sign in themselves."""
        return (self.preference or "").lower() == "member"

    @property
    def usable(self) -> bool:
        """Whether a call through this connection could actually authenticate.

        A member-preference connection needs no stored secret -- the user
        supplies one when prompted -- so `credentials_entered` being false is
        normal there and fatal for a team one.
        """
        if not self.is_configured:
            return False
        return self.prompts_the_user or self.credentials_entered

    @property
    def has_endpoint(self) -> bool:
        """Whether the connection knows which server to call.

        Separate from `usable` because they fail differently and a caller
        needs to tell them apart. A connection can be configured, hold
        credentials, and still have no `server_url` -- and a tool bound to it
        then dies at runtime with `Caught error during ServiceNow client
        initialization: CredentialKeys.BASE_URL`, which names neither the
        connection nor the missing field in terms anybody can act on.
        """
        return bool((self.server_url or "").strip())

    @property
    def ready(self) -> bool:
        """Whether a tool bound to this connection would actually work.

        `usable` answers "can this authenticate"; this also asks "does it know
        where to". Reporting the first as if it were the second is how a
        migration says "every migrated tool already has a working connection"
        over a connection pointed at nothing.
        """
        return self.usable and self.has_endpoint

    def summary(self) -> str:
        if not self.is_configured:
            return f"`{self.app_id}` ({self.environment}) is not configured."
        if not self.has_endpoint:
            return (
                f"`{self.app_id}` ({self.environment}) has no server URL, so a tool using "
                "it cannot reach anything."
            )
        who = "each user signs in themselves" if self.prompts_the_user else "one shared credential"
        state = (
            "no credential stored yet"
            if not self.credentials_entered and not self.prompts_the_user
            else "ready"
        )
        return (
            f"`{self.app_id}` ({self.environment}, {self.security_scheme or 'unknown scheme'}): "
            f"{who}, {state}"
            + (f", {self.server_url}" if self.server_url else "")
        )


def _row(record: dict[str, Any]) -> AppConnection | None:
    app_id = record.get("app_id")
    if not app_id:
        return None
    return AppConnection(
        app_id=str(app_id),
        connection_id=str(record.get("connection_id") or ""),
        environment=str(record.get("environment") or ""),
        security_scheme=record.get("security_scheme"),
        preference=record.get("preference"),
        server_url=record.get("server_url"),
        is_configured=bool(record.get("is_configured")),
        credentials_entered=bool(record.get("credentials_entered")),
        sso=bool(record.get("sso")),
    )


def list_applications(instance_url: str, token: str) -> list[AppConnection]:
    """Every application connection on the instance, both environments."""
    url = instance_url.rstrip("/") + APPLICATIONS_PATH
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=(10, 60),
        )
    except requests.RequestException as exc:
        raise RemoteAPIError(f"Could not read connections from {url}: {exc}") from exc
    if response.status_code >= 400:
        raise RemoteAPIError(
            f"Orchestrate returned {response.status_code} for connections: "
            f"{' '.join(response.text.split())[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteAPIError("Orchestrate returned a non-JSON connections body.") from exc

    records = payload.get("applications") if isinstance(payload, dict) else payload
    return [c for c in (_row(r) for r in (records or [])) if c is not None]


@dataclass(frozen=True)
class Binding:
    """What a tool record itself says about how it authenticates.

    The authoritative answer, and the only one worth reporting as fact. A tool
    whose binding names no connection is not "probably using the
    similarly-named one" -- it is using whatever was configured inside the
    remote server, which Orchestrate cannot see and neither can we.
    """

    app_ids: tuple[str, ...]
    server_url: str | None
    # The tenant-local ids of those connections. Distinct from `app_ids` and
    # never interchangeable with them: an app id (`servicenow_ibm_a1b2c3d4`) is
    # what a tool, a config call and a person all refer to; a connection id is
    # a row identifier that means nothing outside this tenant.
    connection_ids: tuple[str, ...] = ()

    @property
    def declared(self) -> bool:
        return bool(self.app_ids)


def bound_connection(tool_record: dict[str, Any]) -> Binding:
    """Read a tool's own declared connections out of its record.

    Written because the alternative was guessing, and guessing was wrong. On a
    real tenant `SNOWMCPALL:get_record` shares every token with
    `servicenow_oauth2_auth_code_...`, and its binding names no connection at
    all -- the MCP server behind it holds its own credentials. Reporting that
    name match as fact told the reader the agent would prompt users to sign
    in, when nothing would prompt anybody.
    """
    binding = tool_record.get("binding") if isinstance(tool_record.get("binding"), dict) else {}
    app_ids: list[str] = []
    connection_ids: list[str] = []
    server_url = None
    for kind in binding.values():
        if not isinstance(kind, dict):
            continue
        server_url = server_url or kind.get("server_url")
        declared = kind.get("connections")
        if isinstance(declared, dict):
            # `{"servicenow_ibm_a1b2c3d4": "44444444-..."}` -- app id to
            # connection id. Reading the values here was a real bug: it handed
            # callers a UUID where an app id was expected, and the one that
            # creates connections duly created a connection *named* after a
            # UUID, which then failed to configure.
            app_ids.extend(str(k) for k in declared if k)
            connection_ids.extend(str(v) for v in declared.values() if v)
        elif isinstance(declared, list):
            app_ids.extend(str(v) for v in declared if v)
        elif isinstance(declared, str) and declared:
            app_ids.append(declared)
    value = tool_record.get("app_id")
    if value:
        app_ids.append(str(value))
    value = tool_record.get("connection_id")
    if value:
        connection_ids.append(str(value))
    return Binding(
        app_ids=tuple(dict.fromkeys(app_ids)),
        server_url=server_url,
        connection_ids=tuple(dict.fromkeys(connection_ids)),
    )


def matching(connections: list[AppConnection], hint: str) -> list[AppConnection]:
    """Connections that plausibly serve a system named in `hint`.

    Matched on the app id containing a word from the hint rather than anything
    cleverer: a tool called `SNOWMCPALL:get_record` and a connection called
    `Contoso_SNOW_PDI` share `snow`, and that is the whole signal available
    without asking a human which system a tool talks to.
    """
    words = {w for w in _words(hint) if len(w) > 2}
    if not words:
        return []
    scored = []
    for connection in connections:
        hay = _words(f"{connection.app_id} {connection.server_url or ''}")
        overlap = len(words & hay)
        if overlap:
            scored.append((overlap, connection))
    scored.sort(key=lambda row: (row[0], row[1].usable), reverse=True)
    return [connection for _, connection in scored]


def _words(text: str) -> set[str]:
    out: set[str] = set()
    current = ""
    for character in (text or "").lower():
        if character.isalnum():
            current += character
        else:
            if current:
                out.add(current)
            current = ""
    if current:
        out.add(current)
    # `servicenow` and `snow` are the same system under two names; without this
    # a ServiceNow tool matches no ServiceNow connection at all.
    if "servicenow" in out:
        out.add("snow")
    if "snowmcpall" in out or "snowmcp" in out:
        out.update({"snow", "servicenow"})
    return out

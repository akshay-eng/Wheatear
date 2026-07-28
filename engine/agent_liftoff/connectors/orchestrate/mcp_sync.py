"""Decide what to do about a source MCP server, given what the target has.

Three answers, and the first one is the point of the whole module:

  **reuse** -- the target already has a toolkit pointed at this same server.
  Nothing is created, nothing is reconfigured, nothing is overwritten. The
  agent is bound to the tools that toolkit already exposes. A migration that
  re-added the server would leave the tenant with two toolkits for one MCP
  endpoint and no way to tell which an agent is using.

  **create** -- the target has nothing at this URL, and the source told us
  enough to add it: a URL and a transport the platform speaks.

  **conflict** -- something is off in a way a person has to look at. A target
  toolkit with the same *name* but a different URL is the dangerous one:
  pointing it somewhere new would silently change what every existing agent
  using it calls.

Credentials are never part of any of these. A solution export carries
connection *references*, not connections; the secret stays in the source
environment. `needs_credentials` says so out loud rather than letting someone
discover it when the first call 401s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from agent_liftoff.connectors.copilot_studio.mcp_scan import McpServer

Action = str  # "reuse" | "create" | "conflict"


def normalise(url: str | None) -> str:
    """A URL reduced to what identifies the *server* rather than the route.

    Host and port only. An MCP endpoint is written `http://host:8000/sse` by
    one tool and `http://host:8000/` by another, and they are the same server;
    comparing raw strings would call that a conflict and add a duplicate
    toolkit.

    The scheme is deliberately *not* part of the identity, but it is not
    ignored either -- see `scheme_of`. `https://host:8000` and
    `http://host:8000` are the same machine reached two ways, and which one is
    right is a question for a person, not something to decide by string
    comparison in either direction.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip().rstrip("/"))
    return (parts.netloc or parts.path).lower().rstrip("/")


def scheme_of(url: str | None) -> str:
    return urlsplit((url or "").strip()).scheme.lower()


@dataclass
class ToolkitView:
    """The part of a target toolkit this decision depends on."""

    name: str
    url: str | None
    transport: str | None
    tools: list[str] = field(default_factory=list)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ToolkitView:
        mcp = record.get("mcp") if isinstance(record.get("mcp"), dict) else {}
        return cls(
            name=str(record.get("name") or ""),
            url=mcp.get("server_url"),
            transport=mcp.get("transport"),
            tools=[str(t) for t in (record.get("tools") or [])],
        )


@dataclass
class McpPlan:
    """What to do about one source MCP server."""

    server: McpServer
    action: Action
    toolkit: ToolkitView | None = None
    reason: str = ""
    # Credentials never migrate; this is always True for a server that has to
    # be created, and is stated rather than assumed.
    needs_credentials: bool = False

    def command(self, tools: str = "*") -> list[str] | None:
        """The `orchestrate toolkits add` invocation, for a `create` plan.

        Returned rather than run: adding a toolkit changes the tenant for every
        agent on it, and that is a decision the caller makes explicitly.
        """
        if self.action != "create" or not self.server.pointable:
            return None
        return [
            "toolkits",
            "add",
            "--kind",
            "mcp",
            "--name",
            self.server.name,
            "--description",
            f"MCP server migrated from the source solution ({self.server.protocol}).",
            "--url",
            str(self.server.url),
            "--transport",
            str(self.server.transport),
            "--tools",
            tools,
        ]


def plan_servers(servers: list[McpServer], toolkits: list[dict[str, Any]]) -> list[McpPlan]:
    """Decide reuse / create / conflict for each source MCP server."""
    views = [ToolkitView.from_record(record) for record in toolkits or []]
    by_url = {normalise(view.url): view for view in views if view.url}
    by_name = {view.name.lower(): view for view in views if view.name}

    plans: list[McpPlan] = []
    for server in servers:
        key = normalise(server.url)
        existing = by_url.get(key) if key else None
        if existing is not None and scheme_of(server.url) != scheme_of(existing.url):
            plans.append(
                McpPlan(
                    server=server,
                    action="conflict",
                    toolkit=existing,
                    reason=(
                        f"`{existing.name}` on the target is pointed at {existing.url} and "
                        f"this solution's server is {server.url} -- the same host reached over "
                        "a different scheme. Which is correct is a question for a person; "
                        "nothing was created or changed."
                    ),
                )
            )
            continue
        if existing is not None:
            plans.append(
                McpPlan(
                    server=server,
                    action="reuse",
                    toolkit=existing,
                    reason=(
                        f"`{existing.name}` on the target is already pointed at {existing.url}, "
                        f"the same server this solution used. It exposes {len(existing.tools)} "
                        "tool(s); nothing was created or reconfigured."
                    ),
                )
            )
            continue

        clash = by_name.get(server.name.lower())
        if clash is not None:
            plans.append(
                McpPlan(
                    server=server,
                    action="conflict",
                    toolkit=clash,
                    reason=(
                        f"The target has a toolkit called `{clash.name}` pointed at "
                        f"{clash.url}, but this solution's server is {server.url}. Repointing "
                        "it would change what every agent already using it calls, so nothing "
                        "was touched."
                    ),
                )
            )
            continue

        if not server.pointable:
            plans.append(
                McpPlan(
                    server=server,
                    action="conflict",
                    reason=(
                        f"`{server.name}` declares {server.protocol}"
                        + (
                            ", which this target has no transport for."
                            if server.url
                            else " but the export records no URL for it."
                        )
                        + " It has to be added by hand."
                    ),
                )
            )
            continue

        plans.append(
            McpPlan(
                server=server,
                action="create",
                reason=(
                    f"No toolkit on the target is pointed at {server.url}. It can be added "
                    f"as it stands over {server.transport}."
                ),
                needs_credentials=True,
            )
        )
    return plans

"""Find the MCP servers a Copilot Studio solution talks to.

Copilot Studio does not have an "MCP server" component. It reaches one the way
it reaches everything else -- through a Power Platform **custom connector** --
and the only thing that distinguishes such a connector from any other is an
extension in its OpenAPI document:

    x-ms-agentic-protocol: mcp-streamable-1.0

That marker is Microsoft's, not ours, and it is the whole detection. A
connector without it is an ordinary REST connector whose operations happen to
be callable; a connector with it is an MCP server and the agent's tools are
that server's tools.

The distinction matters because the two migrate completely differently. An MCP
server can be *pointed at* from the target: same URL, same protocol, same tool
names, nothing reimplemented. An ordinary connector cannot -- the target has no
way to speak Power Platform's connector protocol, so its operations have to be
matched against tools the target already has (see `pipeline/resolve.py`).

What this cannot recover, and does not pretend to: credentials. A solution
export contains connection *references*, which are placeholders by design --
`connectionreferencelogicalname` and nothing else. The secret lives in the
Power Platform environment and never ships. Whoever finishes the migration
supplies it on the target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Microsoft's marker for "this connector is an MCP server". The value carries
# the protocol revision (`mcp-streamable-1.0` today); it is matched on the key
# so a new revision is still recognised as MCP rather than silently ignored.
AGENTIC_PROTOCOL = "x-ms-agentic-protocol"

# Where a custom connector's OpenAPI lands in a solution export. Matched
# case-insensitively on the file name because the casing of the containing
# directory varies between export tooling versions.
DEFINITION_NAMES = ("apidefinition.swagger.json", "apidefinition.json")

# Transports Orchestrate can be pointed at. `mcp-streamable-1.0` is the HTTP
# streaming transport; anything else is reported rather than guessed at,
# because pointing a target at a transport it cannot speak fails at call time
# rather than at import time, which is far worse.
TRANSPORT_FOR_PROTOCOL = {
    "mcp-streamable-1.0": "streamable_http",
    "mcp-sse-1.0": "sse",
}


@dataclass
class McpServer:
    """One MCP server a source solution talks to."""

    name: str
    url: str | None
    protocol: str
    transport: str | None
    # Operation ids declared in the connector document. Not necessarily the
    # tools the server exposes -- an MCP server advertises those at runtime --
    # but it is what the source solution knew about, and it is what a reviewer
    # needs to check the target's toolkit against.
    operations: list[str] = field(default_factory=list)
    connection_references: list[str] = field(default_factory=list)
    source_path: str = ""

    @property
    def pointable(self) -> bool:
        """Whether a target could be pointed at this server as it stands."""
        return bool(self.url) and self.transport is not None


def _load(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None


def _find_protocol(node: Any) -> str | None:
    """The agentic-protocol value anywhere in a connector document.

    Searched rather than read from a fixed path because Microsoft places the
    extension on the operation, and which operation varies by connector. A
    document either declares the protocol somewhere or it is not MCP.
    """
    if isinstance(node, dict):
        value = node.get(AGENTIC_PROTOCOL)
        if isinstance(value, str) and value:
            return value
        for child in node.values():
            found = _find_protocol(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_protocol(child)
            if found:
                return found
    return None


def _server_url(document: dict) -> str | None:
    """The server's URL, from whichever OpenAPI dialect the document uses."""
    host = document.get("host")
    if host:
        schemes = document.get("schemes") or ["https"]
        base = str(document.get("basePath") or "").rstrip("/")
        return f"{schemes[0]}://{host}{base}"
    servers = document.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = servers[0].get("url")
        return str(url) if url else None
    return None


def _operations(document: dict) -> list[str]:
    found: list[str] = []
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return found
    for methods in paths.values():
        if not isinstance(methods, dict):
            continue
        for operation in methods.values():
            if isinstance(operation, dict) and operation.get("operationId"):
                found.append(str(operation["operationId"]))
    return sorted(set(found))


def find_mcp_servers(root: Path) -> list[McpServer]:
    """Every MCP server declared by a connector in this solution export.

    Empty is the common and correct answer: most Copilot solutions reach their
    systems through Microsoft-published connectors, which are not MCP and
    cannot be pointed at from anywhere else.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    servers: list[McpServer] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.lower() not in DEFINITION_NAMES:
            continue
        document = _load(path)
        if not isinstance(document, dict):
            continue
        protocol = _find_protocol(document)
        if not protocol:
            continue
        info = document.get("info") if isinstance(document.get("info"), dict) else {}
        servers.append(
            McpServer(
                name=str(info.get("title") or path.parent.name),
                url=_server_url(document),
                protocol=protocol,
                transport=TRANSPORT_FOR_PROTOCOL.get(protocol),
                operations=_operations(document),
                source_path=str(path.relative_to(root)),
            )
        )
    return servers

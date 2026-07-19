"""Platform-neutral types shared by every importer and the Map stage.

`ImportResult` is Normalize's output regardless of which platform produced it:
the IR Agent plus the raw references Map resolves into tools/knowledge/
connections. It lives here (not under any one platform package) so the Map
stage and the Orchestrate importer don't have to reach into the Copilot
package for it -- a hub type belongs at the hub.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wheatear.ir.schema import Agent


@dataclass
class RawKnowledgeRef:
    """A knowledge source reference as Normalize found it, with whatever
    structured metadata the source format provided. Map is the one place that
    decides review_required/notes from this -- importers don't make that call.
    """

    name: str
    source_kind: str | None = None
    detail: str | None = None


@dataclass
class RawToolRef:
    """A tool/toolkit reference with the structured metadata an importer could
    recover -- notably an MCP server URL. Richer than a bare name string so Map
    can resolve a portable MCP tool cleanly instead of flagging it for a manual
    rebuild. Importers that only have a name can still use `raw_tool_refs`
    (list[str]); Map handles both.
    """

    name: str
    kind: str = "unknown"  # e.g. "mcp"
    mcp_server_url: str | None = None
    transport: str | None = None
    source_ref: str | None = None
    # Individual tool/operation names this toolkit exposes (e.g. an MCP server's
    # SNOWMCP:create_incident, SNOWMCP:add_comment, ...). Preserved so the
    # migration documents exactly which tools the server provides.
    tool_names: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    """Normalize's output: the IR Agent plus the raw references Map needs to
    resolve into tools/knowledge/connections.
    """

    agent: Agent
    raw_tool_refs: list[str] = field(default_factory=list)
    # Richer tool refs (with MCP URLs etc.); preferred by Map when present.
    raw_tools: list[RawToolRef] = field(default_factory=list)
    raw_knowledge_refs: list[RawKnowledgeRef] = field(default_factory=list)
    raw_connection_refs: list[str] = field(default_factory=list)
    # Process-level notes from Normalize itself (e.g. "skipped an unrecognized
    # component type") -- distinct from Topic.unsupported_notes, which are
    # about agent *content* the parser couldn't model.
    import_notes: list[str] = field(default_factory=list)

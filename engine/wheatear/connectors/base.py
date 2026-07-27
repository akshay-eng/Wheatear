"""Platform-neutral types shared by every importer and the Map stage.

`ImportResult` is Normalize's output regardless of which platform produced it:
the IR Agent plus the raw references Map resolves into tools/knowledge/
connections. It lives here (not under any one platform package) so the Map
stage and the Orchestrate importer don't have to reach into the Copilot
package for it -- a hub type belongs at the hub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    # For file-backed knowledge (a Copilot componenttype-14 component), the
    # extracted document itself. Orchestrate knowledge is ingest-backed rather
    # than reference-backed, so having the actual bytes on disk is the
    # difference between a migration and a TODO -- it's what lets the exporter
    # stage a real upload instead of naming a file nobody can find.
    file_path: Path | None = None
    # True when the source attached files directly to the agent (e.g. an n8n
    # readWriteFile->extractFromFile chain, or a Copilot file upload) rather
    # than pointing at an external searchable source. The export never carries
    # the file bytes, so Map routes these to IngestPlan.UPLOAD and flags them
    # for the human to re-supply the actual files.
    is_file_upload: bool = False


@dataclass
class ToolParam:
    """One input or output of a source tool, with the natural-language
    description the source platform showed its own model.

    That description is the highest-signal field for resolving the tool onto a
    target platform: names collide across vendors ("GetRecord" means nothing on
    its own) but "Gets a single ServiceNow record by its sys_id" is matchable.
    """

    name: str
    description: str | None = None
    type: str | None = None


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

    # ---- Signature ----------------------------------------------------
    # What the tool does and what it takes/returns, as the source platform
    # described it to its own model. Populated for Copilot connector actions
    # (componenttype 9 / kind: TaskDialog). This is the surface a capability
    # matcher compares against the target catalog, so it's kept verbatim
    # rather than summarized.
    description: str | None = None
    operation_id: str | None = None
    inputs: list[ToolParam] = field(default_factory=list)
    outputs: list[ToolParam] = field(default_factory=list)

    # ---- Provenance ---------------------------------------------------
    # Power Platform connector identity, e.g.
    #   "/providers/Microsoft.PowerApps/apis/shared_service-now"
    # Resolving this is what tells us whether the tool is an MCP server behind
    # a connector shim, a prebuilt connector, or a custom one -- which in turn
    # picks the BridgeStrategy. `connection_reference` is the per-agent binding
    # that points at it (the credential lives there, never here).
    connector_id: str | None = None
    connection_reference: str | None = None


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

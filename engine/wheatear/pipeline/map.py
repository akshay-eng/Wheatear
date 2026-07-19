"""Deterministic Map stage: raw references -> IR tools/knowledge/connections.

No LLM call happens in this stage, by design (anything touching schemas or
credentials stays mechanical and auditable). Anything without a confident,
explicit mapping is flagged review_required rather than guessed at.

Map is target-aware: resolving a reference onto Orchestrate is a different
problem from resolving it onto Copilot Studio, so the resolver is chosen by
the destination platform. This is the one place directionality lives -- the
importers and exporters stay direction-agnostic.
"""

from __future__ import annotations

from wheatear.connectors.base import ImportResult, RawToolRef
from wheatear.errors import MapError
from wheatear.ir.schema import Agent, BridgeStrategy, ConnectionRef, IngestPlan, KnowledgeRef, ToolKind, ToolRef

# Known source-connector -> target-tool mappings. Intentionally near-empty for
# v1: most connectors are org-specific with no universal equivalent. Real
# mappings get added (per corridor) as they're validated against real exports.
KNOWN_TOOL_MAPPINGS: dict[str, str] = {}


def map_agent(import_result: ImportResult, target_platform: str = "orchestrate") -> Agent:
    """Populate tools/knowledge/connections on the IR Agent from the raw
    references Normalize extracted, resolved for `target_platform`. Returns the
    same Agent object, mutated.
    """
    if target_platform == "orchestrate":
        return _map_to_orchestrate(import_result)
    if target_platform == "copilot-studio":
        return _map_to_copilot(import_result)
    raise MapError(f"No Map resolver for target platform '{target_platform}'.")


def _mcp_tool(raw: RawToolRef) -> ToolRef:
    """An MCP toolkit with a server URL is the one tool type both platforms
    consume natively -- migrate it cleanly (re-point the endpoint) instead of
    flagging a manual rebuild. Still surfaced (review_required) only if the URL
    is missing, since then there's nothing to re-point.
    """
    if raw.mcp_server_url:
        return ToolRef(
            ref=raw.name,
            source_ref=raw.source_ref or raw.name,
            kind=ToolKind.MCP,
            bridge=BridgeStrategy.NATIVE_MCP,
            confidence=1.0,
            review_required=False,
            mcp_server_url=raw.mcp_server_url,
            transport=raw.transport,
            member_tools=raw.tool_names,
            notes=(
                f"MCP server re-pointed to {raw.mcp_server_url}"
                f"{f' ({raw.transport})' if raw.transport else ''}; "
                "ensure the endpoint is reachable from the target platform."
            ),
        )
    return ToolRef(
        ref=raw.name,
        source_ref=raw.source_ref or raw.name,
        kind=ToolKind.MCP,
        confidence=0.0,
        review_required=True,
        notes=f"MCP toolkit '{raw.name}' has no server URL in the export; provide the endpoint before import.",
    )


def _map_to_orchestrate(import_result: ImportResult) -> Agent:
    agent = import_result.agent

    for raw in import_result.raw_tools:
        if raw.kind == "mcp":
            agent.tools.append(_mcp_tool(raw))
        else:
            agent.tools.append(
                ToolRef(
                    ref=raw.name,
                    source_ref=raw.source_ref or raw.name,
                    confidence=0.0,
                    review_required=True,
                    notes=f"Non-MCP tool '{raw.name}' needs an Orchestrate tool (MCP/OpenAPI) before import.",
                )
            )

    for raw_ref in import_result.raw_tool_refs:
        mapped = KNOWN_TOOL_MAPPINGS.get(raw_ref)
        if mapped:
            agent.tools.append(ToolRef(ref=mapped, source_ref=raw_ref, confidence=1.0))
        else:
            agent.tools.append(
                ToolRef(
                    ref=raw_ref,
                    source_ref=raw_ref,
                    confidence=0.0,
                    review_required=True,
                    notes=(
                        f"No known Orchestrate equivalent for connector '{raw_ref}'; "
                        "implement the tool (MCP server or OpenAPI import) and update this "
                        "reference before import."
                    ),
                )
            )

    for raw_knowledge in import_result.raw_knowledge_refs:
        if raw_knowledge.source_kind:
            # An external connector-backed source (e.g. SharePoint search) --
            # real content that needs re-ingestion into an Orchestrate
            # knowledge base, not a reference Wheatear can just copy over.
            detail = f" ('{raw_knowledge.detail}')" if raw_knowledge.detail else ""
            agent.knowledge.append(
                KnowledgeRef(
                    ref=raw_knowledge.name,
                    source_ref=raw_knowledge.name,
                    review_required=True,
                    ingest_plan=IngestPlan.REINDEX_VECTOR,
                    notes=(
                        f"{raw_knowledge.source_kind} source{detail} needs re-indexing into an "
                        "Orchestrate knowledge base (e.g. Milvus/Elasticsearch); this is not a "
                        "reference copy."
                    ),
                )
            )
        else:
            agent.knowledge.append(KnowledgeRef(ref=raw_knowledge.name, source_ref=raw_knowledge.name))

    for raw_ref in import_result.raw_connection_refs:
        agent.connections.append(
            ConnectionRef(ref=raw_ref, source_ref=raw_ref, auth_type="unknown", review_required=True)
        )

    return agent


def _map_to_copilot(import_result: ImportResult) -> Agent:
    """Resolve references onto Copilot Studio. Copilot has a huge prebuilt
    connector catalog but no automatic way to reconstruct an arbitrary
    Orchestrate MCP tool or vector-DB knowledge base, so these become
    best-effort stubs flagged for a human to wire up in Copilot Studio.
    """
    agent = import_result.agent

    # Copilot Studio also consumes MCP tools natively, so an MCP toolkit with a
    # URL migrates cleanly here too; everything else is a manual rebuild.
    for raw in import_result.raw_tools:
        if raw.kind == "mcp" and raw.mcp_server_url:
            agent.tools.append(_mcp_tool(raw))
        else:
            agent.tools.append(
                ToolRef(
                    ref=raw.name,
                    source_ref=raw.source_ref or raw.name,
                    confidence=0.0,
                    review_required=True,
                    notes=(
                        f"No automatic Copilot Studio equivalent for tool '{raw.name}'; recreate it as a "
                        "connector, custom connector, or MCP tool before publishing."
                    ),
                )
            )

    for raw_ref in import_result.raw_tool_refs:
        agent.tools.append(
            ToolRef(
                ref=raw_ref,
                source_ref=raw_ref,
                confidence=0.0,
                review_required=True,
                notes=(
                    f"No automatic Copilot Studio equivalent for tool '{raw_ref}'; recreate it as a "
                    "connector, custom connector, or MCP tool before publishing."
                ),
            )
        )

    for raw_knowledge in import_result.raw_knowledge_refs:
        agent.knowledge.append(
            KnowledgeRef(
                ref=raw_knowledge.name,
                source_ref=raw_knowledge.name,
                review_required=True,
                ingest_plan=IngestPlan.UNSUPPORTED,
                notes=(
                    f"Knowledge base '{raw_knowledge.name}' needs reconnecting to a real Copilot "
                    "Studio knowledge source (SharePoint, Dataverse, file upload, etc.)."
                ),
            )
        )

    for raw_ref in import_result.raw_connection_refs:
        agent.connections.append(
            ConnectionRef(ref=raw_ref, source_ref=raw_ref, auth_type="unknown", review_required=True)
        )

    return agent

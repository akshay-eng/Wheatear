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
from wheatear.ir.schema import (
    Agent,
    BridgeStrategy,
    ConnectionRef,
    IngestPlan,
    KnowledgeRef,
    ToolKind,
    ToolParameter,
    ToolRef,
)

# Known source-connector -> target-tool mappings. Intentionally near-empty for
# v1: most connectors are org-specific with no universal equivalent. Real
# mappings get added (per corridor) as they're validated against real exports.
KNOWN_TOOL_MAPPINGS: dict[str, str] = {}

# A custom connector's Power Platform id carries the publisher prefix with the
# underscore percent-encoded, e.g. ".../apis/shared_cr3ea-5fservice-20now-...".
# Microsoft's prebuilt connectors have no publisher prefix
# (".../apis/shared_service-now"). The distinction matters because a custom
# connector's OpenAPI definition is downloadable from the source tenant, while
# a prebuilt one has to come from the connector catalog.
_CUSTOM_CONNECTOR_MARKER = "-5f"


def _params(raw_params) -> list[ToolParameter]:
    return [
        ToolParameter(name=p.name, description=p.description, type=p.type) for p in raw_params
    ]


def _connector_kind(connector_id: str | None) -> ToolKind:
    if not connector_id:
        return ToolKind.UNKNOWN
    tail = connector_id.rsplit("/", 1)[-1]
    return ToolKind.CUSTOM_CONNECTOR if _CUSTOM_CONNECTOR_MARKER in tail else ToolKind.CONNECTOR


def _connector_tool(raw: RawToolRef) -> ToolRef:
    """A Power Platform connector operation bound to the source agent.

    Both prebuilt and custom connectors are OpenAPI underneath, so the bridge
    is a spec conversion rather than a hand-rebuild -- but the spec still has
    to be fetched and the tool created on the target, so this stays
    review_required until that actually happens. The full signature travels
    with it so whoever (or whatever) resolves it next doesn't need the
    original export.
    """
    kind = _connector_kind(raw.connector_id)
    connector_name = (raw.connector_id or "").rsplit("/", 1)[-1] or "unknown connector"
    operation = raw.operation_id or raw.name

    if kind == ToolKind.CUSTOM_CONNECTOR:
        detail = (
            f"Custom connector '{connector_name}' operation '{operation}'. Its OpenAPI definition "
            "can be exported from the source tenant (pac connector download) and imported as an "
            "Orchestrate OpenAPI tool."
        )
    else:
        detail = (
            f"Prebuilt Power Platform connector '{connector_name}' operation '{operation}'. Needs "
            "an equivalent Orchestrate tool -- either an existing toolkit that covers the same "
            "operation, or an OpenAPI/MCP tool built against the same backend."
        )

    return ToolRef(
        ref=raw.name,
        source_ref=raw.source_ref or raw.name,
        kind=kind,
        bridge=BridgeStrategy.OPENAPI,
        confidence=0.0,
        review_required=True,
        notes=detail,
        description=raw.description,
        operation_id=raw.operation_id,
        connector_id=raw.connector_id,
        inputs=_params(raw.inputs),
        outputs=_params(raw.outputs),
    )


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
        elif raw.kind == "connector":
            agent.tools.append(_connector_tool(raw))
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
        if raw_knowledge.file_path is not None:
            # The document itself came across in the export, so this is a real
            # upload rather than a re-ingestion project. Still flagged for
            # review: Orchestrate enforces a size cap and the human picks which
            # knowledge base receives it.
            agent.knowledge.append(
                KnowledgeRef(
                    ref=raw_knowledge.name,
                    source_ref=raw_knowledge.name,
                    review_required=True,
                    ingest_plan=IngestPlan.UPLOAD,
                    file_path=str(raw_knowledge.file_path),
                    notes=(
                        f"File '{raw_knowledge.file_path.name}' shipped with the source export; "
                        "upload it into an Orchestrate knowledge base (mind the 30MB cap)."
                    ),
                )
            )
        elif raw_knowledge.source_kind:
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

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

from collections.abc import Callable
from typing import Any

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

# Known source-connector -> target-tool mappings. A hand-curated override table
# checked ahead of the catalog resolver, so a validated corridor can pin a
# specific target the fuzzy matcher would get wrong. Still near-empty by
# default; the catalog resolver (below) does the general resolution.
KNOWN_TOOL_MAPPINGS: dict[str, str] = {}

# A connector resolver maps (app_name, description) -> a catalog match object
# (or None). The object is duck-typed: it must expose .install_ref, .name,
# .confidence (0-1), and .member_tools (iterable). See
# connectors/orchestrate/catalog.py:connector_resolver. Kept as an injected
# callable so this module stays decoupled from any one target's catalog and so
# the default (None) preserves the pre-catalog behavior exactly.
ConnectorResolver = Callable[[str, str | None], Any]

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


# At/above this catalog confidence AND a single-tool match, the pick is trusted
# (not flagged for review). A multi-tool app match always needs human tool
# selection, so it's flagged regardless.
_TRUST_CONFIDENCE = 0.9


def map_agent(
    import_result: ImportResult,
    target_platform: str = "orchestrate",
    *,
    connector_resolver: ConnectorResolver | None = None,
) -> Agent:
    """Populate tools/knowledge/connections on the IR Agent from the raw
    references Normalize extracted, resolved for `target_platform`. Returns the
    same Agent object, mutated.

    `connector_resolver` (optional) resolves a source built-in connector to a
    target catalog tool. When None (the default), behavior is exactly the
    pre-catalog manual-flag path -- so existing call sites are unaffected.
    """
    if target_platform == "orchestrate":
        return _map_to_orchestrate(import_result, connector_resolver)
    if target_platform == "copilot-studio":
        return _map_to_copilot(import_result)
    raise MapError(f"No Map resolver for target platform '{target_platform}'.")


def _resolve_connector(
    ref: str,
    description: str | None,
    connector_resolver: ConnectorResolver | None,
) -> ToolRef | None:
    """Try the curated override table, then the injected catalog resolver.
    Returns a resolved ToolRef, or None to fall back to the manual-flag path.
    """
    mapped = KNOWN_TOOL_MAPPINGS.get(ref)
    if mapped:
        return ToolRef(ref=mapped, source_ref=ref, confidence=1.0)
    if connector_resolver is None:
        return None
    match = connector_resolver(ref, description)
    if match is None:
        return None
    members = list(getattr(match, "member_tools", ()) or ())
    needs_review = match.confidence < _TRUST_CONFIDENCE or len(members) > 1
    detail = f" ({len(members)} tools in this toolkit; confirm which to install)" if len(members) > 1 else ""
    return ToolRef(
        ref=match.install_ref,
        source_ref=ref,
        kind=ToolKind.CONNECTOR,
        bridge=BridgeStrategy.MCP_CATALOG,
        confidence=match.confidence,
        review_required=needs_review,
        member_tools=members,
        notes=(
            f"Resolved connector '{ref}' to Orchestrate catalog '{match.name}' "
            f"({match.install_ref}){detail}."
        ),
    )


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


def _map_to_orchestrate(
    import_result: ImportResult,
    connector_resolver: ConnectorResolver | None = None,
) -> Agent:
    agent = import_result.agent

    for raw in import_result.raw_tools:
        if raw.kind == "mcp":
            agent.tools.append(_mcp_tool(raw))
            continue
        # The curated resolver first: a validated corridor can pin a specific
        # target the fuzzy matcher would get wrong. When it has no answer, a
        # Power Platform connector still carries its full signature forward so
        # the Resolve stage has something to match on.
        resolved = _resolve_connector(raw.name, None, connector_resolver)
        if resolved is not None:
            agent.tools.append(resolved)
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
        resolved = _resolve_connector(raw_ref, None, connector_resolver)
        if resolved is not None:
            agent.tools.append(resolved)
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
        if raw_knowledge.file_path is not None or raw_knowledge.is_file_upload:
            # Two shapes reach here. A Copilot componenttype-14 component ships
            # the document itself, so `file_path` points at real bytes and the
            # upload is something Wheatear can stage. An n8n
            # readWriteFile->extractFromFile chain carries only a path, so the
            # human re-supplies the files. Both are flagged for review either
            # way: Orchestrate enforces a size cap and somebody picks which
            # knowledge base receives them.
            detail = f" (source path: {raw_knowledge.detail})" if raw_knowledge.detail else ""
            agent.knowledge.append(
                KnowledgeRef(
                    ref=raw_knowledge.name,
                    source_ref=raw_knowledge.name,
                    review_required=True,
                    ingest_plan=IngestPlan.UPLOAD,
                    file_path=str(raw_knowledge.file_path) if raw_knowledge.file_path else None,
                    notes=(
                        f"File '{raw_knowledge.file_path.name}' shipped with the source export; "
                        "upload it into an Orchestrate knowledge base (mind the 30MB cap)."
                        if raw_knowledge.file_path
                        else f"Direct file upload{detail}: re-supply the actual files to "
                        "Orchestrate (the export contains only a path, not the file bytes; "
                        "enforce the 30MB/file cap)."
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

    # Dedup tools by target ref: several source operations of one connector can
    # resolve to the same catalog tool (e.g. ServiceNow ListRecords + GetRecords
    # both land on the ServiceNow toolkit), and a duplicate tool reference in an
    # Orchestrate agent is meaningless. Keep first occurrence (order-stable).
    seen: set[str] = set()
    deduped = []
    for tool in agent.tools:
        if tool.ref in seen:
            continue
        seen.add(tool.ref)
        deduped.append(tool)
    agent.tools = deduped

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

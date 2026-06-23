"""Deterministic Map stage: raw references -> IR tools/knowledge/connections.

No LLM call happens in this stage, by design (per the architecture: anything
touching schemas or credentials stays mechanical and auditable). Anything
without a confident, explicit mapping is flagged review_required rather than
guessed at.
"""

from __future__ import annotations

from wheatear.connectors.copilot_studio.importer import ImportResult
from wheatear.ir.schema import Agent, ConnectionRef, KnowledgeRef, ToolRef

# Known Copilot Studio connector -> Orchestrate tool mappings. Intentionally
# near-empty for v1: most custom connectors are org-specific and have no
# universal Orchestrate equivalent. Real mappings get added here as
# corridors are validated against real exports, not guessed in advance.
KNOWN_TOOL_MAPPINGS: dict[str, str] = {}


def map_agent(import_result: ImportResult) -> Agent:
    """Populate tools/knowledge/connections on the IR Agent from the raw
    references Normalize extracted. Returns the same Agent object, mutated.
    """
    agent = import_result.agent

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
                        "implement the tool and update this reference before import."
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
                    notes=(
                        f"{raw_knowledge.source_kind} source{detail} needs re-indexing into an "
                        "Orchestrate knowledge base; this is not a reference copy."
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

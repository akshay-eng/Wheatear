"""n8n workflow JSON -> canonical IR importer.

Handles a single workflow file, a directory/bundle of workflow files, or (via
the wizard) a set of workflows fetched from the n8n REST API. n8n multi-agent
"workflows" are actually several workflow files wired together by `toolWorkflow`
nodes, so the real entry point is `import_bundle`, which does a two-pass parse:
Pass 1 classifies every workflow (Class A = has an agent node), Pass 2 imports
each Class-A agent and resolves its `toolWorkflow` references against the whole
bundle (Class-A target -> collaborator, Class-B target -> tool, absent ->
flagged). Assembly reuses `wheatear/workflow.py`.

Module contract (`detect_format`, `import_agent`) is preserved for the
single-file case and the registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from wheatear.connectors.base import ImportResult, RawKnowledgeRef, RawToolRef
from wheatear.connectors.n8n import graph, http_tools
from wheatear.ir.schema import Agent, AgentRef, Workflow
from wheatear.workflow import assemble_workflow

__all__ = [
    "ImportResult",
    "N8nImport",
    "detect_format",
    "import_agent",
    "import_bundle",
    "import_workflows",
]


@dataclass
class N8nImport:
    """A multi-agent n8n import: the assembled Workflow plus the per-agent
    ImportResults. The `.agent` in each result is the *same object* held in
    `workflow.agents`, so running Map over each result mutates the workflow's
    agents in place (matching how the other corridors thread Map)."""

    workflow: Workflow
    results: list[ImportResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Format detection
# --------------------------------------------------------------------------- #

def _looks_like_n8n_workflow(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("nodes"), list)
        and isinstance(data.get("connections"), dict)
        and any(
            isinstance(n, dict) and str(n.get("type", "")).startswith(("@n8n/", "n8n-nodes-"))
            for n in data["nodes"]
        )
    )


def detect_format(path: Path) -> str | None:
    """Return 'n8n_workflow' (single JSON file), 'n8n_bundle' (dir with >=1
    workflow JSON), or None."""
    path = Path(path)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return "n8n_workflow" if _looks_like_n8n_workflow(data) else None
    if path.is_dir():
        for jf in sorted(path.glob("*.json")):
            try:
                if _looks_like_n8n_workflow(json.loads(jf.read_text())):
                    return "n8n_bundle"
            except (OSError, json.JSONDecodeError):
                continue
    return None


# --------------------------------------------------------------------------- #
# Per-agent import (one Class-A workflow -> one ImportResult)
# --------------------------------------------------------------------------- #

def _dedup_connection_refs(nodes: list[dict]) -> list[str]:
    """Every reachable node's credentials, deduped by credential id (five nodes
    sharing one credential = one connection ref)."""
    seen_ids: set[str] = set()
    refs: list[str] = []
    for node in nodes:
        for cred_type, cred in (node.get("credentials") or {}).items():
            cid = cred.get("id") if isinstance(cred, dict) else None
            name = cred.get("name") if isinstance(cred, dict) else None
            key = cid or f"{cred_type}:{name}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            refs.append(name or cred_type)
    return refs


def _file_kb_from_main_chain(wf: graph.N8nWorkflow, agent_name: str) -> RawKnowledgeRef | None:
    """Detect a readWriteFile -> extractFromFile -> ... -> agent (main) chain and
    turn it into a file-upload knowledge ref. The file bytes are never in the
    export, so this is inherently review-required downstream."""
    # BFS backward over main edges from the agent, looking for a readWriteFile.
    visited: set[str] = set()
    queue = [agent_name]
    file_selector = None
    found = False
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        for pred in wf.main_predecessors(cur):
            ptype = pred.get("type")
            if ptype == graph.READ_FILE:
                found = True
                file_selector = (pred.get("parameters") or {}).get("fileSelector")
            queue.append(pred["name"])
    if not found:
        return None
    return RawKnowledgeRef(
        name=f"{wf.name} Knowledge Base",
        source_kind="file_upload",
        detail=file_selector,
        is_file_upload=True,
    )


def _import_class_a(wf: graph.N8nWorkflow, agent_name: str, classify) -> ImportResult:
    """Import one agent node from a Class-A workflow. `classify(workflow_id)`
    returns 'A', 'B', or None for a toolWorkflow target."""
    node = wf.nodes_by_name[agent_name]
    params = node.get("parameters") or {}
    options = params.get("options") or {}

    agent = Agent(name=wf.name, source_platform="n8n")
    agent.existing_instructions = graph.strip_expression_prefix(options.get("systemMessage"))
    # n8n agents have no description field; derive a short one from the system
    # prompt's first sentence so the target has something meaningful (the
    # Orchestrate ADK requires a non-empty description).
    if agent.existing_instructions:
        first = agent.existing_instructions.strip().split("\n", 1)[0].split(". ", 1)[0].strip()
        agent.description = (first[:240] or wf.name) if first else wf.name

    import_notes: list[str] = []
    raw_tools: list[RawToolRef] = []
    raw_knowledge: list[RawKnowledgeRef] = []
    http_specs: list[http_tools.HttpToolSpec] = []

    # Model (ai_languageModel edge).
    for lm in wf.sources_into(agent_name, graph.OUT_MODEL):
        if graph.is_lm_node(lm):
            model_name = (lm.get("parameters") or {}).get("modelName") or (lm.get("parameters") or {}).get("model")
            if isinstance(model_name, dict):
                model_name = model_name.get("value")
            agent.model_hint = model_name
            break

    # Memory (ai_memory edge) -> Orchestrate manages memory natively.
    if wf.sources_into(agent_name, graph.OUT_MEMORY):
        import_notes.append(
            "Source used an n8n memory node; Orchestrate manages session memory "
            "natively, so no explicit memory tool was migrated."
        )

    # Tools (ai_tool edges).
    for tool in wf.sources_into(agent_name, graph.OUT_TOOL):
        ttype = tool.get("type")
        tparams = tool.get("parameters") or {}
        if ttype == graph.MCP_CLIENT:
            raw_tools.append(
                RawToolRef(
                    name=tool["name"],
                    kind="mcp",
                    mcp_server_url=tparams.get("endpointUrl") or tparams.get("sseEndpoint"),
                    transport=tparams.get("serverTransport"),
                    source_ref=tool["name"],
                    tool_names=list(tparams.get("includeTools") or []),
                )
            )
        elif ttype == graph.TOOL_WF:
            target_id = graph.workflow_id_of_tool_workflow(tool)
            cached = graph.cached_name_of_tool_workflow(tool) or target_id
            kind = classify(target_id) if target_id else None
            if kind == "A":
                agent.collaborators.append(
                    AgentRef(ref=cached, source_ref=target_id, notes=tparams.get("description"))
                )
            elif kind == "B":
                raw_tools.append(RawToolRef(name=cached, kind="unknown", source_ref=target_id))
            else:
                agent.collaborators.append(
                    AgentRef(
                        ref=cached,
                        source_ref=target_id,
                        review_required=True,
                        notes="Referenced workflow not provided in this import; fetch it to complete migration.",
                    )
                )
        elif http_tools.is_http_tool(tool):
            # An HTTP request tool is a complete API call, not an opaque
            # connector: it names its own endpoint, its parameters and which of
            # them the model supplies. Classified as "http" so the provisioner
            # can rebuild it on the target instead of filing it for a human --
            # as "connector" these were reported as unmigratable and the agent
            # arrived with no tools at all.
            spec = http_tools.extract(tool)
            raw_tools.append(
                RawToolRef(
                    name=spec.name,
                    kind="http",
                    source_ref=ttype,
                    description=spec.description or None,
                    operation_id=spec.operation_id(),
                    inputs=[p.to_tool_param() for p in spec.params],
                    connection_reference=spec.credential_ref,
                )
            )
            http_specs.append(spec)
            import_notes.extend(f"{spec.name}: {note}" for note in spec.notes)
        else:
            # A plain connector node wired as a tool -> catalog match downstream.
            app = tool.get("name")
            raw_tools.append(RawToolRef(name=app, kind="connector", source_ref=ttype))

    # File-upload knowledge base (main-flow chain).
    kb = _file_kb_from_main_chain(wf, agent_name)
    if kb is not None:
        raw_knowledge.append(kb)

    # Credentials -> connection refs (deduped).
    raw_connection_refs = _dedup_connection_refs(list(wf.nodes_by_name.values()))

    return ImportResult(
        agent=agent,
        raw_tools=raw_tools,
        raw_knowledge_refs=raw_knowledge,
        raw_connection_refs=raw_connection_refs,
        endpoint_tools=http_specs,
        import_notes=import_notes,
    )


# --------------------------------------------------------------------------- #
# Bundle assembly (two-pass, multi-workflow)
# --------------------------------------------------------------------------- #

def import_workflows(raw_workflows: list[dict]) -> N8nImport:
    """Import a list of raw n8n workflow dicts (e.g. fetched from the REST API)
    into an N8nImport (Workflow + per-agent ImportResults). The primary entry
    point for the corridor."""
    # Pass 1: parse + classify.
    parsed = [graph.build_workflow(w) for w in raw_workflows]
    by_id = {wf.id: wf for wf in parsed}

    def classify(workflow_id: str | None) -> str | None:
        wf = by_id.get(workflow_id) if workflow_id else None
        if wf is None:
            return None
        return "A" if wf.is_class_a else "B"

    # Pass 2: import each Class-A agent, resolving cross-refs against the bundle.
    results: list[ImportResult] = []
    for wf in parsed:
        if not wf.is_class_a:
            continue
        for agent_name in wf.agent_node_names:
            results.append(_import_class_a(wf, agent_name, classify))

    root = _pick_root(parsed)
    workflow = assemble_workflow([r.agent for r in results], source_platform="n8n", root=root)
    return N8nImport(workflow=workflow, results=results)


def _pick_root(parsed: list[graph.N8nWorkflow]) -> str | None:
    """The entry agent: the Class-A workflow with a chatTrigger, else the one no
    other workflow's toolWorkflow references."""
    class_a = [wf for wf in parsed if wf.is_class_a]
    for wf in class_a:
        if wf.has_chat_trigger:
            return wf.name
    referenced: set[str] = set()
    for wf in parsed:
        for node in wf.nodes_by_name.values():
            if node.get("type") == graph.TOOL_WF:
                tid = graph.workflow_id_of_tool_workflow(node)
                if tid:
                    referenced.add(tid)
    for wf in class_a:
        if wf.id not in referenced:
            return wf.name
    return class_a[0].name if class_a else None


# --------------------------------------------------------------------------- #
# Path-based entry points (module contract + local files)
# --------------------------------------------------------------------------- #

def _load_json_files(path: Path) -> list[dict]:
    path = Path(path)
    if path.is_file():
        return [json.loads(path.read_text())]
    workflows = []
    for jf in sorted(path.glob("*.json")):
        try:
            data = json.loads(jf.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if _looks_like_n8n_workflow(data):
            workflows.append(data)
    return workflows


def import_bundle(path: Path) -> N8nImport:
    """Import a directory of n8n workflow JSON files (or a single file) into an
    N8nImport (Workflow + per-agent ImportResults)."""
    raw = _load_json_files(path)
    if not raw:
        raise FileNotFoundError(f"{path} contains no recognizable n8n workflow JSON.")
    return import_workflows(raw)


def import_agent(path: Path) -> ImportResult:
    """Single-workflow module-contract entry. Returns the ImportResult for the
    (first) Class-A agent in the file. Callers wanting the multi-agent graph
    use import_bundle / import_workflows."""
    raw = _load_json_files(path)
    parsed = [graph.build_workflow(w) for w in raw]
    by_id = {wf.id: wf for wf in parsed}

    def classify(workflow_id: str | None) -> str | None:
        wf = by_id.get(workflow_id) if workflow_id else None
        return None if wf is None else ("A" if wf.is_class_a else "B")

    for wf in parsed:
        if wf.is_class_a:
            return _import_class_a(wf, wf.agent_node_names[0], classify)
    raise ValueError(f"{path} has no n8n agent node (Class-A) to import.")

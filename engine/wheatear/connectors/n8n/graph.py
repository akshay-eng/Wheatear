"""Pure n8n workflow graph model + walk helpers (no I/O, no LLM).

n8n stores a workflow as `nodes: [...]` + `connections: {...}`. The connections
block is keyed by the *source* node name, with typed output arrays
(`main`, `ai_languageModel`, `ai_memory`, `ai_tool`, `ai_outputParser`). AI
sub-nodes (the model, memory, tools) connect *into* the agent node, so to find
an agent's model/tools we need a reverse index: target -> [(source, type)].
Kept pure and separately unit-tested, mirroring how `wheatear/workflow.py`
isolates its graph logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- node types we care about -------------------------------------------------
AGENT = "@n8n/n8n-nodes-langchain.agent"
CHAT_TRIGGER = "@n8n/n8n-nodes-langchain.chatTrigger"
EXEC_WF_TRIGGER = "n8n-nodes-base.executeWorkflowTrigger"
MCP_CLIENT = "@n8n/n8n-nodes-langchain.mcpClientTool"
TOOL_WF = "@n8n/n8n-nodes-langchain.toolWorkflow"
READ_FILE = "n8n-nodes-base.readWriteFile"
EXTRACT_FILE = "n8n-nodes-base.extractFromFile"

# prefixes (family covers many concrete node types)
LMCHAT_PREFIX = "@n8n/n8n-nodes-langchain.lmChat"
LM_PREFIX = "@n8n/n8n-nodes-langchain.lm"  # lmChat*, lmOpenAi, ...
MEMORY_PREFIX = "@n8n/n8n-nodes-langchain.memory"

# connection output types
OUT_MAIN = "main"
OUT_MODEL = "ai_languageModel"
OUT_MEMORY = "ai_memory"
OUT_TOOL = "ai_tool"


@dataclass
class N8nWorkflow:
    """One parsed n8n workflow with a reverse-connection index."""

    id: str
    name: str
    active: bool
    nodes_by_name: dict[str, dict]
    # target node name -> list of (source node name, output_type)
    incoming: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def agent_node_names(self) -> list[str]:
        return [n["name"] for n in self.nodes_by_name.values() if n.get("type") == AGENT]

    @property
    def is_class_a(self) -> bool:
        """Class A = contains an agent node (-> an Orchestrate agent). Class B =
        pure automation (-> a tool or no clean Orchestrate target)."""
        return bool(self.agent_node_names)

    @property
    def has_chat_trigger(self) -> bool:
        return any(n.get("type") == CHAT_TRIGGER for n in self.nodes_by_name.values())

    def sources_into(self, target_name: str, output_type: str) -> list[dict]:
        """Nodes that connect into `target_name` on `output_type`, as node dicts."""
        return [
            self.nodes_by_name[src]
            for src, otype in self.incoming.get(target_name, [])
            if otype == output_type and src in self.nodes_by_name
        ]

    def main_predecessors(self, target_name: str) -> list[dict]:
        return self.sources_into(target_name, OUT_MAIN)


def build_workflow(raw: dict) -> N8nWorkflow:
    """Parse a raw n8n workflow JSON dict into an N8nWorkflow with reverse index."""
    nodes = raw.get("nodes") or []
    nodes_by_name = {n["name"]: n for n in nodes if n.get("name")}

    incoming: dict[str, list[tuple[str, str]]] = {}
    for src_name, outputs in (raw.get("connections") or {}).items():
        for output_type, arrays in (outputs or {}).items():
            for arr in arrays or []:
                for conn in arr or []:
                    target = conn.get("node")
                    if target:
                        incoming.setdefault(target, []).append((src_name, output_type))

    return N8nWorkflow(
        id=str(raw.get("id") or raw.get("name") or ""),
        name=raw.get("name") or str(raw.get("id") or "n8n workflow"),
        active=bool(raw.get("active", False)),
        nodes_by_name=nodes_by_name,
        incoming=incoming,
    )


def is_lm_node(node: dict) -> bool:
    return str(node.get("type", "")).startswith(LM_PREFIX)


def is_memory_node(node: dict) -> bool:
    return str(node.get("type", "")).startswith(MEMORY_PREFIX)


def strip_expression_prefix(value: str | None) -> str | None:
    """n8n prefixes expression-mode string fields with '='. Strip it so the
    system prompt reads as plain text."""
    if isinstance(value, str) and value.startswith("="):
        return value[1:]
    return value


def workflow_id_of_tool_workflow(node: dict) -> str | None:
    """Extract the referenced workflow id from a toolWorkflow node's
    parameters.workflowId (a resourceLocator: {value, cachedResultName})."""
    wid = (node.get("parameters") or {}).get("workflowId")
    if isinstance(wid, dict):
        return wid.get("value")
    if isinstance(wid, str):
        return wid
    return None


def cached_name_of_tool_workflow(node: dict) -> str | None:
    wid = (node.get("parameters") or {}).get("workflowId")
    if isinstance(wid, dict):
        return wid.get("cachedResultName")
    return None

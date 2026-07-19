"""IBM watsonx Orchestrate YAML → canonical IR importer.

Three YAML shapes are handled:

  1. REST export (Wheatear's rich format — agent: top-level key):
       agent:
         name: my-agent
         instructions: ...
         llm: ...
         guidelines: [...]
       toolkits:
         - name: SNOWMCP
           current_tools:
             - name: SNOWMCP:create_incident
               description: ...
       knowledge_bases: []

  2. Native Orchestrate ADK export (apiVersion / metadata / spec):
       apiVersion: watsonx-orchestrate/v1alpha1
       kind: NativeAgent
       metadata:
         name: my-agent
       spec:
         instructions: ...

  3. Wheatear's own flat exporter format (spec_version field):
       spec_version: v1
       kind: native
       name: my-agent
       instructions: ...
"""

from __future__ import annotations

from pathlib import Path

import yaml

from wheatear.connectors.base import ImportResult, RawKnowledgeRef, RawToolRef
from wheatear.ir.schema import Agent, AgentRef, Guideline


def detect_format(path: Path) -> str | None:
    """Return 'orchestrate' if this looks like an Orchestrate agent export, else None."""
    path = Path(path)
    candidate = path if path.is_file() else path / "agent.yaml"
    if not candidate.exists():
        return None
    try:
        with open(candidate) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return None
        # REST export format (Wheatear rich export)
        if isinstance(data.get("agent"), dict) and "instructions" in data["agent"]:
            return "orchestrate"
        # Native Orchestrate ADK format
        if "orchestrate" in str(data.get("apiVersion", "")).lower():
            return "orchestrate"
        # Wheatear flat exporter format
        if "v1" in str(data.get("spec_version", "")) and data.get("kind") in ("native", "NativeAgent"):
            return "orchestrate"
    except (yaml.YAMLError, OSError):
        # Unreadable/malformed YAML just means "not a recognized export"; any
        # other error is a real bug and should surface, not be swallowed.
        return None
    return None


def import_agent(export_path: Path) -> ImportResult:
    """Load an Orchestrate agent YAML export and return a canonical ImportResult.

    `export_path` may be:
      - a single agent YAML file
      - a directory containing agent.yaml (Wheatear exporter layout)
    """
    export_path = Path(export_path)

    if export_path.is_dir():
        yaml_file = export_path / "agent.yaml"
        if not yaml_file.exists():
            candidates = list(export_path.glob("**/agent.yaml"))
            if not candidates:
                raise FileNotFoundError(
                    f"No agent.yaml found in {export_path}"
                )
            yaml_file = candidates[0]
    else:
        yaml_file = export_path

    with open(yaml_file) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{yaml_file} doesn't look like a valid agent YAML file")

    return _parse(data)


def _parse(data: dict) -> ImportResult:
    # REST export: agent: top-level key
    if isinstance(data.get("agent"), dict):
        return _parse_rest_export(data)
    # Native ADK export: apiVersion / metadata / spec
    if "apiVersion" in data or "metadata" in data:
        return _parse_native(data)
    # Wheatear flat format
    return _parse_wheatear(data)


def _tool_id_to_name(toolkits: list[dict]) -> dict[str, str]:
    """Map each tool UUID to its human name, across all toolkits' current_tools.
    Used to resolve a guideline's `tool` UUID back to a readable tool name.
    """
    mapping: dict[str, str] = {}
    for tk in toolkits or []:
        for t in tk.get("current_tools") or []:
            tid, tname = t.get("id"), t.get("name")
            if tid and tname:
                mapping[tid] = tname
    return mapping


def _guidelines(raw_guidelines: list[dict], tool_names: dict[str, str]) -> list[Guideline]:
    """Convert exported guidelines into IR Guidelines, normalizing the `tool`
    field: the export writes the string "None" for no tool, and a tool UUID
    otherwise -- resolve the UUID to a readable name when we can.
    """
    result: list[Guideline] = []
    for g in raw_guidelines or []:
        condition, action = g.get("condition"), g.get("action")
        if not condition or not action:
            continue
        tool = g.get("tool")
        if not tool or str(tool) == "None":
            tool_ref = None
        else:
            tool_ref = tool_names.get(tool, tool)  # name if resolvable, else the raw id
        result.append(
            Guideline(name=g.get("display_name"), condition=condition, action=action, tool_ref=tool_ref)
        )
    return result


def _rest_toolkit_refs(toolkits: list[dict]) -> list[RawToolRef]:
    """Each toolkit becomes a RawToolRef carrying its MCP server URL/transport
    when it's an MCP toolkit -- the metadata Map needs to migrate it cleanly.
    """
    refs: list[RawToolRef] = []
    for tk in toolkits or []:
        name = tk.get("name")
        if not name:
            continue
        kind = (tk.get("type") or "unknown").lower()
        tool_names = [t.get("name") for t in tk.get("current_tools") or [] if t.get("name")]
        refs.append(
            RawToolRef(
                name=name,
                kind=kind,
                mcp_server_url=tk.get("mcp_server_url"),
                transport=tk.get("transport"),
                source_ref=name,
                tool_names=tool_names,
            )
        )
    return refs


def _parse_rest_export(data: dict) -> ImportResult:
    """Parse Wheatear's rich REST export format (agent: top-level key)."""
    agent_data = data.get("agent") or {}
    toolkits = data.get("toolkits") or []

    name = agent_data.get("name") or agent_data.get("display_name") or "unknown"
    instructions = (agent_data.get("instructions") or "").strip() or None
    description = (agent_data.get("description") or "").strip() or None
    if not instructions and description:
        instructions = description

    raw_tools = _rest_toolkit_refs(toolkits)

    raw_knowledge: list[RawKnowledgeRef] = []
    for kb in data.get("knowledge_bases") or []:
        kb_name = kb.get("name") or kb.get("id", "")
        if kb_name:
            raw_knowledge.append(RawKnowledgeRef(name=kb_name))

    notes: list[str] = []
    unmapped = data.get("unmapped_tool_ids") or []
    if unmapped:
        notes.append(
            f"{len(unmapped)} tool UUID(s) on the source agent could not be resolved to a "
            "toolkit (stale/deleted registrations); verify no tool was lost."
        )

    agent = Agent(
        name=name,
        source_platform="orchestrate",
        description=description,
        existing_instructions=instructions,
        model_hint=agent_data.get("llm") or None,
        agent_style=agent_data.get("style") or None,
        guidelines=_guidelines(agent_data.get("guidelines"), _tool_id_to_name(toolkits)),
        collaborators=_collaborator_refs(agent_data.get("collaborators")),
        translation_notes=notes,
    )
    return ImportResult(
        agent=agent,
        raw_tools=raw_tools,
        raw_knowledge_refs=raw_knowledge,
    )


def _collaborator_refs(raw) -> list[AgentRef]:
    """Normalize a source 'collaborators' value (list of names or dicts) into
    IR AgentRefs. Tolerant of both shapes and of a missing/None value.
    """
    refs: list[AgentRef] = []
    for c in raw or []:
        if isinstance(c, dict):
            name = c.get("name") or c.get("ref") or c.get("id")
        else:
            name = str(c)
        if name:
            refs.append(AgentRef(ref=name, source_ref=name))
    return refs


def _parse_native(data: dict) -> ImportResult:
    """Parse native Orchestrate agent YAML (from 'orchestrate agents export')."""
    metadata = data.get("metadata") or {}
    spec = data.get("spec") or {}

    name = (
        metadata.get("name")
        or spec.get("name")
        or data.get("name")
        or "unknown"
    )
    description = spec.get("description") or data.get("description", "")
    instructions = spec.get("instructions") or spec.get("prompt") or ""
    # Fall back to description if no system prompt
    existing_instructions: str | None = instructions.strip() if instructions.strip() else None
    if not existing_instructions and description:
        existing_instructions = description.strip()

    # Tools — two shapes: list of dicts {name: ...} or list of strings
    raw_tools: list[str] = []
    for t in spec.get("tools", []) or []:
        if isinstance(t, dict):
            raw_tools.append(t.get("name") or t.get("ref") or "")
        elif isinstance(t, str):
            raw_tools.append(t)
    raw_tools = [r for r in raw_tools if r]

    # Knowledge bases
    raw_knowledge: list[RawKnowledgeRef] = []
    for k in spec.get("knowledge", []) or []:
        if isinstance(k, dict):
            raw_knowledge.append(RawKnowledgeRef(name=k.get("name") or k.get("ref") or ""))
        elif isinstance(k, str):
            raw_knowledge.append(RawKnowledgeRef(name=k))

    notes: list[str] = []
    if data.get("kind") == "NativeAgent":
        notes.append("Imported from native Orchestrate NativeAgent format")

    agent = Agent(
        name=name,
        source_platform="orchestrate",
        description=(description or None),
        existing_instructions=existing_instructions,
        model_hint=spec.get("llm") or None,
        agent_style=spec.get("style") or None,
        guidelines=_guidelines(spec.get("guidelines"), {}),
        translation_notes=notes,
        collaborators=_collaborator_refs(spec.get("collaborators")),
    )
    return ImportResult(
        agent=agent,
        raw_tool_refs=raw_tools,
        raw_knowledge_refs=raw_knowledge,
    )


def _parse_wheatear(data: dict) -> ImportResult:
    """Parse Wheatear's own Orchestrate exporter output format."""
    name = data.get("name", "unknown")
    instructions = data.get("instructions", "")
    existing_instructions: str | None = instructions.strip() if instructions.strip() else None

    # Tools — list of strings in Wheatear format
    raw_tools = [t for t in data.get("tools", []) or [] if isinstance(t, str) and t]

    # Knowledge bases — stored as "knowledge_base" in Wheatear format
    raw_knowledge = [
        RawKnowledgeRef(name=k)
        for k in data.get("knowledge_base", []) or []
        if isinstance(k, str) and k
    ]

    notes: list[str] = ["Imported from Wheatear Orchestrate export format"]

    agent = Agent(
        name=name,
        source_platform="orchestrate",
        existing_instructions=existing_instructions,
        model_hint=data.get("llm") or None,
        agent_style=data.get("style") or None,
        guidelines=_guidelines(data.get("guidelines"), {}),
        translation_notes=notes,
        collaborators=_collaborator_refs(data.get("collaborators")),
    )
    return ImportResult(
        agent=agent,
        raw_tool_refs=raw_tools,
        raw_knowledge_refs=raw_knowledge,
    )

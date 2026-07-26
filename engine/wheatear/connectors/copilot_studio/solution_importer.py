"""Copilot Studio solution export (Dataverse XML) -> IR importer.

A different export mechanism from `pac copilot clone`'s .mcs.yml workspace:
`solution.xml` + `bots/*/bot.xml` + `botcomponents/*/botcomponent.xml`+`data`
sidecar files, produced by exporting an agent as a Dataverse solution. This
is the shape actually seen calibrating against a real agent export (see
mcs_yaml_importer.py for the other one, importer.py for dispatch).

Component identity is carried by `<componenttype>` in each botcomponent.xml,
confirmed against real data:
  9  = Dialog component -- see below, this is NOT only topics
  14 = File knowledge source; the document itself sits in a `filedata/`
       subdirectory next to botcomponent.xml
  15 = GPT component (GptComponentMetadata) -- the generative agent's own
       system prompt, present on generative/GPT-orchestrated agents
  16 = Knowledge source configuration (connector-backed, e.g. SharePoint)

componenttype 9 covers three different things, discriminated by the `kind`
field *inside* the data sidecar -- not by the component type, and not by the
name. Confirmed against a real 3-agent export:

  kind: AdaptiveDialog  -> a conversational topic (the dialog tree)
  kind: TaskDialog      -> an agent-level capability, further split by
                           `action.kind`:
      InvokeConnectorTaskAction      -> a tool (Power Platform connector op)
      InvokeConnectedAgentTaskAction -> a collaborator (connected agent)

Treating every type-9 component as a topic silently loses every tool and
every multi-agent edge, and worse, feeds empty pseudo-topics named after
the tools into Translate as though they were business logic.

For a generative agent, almost all of its real behavior lives in the GPT
component's `instructions` field, not in the topic tree -- the topics here
are typically all boilerplate lifecycle scaffolding (see
common.SYSTEM_TOPIC_NAMES). Treating that prompt as the migration's primary
source, rather than trying to reconstruct equivalent behavior from 13
template topics, is the whole reason this importer exists separately from
the dialog-tree-shaped mcs_yaml path.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from wheatear.connectors.base import RawToolRef, ToolParam
from wheatear.connectors.copilot_studio.common import SYSTEM_TOPIC_NAMES, ImportResult, RawKnowledgeRef
from wheatear.ir.schema import Agent, AgentRef, DialogNode, DialogNodeKind, Topic, Workflow

SOURCE_PLATFORM = "copilot-studio"

COMPONENT_TYPE_DIALOG = 9
COMPONENT_TYPE_FILE_KNOWLEDGE = 14
COMPONENT_TYPE_GPT = 15
COMPONENT_TYPE_KNOWLEDGE = 16

# Backwards-compatible alias: type 9 was modeled as "topic" before connector
# actions and connected agents were found sharing the code.
COMPONENT_TYPE_TOPIC = COMPONENT_TYPE_DIALOG

# `kind` values inside a type-9 component's data sidecar.
DIALOG_KIND_TOPIC = "AdaptiveDialog"
DIALOG_KIND_TASK = "TaskDialog"

# `action.kind` values inside a TaskDialog.
ACTION_CONNECTOR = "InvokeConnectorTaskAction"
ACTION_CONNECTED_AGENT = "InvokeConnectedAgentTaskAction"

# Dialog plumbing, not agent behavior -- skipped without a note since
# nothing about the migration is lost by not modeling these.
NOOP_ACTION_KINDS = {"CancelAllDialogs", "EndDialog"}


def _walk_actions(
    actions: list[dict],
    topic: Topic,
    raw_tool_refs: list[str],
    raw_knowledge_refs: list[RawKnowledgeRef],
) -> list[DialogNode]:
    nodes: list[DialogNode] = []

    for action in actions:
        kind = action.get("kind")

        if kind == "SendActivity":
            activity = action.get("activity", {})
            texts = activity.get("text", []) if isinstance(activity, dict) else []
            if not texts and isinstance(activity, str):
                texts = [activity]
            text = texts[0] if texts else None
            if len(texts) > 1:
                topic.unsupported_notes.append(
                    f"SendActivity '{action.get('id')}' has {len(texts)} phrasing variants; "
                    "only the first was kept."
                )
            nodes.append(DialogNode(kind=DialogNodeKind.MESSAGE, text=text))

        elif kind == "Question":
            prompt = action.get("prompt", {})
            text = None
            if isinstance(prompt, dict):
                prompt_texts = prompt.get("activity", {}).get("text", [])
                text = prompt_texts[0] if prompt_texts else None
            elif isinstance(prompt, str):
                text = prompt
            nodes.append(
                DialogNode(kind=DialogNodeKind.QUESTION, text=text, variable=action.get("variable"))
            )

        elif kind in NOOP_ACTION_KINDS:
            continue

        elif kind == "InvokeConnectorAction":
            connector = action.get("connector", action.get("id", "unknown_connector"))
            raw_tool_refs.append(connector)
            topic.unsupported_notes.append(
                f"Action '{action.get('id')}' invokes connector '{connector}'; "
                "extracted as a tool reference for the Map stage, not modeled as a dialog node."
            )

        elif kind == "SearchAndSummarizeContent":
            knowledge_source = action.get("knowledgeSource")
            if knowledge_source:
                raw_knowledge_refs.append(RawKnowledgeRef(name=knowledge_source))
                topic.unsupported_notes.append(
                    f"Action '{action.get('id')}' searches knowledge source '{knowledge_source}'; "
                    "extracted as a knowledge reference for the Map stage, not modeled as a dialog node."
                )
            else:
                # No explicit knowledgeSource: this is generative search over
                # whatever knowledge the agent already has configured (the
                # default "Conversational boosting" topic), not a distinct
                # source. Fabricating a knowledge ref here produced a phantom
                # "search-content" knowledge base in the export -- so don't.
                topic.unsupported_notes.append(
                    f"Action '{action.get('id')}' does generative search over the agent's own "
                    "configured knowledge sources; no separate knowledge source to map."
                )

        else:
            topic.unsupported_notes.append(
                f"Unrecognized action kind '{kind}' (id: {action.get('id')}); skipped, not translated."
            )

    return nodes


def _parse_topic_component(
    name: str, schema_suffix: str, data: dict
) -> tuple[Topic, list[str], list[RawKnowledgeRef]]:
    topic = Topic(name=name, is_system_topic=schema_suffix in SYSTEM_TOPIC_NAMES)

    if data.get("kind") != "AdaptiveDialog":
        topic.unsupported_notes.append(f"Unrecognized topic kind '{data.get('kind')}'; nodes not parsed.")
        return topic, [], []

    begin_dialog = data.get("beginDialog", {})
    topic.trigger_phrases = begin_dialog.get("intent", {}).get("triggerQueries", [])

    raw_tool_refs: list[str] = []
    raw_knowledge_refs: list[RawKnowledgeRef] = []
    topic.nodes = _walk_actions(begin_dialog.get("actions", []), topic, raw_tool_refs, raw_knowledge_refs)
    return topic, raw_tool_refs, raw_knowledge_refs


def _parse_gpt_component(data: dict) -> tuple[str | None, str | None, bool]:
    instructions = (data.get("instructions") or "").strip()
    model_hint = data.get("aISettings", {}).get("model", {}).get("modelNameHint")
    web_search = bool(data.get("gptCapabilities", {}).get("webBrowsing", False))
    return (instructions or None), model_hint, web_search


def _tool_params(entries: list | None) -> list[ToolParam]:
    """Normalize a TaskDialog inputs/outputs list into ToolParams.

    Inputs carry `propertyName` plus a free-text `description` written for the
    source platform's own model; outputs sometimes carry only `name`. Both are
    kept verbatim -- the descriptions are the matching surface downstream.
    """
    params: list[ToolParam] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("propertyName") or entry.get("name")
        if not name:
            continue
        description = entry.get("description")
        params.append(
            ToolParam(
                name=name,
                description=description.strip() if isinstance(description, str) else None,
                type=entry.get("type"),
            )
        )
    return params


def _parse_task_dialog(
    schema_suffix: str,
    display_name: str,
    description: str | None,
    data: dict,
    connector_ids: dict[str, str],
) -> tuple[RawToolRef | None, AgentRef | None, str | None]:
    """Split a type-9 TaskDialog into whichever of the two things it is.

    Returns (tool, collaborator, note) with at most one of the first two set.
    An unrecognized action kind yields a note instead of a silent drop, so a
    new Copilot capability shows up in the review manifest rather than
    vanishing.
    """
    action = data.get("action")
    if not isinstance(action, dict):
        return None, None, (
            f"Task '{display_name}' has no action block; skipped, not migrated."
        )

    kind = action.get("kind")
    # modelDescription is what Copilot showed its own orchestrator to decide
    # when to call this -- richer and more portable than the display name.
    model_description = data.get("modelDescription") or description
    name = data.get("modelDisplayName") or display_name or schema_suffix

    if kind == ACTION_CONNECTOR:
        connection_reference = action.get("connectionReference")
        return (
            RawToolRef(
                name=name,
                kind="connector",
                source_ref=schema_suffix,
                description=(model_description or "").strip() or None,
                operation_id=action.get("operationId"),
                inputs=_tool_params(data.get("inputs")),
                outputs=_tool_params(data.get("outputs")),
                connection_reference=connection_reference,
                connector_id=connector_ids.get(connection_reference or ""),
            ),
            None,
            None,
        )

    if kind == ACTION_CONNECTED_AGENT:
        # botSchemaName is the stable join key to the other bot in this
        # solution; Workflow.by_name resolves it once every agent is imported.
        target = action.get("botSchemaName")
        if not target:
            return None, None, (
                f"Connected agent '{name}' names no target bot; the delegation edge was dropped."
            )
        return (
            None,
            AgentRef(
                ref=target,
                source_ref=schema_suffix,
                notes=(model_description or "").strip() or None,
            ),
            None,
        )

    return None, None, (
        f"Unrecognized task action kind '{kind}' on '{name}'; skipped, not migrated."
    )


def _parse_file_knowledge(component_dir: Path, name: str, description: str | None) -> RawKnowledgeRef:
    """A componenttype-14 component: an uploaded document used as knowledge.

    The document itself ships inside the solution under `filedata/`, so unlike
    a connector-backed source this one can actually be re-uploaded to the
    target rather than merely described.
    """
    filedata_dir = component_dir / "filedata"
    file_path = None
    if filedata_dir.is_dir():
        files = sorted(f for f in filedata_dir.iterdir() if f.is_file())
        if files:
            file_path = files[0]
    return RawKnowledgeRef(
        name=name,
        source_kind="file",
        detail=description,
        file_path=file_path,
    )


def _load_connector_ids(solution_dir: Path) -> dict[str, str]:
    """Map connectionreferencelogicalname -> Power Platform connector id from
    customizations.xml.

    A TaskDialog names only its connection reference; the connector identity
    behind it (".../apis/shared_service-now") is what decides whether the tool
    is an MCP server, a prebuilt connector, or a custom one. Returns {} when
    the file is absent -- a bot slice may not carry it, and a missing map only
    costs resolution detail, never the tool itself.
    """
    customizations = solution_dir / "customizations.xml"
    if not customizations.exists():
        return {}
    try:
        root = ET.parse(customizations).getroot()
    except ET.ParseError:
        return {}

    mapping: dict[str, str] = {}
    for reference in root.iter("connectionreference"):
        logical_name = reference.get("connectionreferencelogicalname")
        connector_el = reference.find("connectorid")
        if logical_name and connector_el is not None and connector_el.text:
            mapping[logical_name] = connector_el.text.strip()
    return mapping


def _parse_configuration(bots_dir: Path) -> tuple[list[str], str | None]:
    """Read a bot's configuration.json for deployment channels and the
    content-moderation posture. Returns ([], None) if absent so a missing file
    never breaks an import.
    """
    # Accept either one bot's directory or the bots/ root: a multi-bot export
    # has one configuration.json per bot, and taking the first would attribute
    # some other agent's channels to this one.
    candidates = [bots_dir / "configuration.json"]
    candidates += sorted(bots_dir.glob("*/configuration.json"))
    config_files = [c for c in candidates if c.exists()]
    if not config_files:
        return [], None
    try:
        config = json.loads(config_files[0].read_text())
    except (json.JSONDecodeError, OSError):
        return [], None
    channels = [c.get("channelId") for c in config.get("channels", []) if c.get("channelId")]
    content_moderation = config.get("aISettings", {}).get("contentModeration")
    return channels, content_moderation


def _welcome_from_conversation_start(topic: Topic, agent_name: str) -> str | None:
    """The first message in the ConversationStart topic is the agent's welcome
    message. Copilot templates it with {System.Bot.Name}; substitute the real
    name so it reads correctly on the target.
    """
    for node in topic.nodes:
        if node.kind == DialogNodeKind.MESSAGE and node.text:
            return node.text.replace("{System.Bot.Name}", agent_name).strip()
    return None


def _parse_knowledge_component(name: str, description: str | None, data: dict) -> RawKnowledgeRef:
    source = data.get("source", {})
    return RawKnowledgeRef(
        name=name,
        source_kind=source.get("kind"),
        detail=source.get("site") or source.get("url") or description,
    )


def list_bots(solution_dir: Path) -> list[tuple[str, str]]:
    """Return (schema_name, display_name) for every bot in the solution.

    A solution can hold a whole multi-agent system -- the calibration export
    carries three -- so callers need to know what's in there before asking for
    one of them.
    """
    bots_dir = Path(solution_dir) / "bots"
    if not bots_dir.is_dir():
        return []
    bots: list[tuple[str, str]] = []
    for bot_xml in sorted(bots_dir.glob("*/bot.xml")):
        schema = bot_xml.parent.name
        try:
            root = ET.parse(bot_xml).getroot()
        except ET.ParseError:
            bots.append((schema, schema))
            continue
        name_el = root.find("name")
        display = name_el.text if name_el is not None and name_el.text else schema
        bots.append((root.get("schemaname") or schema, display))
    return bots


def _read_botcomponent_meta(
    botcomponent_xml: Path,
) -> tuple[int | None, str, str | None, str, str]:
    root = ET.parse(botcomponent_xml).getroot()
    type_el = root.find("componenttype")
    name_el = root.find("name")
    desc_el = root.find("description")
    component_type = int(type_el.text) if type_el is not None and type_el.text else None
    name = name_el.text if name_el is not None and name_el.text else botcomponent_xml.parent.name
    description = desc_el.text if desc_el is not None else None
    # The schemaname suffix (e.g. "ai_HelperBee.topic.MultipleTopicsMatched"
    # -> "MultipleTopicsMatched") is the stable identifier for system-topic
    # detection. The human <name> field is NOT reliable for this: it's
    # editable display text that can have spaces or even be renamed entirely
    # (a real export had schemaname "...topic.Search" displayed as
    # "Conversational boosting") -- confirmed against a real export.
    schemaname = root.get("schemaname") or botcomponent_xml.parent.name
    schema_suffix = schemaname.rsplit(".", 1)[-1]
    return component_type, name, description, schema_suffix, schemaname


def _owns(schemaname: str, bot_schema: str) -> bool:
    """True if a component belongs to the given bot.

    Ownership is by schemaname prefix, not by the `parentbotid` element: that
    element is present but *empty* in every component of a real export, so
    trusting it would attribute nothing to anyone.
    """
    return schemaname == bot_schema or schemaname.startswith(bot_schema + ".")


def import_workflow(solution_dir: Path) -> tuple[Workflow, list[ImportResult]]:
    """Import every bot in a solution as one multi-agent Workflow.

    Connected-agent edges are recorded by *schema* name (crd07_HRAgent) while
    agents are named by their *display* name (HR Agent), so the references are
    rewritten here -- the one place that can see every bot at once. An edge
    pointing outside the solution is kept but flagged, since that's a real
    migration gap (the collaborator won't exist on the target) rather than a
    parse failure.
    """
    solution_dir = Path(solution_dir)
    bots = list_bots(solution_dir)

    results = [import_agent(solution_dir, bot_schema=schema) for schema, _ in bots]
    schema_to_name = {schema: result.agent.name for (schema, _), result in zip(bots, results)}

    for result in results:
        for collaborator in result.agent.collaborators:
            resolved = schema_to_name.get(collaborator.ref)
            if resolved is not None:
                collaborator.ref = resolved
            else:
                collaborator.review_required = True
                collaborator.notes = (
                    f"Connected agent '{collaborator.ref}' is not part of this export; "
                    "migrate it separately and re-point this collaborator."
                )

    agents = [result.agent for result in results]
    # The root is the agent nobody delegates to -- the entry point a user
    # actually talks to. Ambiguous cases (several or none) leave it unset
    # rather than picking arbitrarily.
    delegated_to = {c.ref for agent in agents for c in agent.collaborators}
    roots = [a.name for a in agents if a.name not in delegated_to and a.collaborators]
    workflow = Workflow(
        source_platform=SOURCE_PLATFORM,
        root=roots[0] if len(roots) == 1 else None,
        agents=agents,
    )
    return workflow, results


def _select_bot(solution_dir: Path, bot_schema: str | None) -> tuple[str, str]:
    """Pick which bot in the solution to import, as (schema_name, display_name).

    A solution with several bots used to be imported by merging all of them
    into one agent -- 43 topics from three different agents in one IR object,
    with each bot's GPT component overwriting the last. Requiring an explicit
    choice makes that failure impossible to reach by accident.
    """
    bots = list_bots(solution_dir)
    if not bots:
        # No bot.xml at all: fall back to the directory name, as before.
        return solution_dir.name, solution_dir.name

    if bot_schema is not None:
        for schema, display in bots:
            if schema == bot_schema:
                return schema, display
        available = ", ".join(schema for schema, _ in bots)
        raise ValueError(
            f"No bot '{bot_schema}' in {solution_dir}. Available: {available}"
        )

    if len(bots) > 1:
        available = ", ".join(f"{schema} ({display})" for schema, display in bots)
        raise ValueError(
            f"{solution_dir} contains {len(bots)} bots; pass bot_schema to choose one "
            f"(or slice the solution first). Available: {available}"
        )

    return bots[0]


def import_agent(solution_dir: Path, bot_schema: str | None = None) -> ImportResult:
    """Parse one bot from a Copilot Studio solution export into the canonical IR.

    `bot_schema` selects the bot when the solution holds more than one; it may
    be omitted for a single-bot solution (what `create_bot_slice` produces).
    """
    solution_dir = Path(solution_dir)
    bots_dir = solution_dir / "bots"
    components_dir = solution_dir / "botcomponents"

    if not (solution_dir / "solution.xml").exists() or not bots_dir.is_dir():
        raise FileNotFoundError(
            f"{solution_dir} doesn't look like a Copilot Studio solution export "
            "(expected solution.xml and a bots/ directory)."
        )

    bot_schema_name, agent_name = _select_bot(solution_dir, bot_schema)
    connector_ids = _load_connector_ids(solution_dir)

    topics: list[Topic] = []
    raw_tool_refs: list[str] = []
    raw_tools: list[RawToolRef] = []
    raw_knowledge_refs: list[RawKnowledgeRef] = []
    collaborators: list[AgentRef] = []
    import_notes: list[str] = []
    existing_instructions: str | None = None
    model_hint: str | None = None
    web_search = False
    welcome_message: str | None = None

    component_dirs = sorted(components_dir.iterdir()) if components_dir.is_dir() else []
    for component_dir in component_dirs:
        botcomponent_xml = component_dir / "botcomponent.xml"
        if not botcomponent_xml.exists():
            continue

        component_type, name, description, schema_suffix, schemaname = _read_botcomponent_meta(
            botcomponent_xml
        )
        if not _owns(schemaname, bot_schema_name):
            continue

        # File knowledge carries its payload in filedata/ and has no data
        # sidecar, so it has to be handled before the data-file gate below.
        if component_type == COMPONENT_TYPE_FILE_KNOWLEDGE:
            raw_knowledge_refs.append(_parse_file_knowledge(component_dir, name, description))
            continue

        data_file = component_dir / "data"
        if not data_file.exists():
            continue
        data = yaml.safe_load(data_file.read_text()) or {}

        if component_type == COMPONENT_TYPE_DIALOG:
            # Type 9 is three different things; `kind` in the sidecar decides.
            if data.get("kind") == DIALOG_KIND_TASK:
                tool, collaborator, note = _parse_task_dialog(
                    schema_suffix, name, description, data, connector_ids
                )
                if tool is not None:
                    raw_tools.append(tool)
                if collaborator is not None:
                    collaborators.append(collaborator)
                if note:
                    import_notes.append(note)
                continue

            topic, tool_refs, knowledge_refs = _parse_topic_component(name, schema_suffix, data)
            raw_tool_refs.extend(tool_refs)
            raw_knowledge_refs.extend(knowledge_refs)
            if schema_suffix == "ConversationStart":
                welcome_message = _welcome_from_conversation_start(topic, agent_name)
            topics.append(topic)

        elif component_type == COMPONENT_TYPE_GPT:
            existing_instructions, model_hint, web_search = _parse_gpt_component(data)

        elif component_type == COMPONENT_TYPE_KNOWLEDGE:
            raw_knowledge_refs.append(_parse_knowledge_component(name, description, data))

        else:
            import_notes.append(
                f"Skipped component '{component_dir.name}' (componenttype={component_type}); unrecognized type."
            )

    channels, content_moderation = _parse_configuration(bots_dir / bot_schema_name)

    agent = Agent(
        name=agent_name,
        source_platform=SOURCE_PLATFORM,
        topics=topics,
        existing_instructions=existing_instructions,
        model_hint=model_hint,
        welcome_message=welcome_message,
        channels=channels,
        content_moderation=content_moderation,
        web_search=web_search,
        collaborators=collaborators,
    )

    return ImportResult(
        agent=agent,
        raw_tool_refs=raw_tool_refs,
        raw_tools=raw_tools,
        raw_knowledge_refs=raw_knowledge_refs,
        raw_connection_refs=[],
        import_notes=import_notes,
    )

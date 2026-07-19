"""Copilot Studio solution export (Dataverse XML) -> IR importer.

A different export mechanism from `pac copilot clone`'s .mcs.yml workspace:
`solution.xml` + `bots/*/bot.xml` + `botcomponents/*/botcomponent.xml`+`data`
sidecar files, produced by exporting an agent as a Dataverse solution. This
is the shape actually seen calibrating against a real agent export (see
mcs_yaml_importer.py for the other one, importer.py for dispatch).

Component identity is carried by `<componenttype>` in each botcomponent.xml,
confirmed against real data:
  9  = Topic (AdaptiveDialog)
  15 = GPT component (GptComponentMetadata) -- the generative agent's own
       system prompt, present on generative/GPT-orchestrated agents
  16 = Knowledge source (KnowledgeSourceConfiguration)

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

from wheatear.connectors.copilot_studio.common import SYSTEM_TOPIC_NAMES, ImportResult, RawKnowledgeRef
from wheatear.ir.schema import Agent, DialogNode, DialogNodeKind, Topic

SOURCE_PLATFORM = "copilot-studio"

COMPONENT_TYPE_TOPIC = 9
COMPONENT_TYPE_GPT = 15
COMPONENT_TYPE_KNOWLEDGE = 16

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


def _parse_configuration(bots_dir: Path) -> tuple[list[str], str | None]:
    """Read bots/*/configuration.json for deployment channels and the
    content-moderation posture. Returns ([] , None) if absent so a missing
    file never breaks an import.
    """
    config_files = list(bots_dir.glob("*/configuration.json"))
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


def _parse_bot_name(bots_dir: Path) -> str:
    bot_xml_files = list(bots_dir.glob("*/bot.xml"))
    if not bot_xml_files:
        return bots_dir.parent.name
    root = ET.parse(bot_xml_files[0]).getroot()
    name_el = root.find("name")
    if name_el is not None and name_el.text:
        return name_el.text
    return bot_xml_files[0].parent.name


def _read_botcomponent_meta(botcomponent_xml: Path) -> tuple[int | None, str, str | None, str]:
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
    return component_type, name, description, schema_suffix


def import_agent(solution_dir: Path) -> ImportResult:
    """Parse a Copilot Studio solution export directory into the canonical IR."""
    solution_dir = Path(solution_dir)
    bots_dir = solution_dir / "bots"
    components_dir = solution_dir / "botcomponents"

    if not (solution_dir / "solution.xml").exists() or not bots_dir.is_dir():
        raise FileNotFoundError(
            f"{solution_dir} doesn't look like a Copilot Studio solution export "
            "(expected solution.xml and a bots/ directory)."
        )

    agent_name = _parse_bot_name(bots_dir)

    topics: list[Topic] = []
    raw_tool_refs: list[str] = []
    raw_knowledge_refs: list[RawKnowledgeRef] = []
    import_notes: list[str] = []
    existing_instructions: str | None = None
    model_hint: str | None = None
    web_search = False
    welcome_message: str | None = None

    component_dirs = sorted(components_dir.iterdir()) if components_dir.is_dir() else []
    for component_dir in component_dirs:
        botcomponent_xml = component_dir / "botcomponent.xml"
        data_file = component_dir / "data"
        if not botcomponent_xml.exists() or not data_file.exists():
            continue

        component_type, name, description, schema_suffix = _read_botcomponent_meta(botcomponent_xml)
        data = yaml.safe_load(data_file.read_text()) or {}

        if component_type == COMPONENT_TYPE_TOPIC:
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

    channels, content_moderation = _parse_configuration(bots_dir)

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
    )

    return ImportResult(
        agent=agent,
        raw_tool_refs=raw_tool_refs,
        raw_knowledge_refs=raw_knowledge_refs,
        raw_connection_refs=[],
        import_notes=import_notes,
    )

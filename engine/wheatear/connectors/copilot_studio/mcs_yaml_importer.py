"""Copilot Studio `pac copilot clone` workspace (.mcs.yml) -> IR importer.

Built against Microsoft's documented .mcs.yml / AdaptiveDialog schema. This
is one of two real-world Copilot Studio export shapes Wheatear handles; see
connectors/copilot_studio/solution_importer.py for the other (a Dataverse
solution export), and importer.py for how the two get dispatched.

The parser is deliberately defensive: anything outside the constrained node
set we model (SendActivity, Question, ConditionGroup) is recorded as a note
rather than raising, so an importer gap never silently drops content from
the migration.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from wheatear.connectors.copilot_studio.common import ImportResult, RawKnowledgeRef
from wheatear.ir.schema import Agent, DialogNode, DialogNodeKind, Topic

SOURCE_PLATFORM = "copilot-studio"


def _load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f) or {}


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
            nodes.append(DialogNode(kind=DialogNodeKind.MESSAGE, text=action.get("activity")))

        elif kind == "Question":
            nodes.append(
                DialogNode(
                    kind=DialogNodeKind.QUESTION,
                    text=action.get("prompt"),
                    variable=action.get("variable"),
                )
            )

        elif kind == "ConditionGroup":
            children: list[DialogNode] = []
            for branch in action.get("conditions", []):
                branch_nodes = _walk_actions(
                    branch.get("actions", []), topic, raw_tool_refs, raw_knowledge_refs
                )
                children.append(
                    DialogNode(kind=DialogNodeKind.CONDITION, text=branch.get("condition"), children=branch_nodes)
                )
            else_actions = action.get("elseActions", [])
            if else_actions:
                else_nodes = _walk_actions(else_actions, topic, raw_tool_refs, raw_knowledge_refs)
                children.append(DialogNode(kind=DialogNodeKind.CONDITION, text="else", children=else_nodes))
            nodes.append(DialogNode(kind=DialogNodeKind.CONDITION, text=action.get("id"), children=children))

        elif kind == "InvokeConnectorAction":
            connector = action.get("connector", action.get("id", "unknown_connector"))
            raw_tool_refs.append(connector)
            topic.unsupported_notes.append(
                f"Action '{action.get('id')}' invokes connector '{connector}'; "
                "extracted as a tool reference for the Map stage, not modeled as a dialog node."
            )

        elif kind == "SearchAndSummarizeContent":
            knowledge_source = action.get("knowledgeSource", action.get("id", "unknown_knowledge_source"))
            raw_knowledge_refs.append(RawKnowledgeRef(name=knowledge_source))
            topic.unsupported_notes.append(
                f"Action '{action.get('id')}' searches knowledge source '{knowledge_source}'; "
                "extracted as a knowledge reference for the Map stage, not modeled as a dialog node."
            )

        else:
            topic.unsupported_notes.append(
                f"Unrecognized action kind '{kind}' (id: {action.get('id')}); skipped, not translated."
            )

    return nodes


def _parse_topic_file(path: Path) -> tuple[Topic, list[str], list[RawKnowledgeRef]]:
    data = _load_yaml(path)
    topic = Topic(
        name=path.stem.removesuffix(".mcs"),
        trigger_phrases=data.get("trigger", {}).get("phrases", []),
    )

    if data.get("kind") != "AdaptiveDialog":
        topic.unsupported_notes.append(f"Unrecognized topic kind '{data.get('kind')}'; nodes not parsed.")
        return topic, [], []

    raw_tool_refs: list[str] = []
    raw_knowledge_refs: list[RawKnowledgeRef] = []
    begin_dialog = data.get("beginDialog", {})
    topic.nodes = _walk_actions(begin_dialog.get("actions", []), topic, raw_tool_refs, raw_knowledge_refs)
    return topic, raw_tool_refs, raw_knowledge_refs


def import_agent(clone_dir: Path) -> ImportResult:
    """Parse a `pac copilot clone` output directory into the canonical IR."""
    clone_dir = Path(clone_dir)
    root_files = list(clone_dir.glob("*.mcs.yaml"))
    if not root_files:
        raise FileNotFoundError(
            f"No *.mcs.yaml root file found in {clone_dir}; is this a `pac copilot clone` output directory?"
        )

    root = _load_yaml(root_files[0])
    agent_name = root.get("displayName") or root.get("schemaName") or clone_dir.name

    topics: list[Topic] = []
    raw_tool_refs: list[str] = []
    raw_knowledge_refs: list[RawKnowledgeRef] = []

    topics_dir = clone_dir / "topics"
    for topic_file in sorted(topics_dir.glob("*.mcs.yml")) if topics_dir.is_dir() else []:
        topic, tool_refs, knowledge_refs = _parse_topic_file(topic_file)
        raw_tool_refs.extend(tool_refs)
        raw_knowledge_refs.extend(knowledge_refs)
        topics.append(topic)

    agent = Agent(name=agent_name, source_platform=SOURCE_PLATFORM, topics=topics)

    return ImportResult(
        agent=agent,
        raw_tool_refs=raw_tool_refs,
        raw_knowledge_refs=raw_knowledge_refs,
        raw_connection_refs=[],
    )

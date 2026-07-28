from pathlib import Path

from agent_liftoff.connectors.copilot_studio.importer import detect_format
from agent_liftoff.connectors.copilot_studio.importer import import_agent as dispatch_import_agent
from agent_liftoff.connectors.copilot_studio.solution_importer import import_agent
from agent_liftoff.ir.schema import DialogNodeKind

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "agent_liftoff"
    / "connectors"
    / "copilot_studio"
    / "fixtures"
    / "sample_solution_agent"
)


def test_detect_format_recognizes_solution_export():
    assert detect_format(FIXTURE_DIR) == "solution"


def test_dispatcher_routes_solution_export_correctly():
    result = dispatch_import_agent(FIXTURE_DIR)
    assert result.agent.name == "IT Help Bot"


def test_import_reads_agent_name_from_bot_xml():
    result = import_agent(FIXTURE_DIR)
    assert result.agent.name == "IT Help Bot"
    assert result.agent.source_platform == "copilot-studio"


def test_import_extracts_gpt_instructions_as_existing_instructions():
    result = import_agent(FIXTURE_DIR)
    assert result.agent.existing_instructions is not None
    assert "IT Help Bot" in result.agent.existing_instructions
    assert "Escalate" in result.agent.existing_instructions
    assert result.agent.model_hint == "GPT5Chat"


def test_import_flags_system_topics_correctly():
    result = import_agent(FIXTURE_DIR)
    topics_by_name = {t.name: t for t in result.agent.topics}

    assert topics_by_name["Greeting"].is_system_topic is True
    assert topics_by_name["PasswordReset"].is_system_topic is False


def test_import_classifies_by_schemaname_not_display_name():
    """The display <name> is editable, human-decorated text and is NOT a
    reliable signal: a real export had schemaname '...topic.Search'
    displayed as 'Conversational boosting'. Classification must use the
    stable schemaname suffix instead. This fixture's Escalate topic is
    deliberately named 'Talk to a person' to exercise exactly that gap.
    """
    result = import_agent(FIXTURE_DIR)
    talk_to_a_person = next(t for t in result.agent.topics if t.name == "Talk to a person")
    assert talk_to_a_person.is_system_topic is True


def test_import_parses_trigger_queries_from_intent():
    result = import_agent(FIXTURE_DIR)
    greeting = next(t for t in result.agent.topics if t.name == "Greeting")
    assert "Hi" in greeting.trigger_phrases
    assert "Hello" in greeting.trigger_phrases


def test_import_parses_send_activity_text_list():
    result = import_agent(FIXTURE_DIR)
    greeting = next(t for t in result.agent.topics if t.name == "Greeting")
    message_nodes = [n for n in greeting.nodes if n.kind == DialogNodeKind.MESSAGE]
    assert len(message_nodes) == 1
    assert "how can I help" in message_nodes[0].text


def test_import_skips_cancel_all_dialogs_silently():
    """CancelAllDialogs is dialog plumbing, not lost agent behavior -- it
    should not produce a node or an unsupported_notes entry.
    """
    result = import_agent(FIXTURE_DIR)
    greeting = next(t for t in result.agent.topics if t.name == "Greeting")
    assert len(greeting.nodes) == 1  # just the SendActivity, no node for CancelAllDialogs
    assert not any("CancelAllDialogs" in n for n in greeting.unsupported_notes)


def test_import_parses_question_node_on_custom_topic():
    result = import_agent(FIXTURE_DIR)
    password_reset = next(t for t in result.agent.topics if t.name == "PasswordReset")
    question_nodes = [n for n in password_reset.nodes if n.kind == DialogNodeKind.QUESTION]
    assert len(question_nodes) == 1
    assert "username" in question_nodes[0].text.lower()


def test_import_extracts_knowledge_source_with_structured_metadata():
    result = import_agent(FIXTURE_DIR)
    assert len(result.raw_knowledge_refs) == 1
    ref = result.raw_knowledge_refs[0]
    assert ref.name == "ITPolicies"
    assert ref.source_kind == "SharePointSearchSource"
    assert "acme.sharepoint.com" in ref.detail


def test_import_extracts_welcome_message_from_conversation_start():
    """The ConversationStart topic's first message is the welcome message, and
    the {System.Bot.Name} placeholder is substituted with the real agent name.
    """
    result = import_agent(FIXTURE_DIR)
    assert result.agent.welcome_message is not None
    assert result.agent.welcome_message.startswith("Hello, I'm IT Help Bot.")
    assert "{System.Bot.Name}" not in result.agent.welcome_message


def test_import_reads_channels_and_content_moderation_from_configuration():
    result = import_agent(FIXTURE_DIR)
    assert result.agent.channels == ["msteams"]
    assert result.agent.content_moderation == "Low"


def test_import_extracts_web_search_capability_from_gpt_component():
    result = import_agent(FIXTURE_DIR)
    assert result.agent.web_search is True


def test_bare_search_and_summarize_does_not_create_phantom_knowledge():
    """A SearchAndSummarizeContent action with no explicit knowledgeSource is
    generative search over the agent's own knowledge, not a distinct source.
    It must not fabricate a knowledge ref (regression: it produced a phantom
    'search-content' knowledge base). Only the real ITPolicies source remains.
    """
    result = import_agent(FIXTURE_DIR)
    names = [ref.name for ref in result.raw_knowledge_refs]
    assert names == ["ITPolicies"]
    assert "search-content" not in names


def test_import_raises_clear_error_on_non_solution_dir(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError, match="solution export"):
        import_agent(tmp_path)


# ---------------------------------------------------------------------------
# componenttype 9 is not only topics
#
# Shapes below are transcribed from a real 3-agent Copilot Studio solution
# export ("Wheateardemo"): connector actions and connected agents ship as
# componenttype 9 alongside topics, discriminated only by `kind` inside the
# data sidecar. Treating them all as topics silently dropped every tool and
# every delegation edge, and fed empty pseudo-topics into Translate.
# ---------------------------------------------------------------------------

CONNECTOR_ACTION_DATA = """\
kind: TaskDialog
inputs:
  - kind: AutomaticTaskInput
    propertyName: tableType
    description: ServiceNow table name -- lowercase and singular.
  - kind: AutomaticTaskInput
    propertyName: sysid
    description: The record's 32-character hex sys_id.
modelDisplayName: Get Record
modelDescription: Gets a single ServiceNow record by its sys_id.
outputs:
  - propertyName: result
    name: result
action:
  kind: InvokeConnectorTaskAction
  connectionReference: bot_x.shared_service-now.17b56b0e
  operationId: GetRecord
outputMode: All
"""

CONNECTED_AGENT_DATA = """\
kind: TaskDialog
modelDisplayName: HR Agent
modelDescription: Answers questions about HR policy.
action:
  kind: InvokeConnectedAgentTaskAction
  botSchemaName: bot_hr
  historyType:
    kind: ConversationHistory
"""

GPT_DATA = """\
kind: GptComponentMetadata
instructions: You route questions to the right specialist.
"""


def _write_component(solution: Path, schemaname: str, component_type: int, name: str, data: str | None):
    """Write one botcomponent the way a real export lays it out. parentbotid is
    written empty on purpose -- that is what a real export contains, which is
    why ownership must be derived from the schemaname prefix instead.
    """
    comp = solution / "botcomponents" / schemaname
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "botcomponent.xml").write_text(
        f'<botcomponent schemaname="{schemaname}">'
        f"<componenttype>{component_type}</componenttype>"
        f"<name>{name}</name><parentbotid></parentbotid>"
        f"</botcomponent>"
    )
    if data is not None:
        (comp / "data").write_text(data)
    return comp


def _write_bot(solution: Path, schema: str, display_name: str):
    bot = solution / "bots" / schema
    bot.mkdir(parents=True, exist_ok=True)
    (bot / "bot.xml").write_text(f'<bot schemaname="{schema}"><name>{display_name}</name></bot>')


def _make_solution(tmp_path: Path, *, with_customizations: bool = True) -> Path:
    solution = tmp_path / "solution"
    solution.mkdir()
    (solution / "solution.xml").write_text("<ImportExportXml />")
    if with_customizations:
        (solution / "customizations.xml").write_text(
            "<ImportExportXml><connectionreferences>"
            '<connectionreference connectionreferencelogicalname="bot_x.shared_service-now.17b56b0e">'
            "<connectorid>/providers/Microsoft.PowerApps/apis/shared_service-now</connectorid>"
            "</connectionreference></connectionreferences></ImportExportXml>"
        )
    _write_bot(solution, "bot_x", "Router Agent")
    _write_component(solution, "bot_x.gpt.default", 15, "gpt", GPT_DATA)
    _write_component(solution, "bot_x.action.ServiceNow-GetRecord", 9, "ServiceNow - Get Record", CONNECTOR_ACTION_DATA)
    _write_component(solution, "bot_x.InvokeConnectedAgentTaskAction.HRAgent", 9, "HR Agent", CONNECTED_AGENT_DATA)
    return solution


def test_connector_task_dialog_becomes_a_tool_not_a_topic(tmp_path):
    result = import_agent(_make_solution(tmp_path))

    assert [t.name for t in result.agent.topics] == []
    assert len(result.raw_tools) == 1
    tool = result.raw_tools[0]
    assert tool.name == "Get Record"
    assert tool.kind == "connector"
    assert tool.operation_id == "GetRecord"
    assert tool.description.startswith("Gets a single ServiceNow record")


def test_connector_tool_keeps_its_input_descriptions(tmp_path):
    """The per-parameter descriptions are the matching surface for resolving
    this tool onto a target platform -- "GetRecord" alone is meaningless.
    """
    result = import_agent(_make_solution(tmp_path))
    tool = result.raw_tools[0]

    assert [p.name for p in tool.inputs] == ["tableType", "sysid"]
    assert "lowercase and singular" in tool.inputs[0].description
    assert [p.name for p in tool.outputs] == ["result"]


def test_connection_reference_resolves_to_a_connector_id(tmp_path):
    result = import_agent(_make_solution(tmp_path))

    assert result.raw_tools[0].connector_id == (
        "/providers/Microsoft.PowerApps/apis/shared_service-now"
    )


def test_missing_customizations_costs_the_connector_id_but_not_the_tool(tmp_path):
    """A bot slice may not carry customizations.xml; the tool must still
    survive, just without the connector identity resolved.
    """
    result = import_agent(_make_solution(tmp_path, with_customizations=False))

    assert len(result.raw_tools) == 1
    assert result.raw_tools[0].connector_id is None
    assert result.raw_tools[0].connection_reference == "bot_x.shared_service-now.17b56b0e"


def test_connected_agent_task_dialog_becomes_a_collaborator(tmp_path):
    result = import_agent(_make_solution(tmp_path))

    assert [c.ref for c in result.agent.collaborators] == ["bot_hr"]
    assert result.agent.topics == []


def test_unrecognized_task_action_is_noted_rather_than_dropped(tmp_path):
    solution = _make_solution(tmp_path)
    _write_component(
        solution,
        "bot_x.action.Mystery",
        9,
        "Mystery",
        "kind: TaskDialog\nmodelDisplayName: Mystery\naction:\n  kind: InvokeSomethingNew\n",
    )

    result = import_agent(solution)

    assert any("InvokeSomethingNew" in note for note in result.import_notes)


def test_file_knowledge_component_carries_the_document_itself(tmp_path):
    """componenttype 14 has no data sidecar -- the document lives in filedata/.
    Having the bytes is what makes this an upload rather than a TODO.
    """
    solution = _make_solution(tmp_path)
    comp = _write_component(solution, "bot_x.file.Handbook.pdf_Tiu", 14, "Handbook.pdf", None)
    (comp / "filedata").mkdir()
    (comp / "filedata" / "Handbook.pdf").write_bytes(b"%PDF-1.4 fake")

    result = import_agent(solution)

    assert len(result.raw_knowledge_refs) == 1
    knowledge = result.raw_knowledge_refs[0]
    assert knowledge.source_kind == "file"
    assert knowledge.file_path.name == "Handbook.pdf"
    assert knowledge.file_path.read_bytes().startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Multi-bot solutions
# ---------------------------------------------------------------------------

def _make_multi_bot_solution(tmp_path: Path) -> Path:
    solution = _make_solution(tmp_path)
    _write_bot(solution, "bot_hr", "HR Agent")
    _write_component(
        solution,
        "bot_hr.gpt.default",
        15,
        "gpt",
        "kind: GptComponentMetadata\ninstructions: You answer HR questions.\n",
    )
    return solution


def test_multi_bot_solution_refuses_to_silently_merge(tmp_path):
    """Regression: importing a 3-bot export merged all 43 topics into one
    agent and let each bot's GPT component overwrite the last.
    """
    import pytest

    with pytest.raises(ValueError, match="contains 2 bots"):
        import_agent(_make_multi_bot_solution(tmp_path))


def test_bot_schema_selects_one_bot_and_only_its_components(tmp_path):
    result = import_agent(_make_multi_bot_solution(tmp_path), bot_schema="bot_hr")

    assert result.agent.name == "HR Agent"
    assert "HR questions" in result.agent.existing_instructions
    # bot_x's tool must not leak into bot_hr.
    assert result.raw_tools == []
    assert result.agent.collaborators == []


def test_unknown_bot_schema_lists_what_is_available(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="bot_x"):
        import_agent(_make_multi_bot_solution(tmp_path), bot_schema="nope")


def test_import_workflow_rewrites_collaborators_to_agent_names(tmp_path):
    """Edges are stored by schema name (bot_hr) but agents are named by
    display name (HR Agent); unresolved refs wouldn't exist on the target.
    """
    from agent_liftoff.connectors.copilot_studio.solution_importer import import_workflow

    workflow, _ = import_workflow(_make_multi_bot_solution(tmp_path))

    router = workflow.by_name("Router Agent")
    assert [c.ref for c in router.collaborators] == ["HR Agent"]
    assert router.collaborators[0].review_required is False
    assert workflow.root == "Router Agent"
    # Leaf-first, so a collaborator exists before the agent referencing it.
    assert [a.name for a in workflow.migration_order()] == ["HR Agent", "Router Agent"]


def test_import_workflow_flags_an_edge_leaving_the_export(tmp_path):
    from agent_liftoff.connectors.copilot_studio.solution_importer import import_workflow

    workflow, _ = import_workflow(_make_solution(tmp_path))

    router = workflow.by_name("Router Agent")
    assert router.collaborators[0].review_required is True
    assert "not part of this export" in router.collaborators[0].notes

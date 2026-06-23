from pathlib import Path

from wheatear.connectors.copilot_studio.importer import detect_format
from wheatear.connectors.copilot_studio.importer import import_agent as dispatch_import_agent
from wheatear.connectors.copilot_studio.solution_importer import import_agent
from wheatear.ir.schema import DialogNodeKind

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "wheatear"
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


def test_import_raises_clear_error_on_non_solution_dir(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError, match="solution export"):
        import_agent(tmp_path)

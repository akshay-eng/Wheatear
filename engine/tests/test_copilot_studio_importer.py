from pathlib import Path

import pytest

from wheatear.connectors.copilot_studio.importer import import_agent
from wheatear.ir.schema import DialogNodeKind

FIXTURE_DIR = Path(__file__).parent.parent / "wheatear" / "connectors" / "copilot_studio" / "fixtures" / "sample_agent"


def test_import_reads_agent_name_from_root_file():
    result = import_agent(FIXTURE_DIR)
    assert result.agent.name == "Support Router"
    assert result.agent.source_platform == "copilot-studio"


def test_import_parses_both_topics():
    result = import_agent(FIXTURE_DIR)
    topic_names = {t.name for t in result.agent.topics}
    assert topic_names == {"greeting", "order_status"}


def test_import_parses_message_node():
    result = import_agent(FIXTURE_DIR)
    greeting = next(t for t in result.agent.topics if t.name == "greeting")
    assert greeting.trigger_phrases == ["hi", "hello", "hey there"]
    assert len(greeting.nodes) == 1
    assert greeting.nodes[0].kind == DialogNodeKind.MESSAGE
    assert "how can I help" in greeting.nodes[0].text


def test_import_parses_question_and_condition_nodes():
    result = import_agent(FIXTURE_DIR)
    order = next(t for t in result.agent.topics if t.name == "order_status")
    kinds = [n.kind for n in order.nodes]
    assert DialogNodeKind.QUESTION in kinds
    assert DialogNodeKind.CONDITION in kinds


def test_import_extracts_connector_as_raw_tool_ref_not_silently_dropped():
    result = import_agent(FIXTURE_DIR)
    assert "SalesforceOrderLookup" in result.raw_tool_refs

    order = next(t for t in result.agent.topics if t.name == "order_status")
    assert any("SalesforceOrderLookup" in note for note in order.unsupported_notes)


def test_import_extracts_knowledge_source_as_raw_knowledge_ref():
    result = import_agent(FIXTURE_DIR)
    assert any(ref.name == "ReturnsPolicyKB" for ref in result.raw_knowledge_refs)


def test_import_raises_clear_error_on_missing_root_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="pac copilot clone"):
        import_agent(tmp_path)

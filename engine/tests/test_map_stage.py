from pathlib import Path

from wheatear.connectors.copilot_studio.importer import import_agent
from wheatear.pipeline.map import KNOWN_TOOL_MAPPINGS, map_agent

FIXTURE_DIR = Path(__file__).parent.parent / "wheatear" / "connectors" / "copilot_studio" / "fixtures" / "sample_agent"


def test_map_flags_unknown_connector_for_review_not_a_guess():
    import_result = import_agent(FIXTURE_DIR)

    agent = map_agent(import_result)

    tool = next(t for t in agent.tools if t.source_ref == "SalesforceOrderLookup")
    assert tool.review_required is True
    assert tool.confidence == 0.0
    assert "No known Orchestrate equivalent" in tool.notes


def test_map_uses_known_mapping_table_when_present(monkeypatch):
    monkeypatch.setitem(KNOWN_TOOL_MAPPINGS, "SalesforceOrderLookup", "salesforce_lookup_order")
    import_result = import_agent(FIXTURE_DIR)

    agent = map_agent(import_result)

    tool = next(t for t in agent.tools if t.source_ref == "SalesforceOrderLookup")
    assert tool.ref == "salesforce_lookup_order"
    assert tool.review_required is False
    assert tool.confidence == 1.0


def test_map_passes_through_knowledge_refs():
    import_result = import_agent(FIXTURE_DIR)

    agent = map_agent(import_result)

    assert any(k.ref == "ReturnsPolicyKB" for k in agent.knowledge)


def test_map_flags_connector_backed_knowledge_source_for_review():
    """A SharePoint (or any externally-connected) knowledge source needs
    real re-ingestion into Orchestrate, not a reference copy -- Map must not
    treat it as a clean pass-through.
    """
    solution_fixture = (
        Path(__file__).parent.parent
        / "wheatear"
        / "connectors"
        / "copilot_studio"
        / "fixtures"
        / "sample_solution_agent"
    )
    import_result = import_agent(solution_fixture)

    agent = map_agent(import_result)

    knowledge = next(k for k in agent.knowledge if k.ref == "ITPolicies")
    assert knowledge.review_required is True
    assert "SharePointSearchSource" in knowledge.notes
    assert agent.needs_review is True


def test_map_never_touches_an_llm():
    """Map stage takes no LLM provider argument at all -- this is enforced
    by the function signature, not just convention.
    """
    import inspect

    sig = inspect.signature(map_agent)
    assert "llm" not in sig.parameters
    assert "provider" not in sig.parameters


def _fake_match(install_ref, name, confidence, member_tools=()):
    from dataclasses import dataclass, field

    @dataclass
    class _M:
        install_ref: str
        name: str
        confidence: float
        member_tools: tuple = field(default_factory=tuple)

    return _M(install_ref, name, confidence, tuple(member_tools))


def test_map_resolves_connector_via_catalog_resolver():
    import_result = import_agent(FIXTURE_DIR)

    def resolver(app, desc):
        if app == "SalesforceOrderLookup":
            return _fake_match("salesforce_get_order", "Get order in Salesforce", 1.0)
        return None

    agent = map_agent(import_result, connector_resolver=resolver)
    tool = next(t for t in agent.tools if t.source_ref == "SalesforceOrderLookup")
    assert tool.ref == "salesforce_get_order"
    assert tool.review_required is False  # single-tool, high confidence -> trusted


def test_map_flags_multi_tool_catalog_match_for_selection():
    import_result = import_agent(FIXTURE_DIR)

    def resolver(app, desc):
        return _fake_match("get_users_in_slack", "Get users in Slack", 0.9,
                           member_tools=("Get users in Slack", "Post message in Slack"))

    agent = map_agent(import_result, connector_resolver=resolver)
    tool = next(t for t in agent.tools if t.source_ref == "SalesforceOrderLookup")
    assert tool.review_required is True  # toolkit-level -> human picks operations
    assert len(tool.member_tools) == 2


def test_map_resolver_miss_falls_back_to_manual_flag():
    import_result = import_agent(FIXTURE_DIR)
    agent = map_agent(import_result, connector_resolver=lambda a, d: None)
    tool = next(t for t in agent.tools if t.source_ref == "SalesforceOrderLookup")
    assert tool.review_required is True
    assert tool.confidence == 0.0


def test_map_file_upload_knowledge_routes_to_upload_plan():
    from wheatear.connectors.base import ImportResult, RawKnowledgeRef
    from wheatear.ir.schema import Agent, IngestPlan

    ir = ImportResult(
        agent=Agent(name="a", source_platform="n8n"),
        raw_knowledge_refs=[RawKnowledgeRef(name="HR KB", source_kind="file_upload",
                                            detail="/path/*.pdf", is_file_upload=True)],
    )
    agent = map_agent(ir)
    kb = agent.knowledge[0]
    assert kb.ingest_plan == IngestPlan.UPLOAD
    assert kb.review_required is True

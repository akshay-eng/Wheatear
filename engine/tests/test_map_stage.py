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


# ---------------------------------------------------------------------------
# Power Platform connector operations
#
# Both prebuilt and custom connectors are OpenAPI underneath, so the bridge is
# a spec conversion rather than a hand-rebuild. Map records that intent and
# carries the signature forward; it does not yet resolve against a live target
# catalog, so these stay review_required.
# ---------------------------------------------------------------------------

def _connector_import(connector_id, **overrides):
    from wheatear.connectors.base import ImportResult, RawToolRef, ToolParam
    from wheatear.ir.schema import Agent

    raw = RawToolRef(
        name="Get Record",
        kind="connector",
        source_ref="ServiceNow-GetRecord",
        description="Gets a single ServiceNow record by its sys_id.",
        operation_id="GetRecord",
        inputs=[ToolParam(name="sysid", description="The 32-char hex sys_id.")],
        outputs=[ToolParam(name="result")],
        connector_id=connector_id,
        **overrides,
    )
    return ImportResult(
        agent=Agent(name="ITSM Agent", source_platform="copilot-studio"), raw_tools=[raw]
    )


def test_prebuilt_connector_is_mapped_as_an_openapi_bridge():
    from wheatear.ir.schema import BridgeStrategy, ToolKind

    agent = map_agent(
        _connector_import("/providers/Microsoft.PowerApps/apis/shared_service-now"), "orchestrate"
    )

    tool = agent.tools[0]
    assert tool.kind == ToolKind.CONNECTOR
    assert tool.bridge == BridgeStrategy.OPENAPI
    assert tool.review_required is True
    assert "Prebuilt" in tool.notes
    assert "shared_service-now" in tool.notes


def test_custom_connector_is_distinguished_from_a_prebuilt_one():
    """A custom connector's id carries the publisher prefix with the underscore
    percent-encoded (cr3ea-5f...). It matters because a custom connector's
    OpenAPI definition is downloadable from the source tenant.
    """
    from wheatear.ir.schema import ToolKind

    agent = map_agent(
        _connector_import("/providers/Microsoft.PowerApps/apis/shared_cr3ea-5fservice-20now-5f96f"),
        "orchestrate",
    )

    tool = agent.tools[0]
    assert tool.kind == ToolKind.CUSTOM_CONNECTOR
    assert "pac connector download" in tool.notes


def test_connector_tool_signature_survives_into_the_ir():
    """Whoever resolves this next -- a human or a matcher -- must not have to
    go back to the source export to find out what the tool took or did.
    """
    agent = map_agent(
        _connector_import("/providers/Microsoft.PowerApps/apis/shared_service-now"), "orchestrate"
    )

    tool = agent.tools[0]
    assert tool.operation_id == "GetRecord"
    assert tool.description.startswith("Gets a single ServiceNow record")
    assert [p.name for p in tool.inputs] == ["sysid"]
    assert "32-char hex" in tool.inputs[0].description
    assert tool.connector_id.endswith("shared_service-now")


def test_file_backed_knowledge_is_an_upload_not_a_reindex(tmp_path):
    """A document that shipped inside the export can actually be uploaded;
    a connector-backed source (SharePoint) needs real re-ingestion instead.
    """
    from wheatear.connectors.base import ImportResult, RawKnowledgeRef
    from wheatear.ir.schema import Agent, IngestPlan

    pdf = tmp_path / "Handbook.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    import_result = ImportResult(
        agent=Agent(name="HR Agent", source_platform="copilot-studio"),
        raw_knowledge_refs=[
            RawKnowledgeRef(name="Handbook.pdf", source_kind="file", file_path=pdf),
            RawKnowledgeRef(name="Policies", source_kind="SharePoint", detail="https://x/sites/hr"),
        ],
    )

    agent = map_agent(import_result, "orchestrate")

    uploaded = next(k for k in agent.knowledge if k.ref == "Handbook.pdf")
    assert uploaded.ingest_plan == IngestPlan.UPLOAD
    assert uploaded.file_path == str(pdf)
    assert "30MB" in uploaded.notes

    reindexed = next(k for k in agent.knowledge if k.ref == "Policies")
    assert reindexed.ingest_plan == IngestPlan.REINDEX_VECTOR
    assert reindexed.file_path is None

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

import yaml

from wheatear.connectors.orchestrate.exporter import export_agent
from wheatear.ir.schema import Agent, ConnectionRef, KnowledgeRef, ToolRef


def make_agent(**overrides) -> Agent:
    defaults = dict(name="support_router", source_platform="copilot-studio", instructions="Route tickets.")
    defaults.update(overrides)
    return Agent(**defaults)


def test_export_writes_agent_yaml_in_orchestrate_shape(tmp_path):
    agent = make_agent(tools=[ToolRef(ref="lookup_ticket", confidence=0.99)])

    result = export_agent(agent, tmp_path)

    assert result.agent_path.exists()
    spec = yaml.safe_load(result.agent_path.read_text())
    assert spec["spec_version"] == "v1"
    assert spec["kind"] == "native"
    assert spec["name"] == "support_router"
    assert spec["instructions"] == "Route tickets."
    assert spec["tools"] == ["lookup_ticket"]
    assert spec["collaborators"] == []


def test_export_never_autofills_connection_credentials(tmp_path):
    agent = make_agent(connections=[ConnectionRef(ref="salesforce_conn", auth_type="oauth2")])

    result = export_agent(agent, tmp_path)

    assert len(result.connection_paths) == 1
    spec = yaml.safe_load(result.connection_paths[0].read_text())
    assert spec["credentials"] == "REPLACE_ME"
    assert spec["auth_type"] == "oauth2"


def test_export_flags_low_confidence_and_review_required_items(tmp_path):
    agent = make_agent(
        translation_confidence=0.6,
        translation_notes=["Collapsed 3 branching conditions into one instruction."],
        tools=[ToolRef(ref="legacy_flow", confidence=0.3, review_required=True, notes="No 1:1 Orchestrate equivalent.")],
        connections=[ConnectionRef(ref="salesforce_conn", auth_type="oauth2")],
    )

    result = export_agent(agent, tmp_path)

    assert result.needs_review
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    types = {item["type"] for item in manifest["review_items"]}
    assert types == {"translation", "tool", "connection"}


def test_export_flags_review_required_knowledge_sources(tmp_path):
    """A SharePoint-backed (or any connector-backed) knowledge source needs
    re-ingestion, not a reference copy -- this must surface in the review
    manifest the same way tools/connections do.
    """
    agent = make_agent(
        knowledge=[
            KnowledgeRef(
                ref="HumanResources",
                review_required=True,
                notes="SharePointSearchSource source needs re-indexing into an Orchestrate knowledge base.",
            )
        ]
    )

    result = export_agent(agent, tmp_path)

    assert result.needs_review
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    knowledge_items = [item for item in manifest["review_items"] if item["type"] == "knowledge"]
    assert len(knowledge_items) == 1
    assert knowledge_items[0]["ref"] == "HumanResources"


def test_export_omits_review_manifest_when_nothing_needs_review(tmp_path):
    agent = make_agent(tools=[ToolRef(ref="lookup_ticket", confidence=1.0)])

    result = export_agent(agent, tmp_path)

    assert not result.needs_review
    assert result.review_manifest_path is None

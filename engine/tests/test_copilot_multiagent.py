"""Multi-agent Copilot solution extraction, against the managed demo export."""

from pathlib import Path

import pytest

from wheatear.connectors.copilot_studio import solution_importer as si
from wheatear.pipeline.map import map_agent

_CANDIDATES = [
    Path("/Users/akshay/Documents/AgentMigrate/migration_assets/Wheateardemo_1_0_0_1_managed"),
    Path("/Users/akshay/Documents/AgentMigrate/Wheateardemo_1_0_0_1_managed"),
]
DEMO = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

pytestmark = pytest.mark.skipif(
    not DEMO.exists(), reason="managed Copilot demo export not present"
)


def test_bundle_extracts_all_three_agents():
    bundle = si.import_bundle(DEMO)
    names = {a.name for a in bundle.workflow.agents}
    assert len(names) == 3
    assert bundle.workflow.root == "Supervisor-agent"


def test_supervisor_connected_agents_become_collaborators():
    bundle = si.import_bundle(DEMO)
    sup = bundle.workflow.by_name("Supervisor-agent")
    refs = {c.ref for c in sup.collaborators}
    # both connected agents resolved to real agents in the bundle (not flagged)
    assert refs == {"HR Agent", "ITSM Agent"}
    assert all(c.review_required is False for c in sup.collaborators)


def test_migration_order_is_leaf_first():
    bundle = si.import_bundle(DEMO)
    order = [a.name for a in bundle.workflow.migration_order()]
    assert order.index("HR Agent") < order.index("Supervisor-agent")
    assert order.index("ITSM Agent") < order.index("Supervisor-agent")


def test_servicenow_connector_actions_extracted_as_openapi_tools():
    # The ITSM bot's ServiceNow actions come through as structured connector
    # tools carrying the Power Platform connector id + operation, and Map turns
    # each into a review-required OpenAPI-bridge tool (connectors are OpenAPI
    # underneath, so the migration path is a spec conversion, not a rebuild).
    bundle = si.import_bundle(DEMO)
    itsm = next(r for r in bundle.results if r.agent.name == "ITSM Agent")
    connectors = [t for t in itsm.raw_tools if t.kind == "connector"]
    assert connectors, "expected ServiceNow connector actions on the ITSM agent"
    assert all("service-now" in (t.connector_id or "") for t in connectors)
    assert {t.operation_id for t in connectors} >= {"GetRecord", "GetRecords"}

    map_agent(itsm)
    tools = itsm.agent.tools
    assert tools and all(t.bridge.value == "openapi" for t in tools)
    assert all(t.review_required is True for t in tools)


def test_import_agent_requires_bot_schema_for_multi_bot():
    # The branch's contract: a multi-bot solution has no single "root" agent to
    # return, so import_agent asks the caller to name the bot rather than guess.
    with pytest.raises(ValueError, match="bot_schema|contains .* bots"):
        si.import_agent(DEMO)
    # Naming the bot returns exactly that one.
    result = si.import_agent(DEMO, bot_schema="crd07_Supervisoragent")
    assert result.agent.name == "Supervisor-agent"

"""Multi-agent Copilot solution extraction, against the managed demo export."""

from pathlib import Path

import pytest

from wheatear.connectors.copilot_studio import solution_importer as si
from wheatear.connectors.orchestrate.catalog import connector_resolver
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


def test_servicenow_connector_actions_extracted_and_catalog_matched():
    bundle = si.import_bundle(DEMO)
    itsm = next(r for r in bundle.results if r.agent.name == "ITSM Agent")
    assert "ServiceNow" in itsm.raw_tool_refs
    assert itsm.raw_connection_refs  # the shared ServiceNow connection
    # catalog match resolves ServiceNow to a real Orchestrate catalog tool
    map_agent(itsm, connector_resolver=connector_resolver())
    tool = itsm.agent.tools[0]
    assert tool.bridge.value == "mcp_catalog"
    assert tool.review_required is True  # toolkit-level -> human confirms ops


def test_single_agent_contract_returns_root():
    result = si.import_agent(DEMO)
    assert result.agent.name == "Supervisor-agent"

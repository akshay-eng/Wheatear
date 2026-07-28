"""n8n importer + graph tests, run against the real 3-workflow fixture bundle."""

from pathlib import Path

from agent_liftoff.connectors.n8n import graph, importer
from agent_liftoff.connectors.orchestrate.catalog import connector_resolver
from agent_liftoff.pipeline.map import map_agent

FIXTURES = Path(__file__).parent.parent / "agent_liftoff" / "connectors" / "n8n" / "fixtures"


# --- graph.py ---------------------------------------------------------------

def test_build_workflow_reverse_index():
    raw = {
        "id": "w1",
        "name": "WF",
        "nodes": [
            {"name": "Trig", "type": graph.CHAT_TRIGGER},
            {"name": "Ag", "type": graph.AGENT},
            {"name": "LM", "type": "@n8n/n8n-nodes-langchain.lmChatOpenAi"},
        ],
        "connections": {
            "Trig": {"main": [[{"node": "Ag", "type": "main", "index": 0}]]},
            "LM": {"ai_languageModel": [[{"node": "Ag", "type": "ai_languageModel", "index": 0}]]},
        },
    }
    wf = graph.build_workflow(raw)
    assert wf.is_class_a
    assert wf.has_chat_trigger
    models = wf.sources_into("Ag", graph.OUT_MODEL)
    assert [n["name"] for n in models] == ["LM"]


def test_class_b_workflow_has_no_agent():
    raw = {"id": "b", "name": "B", "nodes": [{"name": "S", "type": "n8n-nodes-base.slack"}], "connections": {}}
    assert graph.build_workflow(raw).is_class_a is False


def test_strip_expression_prefix():
    assert graph.strip_expression_prefix("=hello") == "hello"
    assert graph.strip_expression_prefix("plain") == "plain"
    assert graph.strip_expression_prefix(None) is None


# --- importer.py (against real fixtures) ------------------------------------

def test_detect_format_bundle_and_file():
    assert importer.detect_format(FIXTURES) == "n8n_bundle"
    assert importer.detect_format(FIXTURES / "supervisor.json") == "n8n_workflow"
    assert importer.detect_format(FIXTURES / "nonexistent.json") is None


def test_bundle_assembles_three_agents_leaf_first():
    bundle = importer.import_bundle(FIXTURES)
    wf = bundle.workflow
    assert wf.root == "Supervisor"
    assert {a.name for a in wf.agents} == {"Supervisor", "HR Agent", "Candidate Agent"}
    order = [a.name for a in wf.migration_order()]
    # collaborators emitted before the supervisor that references them
    assert order.index("HR Agent") < order.index("Supervisor")
    assert order.index("Candidate Agent") < order.index("Supervisor")


def test_supervisor_collaborators_resolved_not_flagged():
    bundle = importer.import_bundle(FIXTURES)
    sup = bundle.workflow.by_name("Supervisor")
    refs = {c.ref: c.review_required for c in sup.collaborators}
    assert refs == {"HR Agent": False, "Candidate Agent": False}


def test_candidate_agent_has_mcp_tool_with_url():
    bundle = importer.import_bundle(FIXTURES)
    cand = next(r for r in bundle.results if r.agent.name == "Candidate Agent")
    mcp = next(t for t in cand.raw_tools if t.kind == "mcp")
    assert mcp.mcp_server_url == "http://150.239.165.119:8010/sse"
    assert mcp.transport == "sse"


def test_hr_agent_has_file_upload_knowledge():
    bundle = importer.import_bundle(FIXTURES)
    hr = next(r for r in bundle.results if r.agent.name == "HR Agent")
    kb = hr.raw_knowledge_refs[0]
    assert kb.is_file_upload is True
    assert kb.detail.endswith(".pdf")


def test_model_hint_extracted_and_creds_deduped():
    bundle = importer.import_bundle(FIXTURES)
    for r in bundle.results:
        assert r.agent.model_hint == "models/gemini-2.5-pro"
        # one Gemini credential, deduped to a single ref
        assert r.raw_connection_refs == ["Google Gemini - Recruitment Demo"]


def test_full_map_pipeline_over_bundle():
    bundle = importer.import_bundle(FIXTURES)
    resolver = connector_resolver()
    for r in bundle.results:
        map_agent(r, connector_resolver=resolver)
    cand = bundle.workflow.by_name("Candidate Agent")
    mcp_tool = cand.tools[0]
    assert mcp_tool.bridge.value == "native_mcp"
    assert mcp_tool.review_required is False
    hr = bundle.workflow.by_name("HR Agent")
    assert hr.knowledge[0].ingest_plan.value == "upload"


def test_single_file_import_agent_contract():
    result = importer.import_agent(FIXTURES / "candidate-agent.json")
    assert result.agent.name == "Candidate Agent"
    assert any(t.kind == "mcp" for t in result.raw_tools)

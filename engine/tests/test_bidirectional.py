"""Bidirectional + multi-agent + error-path coverage for the IR hub.

The core guarantee: an agent can go Copilot -> Orchestrate -> Copilot (and the
reverse) through the same IR, with the high-value content (name, instructions)
surviving, and every unsupported/lossy thing surfaced for review rather than
dropped.
"""

import pytest
import yaml

from wheatear.connectors.copilot_studio import exporter as cp_exp
from wheatear.connectors.copilot_studio import importer as cp_imp
from wheatear.connectors.orchestrate import exporter as orch_exp
from wheatear.connectors.orchestrate import importer as orch_imp
from wheatear.connectors.registry import load_exporter, load_importer
from wheatear.errors import MapError, UnsupportedCorridorError
from wheatear.ir.schema import Agent, AgentRef, KnowledgeRef, ToolRef, Workflow
from wheatear.pipeline.map import map_agent


def _agent(**kw) -> Agent:
    defaults = dict(name="Helper Bee", source_platform="orchestrate", instructions="You are HR BOT.")
    defaults.update(kw)
    return Agent(**defaults)


# ---- Copilot exporter (the reverse direction) --------------------------------


def test_copilot_export_is_recognized_as_a_solution(tmp_path):
    result = cp_exp.export_agent(_agent(), tmp_path)
    assert cp_imp.detect_format(result.agent_path) == "solution"


def test_copilot_export_round_trips_name_and_instructions(tmp_path):
    original = _agent(name="Helper Bee", instructions="You are HR BOT for Choice Bank.")
    cp_exp.export_agent(original, tmp_path)

    reimported = cp_imp.import_agent(tmp_path)
    assert reimported.agent.name == "Helper Bee"
    assert "HR BOT" in reimported.agent.existing_instructions


def test_copilot_export_folds_guidelines_into_instructions(tmp_path):
    from wheatear.ir.schema import Guideline

    agent = _agent(
        instructions="Base prompt.",
        guidelines=[Guideline(condition="asked about law", action="decline and escalate")],
    )
    cp_exp.export_agent(agent, tmp_path)
    reimported = cp_imp.import_agent(tmp_path)
    # Copilot has no Guidelines slot, so they must land in the instructions text.
    assert "decline and escalate" in reimported.agent.existing_instructions


def test_copilot_export_flags_tools_and_knowledge_for_review(tmp_path):
    agent = _agent(
        tools=[ToolRef(ref="SNOWMCP:create_incident")],
        knowledge=[KnowledgeRef(ref="HRDocs")],
    )
    result = cp_exp.export_agent(agent, tmp_path)
    assert result.needs_review
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    types = {i["type"] for i in manifest["review_items"]}
    assert {"tool", "knowledge"} <= types


# ---- Full round trip Copilot -> Orchestrate -> Copilot -----------------------


def test_full_round_trip_preserves_core(tmp_path):
    o_out, c_out = tmp_path / "orch", tmp_path / "cp"

    start = _agent(name="Helper Bee", source_platform="copilot-studio", instructions="You are HR BOT.")
    orch_exp.export_agent(start, o_out)
    assert orch_imp.detect_format(o_out) == "orchestrate"

    mid = map_agent(orch_imp.import_agent(o_out), target_platform="copilot-studio")
    mid.instructions = mid.existing_instructions or mid.instructions
    cp_exp.export_agent(mid, c_out)

    end = cp_imp.import_agent(c_out)
    assert end.agent.name == "Helper Bee"
    assert "HR BOT" in end.agent.existing_instructions


# ---- Multi-agent -------------------------------------------------------------


def test_collaborators_survive_orchestrate_export_import(tmp_path):
    agent = _agent(collaborators=[AgentRef(ref="Billing"), AgentRef(ref="Support")])
    orch_exp.export_agent(agent, tmp_path)

    spec = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert spec["collaborators"] == ["Billing", "Support"]

    reimported = orch_imp.import_agent(tmp_path)
    assert [c.ref for c in reimported.agent.collaborators] == ["Billing", "Support"]


def test_workflow_migration_order_is_leaf_first():
    root = Agent(name="Router", source_platform="orchestrate",
                 collaborators=[AgentRef(ref="Billing"), AgentRef(ref="Support")])
    billing = Agent(name="Billing", source_platform="orchestrate")
    support = Agent(name="Support", source_platform="orchestrate",
                    collaborators=[AgentRef(ref="Billing")])
    wf = Workflow(source_platform="orchestrate", root="Router", agents=[root, billing, support])

    order = [a.name for a in wf.migration_order()]
    # every collaborator appears before the agent that references it
    assert order.index("Billing") < order.index("Support") < order.index("Router")


def test_workflow_migration_order_handles_cycles_without_hanging():
    a = Agent(name="A", source_platform="orchestrate", collaborators=[AgentRef(ref="B")])
    b = Agent(name="B", source_platform="orchestrate", collaborators=[AgentRef(ref="A")])
    wf = Workflow(source_platform="orchestrate", agents=[a, b])
    order = [x.name for x in wf.migration_order()]
    assert set(order) == {"A", "B"}  # terminates, emits both


# ---- Error paths -------------------------------------------------------------


def test_map_rejects_unknown_target_platform():
    from wheatear.connectors.base import ImportResult

    with pytest.raises(MapError):
        map_agent(ImportResult(agent=_agent()), target_platform="vertex-ai")


def test_registry_rejects_unknown_platform():
    with pytest.raises(UnsupportedCorridorError):
        load_importer("nonesuch")


def test_registry_and_both_exporters_are_loadable():
    assert load_importer("copilot-studio") is not None
    assert load_importer("orchestrate") is not None
    assert load_exporter("copilot-studio") is not None
    assert load_exporter("orchestrate") is not None

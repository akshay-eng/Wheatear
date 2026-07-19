"""Multi-agent collaborator-graph discovery and leaf-first ordering."""

from wheatear.ir.schema import Agent, AgentRef
from wheatear.workflow import assemble_workflow, reachable_ids


def _agent(name, collaborators=()):
    return Agent(
        name=name,
        source_platform="orchestrate",
        collaborators=[AgentRef(ref=c) for c in collaborators],
    )


# ---- reachable_ids (the transitive closure a discovery walk performs) --------


def test_reachable_pulls_in_transitive_collaborators():
    graph = {"Router": ["Billing", "Support"], "Support": ["Billing"], "Billing": []}
    assert set(reachable_ids(["Router"], lambda n: graph.get(n, []))) == {"Router", "Billing", "Support"}


def test_reachable_is_stable_first_seen_order():
    graph = {"Root": ["A", "B"], "A": ["C"], "B": [], "C": []}
    assert reachable_ids(["Root"], lambda n: graph[n]) == ["Root", "A", "B", "C"]


def test_reachable_terminates_on_cycles():
    graph = {"A": ["B"], "B": ["A"]}
    assert set(reachable_ids(["A"], lambda n: graph[n])) == {"A", "B"}


def test_reachable_seed_only_when_no_neighbors():
    assert reachable_ids(["Solo"], lambda n: []) == ["Solo"]


# ---- assemble_workflow -------------------------------------------------------


def test_workflow_orders_collaborators_before_callers():
    wf = assemble_workflow(
        [_agent("Router", ["Billing", "Support"]), _agent("Support", ["Billing"]), _agent("Billing")],
        source_platform="orchestrate",
    )
    order = [a.name for a in wf.migration_order()]
    assert order.index("Billing") < order.index("Support") < order.index("Router")


def test_workflow_flags_dangling_collaborator():
    # Router references "Ghost", which was not part of the migration.
    wf = assemble_workflow([_agent("Router", ["Ghost"])], source_platform="orchestrate")
    router = wf.by_name("Router")
    assert router.collaborators[0].review_required is True
    assert "Ghost" in router.collaborators[0].notes


def test_workflow_does_not_flag_present_collaborator():
    wf = assemble_workflow(
        [_agent("Router", ["Billing"]), _agent("Billing")], source_platform="orchestrate"
    )
    assert wf.by_name("Router").collaborators[0].review_required is False

"""The three post-migration documents.

The property worth protecting is that they disagree with each other only in
detail, never in fact: the executive summary must not say "no follow-up work"
while the engineer's report lists blocking steps, and the evaluation must not
invent a test for a tool the agent did not actually get.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wheatear import reporting
from wheatear.reporting import (
    AgentFact,
    MigrationFacts,
    ToolFact,
    render_evaluation,
    render_report,
    render_summary,
)


@dataclass
class FakeDeploy:
    name: str
    ok: bool = True
    tools: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    collaborators: list = field(default_factory=list)
    description: str = ""
    validation: str = ""
    error: str | None = None
    agent_id: str = "abc123"


def facts(**kw) -> MigrationFacts:
    base = MigrationFacts(source_platform="n8n")
    for key, value in kw.items():
        setattr(base, key, value)
    return base


# --------------------------------------------------------------------------- #
# The documents agree with each other
# --------------------------------------------------------------------------- #

def test_summary_does_not_claim_completeness_when_steps_remain():
    f = facts(
        agents=[AgentFact(name="ITSM", deployed=True)],
        manual_steps=[("Install servicenow", "", "console", True)],
    )
    summary = render_summary(f)
    assert "No follow-up work is required" not in summary
    assert "still need attention" in summary


def test_summary_says_complete_only_when_it_is():
    f = facts(agents=[AgentFact(name="ITSM", deployed=True)])
    assert "No follow-up work is required" in render_summary(f)


def test_a_failed_agent_is_not_counted_as_migrated():
    f = facts(agents=[AgentFact(name="A", deployed=True), AgentFact(name="B", deployed=False)])
    assert "**1 of 2**" in render_summary(f)
    assert "| B | No |" in render_summary(f)


# --------------------------------------------------------------------------- #
# The evaluation is generated from what actually landed
# --------------------------------------------------------------------------- #

def test_evaluation_only_tests_tools_the_agent_actually_has():
    f = facts(
        agents=[
            AgentFact(
                name="ITSM",
                deployed=True,
                tools=[ToolFact(name="get_record", parameters=["table", "recordNumber"])],
            )
        ]
    )
    body = render_evaluation(f)
    assert "get_record" in body
    assert "list_records" not in body
    # The parameters make the instruction concrete rather than generic.
    assert "table, recordNumber" in body


def test_an_agent_with_no_capability_is_called_out_not_skipped():
    """A silently empty section reads as 'nothing to check here'."""
    f = facts(agents=[AgentFact(name="HR", deployed=True)])
    body = render_evaluation(f)
    assert "migrated with no tools and no delegates" in body


def test_a_supervisor_gets_a_delegation_check():
    f = facts(agents=[AgentFact(name="Sup", deployed=True, collaborators=["HR", "ITSM"])])
    body = render_evaluation(f)
    assert "HR or ITSM" in body


def test_known_gaps_are_listed_as_expected_failures():
    f = facts(
        agents=[AgentFact(name="ITSM", deployed=True, tools=[ToolFact(name="get_record")])],
        manual_steps=[("Install servicenow_get_record", "", "console", True)],
    )
    body = render_evaluation(f)
    assert "expect these to fail" in body
    assert "Install servicenow_get_record" in body


def test_an_undeployed_agent_has_nothing_to_evaluate():
    f = facts(agents=[AgentFact(name="X", deployed=False, tools=[ToolFact(name="get_record")])])
    assert "Not deployed" in render_evaluation(f)


# --------------------------------------------------------------------------- #
# The engineer's report keeps the identifiers
# --------------------------------------------------------------------------- #

def test_report_keeps_ids_and_endpoints_that_the_summary_omits():
    f = facts(
        target_instance="https://api.example.com/instances/xyz",
        agents=[
            AgentFact(
                name="ITSM",
                deployed=True,
                agent_id="9d5c5c07",
                tools=[
                    ToolFact(
                        name="get_record",
                        origin="rebuilt from an HTTP request tool",
                        detail="GET https://x.service-now.com/api/now/table/{table}",
                        parameters=["table"],
                    )
                ],
            )
        ],
    )
    report = render_report(f)
    assert "9d5c5c07" in report and "service-now.com" in report
    summary = render_summary(f)
    assert "9d5c5c07" not in summary and "service-now.com" not in summary


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #

def test_deploy_reports_flatten_with_their_warnings_as_manual_steps():
    reports = [
        FakeDeploy(
            name="ITSM",
            tools=["get_record"],
            validation="tools ✓ | tool 'x' not auto-provisioned",
        )
    ]
    f = reporting.facts_from_deploy_reports(reports, source_platform="n8n")
    assert f.agents[0].warnings == ["tool 'x' not auto-provisioned"]
    assert f.manual_steps and f.manual_steps[0][0] == "tool 'x' not auto-provisioned"
    # And the summary must therefore not claim completeness.
    assert "No follow-up work is required" not in render_summary(f)


def test_mcp_tools_are_labelled_by_their_toolkit():
    f = reporting.facts_from_deploy_reports([FakeDeploy(name="C", tools=["MCP_Client_Tool:run_sql"])])
    assert f.agents[0].tools[0].origin == "MCP toolkit"


def test_write_all_produces_three_named_files(tmp_path):
    written = reporting.write_all(facts(agents=[AgentFact(name="A", deployed=True)]), tmp_path)
    assert {p.name for p in written} == {
        reporting.REPORT_FILE,
        reporting.SUMMARY_FILE,
        reporting.EVALUATION_FILE,
    }
    assert all(p.read_text().strip() for p in written)

"""What a migration hands back, and what it says is left to do.

The manual steps are the part worth testing hardest. A migrated agent that is
missing a tool is not a failure anybody can see from the outside -- it imports,
it runs, and it quietly cannot do one of the jobs it used to. The only thing
standing between that and a person fixing it is this list being right.
"""

from __future__ import annotations

from pathlib import Path

from wheatear.connectors.copilot_studio.mcp_scan import McpServer
from wheatear.connectors.orchestrate.connections import AppConnection
from wheatear.connectors.orchestrate.mcp_sync import plan_servers
from wheatear.foundry.store import FoundryStore
from wheatear.ir.schema import Agent, AgentRef, BridgeStrategy, ToolRef
from wheatear.pipeline.solution_migration import (
    adapters_ready,
    pending_installs,
    still_missing,
    free_name,
    install_steps,
    login_steps,
    select_agents,
    slug,
    toolkit_steps,
)


def agent(name: str, tools: list[ToolRef] | None = None, collaborators: list[str] | None = None):
    return Agent(
        name=name,
        source_platform="copilot-studio",
        tools=tools or [],
        collaborators=[AgentRef(ref=c) for c in (collaborators or [])],
    )


def uninstalled(source: str, title: str, install_ref: str, connections: list[str] | None = None):
    """A source tool matched to a catalog entry nobody has installed."""
    return ToolRef(
        ref=install_ref,
        source_ref=source,
        confidence=0.9,
        review_required=True,
        bridge=BridgeStrategy.CATALOG_INSTALL,
        catalog_title=title,
        catalog_install_ref=install_ref,
        catalog_connections=connections or [],
    )


def on_fallback(source: str, running: str, title: str, install_ref: str):
    """A source tool running on a lesser installed tool while a better one waits."""
    return ToolRef(
        ref=running,
        source_ref=source,
        confidence=0.8,
        review_required=True,
        bridge=BridgeStrategy.MCP_CATALOG,
        catalog_title=title,
        catalog_install_ref=install_ref,
    )


def resolved(ref: str) -> ToolRef:
    return ToolRef(ref=ref, source_ref=ref, confidence=1.0, bridge=BridgeStrategy.MCP_CATALOG)


# ----------------------------------------------------------------------
# Tools somebody has to install
# ----------------------------------------------------------------------


def test_a_tool_that_did_not_land_is_a_required_install_step():
    one = agent(
        "ITSM_Agent",
        [uninstalled("ServiceNow-GetRecord", "Get Records in ServiceNow", "get_records")],
    )

    steps = install_steps([("k", one)])

    assert len(steps) == 1
    step = steps[0]
    assert step.blocking is True
    assert step.kind == "install-tool"
    # Both names have to appear: the catalog is searched by the human title,
    # and the agent will reference the install ref.
    assert "Get Records in ServiceNow" in step.title
    assert "get_records" in step.detail
    assert step.agents == ["ITSM_Agent"]
    assert "catalog" in step.where.lower()


def test_the_connection_the_catalog_declares_is_named_in_the_step():
    one = agent(
        "ITSM_Agent",
        [
            uninstalled(
                "ServiceNow-GetRecord",
                "Get Records in ServiceNow",
                "get_records",
                connections=["servicenow_oauth2"],
            )
        ],
    )

    assert "servicenow_oauth2" in install_steps([("k", one)])[0].detail


def test_a_tool_running_on_an_installed_stand_in_is_only_an_improvement():
    """Not blocking: the agent can do the job today, just less well."""
    one = agent(
        "ITSM_Agent",
        [on_fallback("ServiceNow-GetRecord", "SNOWMCPALL:get_record", "Get Records", "get_records")],
    )

    step = install_steps([("k", one)])[0]

    assert step.blocking is False
    assert "SNOWMCPALL:get_record" in step.detail


def test_one_tool_wanted_by_two_agents_is_one_step_naming_both():
    """Installing `get_records` once serves both; two rows would read as two jobs."""
    tool = lambda: uninstalled("ServiceNow-GetRecord", "Get Records in ServiceNow", "get_records")
    agents = [("a", agent("ITSM_Agent", [tool()])), ("b", agent("HR_Agent", [tool()]))]

    steps = install_steps(agents)

    assert len(steps) == 1
    assert steps[0].agents == ["ITSM_Agent", "HR_Agent"]


def test_a_step_stays_required_if_any_agent_lost_the_capability():
    """One agent limping along on a stand-in does not excuse the one that has nothing."""
    agents = [
        (
            "a",
            agent(
                "HR_Agent",
                [on_fallback("GetRecord", "SNOWMCPALL:get_record", "Get Records", "get_records")],
            ),
        ),
        ("b", agent("ITSM_Agent", [uninstalled("GetRecord", "Get Records", "get_records")])),
    ]

    assert install_steps(agents)[0].blocking is True


def test_tools_that_are_already_installed_leave_nothing_to_do():
    one = agent("ITSM_Agent", [resolved("get_records"), resolved("create_incident")])

    assert install_steps([("k", one)]) == []


# ----------------------------------------------------------------------
# MCP servers
# ----------------------------------------------------------------------


def test_an_mcp_server_the_target_already_points_at_needs_no_step():
    """Reuse means leave it alone -- re-adding it would give the tenant two
    toolkits for one endpoint and no way to tell which an agent is using."""
    server = McpServer(
        name="snow", url="http://10.0.0.4:8000/sse", protocol="mcp-streamable-1.0",
        transport="streamable_http",
    )
    toolkits = [{"name": "snow", "mcp": {"server_url": "http://10.0.0.4:8000/"}}]

    assert toolkit_steps(plan_servers([server], toolkits)) == []


def test_an_unknown_mcp_server_becomes_a_step_carrying_the_exact_command():
    server = McpServer(
        name="snow", url="http://10.0.0.4:8000/sse", protocol="mcp-streamable-1.0",
        transport="streamable_http",
    )

    step = toolkit_steps(plan_servers([server], []))[0]

    assert step.kind == "add-toolkit"
    assert step.blocking is True
    assert step.command.startswith("orchestrate toolkits add")
    assert "http://10.0.0.4:8000/sse" in step.command
    assert "credentials do not migrate" in step.detail.lower()


def test_a_toolkit_name_clash_is_reported_rather_than_repointed():
    server = McpServer(
        name="snow", url="http://10.0.0.4:8000/sse", protocol="mcp-streamable-1.0",
        transport="streamable_http",
    )
    toolkits = [{"name": "snow", "mcp": {"server_url": "http://192.168.1.9:8000/sse"}}]

    step = toolkit_steps(plan_servers([server], toolkits))[0]

    assert step.blocking is True
    assert step.command is None  # nothing safe to run: a person has to decide


# ----------------------------------------------------------------------
# Sign-in
# ----------------------------------------------------------------------


def connection(app_id: str, preference: str, configured=True, entered=True) -> AppConnection:
    return AppConnection(
        app_id=app_id,
        connection_id="c1",
        environment="draft",
        security_scheme="oauth2",
        preference=preference,
        server_url="https://dev1.service-now.com/",
        is_configured=configured,
        credentials_entered=entered,
    )


def test_a_member_connection_leaves_nothing_to_configure():
    """The working case: the agent asks each user to sign in and calls as them."""
    one = agent("ITSM_Agent", [resolved("servicenow_get_records")])

    assert login_steps([("k", one)], [connection("servicenow_oauth2", "member")]) == []


def test_a_shared_team_credential_is_flagged_but_does_not_block():
    one = agent("ITSM_Agent", [resolved("servicenow_get_records")])

    step = login_steps([("k", one)], [connection("servicenow_oauth2", "team")])[0]

    assert step.blocking is False
    assert "member" in step.command
    assert "shared team credential" in step.detail


def test_a_tool_with_no_connection_at_all_blocks():
    """It imports cleanly and fails on the first call -- the failure worth naming."""
    one = agent("ITSM_Agent", [resolved("servicenow_get_records")])

    step = login_steps([("k", one)], [])[0]

    assert step.blocking is True
    assert "first call" in step.detail


def test_a_dropped_tool_raises_no_sign_in_question():
    """It is not on the agent, so nothing will ever call it."""
    one = agent("ITSM_Agent", [uninstalled("GetRecord", "Get Records", "servicenow_get_records")])

    assert login_steps([("k", one)], []) == []


# ----------------------------------------------------------------------
# Choosing which agents to migrate
# ----------------------------------------------------------------------


def test_selecting_a_supervisor_pulls_in_the_agents_it_delegates_to():
    """An Orchestrate agent naming a collaborator that does not exist fails its
    own import, so picking a supervisor alone is a request for a broken agent."""
    agents = [
        ("sup", agent("Supervisor", collaborators=["hr", "itsm"])),
        ("hr", agent("HR_Agent")),
        ("itsm", agent("ITSM_Agent")),
    ]

    chosen, pulled = select_agents(agents, {"sup"})

    assert [key for key, _ in chosen] == ["sup", "hr", "itsm"]
    assert sorted(pulled) == ["HR_Agent", "ITSM_Agent"]


def test_a_leaf_agent_drags_nothing_along():
    agents = [("sup", agent("Supervisor", collaborators=["hr"])), ("hr", agent("HR_Agent"))]

    chosen, pulled = select_agents(agents, {"hr"})

    assert [key for key, _ in chosen] == ["hr"]
    assert pulled == []


def test_agents_can_be_chosen_by_display_name_too():
    agents = [("sup", agent("Supervisor", collaborators=["hr"])), ("hr", agent("HR_Agent"))]

    chosen, _ = select_agents(agents, {"Supervisor"})

    assert [key for key, _ in chosen] == ["sup", "hr"]


def test_no_selection_means_the_whole_solution():
    agents = [("sup", agent("Supervisor")), ("hr", agent("HR_Agent"))]

    chosen, pulled = select_agents(agents, None)

    assert chosen == agents
    assert pulled == []


def test_a_delegation_cycle_terminates():
    agents = [("a", agent("A", collaborators=["b"])), ("b", agent("B", collaborators=["a"]))]

    chosen, _ = select_agents(agents, {"a"})

    assert sorted(key for key, _ in chosen) == ["a", "b"]


# ----------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------


def test_names_are_made_orchestrate_legal():
    assert slug("ITSM Agent") == "ITSM_Agent"
    assert slug("acme_Candidate.agent-v2") == "acme_Candidate_agent_v2"
    assert slug("--HR--") == "HR"


def test_a_taken_name_is_numbered_rather_than_overwritten():
    """The ADK's import updates silently on a name match, so an agent somebody
    else built would be replaced without a word."""
    assert free_name("ITSM_Agent", set()) == "ITSM_Agent"
    assert free_name("ITSM_Agent", {"ITSM_Agent"}) == "ITSM_Agent_2"
    assert free_name("ITSM_Agent", {"ITSM_Agent", "ITSM_Agent_2"}) == "ITSM_Agent_3"


# ----------------------------------------------------------------------
# Readiness
# ----------------------------------------------------------------------


def test_a_fresh_machine_is_ready_from_the_shipped_adapters(tmp_path: Path):
    """The point of shipping a build. A store that has never been probed picks
    up the adapters that travel with Wheatear and can migrate immediately --
    no probe, no model call, no container.
    """
    ready, reason = adapters_ready(FoundryStore(tmp_path))

    assert ready is True
    assert "cached adapter" in reason
    # They were installed into the local store, not read from assets each time.
    assert list(tmp_path.rglob("artifact.json"))


def test_a_machine_with_no_shipped_assets_says_what_to_run(tmp_path, monkeypatch):
    """Checked up front: a missing adapter stops the first stage outright, and
    finding out after the user picked an environment and four agents is worse."""
    from wheatear.pipeline import solution_migration

    monkeypatch.setattr(solution_migration, "ensure_shipped", lambda store, report=None: 0)

    ready, reason = adapters_ready(FoundryStore(tmp_path))

    assert ready is False
    assert "foundry corridor" in reason


def test_a_broken_assets_tree_is_reported_rather_than_looking_empty(tmp_path):
    """"Nothing shipped yet" and "what shipped is broken" are different, and
    only one of them is a bug in this repository. The first version returned 0
    on any exception, so a corpus that failed to validate was indistinguishable
    from an assets tree that was not there."""
    from wheatear.pipeline.solution_migration import ensure_shipped

    said = []
    broken = tmp_path / "assets"
    (broken / "copilot-studio" / "corpora").mkdir(parents=True)
    (broken / "copilot-studio" / "corpora" / "x.json").write_text("{not json")

    import wheatear.assets as assets_mod

    original = assets_mod.ASSETS
    try:
        assets_mod.ASSETS = broken
        installed = ensure_shipped(FoundryStore(tmp_path / "store"), said.append)
    finally:
        assets_mod.ASSETS = original

    assert installed == 0
    assert any("could not be loaded" in event.text for event in said)


# ----------------------------------------------------------------------
# Waiting for a person to install what is missing
# ----------------------------------------------------------------------


def test_a_dropped_tool_is_pending_and_names_the_agents_waiting_for_it():
    agents = [
        ("a", agent("ITSM_Agent", [uninstalled("GetRecord", "Get Records", "get_records")])),
        ("b", agent("HR_Agent", [uninstalled("ListRecords", "Get Records", "get_records")])),
    ]

    pending = pending_installs(agents)

    assert len(pending) == 1
    assert pending[0].install_ref == "get_records"
    assert pending[0].agents == ["ITSM_Agent", "HR_Agent"]


def test_a_tool_running_on_a_stand_in_is_not_something_to_wait_for():
    """The agent can do the job today. Blocking an operator at their terminal
    over an improvement is not what waiting is for."""
    one = agent(
        "ITSM_Agent",
        [on_fallback("GetRecord", "SNOWMCPALL:get_record", "Get Records", "get_records")],
    )

    assert pending_installs([("k", one)]) == []


def test_nothing_is_pending_when_every_tool_landed():
    one = agent("ITSM_Agent", [resolved("get_records_568d4")])

    assert pending_installs([("k", one)]) == []


def test_the_watcher_recognises_the_suffixed_name_the_install_actually_creates():
    """The bug this would otherwise have: `get_records` never appears under
    that name, so an equality check would wait forever."""
    from wheatear.pipeline.solution_migration import PendingInstall

    pending = [PendingInstall("get_records", "Get Records in ServiceNow", ["ITSM_Agent"])]

    assert still_missing(pending, {"send_email", "create_ticket"}) == pending
    assert still_missing(pending, {"send_email", "get_records_568d4"}) == []
    assert still_missing(pending, {"get_records"}) == []


def test_a_similarly_named_tool_does_not_end_the_wait():
    from wheatear.pipeline.solution_migration import PendingInstall

    pending = [PendingInstall("get_records", "Get Records in ServiceNow", ["ITSM_Agent"])]

    assert still_missing(pending, {"get_record_abc12", "list_records_99999"}) == pending


def test_a_failed_poll_reads_as_nothing_installed_rather_than_raising():
    """A dropped connection mid-wait is a retry, not the end of the migration."""
    from wheatear.pipeline.solution_migration import installed_tool_names

    class Broken:
        def list_all_tools(self):
            raise ConnectionError("network went away")

    assert installed_tool_names(Broken()) == set()

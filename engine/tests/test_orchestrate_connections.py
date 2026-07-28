"""Whether the target can actually authenticate to the system a tool talks to.

Agent Liftoff never moves credentials, so the only useful thing it can do is read
what the target already has and say plainly what is missing. A migrated tool
with no connection behind it imports cleanly and fails on the first call.
"""

import pytest

from agent_liftoff.connectors.orchestrate.connections import AppConnection, matching


def _conn(app_id, **overrides):
    defaults = dict(
        app_id=app_id,
        connection_id="c1",
        environment="draft",
        security_scheme="oauth2",
        preference="member",
        server_url="https://dev.service-now.com/",
        is_configured=True,
        credentials_entered=False,
    )
    defaults.update(overrides)
    return AppConnection(**defaults)


def test_a_member_connection_is_usable_without_a_stored_secret():
    """The user supplies one when the agent prompts them, which is the whole
    point of member credentials.
    """
    connection = _conn("servicenow_oauth2")
    assert connection.prompts_the_user
    assert connection.usable


def test_a_team_connection_with_no_secret_is_not_usable():
    """Nobody will be prompted, so the first call has nothing to authenticate
    with -- and it fails at runtime, long after the import looked fine.
    """
    connection = _conn("servicenow_shared", preference="team", credentials_entered=False)
    assert not connection.prompts_the_user
    assert not connection.usable


def test_a_team_connection_with_a_secret_is_usable():
    assert _conn("servicenow_shared", preference="team", credentials_entered=True).usable


def test_an_unconfigured_connection_is_never_usable():
    assert not _conn("servicenow", is_configured=False, preference="member").usable


def test_snow_and_servicenow_are_matched_as_one_system():
    """A tool called `SNOWMCPALL:get_record` and a connection called
    `Contoso_SNOW_PDI` are the same system under two names; without this a
    ServiceNow tool matches no ServiceNow connection at all.
    """
    connections = [_conn("Contoso_SNOW_PDI"), _conn("jira_key_value")]
    found = matching(connections, "SNOWMCPALL:get_record ServiceNow-GetRecord")
    assert [c.app_id for c in found] == ["Contoso_SNOW_PDI"]


def test_a_tool_with_no_plausible_connection_matches_nothing():
    assert matching([_conn("jira_key_value")], "SNOWMCPALL:get_record") == []


def test_the_summary_says_who_signs_in():
    assert "each user signs in themselves" in _conn("a").summary()
    assert "one shared credential" in _conn("a", preference="team").summary()
    assert "not configured" in _conn("a", is_configured=False).summary()


# ----------------------------------------------------------------------
# What a tool actually uses, versus what merely shares its name
# ----------------------------------------------------------------------


def test_a_tool_that_declares_no_connection_says_so():
    """The case that produced a wrong answer once: `SNOWMCPALL:get_record`
    shares every token with `servicenow_oauth2_auth_code_...` and binds to
    none of them. The MCP server behind it holds its own credentials, so
    nothing prompts anybody to sign in.
    """
    from agent_liftoff.connectors.orchestrate.connections import bound_connection

    binding = bound_connection(
        {
            "name": "SNOWMCPALL:get_record",
            "binding": {"mcp": {"server_url": "http://150.239.165.119:8000/sse",
                                "connections": {}}},
        }
    )
    assert not binding.declared
    assert binding.server_url == "http://150.239.165.119:8000/sse"


def test_a_declared_connection_is_read_from_the_binding():
    from agent_liftoff.connectors.orchestrate.connections import bound_connection

    # Real shape, captured from a live tool record: {app_id: connection_id}.
    # The fixture this replaced had them the other way round -- invented rather
    # than captured -- so it agreed with the bug instead of catching it.
    binding = bound_connection(
        {"binding": {"openapi": {"connections": {"servicenow_oauth2": "conn-uuid-1"}}}}
    )
    assert binding.declared
    assert binding.app_ids == ("servicenow_oauth2",)
    assert binding.connection_ids == ("conn-uuid-1",)


def test_a_name_match_is_never_mistaken_for_a_binding():
    """`matching` answers "might be related"; `bound_connection` answers "is
    used". Conflating them is what turned a guess into a stated fact.
    """
    from agent_liftoff.connectors.orchestrate.connections import bound_connection

    record = {"name": "SNOWMCPALL:get_record", "binding": {"mcp": {"connections": {}}}}
    suggestions = matching([_conn("servicenow_oauth2_auth_code_ibm")], record["name"])
    assert suggestions, "the name match still exists as a suggestion"
    assert not bound_connection(record).declared, "but it is not what the tool uses"


def test_a_binding_maps_app_ids_to_connection_ids_and_they_are_not_interchangeable():
    """The bug this exists to prevent, seen live.

    A tool's binding is `{"servicenow_ibm_a1b2c3d4": "44444444-...-4444"}` --
    app id to connection id. Reading the values handed callers a UUID where an
    app id was expected, and the caller that creates connections duly created
    one *named* after a UUID, which then failed to configure with a tenant
    outbound-call policy error naming the pasted credential.
    """
    from agent_liftoff.connectors.orchestrate.connections import bound_connection

    record = {
        "name": "get_records_ae899",
        "binding": {
            "python": {
                "function": "agent_ready_tools.tools.IT.servicenow.get_records:get_records",
                "connections": {"servicenow_ibm_a1b2c3d4": "44444444-4444-4444-4444-444444444444"},
            }
        },
    }

    binding = bound_connection(record)

    assert binding.app_ids == ("servicenow_ibm_a1b2c3d4",)
    assert binding.connection_ids == ("44444444-4444-4444-4444-444444444444",)
    assert binding.declared is True


def test_a_tool_binding_no_connection_still_declares_nothing():
    """Unchanged behaviour: an MCP tool holding its own credentials names no
    connection, and a name that merely looks similar is not evidence."""
    from agent_liftoff.connectors.orchestrate.connections import bound_connection

    assert bound_connection({"binding": {"mcp": {"connections": {}}}}).declared is False
    assert bound_connection({}).app_ids == ()


def test_a_url_prompt_rejects_a_pasted_credential():
    """A server-URL prompt sits next to a token prompt. The platform's answer
    to a token in that field is a policy error that quotes the token back."""
    from agent_liftoff.connectors.orchestrate.provisioning import looks_like_a_url

    assert looks_like_a_url("https://dev000000.service-now.com/")
    assert looks_like_a_url("dev000000.service-now.com")
    assert looks_like_a_url("http://10.0.0.4:8000")
    assert not looks_like_a_url("STRqVUTfz1R6j6oBnX6tYuRjL8dAArYsADOZNVmnhePB8c2_IhE81dLi")
    assert not looks_like_a_url("sk-abc123")
    assert not looks_like_a_url("my token here")
    assert not looks_like_a_url("")


# ----------------------------------------------------------------------
# Keeping the ADK session alive
# ----------------------------------------------------------------------

ENV_LIST = """ agent_liftoff-migration  https://api.us-south.watson-orchestrate.cloud.i…  (active)
 local               http://localhost:4321
"""


def test_a_truncated_env_url_still_matches_its_instance():
    """`orchestrate env list` elides the URL once it is long enough, which
    every real instance URL is -- so a full-string comparison never matches and
    a migration re-registers an environment it already has."""
    from agent_liftoff.connectors.orchestrate.adk_session import parse_env_list

    envs = parse_env_list(ENV_LIST)

    assert [e.name for e in envs] == ["agent_liftoff-migration", "local"]
    assert envs[0].truncated is True
    assert envs[0].active is True
    assert envs[0].matches(
        "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df327b39"
    )
    assert not envs[1].matches(
        "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/df327b39"
    )


def test_an_untruncated_url_is_compared_exactly():
    from agent_liftoff.connectors.orchestrate.adk_session import parse_env_list

    local = parse_env_list(ENV_LIST)[1]

    assert local.matches("http://localhost:4321")
    assert local.matches("http://localhost:4321/")
    assert not local.matches("http://localhost:9999")


def test_activating_without_a_key_is_refused_rather_than_attempted():
    from agent_liftoff.connectors.orchestrate.adk_session import ensure_session
    from agent_liftoff.errors import LiftoffError

    with pytest.raises(LiftoffError, match="No API key"):
        ensure_session("https://example", "", "orchestrate")


def test_a_dead_token_after_activation_is_reported_not_assumed_good(monkeypatch):
    """Activating and then trusting it is how a migration writes every file and
    lands nothing."""
    from agent_liftoff.connectors.orchestrate import adk_session
    from agent_liftoff.errors import LiftoffError

    monkeypatch.setattr(adk_session, "list_environments", lambda cli: adk_session.parse_env_list(ENV_LIST))
    monkeypatch.setattr(
        adk_session, "_run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    )
    monkeypatch.setattr(adk_session, "session_is_live", lambda cli: False)

    with pytest.raises(LiftoffError, match="still does not work"):
        adk_session.ensure_session(
            "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/x", "key", "orchestrate"
        )


# ----------------------------------------------------------------------
# A connection that authenticates but points nowhere
# ----------------------------------------------------------------------


def endpointless(**kw):
    base = dict(
        app_id="servicenow_ibm_a1b2c3d4",
        connection_id="c1",
        environment="draft",
        security_scheme="bearer_token",
        preference="member",
        server_url="",
        is_configured=True,
        credentials_entered=True,
    )
    base.update(kw)
    return AppConnection(**base)


def test_a_connection_with_no_server_url_is_not_ready():
    """The live bug. `usable` only asks whether it can authenticate, so a
    member connection with an empty server_url passed as fine -- and the
    migration announced "every migrated tool already has a working connection"
    over one whose tool then died with
    `CredentialKeys.BASE_URL` at runtime.
    """
    connection = endpointless()

    assert connection.usable is True  # it can authenticate
    assert connection.has_endpoint is False  # but it has nowhere to call
    assert connection.ready is False  # so a tool bound to it cannot work
    assert "no server URL" in connection.summary()


def test_a_fully_configured_connection_is_ready():
    connection = endpointless(server_url="https://dev000000.service-now.com/")

    assert connection.ready is True
    assert "no server URL" not in connection.summary()


def test_an_unconfigured_connection_is_neither_usable_nor_ready():
    connection = endpointless(is_configured=False, server_url="https://x.service-now.com")

    assert connection.usable is False
    assert connection.ready is False


def test_a_whitespace_server_url_does_not_count_as_an_endpoint():
    assert endpointless(server_url="   ").has_endpoint is False

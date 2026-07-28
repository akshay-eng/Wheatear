"""Migrating an MCP server, and knowing when there isn't one.

The expensive mistake this guards against is re-adding a server the target
already has: the tenant ends up with two toolkits for one endpoint and no way
to tell which an agent is calling. The second is repointing an existing
toolkit, which silently changes what every agent already using it does.
"""

import json

from agent_liftoff.connectors.copilot_studio.mcp_scan import find_mcp_servers
from agent_liftoff.connectors.orchestrate.mcp_sync import normalise, plan_servers


def _connector(tmp_path, name, *, protocol="mcp-streamable-1.0", host="mcp.example.com",
               base="/", scheme="https", operations=("InvokeMCP",)):
    directory = tmp_path / "Connectors" / name
    directory.mkdir(parents=True)
    document = {
        "swagger": "2.0",
        "info": {"title": name},
        "host": host,
        "basePath": base,
        "schemes": [scheme],
        "paths": {
            "/mcp": {
                "post": {
                    "operationId": operations[0],
                    **({"x-ms-agentic-protocol": protocol} if protocol else {}),
                }
            }
        },
    }
    (directory / "apiDefinition.swagger.json").write_text(json.dumps(document))
    return directory


def _toolkit(name, url, transport="sse", tools=("a", "b")):
    return {"name": name, "mcp": {"server_url": url, "transport": transport}, "tools": list(tools)}


# ----------------------------------------------------------------------
# Detection
# ----------------------------------------------------------------------


def test_a_connector_declaring_the_agentic_protocol_is_an_mcp_server(tmp_path):
    _connector(tmp_path, "ServiceNowMCP")
    found = find_mcp_servers(tmp_path)
    assert [s.name for s in found] == ["ServiceNowMCP"]
    assert found[0].url == "https://mcp.example.com"
    assert found[0].transport == "streamable_http"
    assert found[0].pointable


def test_an_ordinary_connector_is_not_an_mcp_server(tmp_path):
    """Most Copilot solutions reach their systems through Microsoft-published
    connectors. Treating one as an MCP server would point the target at an
    endpoint that does not speak the protocol.
    """
    _connector(tmp_path, "ServiceNowRest", protocol=None)
    assert find_mcp_servers(tmp_path) == []


def test_a_solution_with_no_connectors_at_all_yields_nothing(tmp_path):
    (tmp_path / "bots").mkdir()
    assert find_mcp_servers(tmp_path) == []


def test_an_unknown_protocol_revision_is_still_recognised_as_mcp(tmp_path):
    """A new revision must not silently stop being an MCP server; it becomes a
    server whose transport we cannot name, which is a review item.
    """
    _connector(tmp_path, "Future", protocol="mcp-streamable-9.9")
    server = find_mcp_servers(tmp_path)[0]
    assert server.protocol == "mcp-streamable-9.9"
    assert server.transport is None
    assert not server.pointable


# ----------------------------------------------------------------------
# Deciding what to do about it
# ----------------------------------------------------------------------


def test_a_server_the_target_already_has_is_reused_not_recreated(tmp_path):
    _connector(tmp_path, "SnowMcp", host="150.239.165.119:8000", base="/", scheme="http")
    servers = find_mcp_servers(tmp_path)
    plans = plan_servers(servers, [_toolkit("SNOWMCPALL", "http://150.239.165.119:8000/sse")])
    assert [p.action for p in plans] == ["reuse"]
    assert plans[0].toolkit.name == "SNOWMCPALL"
    assert plans[0].command() is None


def test_the_same_host_over_a_different_scheme_is_a_question_for_a_person(tmp_path):
    """Deciding it either way by string comparison is wrong: reuse might point
    the agent at an endpoint that refuses the connection, create adds a
    near-duplicate toolkit.
    """
    _connector(tmp_path, "SnowMcp", host="150.239.165.119:8000", base="/", scheme="https")
    plans = plan_servers(find_mcp_servers(tmp_path), [_toolkit("SNOWMCPALL", "http://150.239.165.119:8000/sse")])
    assert plans[0].action == "conflict"
    assert "different scheme" in plans[0].reason


def test_a_route_difference_is_not_a_different_server():
    """`http://host:8000/sse` and `http://host:8000/` are one server. Comparing
    raw strings would add a duplicate toolkit.
    """
    assert normalise("http://host:8000/sse") == normalise("http://host:8000/")


def test_a_server_the_target_does_not_have_becomes_an_add_command(tmp_path):
    _connector(tmp_path, "NewMcp", host="new.example.com")
    plans = plan_servers(find_mcp_servers(tmp_path), [_toolkit("Other", "http://elsewhere:9000")])
    assert plans[0].action == "create"
    assert plans[0].needs_credentials
    command = plans[0].command()
    assert command[:4] == ["toolkits", "add", "--kind", "mcp"]
    assert "https://new.example.com" in command
    assert "streamable_http" in command


def test_the_same_name_at_a_different_url_is_a_conflict_not_a_repoint(tmp_path):
    """Repointing would change what every agent already using that toolkit
    calls -- a change nobody asked this migration to make.
    """
    _connector(tmp_path, "SNOWMCPALL", host="somewhere-else.example.com")
    plans = plan_servers(find_mcp_servers(tmp_path), [_toolkit("SNOWMCPALL", "http://150.239.165.119:8000/sse")])
    assert plans[0].action == "conflict"
    assert plans[0].command() is None
    assert "every agent already using it" in plans[0].reason


def test_a_server_with_no_url_cannot_be_pointed_at(tmp_path):
    directory = tmp_path / "Connectors" / "Vague"
    directory.mkdir(parents=True)
    directory.joinpath("apiDefinition.swagger.json").write_text(
        json.dumps({"swagger": "2.0", "info": {"title": "Vague"},
                    "paths": {"/x": {"post": {"x-ms-agentic-protocol": "mcp-streamable-1.0"}}}})
    )
    plans = plan_servers(find_mcp_servers(tmp_path), [])
    assert plans[0].action == "conflict"
    assert "records no URL" in plans[0].reason

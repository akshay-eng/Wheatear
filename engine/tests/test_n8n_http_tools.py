"""n8n HTTP request tools -> rebuildable Orchestrate Python tools.

The tests that matter here are all about one distinction: which parts of an n8n
HTTP node are the tool's *signature* and which are constants the workflow author
fixed. Getting it wrong in either direction produces a tool that imports
cleanly, deploys cleanly, and returns the wrong data at runtime -- the failure
mode a migration tool has the least excuse for.
"""

from __future__ import annotations

from agent_liftoff.connectors.n8n import http_tools
from agent_liftoff.connectors.orchestrate import http_tool_build as build


def node(**parameters) -> dict:
    base = {
        "toolDescription": "A tool.",
        "method": "GET",
        "url": "https://example.service-now.com/api/now/table/incident",
    }
    base.update(parameters)
    return {
        "name": parameters.pop("_name", "Test Tool"),
        "type": "@n8n/n8n-nodes-langchain.toolHttpRequest",
        "parameters": base,
    }


def keypairs(*values) -> dict:
    return {"values": list(values)}


# --------------------------------------------------------------------------- #
# What is a parameter, and what is a constant
# --------------------------------------------------------------------------- #

def test_authored_query_values_are_constants_not_parameters():
    """An author-fixed value is not something to ask the model for."""
    spec = http_tools.extract(
        node(
            sendQuery=True,
            parametersQuery=keypairs(
                {"name": "sysparm_limit", "valueProvider": "fieldValue", "value": "10"},
            ),
        )
    )
    assert spec.constants == {"sysparm_limit": "10"}
    assert spec.params == []


def test_model_provided_query_values_are_parameters():
    spec = http_tools.extract(
        node(
            sendQuery=True,
            parametersQuery=keypairs(
                {"name": "sysparm_query", "valueProvider": "modelRequired"},
            ),
        )
    )
    assert [p.name for p in spec.params] == ["sysparm_query"]
    assert spec.constants == {}


def test_placeholder_inside_a_constant_becomes_the_parameter():
    """The whole reason this module exists.

    `sysparm_query = "number={recordNumber}"` means the tool takes a record
    number -- not a ServiceNow encoded query. Exposing `sysparm_query` would
    make the target model invent query grammar; exposing nothing would send the
    literal template.
    """
    spec = http_tools.extract(
        node(
            sendQuery=True,
            parametersQuery=keypairs(
                {
                    "name": "sysparm_query",
                    "valueProvider": "fieldValue",
                    "value": "number={recordNumber}",
                },
            ),
            placeholderDefinitions=keypairs(
                {"name": "recordNumber", "description": "e.g. INC0010864", "type": "string"},
            ),
        )
    )
    assert [p.name for p in spec.params] == ["recordNumber"]
    assert spec.constants["sysparm_query"] == "number={recordNumber}"
    assert spec.params[0].description == "e.g. INC0010864"


def test_url_placeholders_become_path_parameters():
    spec = http_tools.extract(node(url="https://x.example.com/api/now/table/{table}"))
    assert [(p.name, p.location) for p in spec.params] == [("table", "path")]
    assert spec.path == "/api/now/table/{table}"
    assert spec.base_url == "https://x.example.com"


def test_one_placeholder_used_twice_yields_one_parameter():
    spec = http_tools.extract(
        node(
            sendQuery=True,
            parametersQuery=keypairs(
                {
                    "name": "sysparm_query",
                    "valueProvider": "fieldValue",
                    "value": "short_descriptionLIKE{term}^ORtextLIKE{term}",
                },
            ),
        )
    )
    assert [p.name for p in spec.params] == ["term"]


# --------------------------------------------------------------------------- #
# Credentials travel as references, never as secrets
# --------------------------------------------------------------------------- #

def test_credential_is_carried_as_a_reference_only():
    spec = http_tools.extract(
        {
            "name": "Get Record",
            "type": "@n8n/n8n-nodes-langchain.toolHttpRequest",
            "parameters": {"url": "https://x.example.com/a", "authentication": "genericCredentialType"},
            "credentials": {"httpHeaderAuth": {"id": "abc", "name": "ServiceNow - Bearer"}},
        }
    )
    assert spec.credential_ref == "ServiceNow - Bearer"
    assert spec.credential_kind == "httpHeaderAuth"


def test_a_node_with_no_usable_url_is_reported_not_dropped():
    spec = http_tools.extract(node(url=""))
    assert spec.base_url == ""
    assert any("base URL" in n for n in spec.notes)


# --------------------------------------------------------------------------- #
# Code generation
# --------------------------------------------------------------------------- #

def _spec_with_template() -> http_tools.HttpToolSpec:
    return http_tools.extract(
        node(
            _name="Get Record",
            url="https://dev.service-now.com/api/now/table/{table}",
            sendQuery=True,
            parametersQuery=keypairs(
                {"name": "sysparm_query", "valueProvider": "fieldValue", "value": "number={recordNumber}"},
                {"name": "sysparm_limit", "valueProvider": "fieldValue", "value": "1"},
            ),
            placeholderDefinitions=keypairs(
                {"name": "table", "description": "the table", "type": "string"},
                {"name": "recordNumber", "description": "the number", "type": "string"},
            ),
        )
    )


def test_generated_module_is_valid_python():
    source = build.render_module([_spec_with_template()], "dev_service_now_com")
    ok, why = build.compiles(source)
    assert ok, why


def test_generated_tool_rebuilds_the_template_and_does_not_leak_the_parameter():
    """The bug this whole design exists to prevent.

    `recordNumber` must reach the API as `sysparm_query=number=INC...`, and
    must NOT also be sent as a bare `recordNumber=` parameter that ServiceNow
    would ignore while returning an unfiltered table.
    """
    source = build.render_module([_spec_with_template()], "app")
    assert "'number={recordNumber}'.replace('{recordNumber}', str(recordNumber))" in source
    assert "params['recordNumber']" not in source
    assert "'/api/now/table/{table}'.replace('{table}', str(table))" in source


def test_generated_signature_matches_the_source_signature():
    source = build.render_module([_spec_with_template()], "app")
    assert "def get_record(table: str, recordNumber: str) -> dict:" in source


def test_endpoint_is_read_from_the_connection_not_baked_in():
    """A migrated tool nearly always points somewhere new."""
    source = build.render_module([_spec_with_template()], "app", "bearer_token")
    assert "connections.bearer_token('app')" in source
    assert "creds.url" in source
    # The source host must not survive into the generated code.
    assert "dev.service-now.com" not in source


def test_every_auth_kind_generates_valid_python():
    """The kind is the operator's choice, so all of them have to work."""
    spec = _spec_with_template()
    for kind in build.AUTH_KINDS:
        ok, why = build.compiles(build.render_module([spec], "app", kind))
        assert ok, f"{kind}: {why}"


def test_the_chosen_auth_kind_reaches_both_the_decorator_and_the_call():
    """A tool declaring one credential type and reading another imports fine
    and fails at run time with an empty credential."""
    source = build.render_module([_spec_with_template()], "app", "basic_auth")
    assert "ConnectionType.BASIC_AUTH" in source
    assert "connections.basic_auth('app')" in source
    assert "auth = (creds.username, creds.password)" in source


def test_a_connection_with_no_url_fails_with_a_readable_message():
    """Rather than requests reporting an invalid URL for `/api/now/table/x`."""
    source = build.render_module([_spec_with_template()], "app", "bearer_token")
    assert "has no server URL configured for this environment" in source


def test_tools_sharing_a_host_share_one_connection():
    a = _spec_with_template()
    b = http_tools.extract(node(_name="List Records", url="https://dev.service-now.com/api/now/table/x"))
    groups = http_tools.group_by_host([a, b])
    assert list(groups) == ["https://dev.service-now.com"]
    assert build.app_id_for("https://dev.service-now.com") == "dev_service_now_com"


def test_app_id_is_stable_across_scheme_and_trailing_path():
    assert build.app_id_for("https://x.example.com/api") == build.app_id_for("http://x.example.com")

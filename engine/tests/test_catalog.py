"""Catalog matcher tests, run against the vendored real Orchestrate snapshot."""

import pytest

from agent_liftoff.connectors.orchestrate.catalog import (
    DEFAULT_N8N_NODE_CATALOG_PATH,
    HIGH_CONFIDENCE,
    auth_kind_for_n8n_credential,
    connector_resolver,
    load_catalog,
    load_n8n_node_index,
    match_connector,
)


def test_catalog_loads_and_indexes():
    cat = load_catalog()
    assert len(cat.items) > 1000
    assert cat.app_index  # app tokens mined from names
    assert "servicenow" in cat.app_index
    assert "slack" in cat.app_index


def test_exact_and_app_name_matches_resolve_to_concrete_tools():
    for app in ["Slack", "ServiceNow", "GitLab", "Gmail", "Salesforce", "Jira"]:
        m = match_connector(app)
        assert m is not None, f"{app} should match"
        # Prefer concrete tools / mcp servers over bundled prebuilt agents.
        assert m.category in ("tool", "mcp_server"), f"{app} -> {m.category}"
        assert m.install_ref
        assert m.confidence >= 0.9


def test_multi_tool_app_surfaces_member_tools():
    m = match_connector("ServiceNow")
    assert m is not None
    # ServiceNow has dozens of catalog tools; the whole set is surfaced.
    assert len(m.member_tools) > 1


def test_offerings_do_not_cross_contaminate_app_index():
    # "Write Data in Excel" is listed under a Coupa *offering*; that must not
    # make Excel tools resolvable as Coupa (regression guard).
    m = match_connector("Coupa")
    assert m is not None
    assert "Excel" not in m.name


def test_unknown_app_returns_none():
    assert match_connector("totally-not-a-real-app-xyz") is None
    assert match_connector("") is None
    # Postgres is a DB connection, not a catalog tool app.
    assert match_connector("Postgres") is None


def test_connector_resolver_closure_matches_signature():
    resolve = connector_resolver()
    m = resolve("Slack", "post a message")
    assert m is not None and m.install_ref


@pytest.mark.skipif(
    not DEFAULT_N8N_NODE_CATALOG_PATH.exists(),
    reason="n8n node catalog snapshot not vendored (optional, not on the critical path)",
)
def test_n8n_node_index_maps_type_to_app_and_credentials():
    idx = load_n8n_node_index()
    assert idx  # snapshot present
    slack = idx.get("n8n-nodes-base.slack")
    assert slack and slack["name"] == "Slack"
    assert "slackApi" in slack["credentials"]


def test_n8n_credential_auth_kind_mapping():
    assert auth_kind_for_n8n_credential("slackApi") == "api_key"
    assert auth_kind_for_n8n_credential("postgres") == "key_value"
    assert auth_kind_for_n8n_credential("githubOAuth2Api") == "oauth_auth_code_flow"
    # Unknown types default to api_key (most common), never crash.
    assert auth_kind_for_n8n_credential("someUnknownCredType") == "api_key"


def test_high_confidence_threshold_constant_present():
    assert 0.0 < HIGH_CONFIDENCE <= 1.0

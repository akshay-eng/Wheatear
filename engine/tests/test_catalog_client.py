"""Tests for the console catalog client.

Payload shapes here are copied from a real browser capture of the catalog page
against a live us-south instance, so they test against what the service
actually returns rather than what its docs imply.
"""

from __future__ import annotations

import hashlib

import pytest
import requests

from wheatear.connectors.orchestrate.catalog_client import (
    OrchestrateCatalogClient,
    _csrf_from_cookie,
    apply_detail,
    console_base,
    console_candidates,
    enrich_artifacts,
    to_artifacts,
)
from wheatear.errors import RemoteAPIError

INSTANCE = "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/00000000-0000-0000"

# One real record, icon stripped.
SAMPLE_TOOL = {
    "id": "a35ecf5f-0f5e-4c8f-9172-73e49cf762d5",
    "name": "Accept a Merge Request in GitLab",
    "description": "Accept a GitLab merge request with optional parameters.\n",
    "category": "tool",
    "publisher": "IBM",
    "kind": "native",
    "author": None,
    "type": "python",
    "external_identifier": "accept_a_merge_request",
    "tags": ["IT"],
    "artifact_group": [
        {"id": "75aba932", "name": "Devops and CICD Management with Gitlab", "type": "OFFERING"}
    ],
    "isLocked": False,
    "artifact_origin": "global",
}


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# What the live /artifacts/filters returns, trimmed to the category group.
FILTERS = {
    "filters": [
        {
            "id": "category",
            "options": [
                {"value": "agent", "id": "kind", "options": [{"value": "native"}]},
                {"value": "tool", "id": "type", "options": [{"value": "python"}]},
                {"value": "mcp_server"},
            ],
        },
        {"id": "tags", "options": [{"value": "IT"}]},
    ]
}


class FakeSession:
    """Stands in for requests.Session, recording what was sent.

    `pages` are consumed in order for POSTs; GETs are answered from `gets`,
    keyed by path suffix.
    """

    def __init__(self, pages: list[FakeResponse], gets: dict | None = None):
        self.headers: dict[str, str] = {}
        self._pages = pages
        self._gets = gets if gets is not None else {"/artifacts/filters": FakeResponse(200, FILTERS)}
        self.calls: list[dict] = []
        self.gets: list[str] = []

    def request(self, method, url, timeout=None, **kwargs):
        if method == "GET":
            self.gets.append(url)
            for suffix, resp in self._gets.items():
                if url.endswith(suffix):
                    return resp
            return FakeResponse(404, None, "not found")
        self.calls.append({"url": url, "body": kwargs.get("json"), "headers": dict(self.headers)})
        return self._pages[min(len(self.calls) - 1, len(self._pages) - 1)]


def _client(
    pages: list[FakeResponse],
    monkeypatch,
    cookie: str = "__Secure-fgp=abc",
    gets: dict | None = None,
) -> OrchestrateCatalogClient:
    session = FakeSession(pages, gets)
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = OrchestrateCatalogClient(INSTANCE, session_cookie=cookie)
    client.session = session  # type: ignore[attr-defined]  # for assertions
    return client


# ----------------------------------------------------------------------
# URL derivation -- the thing an earlier probe got wrong
# ----------------------------------------------------------------------


def test_console_base_drops_the_api_prefix_and_instance_path():
    assert console_base(INSTANCE) == (
        "https://us-south.watson-orchestrate.cloud.ibm.com/mfe_catalog/api/catalogv3"
    )


def test_console_base_preserves_a_non_api_host():
    """Developer Edition and self-hosted instances have no `api.` prefix."""
    assert console_base("https://wxo.internal.example.com/instances/x") == (
        "https://wxo.internal.example.com/mfe_catalog/api/catalogv3"
    )


def test_console_base_rejects_a_url_with_no_host():
    with pytest.raises(RemoteAPIError):
        console_base("/instances/00000000-0000-0000")


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------


def test_csrf_is_the_sha256_of_the_fingerprint_cookie():
    cookie = "bm_sz=x; __Secure-fgp=5cf12920dd01d910; crn=crn:v1:bluemix"
    assert _csrf_from_cookie(cookie) == hashlib.sha256(b"5cf12920dd01d910").hexdigest()


def test_csrf_is_none_when_the_fingerprint_cookie_is_absent():
    assert _csrf_from_cookie("bm_sz=x; utag_main=y") is None


def test_session_auth_sets_csrf_and_origin(monkeypatch):
    client = _client([FakeResponse(200, {"artifacts": [], "total": 0})], monkeypatch)
    headers = client.session.headers  # type: ignore[attr-defined]
    assert headers["x-ibm-wo-csrf"] == hashlib.sha256(b"abc").hexdigest()
    assert headers["Origin"] == "https://us-south.watson-orchestrate.cloud.ibm.com"
    assert client.auth_mode == "session"


def test_a_cookie_without_the_fingerprint_is_rejected_up_front(monkeypatch):
    monkeypatch.setattr(requests, "Session", lambda: FakeSession([]))
    with pytest.raises(RemoteAPIError, match="__Secure-fgp"):
        OrchestrateCatalogClient(INSTANCE, session_cookie="bm_sz=x")


def test_no_credentials_at_all_is_rejected():
    with pytest.raises(RemoteAPIError):
        OrchestrateCatalogClient(INSTANCE)


def test_iam_rejection_explains_the_cookie_fallback(monkeypatch):
    """The whole point of the message: an IAM rejection here is expected, not a
    bug, and the user needs to know the next step rather than see a status."""
    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.rest_client.get_iam_token", lambda key: "tok"
    )
    session = FakeSession([FakeResponse(401, None, "Unauthorized")])
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = OrchestrateCatalogClient(INSTANCE, api_key="key")
    with pytest.raises(RemoteAPIError, match="WXO_CONSOLE_COOKIE"):
        client.list_artifacts(["tool"])


def test_an_iam_500_is_reported_as_auth_not_as_an_outage(monkeypatch):
    """Verified live behaviour: the proxy takes the bearer token and then fails
    to resolve tenant context, answering 500. Calling that a server fault would
    send someone chasing an outage that isn't happening."""
    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.rest_client.get_iam_token", lambda key: "tok"
    )
    session = FakeSession([FakeResponse(500, None, '{"code":"WXO-PROXY-11076E"}')])
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = OrchestrateCatalogClient(INSTANCE, api_key="key")
    with pytest.raises(RemoteAPIError, match="console session"):
        client.list_artifacts(["tool"])


def test_a_network_failure_becomes_a_typed_error(monkeypatch):
    class Boom(FakeSession):
        def request(self, method, url, timeout=None, **kwargs):
            raise requests.ConnectionError("dns")

    monkeypatch.setattr(requests, "Session", lambda: Boom([]))
    client = OrchestrateCatalogClient(INSTANCE, session_cookie="__Secure-fgp=abc")
    with pytest.raises(RemoteAPIError, match="Could not reach"):
        client.list_artifacts(["tool"])


def test_the_cookie_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("WXO_CONSOLE_COOKIE", "__Secure-fgp=fromenv")
    client = _client([FakeResponse(200, {"artifacts": [], "total": 0})], monkeypatch, cookie=None)
    assert client.auth_mode == "session"


# ----------------------------------------------------------------------
# Pagination -- what makes 1152 reachable instead of the first 12
# ----------------------------------------------------------------------


def _page(ids, total):
    return FakeResponse(200, {"artifacts": [dict(SAMPLE_TOOL, id=str(i)) for i in ids], "total": total})


def test_pagination_walks_the_whole_catalog(monkeypatch):
    client = _client(
        [_page(range(500), 1152), _page(range(500, 1000), 1152), _page(range(1000, 1152), 1152)],
        monkeypatch,
    )
    assert len(client.list_artifacts(["tool"])) == 1152
    offsets = [call["body"]["offset"] for call in client.session.calls]  # type: ignore[attr-defined]
    assert offsets == [0, 500, 1000]


def test_pagination_stops_on_an_empty_page_even_if_total_lies(monkeypatch):
    """A total that never matches what's returned must not spin forever."""
    client = _client(
        [_page([1], 9999), FakeResponse(200, {"artifacts": [], "total": 9999})], monkeypatch
    )
    assert len(client.list_artifacts(["tool"])) == 1


def test_the_page_size_ceiling_is_taken_from_the_service(monkeypatch):
    """The service answers 'The limit value must be between 1 and 1000'. Read
    it and retry, rather than carrying a guess about someone else's limit."""
    client = _client(
        [
            FakeResponse(400, None, '{"errors":"The limit value must be between 1 and 1000."}'),
            _page(range(2), 2),
        ],
        monkeypatch,
    )
    client.page_size = 5000

    assert len(client.list_artifacts(["tool"])) == 2
    assert client.page_size == 1000
    assert client.session.calls[1]["body"]["limit"] == 1000  # type: ignore[attr-defined]


def test_list_asks_for_flat_results_so_total_is_present(monkeypatch):
    """The grouped response keys by category and carries no total, which is
    exactly what pagination needs -- so we must ask for flat."""
    client = _client([FakeResponse(200, {"artifacts": [], "total": 0})], monkeypatch)
    client.list_artifacts(["tool"])
    body = client.session.calls[0]["body"]  # type: ignore[attr-defined]
    assert body["grouped_results"] is False
    assert "external_identifier" in body["select_fields"]


def test_no_type_filter_is_sent(monkeypatch):
    """Filtering tools by type returns exactly the same set as not filtering,
    and one unrecognised value rejects the whole query with a 400. Sending a
    hardcoded type list is therefore all risk and no benefit."""
    client = _client([FakeResponse(200, {"artifacts": [], "total": 0})], monkeypatch)
    client.list_artifacts(["tool"])
    filters = client.session.calls[0]["body"]["search_criteria"][0]["filter_groups"][0]["filters"]  # type: ignore[attr-defined]
    assert [f["id"] for f in filters] == ["category"]


def test_categories_are_discovered_from_the_service(monkeypatch):
    client = _client([FakeResponse(200, {"artifacts": [], "total": 0})], monkeypatch)
    assert client.discover_categories() == ["agent", "tool", "mcp_server"]


def test_categories_fall_back_when_the_filters_endpoint_is_down(monkeypatch):
    """A filters outage must not cost us the catalog."""
    client = _client(
        [FakeResponse(200, {"artifacts": [], "total": 0})],
        monkeypatch,
        gets={"/artifacts/filters": FakeResponse(503, None, "unavailable")},
    )
    assert client.discover_categories() == ["tool", "mcp_server", "agent"]


def test_one_query_covers_every_discovered_category(monkeypatch):
    """Not one request per category: the filter takes a list, and a single
    sweep keeps pagination honest against a single total."""
    client = _client([FakeResponse(200, {"artifacts": [SAMPLE_TOOL], "total": 1})], monkeypatch)
    client.list_artifacts()
    bodies = client.session.calls  # type: ignore[attr-defined]
    assert len(bodies) == 1
    assert bodies[0]["body"]["search_criteria"][0]["filter_groups"][0]["filters"][0]["value"] == [
        "agent",
        "tool",
        "mcp_server",
    ]


def test_installable_excludes_catalog_agents_by_default(monkeypatch):
    """A catalog agent is a collaborator, not a tool -- matching a source tool
    onto one would be a category error."""
    client = _client([FakeResponse(200, {"artifacts": [SAMPLE_TOOL], "total": 1})], monkeypatch)
    client.list_installable()
    value = client.session.calls[0]["body"]["search_criteria"][0]["filter_groups"][0]["filters"][0][  # type: ignore[attr-defined]
        "value"
    ]
    assert "agent" not in value
    assert set(value) == {"tool", "mcp_server"}


# ----------------------------------------------------------------------
# Flattening
# ----------------------------------------------------------------------


def test_artifact_install_ref_prefers_the_external_identifier():
    """The display name isn't referenceable from an agent.yaml; the external
    identifier is what the tool is called once installed."""
    artifact = to_artifacts([SAMPLE_TOOL])[0]
    assert artifact.install_ref == "accept_a_merge_request"
    assert artifact.name == "Accept a Merge Request in GitLab"


def test_artifact_falls_back_to_the_display_name():
    artifact = to_artifacts([dict(SAMPLE_TOOL, external_identifier=None)])[0]
    assert artifact.install_ref == "Accept a Merge Request in GitLab"


def test_artifact_carries_tags_and_offering_groups():
    artifact = to_artifacts([SAMPLE_TOOL])[0]
    assert artifact.tags == ("IT",)
    assert artifact.groups == ("Devops and CICD Management with Gitlab",)


def test_unnamed_artifacts_are_dropped():
    assert to_artifacts([{"id": "x", "name": ""}, SAMPLE_TOOL]) == to_artifacts([SAMPLE_TOOL])

# ----------------------------------------------------------------------
# Locating the service on a deployment that isn't IBM Cloud SaaS
# ----------------------------------------------------------------------


def test_an_explicit_console_url_overrides_derivation(monkeypatch):
    """Worst case, the user tells us where the console is. That must win
    outright rather than being one guess among several."""
    monkeypatch.setenv("WXO_CONSOLE_URL", "https://wxo.corp.example.com")
    assert console_candidates(INSTANCE) == [
        "https://wxo.corp.example.com/mfe_catalog/api/catalogv3"
    ]


def test_derivation_offers_the_unmodified_host_as_a_fallback(monkeypatch):
    monkeypatch.delenv("WXO_CONSOLE_URL", raising=False)
    candidates = console_candidates(INSTANCE)
    assert candidates[0].startswith("https://us-south.watson-orchestrate")
    assert candidates[1].startswith("https://api.us-south.watson-orchestrate")


def test_a_404_falls_through_to_the_next_candidate_host(monkeypatch):
    """A host that answers but isn't the console shouldn't look like an outage."""
    monkeypatch.delenv("WXO_CONSOLE_URL", raising=False)
    session = FakeSession(
        [FakeResponse(404, None, "nope"), FakeResponse(200, {"artifacts": [], "total": 0})]
    )
    monkeypatch.setattr(requests, "Session", lambda: session)
    client = OrchestrateCatalogClient(INSTANCE, session_cookie="__Secure-fgp=abc")

    client.list_artifacts(["tool"])

    assert client.base.startswith("https://api.us-south")  # settled on the second


# ----------------------------------------------------------------------
# Detail enrichment -- what makes catalog matching more than prose-matching
# ----------------------------------------------------------------------

DETAIL = {
    "id": "a35ecf5f",
    "name": "Accept a Merge Request in GitLab",
    "spec_file": {
        "name": "accept_a_merge_request",
        "input_schema": {
            "type": "object",
            "required": ["project_id", "merge_request_number"],
            "properties": {"project_id": {}, "merge_request_number": {}, "squash": {}},
        },
        "applications": [
            {"name": "GitLab", "app_id": "gitlab_ibm_184bdbd3", "security_schema": {}}
        ],
    },
}

MCP_DETAIL = {
    "id": "c6a95f1c",
    "name": "Athenium Weather Intelligence",
    "spec_file": {"mcp": {"tools": ["active_tropical_cyclones", "historical_weather"]}},
}


def test_detail_supplies_the_parameter_schema_the_list_endpoint_lacks():
    artifact = to_artifacts([SAMPLE_TOOL])[0]
    assert artifact.params == []

    apply_detail(artifact, DETAIL)

    assert artifact.params == ["merge_request_number", "project_id", "squash"]
    assert artifact.required_params == ["project_id", "merge_request_number"]
    assert artifact.enriched is True


def test_detail_surfaces_the_connection_the_tool_needs():
    """Installing the tool isn't enough -- without its connection configured it
    imports and then fails at runtime, so a reviewer has to see this."""
    artifact = to_artifacts([SAMPLE_TOOL])[0]

    apply_detail(artifact, DETAIL)

    assert artifact.connections == ["gitlab_ibm_184bdbd3"]


def test_detail_of_an_mcp_server_lists_its_member_tools():
    artifact = to_artifacts([dict(SAMPLE_TOOL, category="mcp_server", type=None)])[0]

    apply_detail(artifact, MCP_DETAIL)

    assert artifact.member_tools == ["active_tropical_cyclones", "historical_weather"]


def test_enrichment_only_fetches_what_it_has_to(monkeypatch):
    """One request per artifact means this is for shortlists, never the whole
    catalog -- and an already-enriched entry must not be fetched twice."""
    client = _client(
        [FakeResponse(200, {"artifacts": [], "total": 0})],
        monkeypatch,
        gets={"/artifacts/a1": FakeResponse(200, DETAIL)},
    )
    fresh = to_artifacts([dict(SAMPLE_TOOL, id="a1")])[0]
    already = to_artifacts([dict(SAMPLE_TOOL, id="a2")])[0]
    already.enriched = True

    enrich_artifacts(client, [fresh, already])

    assert fresh.params  # fetched
    assert client.session.gets == [f"{client.base}/artifacts/a1"]  # type: ignore[attr-defined]


def test_a_failed_detail_fetch_leaves_the_artifact_usable(monkeypatch):
    """Prose-only matching is degraded, not broken -- one bad detail response
    must not sink the batch."""
    client = _client(
        [FakeResponse(200, {"artifacts": [], "total": 0})],
        monkeypatch,
        gets={"/artifacts/a1": FakeResponse(500, None, "boom")},
    )
    artifact = to_artifacts([dict(SAMPLE_TOOL, id="a1")])[0]

    enrich_artifacts(client, [artifact])

    assert artifact.enriched is False
    assert artifact.name == "Accept a Merge Request in GitLab"

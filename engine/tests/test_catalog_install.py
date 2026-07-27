"""Installing a catalog tool the way the console does it.

The endpoints and payload shapes here were read off a recorded browser session,
not guessed, so the fixtures below are the real thing trimmed down. The
important invariant is that the specification is posted back **unedited**: its
`file_url` is a short-lived presigned URL and its `binding` names the Python
entry point the runtime imports, so a payload that has been tidied is a tool
that installs and cannot run.
"""

from __future__ import annotations

import json

import pytest

from wheatear.connectors.orchestrate import catalog_install
from wheatear.connectors.orchestrate.catalog_install import ConsoleSession, app_ids_in
from wheatear.errors import RemoteAPIError, WheatearError

INSTANCE = "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/abc123"

# Shape taken from the recorded install of "Get system users in ServiceNow".
SPEC = {
    "id": "94203f20-9c0f-4065-91fb-88fbb648bdd3",
    "name": "get_system_users",
    "display_name": "Get system users in ServiceNow",
    "category": "tool",
    "description": "Retrieves a list of system users details in the ServiceNow.",
    "attachments": [
        {"file_name": "get_system_users.zip", "file_url": "https://cos.example/x.zip?X-Amz-Signature=deadbeef"}
    ],
    "spec": {
        "name": "get_system_users",
        "version": "1.37.0",
        "toolType": "python",
        "binding": {"python": {"function": "agent_ready_tools.tools.IT.servicenow.get_system_users:get_system_users"}},
        "applications": [{"name": "ServiceNow", "app_id": "servicenow_ibm_a1b2c3d4"}],
    },
}

COOKIE = "foo=bar; __Secure-fgp=abc123def456; other=x"


class FakeHTTP:
    """Records requests and replays canned responses."""

    def __init__(self, responses):
        self.responses = responses
        self.sent: list[tuple[str, str, object]] = []
        self.headers: dict[str, str] = {}

    def _reply(self, method, url, body=None):
        self.sent.append((method, url, body))
        for match, status, payload in self.responses:
            if match in url:
                r = type("R", (), {})()
                r.status_code, r.url, r.text = status, url, json.dumps(payload)
                r.json = lambda p=payload: p
                return r
        raise AssertionError(f"no canned response for {url}")

    def get(self, url, params=None, timeout=None):
        return self._reply("GET", url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else ""))

    def post(self, url, data=None, headers=None, timeout=None):
        return self._reply("POST", url, json.loads(data))


def session_with(responses, monkeypatch):
    http = FakeHTTP(responses)
    monkeypatch.setattr(catalog_install.requests, "Session", lambda: http)
    return ConsoleSession(INSTANCE, COOKIE), http


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------


def test_a_session_needs_a_cookie_because_the_api_key_cannot_reach_the_console(monkeypatch):
    monkeypatch.setattr(catalog_install.requests, "Session", lambda: FakeHTTP([]))

    with pytest.raises(WheatearError, match="console session cookie"):
        ConsoleSession(INSTANCE, "")


def test_a_cookie_without_the_fingerprint_is_refused(monkeypatch):
    """The CSRF header is derived from `__Secure-fgp`; without it every write
    is rejected, and failing here says why."""
    monkeypatch.setattr(catalog_install.requests, "Session", lambda: FakeHTTP([]))

    with pytest.raises(WheatearError, match="__Secure-fgp"):
        ConsoleSession(INSTANCE, "foo=bar; baz=qux")


def test_the_session_carries_the_csrf_header_the_console_double_submits(monkeypatch):
    _, http = session_with([], monkeypatch)

    assert http.headers["Cookie"] == COOKIE
    assert http.headers["x-ibm-wo-csrf"]
    assert "watson-orchestrate" in http.headers["Origin"]
    # The console origin is the instance host minus its `api.` prefix.
    assert not http.headers["Origin"].startswith("https://api.")


def test_an_expired_session_says_so_rather_than_reporting_a_tool_failure(monkeypatch):
    session, _ = session_with([("specification", 401, {})], monkeypatch)

    with pytest.raises(RemoteAPIError, match="short-lived"):
        catalog_install.fetch_specification(session, "artifact-1", "1.0.0")


# ----------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------


def test_the_newest_published_version_is_asked_for_not_assumed(monkeypatch):
    """The shipped snapshot records no version, and pinning a plausible number
    would install whatever it happened to mean on every tenant forever."""
    session, _ = session_with(
        [("versions", 200, {"versions": [
            {"version": "1.35.0", "created_at": "2026-06-17T04:10:35Z"},
            {"version": "1.37.0", "created_at": "2026-07-17T07:24:20Z"},
            {"version": "1.36.0", "created_at": "2026-06-27T05:35:10Z"},
        ]})],
        monkeypatch,
    )

    assert catalog_install.latest_version(session, "artifact-1") == "1.37.0"


def test_an_artifact_with_no_versions_is_an_error_not_an_empty_install(monkeypatch):
    session, _ = session_with([("versions", 200, {"versions": []})], monkeypatch)

    with pytest.raises(RemoteAPIError, match="no versions"):
        catalog_install.latest_version(session, "artifact-1")


# ----------------------------------------------------------------------
# Installing
# ----------------------------------------------------------------------


def test_the_specification_content_is_carried_through_untouched(monkeypatch):
    """The presigned url and the binding are what make the tool runnable.
    Editing either -- even to tidy it -- installs something that cannot run.
    Only the envelope differs: the console drops the two response-only keys and
    adds a workspace.
    """
    session, http = session_with(
        [("specification", 200, {"result": [SPEC]}),
         ("create-from-template", 201, {"id": ["new-tool-id"]})],
        monkeypatch,
    )

    installed = catalog_install.install_artifact(session, SPEC["id"], "1.37.0")

    posted = [body for method, _url, body in http.sent if method == "POST"][0]
    assert len(posted) == 1
    body = posted[0]
    assert body["attachments"] == SPEC["attachments"]
    assert body["attachments"][0]["file_url"].endswith("X-Amz-Signature=deadbeef")
    assert body["spec"] == SPEC["spec"]
    assert body["workspace_id"] == catalog_install.DEFAULT_WORKSPACE
    assert "master_artifact_id" not in body and "version" not in body
    assert installed.tool_id == "new-tool-id"
    assert installed.name == "get_system_users"


def test_the_result_envelope_is_unwrapped(monkeypatch):
    """Caught live: the endpoint answers `{"result": [ ... ]}`, and reading the
    envelope as the spec made every install report "no usable specification"
    for artifacts that were perfectly installable."""
    session, _ = session_with([("specification", 200, {"result": [SPEC]})], monkeypatch)

    assert catalog_install.fetch_specification(session, SPEC["id"], "1.37.0")["name"] == "get_system_users"


def test_an_unwrapped_or_single_object_response_also_works(monkeypatch):
    """Tolerated both ways: the shape is the console's to change, and refusing
    a bare object would turn a cosmetic API change into a broken install."""
    session, _ = session_with([("specification", 200, {"result": SPEC})], monkeypatch)
    assert catalog_install.fetch_specification(session, SPEC["id"], "1.0")["name"] == "get_system_users"

    session, _ = session_with([("specification", 200, SPEC)], monkeypatch)
    assert catalog_install.fetch_specification(session, SPEC["id"], "1.0")["name"] == "get_system_users"


def test_the_envelope_keys_are_dropped_but_nothing_else_is():
    spec = dict(SPEC, master_artifact_id="m1", version="1.37.0")

    body = catalog_install.installable_payload(spec, "ws-9")

    assert body["workspace_id"] == "ws-9"
    assert set(SPEC) - set(body) == set()  # every original key survives
    assert "master_artifact_id" not in body and "version" not in body


def test_the_connection_the_tool_needs_is_read_out_of_the_spec():
    """Read, not guessed from the name -- the same reason `bound_connection`
    exists. A ServiceNow-sounding name is not evidence of a binding."""
    assert app_ids_in(SPEC) == ("servicenow_ibm_a1b2c3d4",)
    assert app_ids_in({"spec": {}}) == ()
    assert app_ids_in({}) == ()


def test_an_install_that_names_no_tool_id_is_a_failure(monkeypatch):
    """A 201 with no id leaves nothing to attach to an agent, so reporting
    success would mean claiming a capability that is not there."""
    session, _ = session_with(
        [("specification", 200, SPEC), ("create-from-template", 201, {})], monkeypatch
    )

    with pytest.raises(RemoteAPIError, match="named no tool id"):
        catalog_install.install_artifact(session, SPEC["id"], "1.37.0")


def test_a_missing_specification_stops_before_posting_anything(monkeypatch):
    session, http = session_with([("specification", 200, {})], monkeypatch)

    with pytest.raises(RemoteAPIError, match="no usable specification"):
        catalog_install.fetch_specification(session, "artifact-1", "1.0.0")
    assert not [m for m, _u, _b in http.sent if m == "POST"]


def test_the_version_is_resolved_when_the_caller_does_not_know_it(monkeypatch):
    session, http = session_with(
        [
            ("versions", 200, {"versions": [{"version": "2.0.0", "created_at": "2026-07-01T00:00:00Z"}]}),
            ("specification", 200, SPEC),
            ("create-from-template", 201, {"id": ["t1"]}),
        ],
        monkeypatch,
    )

    catalog_install.install_artifact(session, SPEC["id"])

    assert any("version=2.0.0" in url for _m, url, _b in http.sent)

"""Reaching Dataverse with a browser token instead of the PAC CLI.

This path exists because PAC is a single point of failure an enterprise tenant
can close. Observed live: `AADSTS135010: UserPrincipal doesn't have the key ID
configured` on one tenant, while the same machine and the same PAC install
signed into a second tenant fine. The operator could still open the maker
portal in a browser the whole time.

The behaviours pinned here are the ones that decide whether a failure is
understandable: a token for the wrong tenant and a solution the user cannot
read produce very similar-looking errors and need very different responses.
"""

from __future__ import annotations

import base64
import json

import pytest
import requests

from agent_liftoff.connectors.copilot_studio import dataverse_client as dv

ENV = "https://contoso.crm8.dynamics.com"


class Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append(("GET", url, params, headers))
        return seen.pop_response() if hasattr(seen, "pop_response") else Response(200, {"value": []})

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


# --------------------------------------------------------------------------- #
# Telling the two failures apart
# --------------------------------------------------------------------------- #

def test_a_wrong_tenant_token_says_so_in_words(monkeypatch):
    """401 is the whole-token case: nothing here will work until it is replaced."""
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(401, {}))

    with pytest.raises(dv.DataverseError, match="only valid for the tenant"):
        dv.whoami(ENV, "stale")


def test_a_readable_error_message_is_surfaced_not_just_a_status(monkeypatch):
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: Response(403, {"error": {"message": "does not have ReadAccess right(s)"}}),
    )

    with pytest.raises(dv.DataverseError, match="ReadAccess"):
        dv.whoami(ENV, "t")


def test_a_solution_that_exports_as_not_a_zip_is_refused(monkeypatch):
    """A permission failure inside a solution can still come back 200. Unzipping
    it would raise an error naming a temp file rather than the cause."""
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: Response(200, {"ExportSolutionFile": base64.b64encode(b"<html>nope").decode()}),
    )

    with pytest.raises(dv.DataverseError, match="did not export as a zip"):
        dv.export_solution(ENV, "t", "Broken")


def test_a_real_zip_comes_back_decoded(monkeypatch):
    archive = b"PK\x03\x04rest-of-a-zip"
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: Response(200, {"ExportSolutionFile": base64.b64encode(archive).decode()}),
    )

    assert dv.export_solution(ENV, "t", "Bookings") == archive


def test_an_export_with_no_file_is_refused(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: Response(200, {}))

    with pytest.raises(dv.DataverseError, match="no file attached"):
        dv.export_solution(ENV, "t", "Empty")


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def test_solutions_are_parsed_into_something_selectable(monkeypatch):
    payload = {"value": [
        {"uniquename": "Bookings", "friendlyname": "Bookings", "version": "1.0.0.0", "ismanaged": False},
        {"uniquename": "Demo", "friendlyname": "Demo", "version": "1.0", "ismanaged": True},
        {"version": "9.9"},  # no uniquename -> not a solution anyone can export
    ]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response(200, payload))

    solutions = dv.list_solutions(ENV, "t")

    assert [s.unique_name for s in solutions] == ["Bookings", "Demo"]
    assert "unmanaged" in solutions[0].label()
    assert "managed" in solutions[1].label()


def test_invisible_solutions_are_filtered_out_by_default(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.update(params or {})
        return Response(200, {"value": []})

    monkeypatch.setattr(requests, "get", fake_get)
    dv.list_solutions(ENV, "t")

    assert captured.get("$filter") == "isvisible eq true"


# --------------------------------------------------------------------------- #
# Reading a token out of a browser capture
# --------------------------------------------------------------------------- #

def har(*entries) -> str:
    return json.dumps({"log": {"entries": list(entries)}})


def entry(url, auth=None):
    headers = [{"name": "Accept", "value": "application/json"}]
    if auth:
        headers.append({"name": "Authorization", "value": auth})
    return {"request": {"url": url, "headers": headers}}


def test_a_dataverse_token_is_found_in_a_capture(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(har(entry(f"{ENV}/api/data/v9.2/solutions", "Bearer " + "x" * 60)))

    found = dv.tokens_from_har(path)

    assert found == {ENV: "x" * 60}
    assert dv.environment_of(found) == ENV


def test_tokens_for_anything_that_is_not_dataverse_are_ignored(tmp_path):
    """A browser capture holds tokens for whatever else that browser was doing,
    and none of it is this tool's business."""
    path = tmp_path / "c.har"
    path.write_text(har(
        entry("https://graph.microsoft.com/v1.0/me", "Bearer " + "secret" * 20),
        entry("https://login.microsoftonline.com/x", "Bearer " + "other" * 20),
        entry(f"{ENV}/api/data/v9.2/WhoAmI", "Bearer " + "y" * 60),
    ))

    assert dv.tokens_from_har(path) == {ENV: "y" * 60}


def test_the_last_token_for_an_environment_wins(tmp_path):
    """A capture spanning a refresh should yield the one still valid at the end."""
    path = tmp_path / "c.har"
    path.write_text(har(
        entry(f"{ENV}/api/data/v9.2/solutions", "Bearer " + "old" * 20),
        entry(f"{ENV}/api/data/v9.2/solutions", "Bearer " + "new" * 20),
    ))

    assert dv.tokens_from_har(path)[ENV] == "new" * 20


def test_two_environments_are_both_offered(tmp_path):
    other = "https://fabrikam.crm4.dynamics.com"
    path = tmp_path / "c.har"
    path.write_text(har(
        entry(f"{ENV}/api/data/v9.2/x", "Bearer " + "a" * 60),
        entry(f"{other}/api/data/v9.2/x", "Bearer " + "b" * 60),
    ))

    found = dv.tokens_from_har(path)

    assert set(found) == {ENV, other}
    assert dv.environment_of(found) is None  # ambiguous -> the wizard must ask


def test_a_capture_with_no_dataverse_traffic_yields_nothing(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(har(entry("https://example.com/", "Bearer " + "z" * 60)))

    assert dv.tokens_from_har(path) == {}


def test_an_unreadable_capture_is_reported_not_swallowed(tmp_path):
    path = tmp_path / "c.har"
    path.write_text("{not json")

    with pytest.raises(dv.DataverseError, match="Could not read"):
        dv.tokens_from_har(path)

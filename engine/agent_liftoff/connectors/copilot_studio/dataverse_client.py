"""List and export Power Platform solutions over the Dataverse Web API.

Why this exists alongside `pac_client`
--------------------------------------
The PAC CLI is the sanctioned way in, and it is the right default. It is also a
single point of failure that an enterprise tenant can close without warning:

    pac org who
    Error: Failed to connect to Dataverse
        ExternalTokenManagement Authentication Requested but not configured correctly.
        AADSTS135010: UserPrincipal doesn't have the key ID configured.

That is Conditional Access refusing to issue a Dataverse token to the device
code flow PAC uses -- observed on a real tenant, where the *same machine, same
PAC install* signed into a second tenant fine. Nothing client-side fixes it,
and until it is fixed by an administrator the whole corridor is unreachable
even though the operator can open the maker portal in a browser and see their
solutions perfectly well.

So this module offers the other door. Everything PAC does for us is two plain
Dataverse Web API calls, and both take an ordinary bearer token:

    GET  /api/data/v9.2/solutions      -> what `pac solution list` prints
    POST /api/data/v9.2/ExportSolution -> what `pac solution export` writes

Verified against a live org: 78 solutions listed, and `ExportSolution` returned
a 1,644-byte payload whose first two bytes are `PK` -- a real solution zip.

The token is the operator's own, from their own browser session, held in memory
for the length of one migration and never written to disk. It is not a way
around a policy: if the browser cannot reach Dataverse either, there is no
token to copy and this door is shut too, which is the correct outcome.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

import requests

API_VERSION = "v9.2"
TIMEOUT = 180

# A Dataverse environment URL: `https://<org>.crm<n>.dynamics.com`. Matched
# rather than assumed so a token can be tied to the org it was issued for.
_DATAVERSE_HOST = re.compile(r"https://[a-z0-9-]+\.crm[0-9]*\.dynamics\.com", re.IGNORECASE)


class DataverseError(Exception):
    """A Dataverse call failed in a way the caller should show somebody."""


@dataclass
class Solution:
    """One solution, as the maker portal lists it."""

    unique_name: str
    friendly_name: str
    version: str
    managed: bool

    def label(self) -> str:
        state = "managed" if self.managed else "unmanaged"
        return f"{self.friendly_name}  ({self.unique_name} v{self.version}, {state})"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }


def _describe(response: requests.Response) -> str:
    """The message Dataverse actually returned, not just a status code."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    message = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
    return f"HTTP {response.status_code}: {message or response.text[:200]}"


def whoami(environment_url: str, token: str) -> dict:
    """Confirm the token is accepted by this org before anything else runs.

    Cheap, and it separates the two failures that otherwise look identical: a
    token for the wrong tenant (401 here) and a solution the user cannot read
    (403 later, on one solution only).
    """
    url = f"{environment_url.rstrip('/')}/api/data/{API_VERSION}/WhoAmI"
    try:
        response = requests.get(url, headers=_headers(token), timeout=60)
    except requests.RequestException as exc:
        raise DataverseError(f"Could not reach {environment_url}: {exc}") from exc
    if response.status_code == 401:
        raise DataverseError(
            "That token was refused by this environment. A token is only valid for the "
            "tenant it was issued in — check it came from a browser signed into this "
            "environment, and that it has not expired (they last about an hour)."
        )
    if response.status_code >= 300:
        raise DataverseError(_describe(response))
    return response.json()


def list_solutions(environment_url: str, token: str, visible_only: bool = True) -> list[Solution]:
    """Every solution in the environment. The Web API form of `pac solution list`."""
    url = f"{environment_url.rstrip('/')}/api/data/{API_VERSION}/solutions"
    params = {"$select": "uniquename,friendlyname,version,ismanaged"}
    if visible_only:
        # The invisible ones are Microsoft's internal plumbing; a person
        # choosing what to migrate should not have to scroll past them.
        params["$filter"] = "isvisible eq true"
    try:
        response = requests.get(url, headers=_headers(token), params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise DataverseError(f"Could not reach {environment_url}: {exc}") from exc
    if response.status_code >= 300:
        raise DataverseError(_describe(response))

    rows = response.json().get("value", [])
    return [
        Solution(
            unique_name=str(row.get("uniquename") or ""),
            friendly_name=str(row.get("friendlyname") or row.get("uniquename") or ""),
            version=str(row.get("version") or ""),
            managed=bool(row.get("ismanaged")),
        )
        for row in rows
        if row.get("uniquename")
    ]


def export_solution(
    environment_url: str, token: str, unique_name: str, managed: bool = False
) -> bytes:
    """The solution as a zip. The Web API form of `pac solution export`.

    Returns the decoded archive rather than the base64 the API hands back, so a
    caller writes bytes to a file and is done.
    """
    url = f"{environment_url.rstrip('/')}/api/data/{API_VERSION}/ExportSolution"
    payload = {"SolutionName": unique_name, "Managed": managed}
    try:
        response = requests.post(
            url,
            headers={**_headers(token), "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DataverseError(f"Could not reach {environment_url}: {exc}") from exc
    if response.status_code >= 300:
        raise DataverseError(_describe(response))

    blob = response.json().get("ExportSolutionFile")
    if not blob:
        raise DataverseError(
            f"{unique_name} exported with no file attached. That usually means the export "
            "was refused rather than empty."
        )
    data = base64.b64decode(blob)
    # A permission failure inside the solution can still come back 200 with
    # something that is not an archive. Checked, because the next thing that
    # happens is an unzip whose error would name a temp file, not the cause.
    if data[:2] != b"PK":
        raise DataverseError(
            f"{unique_name} did not export as a zip archive ({len(data)} bytes). "
            "The export may have been blocked by a permission on one of its components."
        )
    return data


# --------------------------------------------------------------------------- #
# Getting a token out of a browser
# --------------------------------------------------------------------------- #

def tokens_from_har(har_path: Path) -> dict[str, str]:
    """Every Dataverse bearer token a HAR contains, keyed by environment URL.

    A capture from the maker portal holds one on the `Authorization` header of
    each Dataverse call. Read rather than asked for because the alternative is
    talking somebody through DevTools' network pane and having them paste a
    2,000-character string without truncating it.

    Only Dataverse hosts are read. A HAR of a browser session contains tokens
    for whatever else that browser was doing, and none of that is this tool's
    business.
    """
    try:
        raw = json.loads(Path(har_path).read_text(errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataverseError(f"Could not read {har_path}: {exc}") from exc

    found: dict[str, str] = {}
    for entry in (raw.get("log") or {}).get("entries") or []:
        request = entry.get("request") or {}
        host = _DATAVERSE_HOST.match(str(request.get("url") or ""))
        if not host:
            continue
        for header in request.get("headers") or []:
            if str(header.get("name", "")).lower() != "authorization":
                continue
            value = str(header.get("value") or "")
            if value.lower().startswith("bearer ") and len(value) > 40:
                # Later entries win: a capture that spans a refresh should
                # yield the token that was still valid at the end of it.
                found[host.group(0)] = value.split(None, 1)[1].strip()
    return found


def environment_of(token_urls: dict[str, str]) -> str | None:
    """The single environment a HAR covered, when there is exactly one."""
    return next(iter(token_urls)) if len(token_urls) == 1 else None

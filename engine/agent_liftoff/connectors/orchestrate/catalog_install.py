"""Install a catalog tool onto the instance, the way the console does it.

For a long time the answer here was "you cannot": the catalog's own list
endpoints expose no download, and the only mutation anywhere near them is a
purchase flow that returns 400 for free IBM artifacts because they have no
plan. The console's page has no Install button to inspect either.

A recorded browser session settled it. Installing a catalog tool is two calls,
neither of them in the catalog API:

    GET  /mfe_builder/api/v2/builder/catalog/artifacts/{artifact_id}
             /specification?version={version}
         -> {"result": [ <the spec> ]}, including `attachments[].file_url`: a
            presigned object-storage URL for the tool's zip, valid briefly.

    POST /mfe_builder/api/v1/builder/tools/create-from-template
         body: [ <that spec, minus the response-only keys, plus workspace_id> ]
         -> 201 {"id": ["<the new tool id>"]}

The spec's *content* crosses untouched. That matters: the `file_url` is signed
and time-limited, the `binding` names the Python entry point the runtime will
import, and the `applications[].app_id` is the connection the tool will
authenticate through. Reconstructing any of it by hand would produce a tool
that installs and then cannot run. Only the envelope is adjusted, exactly as
the console adjusts it.

**This needs a console session, not an API key.** Both calls authenticate with
the browser's cookie plus the double-submit CSRF header, which is why the ADK
cannot do this and why an IAM bearer token gets nowhere. That is a real
limitation, not an oversight: it means unattended installation is not available
to a migration running on its own credentials, and the honest thing is to say
so rather than to half-work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests

from agent_liftoff.connectors.orchestrate.catalog_client import (
    CONSOLE_URL_ENV,
    _csrf_from_cookie,
    console_origin,
)
from agent_liftoff.errors import RemoteAPIError, LiftoffError

SPEC_PATH = "/mfe_builder/api/v2/builder/catalog/artifacts/{artifact_id}/specification"
CREATE_PATH = "/mfe_builder/api/v1/builder/tools/create-from-template"

# The presigned URL inside a specification expires. Fetching the spec and
# posting it back promptly is the whole design; holding one for later is how
# an install fails with an object-storage error nobody can interpret.
TIMEOUT = (10, 120)


@dataclass
class InstalledTool:
    """What the target created."""

    tool_id: str
    name: str
    display_name: str
    app_ids: tuple[str, ...] = ()
    # Auth kinds the tool's application declares. What a caller offers a person
    # to choose between, instead of picking one and hoping.
    security_schemes: tuple[str, ...] = ()

    def summary(self) -> str:
        conn = f", authenticates through {', '.join(self.app_ids)}" if self.app_ids else ""
        return f"{self.display_name} -> `{self.name}` (id {self.tool_id}){conn}"


class ConsoleSession:
    """A console session built from a browser `Cookie:` header.

    The cookie is held for the life of this object and never written anywhere.
    It is a live credential -- anyone holding it is the signed-in user -- so it
    is passed in by the caller rather than read from a file by this module.
    """

    def __init__(self, instance_url: str, cookie: str) -> None:
        cookie = (cookie or "").strip()
        if not cookie:
            raise LiftoffError(
                "Installing a catalog tool needs a console session cookie; the instance "
                "API key cannot authenticate against the console."
            )
        self.origin = console_origin(instance_url)
        csrf = _csrf_from_cookie(cookie)
        if not csrf:
            raise LiftoffError(
                "That cookie has no `__Secure-fgp` value, so the console's CSRF token "
                "cannot be derived from it. Copy the whole Cookie header from a "
                "signed-in console request."
            )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Cookie": cookie,
                "x-ibm-wo-csrf": csrf,
                "Accept": "application/json",
                "Origin": self.origin,
                "Referer": f"{self.origin}/catalog",
            }
        )

    def get(self, path: str, **params: Any) -> Any:
        return self._json(self._session.get(f"{self.origin}{path}", params=params, timeout=TIMEOUT))

    def post(self, path: str, body: Any) -> Any:
        return self._json(
            self._session.post(
                f"{self.origin}{path}",
                data=json.dumps(body),
                headers={"Content-Type": "application/json"},
                timeout=TIMEOUT,
            )
        )

    @staticmethod
    def _json(response: requests.Response) -> Any:
        if response.status_code == 401 or response.status_code == 403:
            raise RemoteAPIError(
                f"The console rejected the session ({response.status_code}). Console "
                "cookies are short-lived -- copy a fresh one from a signed-in browser."
            )
        if response.status_code >= 400:
            raise RemoteAPIError(
                f"Console returned {response.status_code} for {response.url.split('?')[0]}: "
                f"{' '.join(response.text.split())[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            # A console that answers HTML to an API path is answering the SPA
            # shell, which means the path is wrong -- not that the tool failed.
            raise RemoteAPIError(
                f"Console returned a non-JSON body for {response.url.split('?')[0]}. "
                f"Set {CONSOLE_URL_ENV} if your console lives elsewhere."
            ) from exc


VERSIONS_PATH = "/mfe_catalog/api/catalogv3/artifacts/{artifact_id}/versions"


def latest_version(session: ConsoleSession, artifact_id: str) -> str:
    """The newest published version of an artifact.

    Asked rather than assumed. The shipped catalog snapshot records no version
    at all, and the specification endpoint requires one -- so pinning a
    plausible-looking number here would install whatever that number happened
    to mean, on every tenant, forever.

    The endpoint returns versions newest first; that order is trusted only as a
    tiebreak, with the newest `created_at` deciding.
    """
    payload = session.get(VERSIONS_PATH.format(artifact_id=artifact_id), page=1, offset=5)
    versions = payload.get("versions") if isinstance(payload, dict) else None
    rows = [v for v in (versions or []) if isinstance(v, dict) and v.get("version")]
    if not rows:
        raise RemoteAPIError(
            f"The catalog lists no versions for artifact {artifact_id}, so there is "
            "nothing to install."
        )
    rows.sort(key=lambda v: str(v.get("created_at") or ""), reverse=True)
    return str(rows[0]["version"])


# The specification endpoint answers `{"result": [ {...} ]}`, and the console
# does not post that envelope back -- it posts the element inside it, with
# these two keys dropped and `workspace_id` added. Mirrored exactly rather than
# posting the response as-is, because a create endpoint that silently ignores
# unexpected keys today is not obliged to tomorrow.
_SPEC_ONLY_KEYS = ("master_artifact_id", "version")
DEFAULT_WORKSPACE = "00000000-0000-0000-0000-000000000001"


def fetch_specification(session: ConsoleSession, artifact_id: str, version: str) -> dict:
    """The artifact's installable spec, including its presigned download URL."""
    payload = session.get(SPEC_PATH.format(artifact_id=artifact_id), version=version)
    spec = payload.get("result") if isinstance(payload, dict) and "result" in payload else payload
    if isinstance(spec, list):
        spec = spec[0] if spec else {}
    if not isinstance(spec, dict) or not spec.get("name"):
        raise RemoteAPIError(
            f"The console returned no usable specification for artifact {artifact_id} "
            f"version {version}."
        )
    return spec


def installable_payload(spec: dict, workspace_id: str = DEFAULT_WORKSPACE) -> dict:
    """The spec in the shape `create-from-template` is given.

    The presigned `file_url` and the `binding` are carried through untouched --
    they are what make the installed tool runnable. Only the envelope differs.
    """
    body = {k: v for k, v in spec.items() if k not in _SPEC_ONLY_KEYS}
    body["workspace_id"] = workspace_id
    return body


def app_ids_in(spec: dict) -> tuple[str, ...]:
    """Connections the spec says this tool authenticates through.

    Read out of the spec rather than guessed from the tool's name, for the same
    reason `connections.bound_connection` exists: a name that looks like a
    ServiceNow connection is not evidence that the tool uses it.
    """
    inner = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    found = [
        str(app.get("app_id"))
        for app in (inner.get("applications") or [])
        if isinstance(app, dict) and app.get("app_id")
    ]
    return tuple(dict.fromkeys(found))


def security_schemes_in(spec: dict) -> tuple[str, ...]:
    """Auth kinds the tool's application actually supports.

    Read from the spec rather than defaulted, because guessing here is not a
    cosmetic error: configuring a ServiceNow connection as `bearer_token` when
    the tenant uses OAuth produces a connection that saves cleanly and fails on
    the first call, with an error about the response body rather than the auth.
    """
    inner = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    schemes: list[str] = []
    for app in inner.get("applications") or []:
        if isinstance(app, dict) and isinstance(app.get("security_schema"), dict):
            schemes.extend(str(k) for k in app["security_schema"])
    return tuple(dict.fromkeys(schemes))


def install(
    session: ConsoleSession, spec: dict, workspace_id: str = DEFAULT_WORKSPACE
) -> InstalledTool:
    """Create the tool from an artifact specification.

    The spec's own content is posted untouched. Its presigned `file_url` is
    what the platform fetches the implementation from and its `binding` names
    the entry point the runtime imports, so editing either -- even to tidy it
    -- is how an install turns into a tool that exists and cannot run.
    """
    created = session.post(CREATE_PATH, [installable_payload(spec, workspace_id)])
    ids = created.get("id") if isinstance(created, dict) else None
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        raise RemoteAPIError(
            "The console accepted the install but named no tool id, so there is nothing "
            "to attach to an agent. Check the tool list before re-running."
        )
    return InstalledTool(
        tool_id=str(ids[0]),
        name=str(spec.get("name") or ""),
        display_name=str(spec.get("display_name") or spec.get("name") or ""),
        app_ids=app_ids_in(spec),
        security_schemes=security_schemes_in(spec),
    )


def install_artifact(
    session: ConsoleSession,
    artifact_id: str,
    version: str | None = None,
    workspace_id: str = DEFAULT_WORKSPACE,
) -> InstalledTool:
    """Resolve the version if needed, fetch the spec, and install it.

    One function because the parts are not independently useful: the presigned
    URL inside a specification is short-lived, so a spec fetched now and
    installed later is a spec that fails.
    """
    version = version or latest_version(session, artifact_id)
    return install(session, fetch_specification(session, artifact_id, version), workspace_id)

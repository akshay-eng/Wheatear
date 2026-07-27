"""Client for the watsonx Orchestrate **catalog** -- the global library of
IBM- and partner-published tools, MCP servers and agents.

This is a different service from the instance API in `rest_client.py`, and the
distinction is the whole point of this module:

  * `rest_client.list_all_tools()` returns what is **installed** on your
    instance -- the tools an agent.yaml may reference today.
  * this returns what is **installable** -- verified on a live us-south
    tenant as 1152 tools, 21 MCP servers and 337 agents.

They live on different hosts. The instance API is
`https://api.<region>.watson-orchestrate.cloud.ibm.com/instances/<id>`; the
catalog is served by the console at `https://<region>.watson-orchestrate.cloud
.ibm.com/mfe_catalog/api/catalogv3` -- same region, no `api.` prefix, no
instance path segment. Sweeping the instance API's endpoints will never find
it, which is why an earlier probe concluded no catalog existed.

Nothing about the query is guessed at where the service will tell us instead:
categories come from `/artifacts/filters`, the page-size ceiling comes from the
service's own error message, and no type filter is sent at all (an unrecognised
type value rejects the entire query, and filtering by type returns exactly the
same tools as not filtering). What remains hardcoded is documented as a
fallback with a stated reason.

Auth is the awkward part. Verified against a live tenant: this service does not
accept an IAM bearer token -- it answers 500 `WXO-PROXY-11076E` because the
console proxy resolves tenant context from a session, and supplying the tenant
id and CRN as headers or cookies does not substitute. The instance API does
expose `/v1/orchestrate/catalog/artifacts`, which *is* IAM-authenticated, but
it is scoped to the tenant's own artifacts and returns nothing for the global
catalog. So a console session cookie is currently the only way in. We still try
the bearer first, because that costs one request and the day IBM opens it up
this starts working with no code change.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from wheatear.errors import RemoteAPIError

# Set this to override the derived console URL, for a deployment whose console
# doesn't sit at the instance host minus `api.` (Developer Edition, AWS SaaS,
# anything self-hosted).
CONSOLE_URL_ENV = "WXO_CONSOLE_URL"
COOKIE_ENV = "WXO_CONSOLE_COOKIE"

# Requested page size. The service caps this and says so in its own error, which
# `_ceiling_from_error` reads -- so this is a starting point, not a belief.
DEFAULT_PAGE_SIZE = 500

# Used only if `/artifacts/filters` can't be read. Verified as the complete set
# on a live tenant: any other value is rejected with "Invalid category value".
FALLBACK_CATEGORIES = ("tool", "mcp_server", "agent")

# Fields to project. Omitting this is allowed but the server's default set
# drops `tags`, `type` and `artifact_group`, all of which carry matching
# signal -- so we ask, and `to_artifacts` tolerates any of them being absent.
SELECT_FIELDS = [
    "name",
    "id",
    "description",
    "category",
    "publisher",
    "external_identifier",
    "tags",
    "createdAt",
    "artifact_group",
    "kind",
    "author",
    "type",
]

_LIMIT_RE = re.compile(r"between\s+\d+\s+and\s+(\d+)", re.I)


@dataclass
class CatalogArtifact:
    """One installable catalog entry, flattened to what matching needs.

    `params` and `connections` stay empty until `enrich_artifacts` fills them:
    the list endpoint returns no schema, and fetching one for all ~1500 entries
    would be 1500 requests to answer a question about eight of them.
    """

    id: str
    name: str
    description: str
    category: str  # "tool" | "mcp_server" | "agent"
    type: str | None  # "python" | "openapi" | "flow" | None
    external_identifier: str | None
    publisher: str | None
    tags: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    params: list[str] = field(default_factory=list)
    required_params: list[str] = field(default_factory=list)
    # App connections the tool needs before it will run, e.g. "gitlab_ibm_…".
    # An install isn't done until these are configured, so a reviewer has to
    # see them.
    connections: list[str] = field(default_factory=list)
    member_tools: list[str] = field(default_factory=list)
    enriched: bool = False

    @property
    def install_ref(self) -> str:
        """The name an installed copy of this artifact is referenced by.

        `external_identifier` is the tool's real name in the instance; the
        display name ("Add a comment in Google Drive") is not referenceable.
        Falling back to the display name keeps a record usable for review even
        when the identifier is absent.
        """
        return self.external_identifier or self.name


# ----------------------------------------------------------------------
# Locating the service
# ----------------------------------------------------------------------


def console_candidates(instance_url: str) -> list[str]:
    """Console catalog bases to try, best guess first.

    An explicit override wins outright. Otherwise the instance host minus its
    `api.` prefix is the shape IBM Cloud SaaS uses; the unmodified host is the
    fallback for deployments that serve both from one origin.
    """
    override = os.environ.get(CONSOLE_URL_ENV)
    if override:
        return [f"{override.rstrip('/')}/mfe_catalog/api/catalogv3"]

    parts = urlsplit(instance_url if "://" in instance_url else f"https://{instance_url}")
    host = parts.netloc
    if not host:
        raise RemoteAPIError(f"Could not read a host out of the instance URL '{instance_url}'.")
    scheme = parts.scheme or "https"

    hosts = [re.sub(r"^api\.", "", host)]
    if host not in hosts:
        hosts.append(host)
    return [f"{scheme}://{h}/mfe_catalog/api/catalogv3" for h in hosts]


def console_base(instance_url: str) -> str:
    """The single best-guess console catalog base."""
    return console_candidates(instance_url)[0]


def console_origin(instance_url: str) -> str:
    """The console's web origin -- what a person opens in a browser.

    Derived the same way as the catalog base and trimmed back to the host,
    because that host is verifiable and the routes under it are not: the
    console is a single-page app that answers 200 with the same shell for
    every path, including ones that do not exist. So this is as deep as a
    link can honestly go without somebody reading a real URL off their own
    address bar.
    """
    return console_base(instance_url).split("/mfe_catalog")[0]


def _csrf_from_cookie(cookie_header: str) -> str | None:
    """Derive the `x-ibm-wo-csrf` header from a browser `Cookie:` header.

    The console uses a double-submit token: the header is the SHA-256 of the
    `__Secure-fgp` cookie. Deriving it means a user only has to copy one thing.
    """
    for chunk in cookie_header.split(";"):
        name, _, value = chunk.strip().partition("=")
        if name == "__Secure-fgp" and value:
            return hashlib.sha256(value.encode()).hexdigest()
    return None


def _ceiling_from_error(text: str) -> int | None:
    """Read the page-size ceiling out of the service's own rejection.

    It answers "The limit value must be between 1 and 1000", so we never have
    to hardcode 1000 or discover it by bisection.
    """
    match = _LIMIT_RE.search(text or "")
    return int(match.group(1)) if match else None


def _category_criteria(categories: list[str]) -> list[dict]:
    """One filter group covering every category.

    Deliberately no `type` filter: filtering tools by type returns exactly the
    same set as not filtering, and a value the service doesn't recognise
    rejects the whole query -- so sending one is all risk and no benefit.
    """
    return [
        {
            "filters_operator": "AND",
            "filters": [{"condition_type": "in", "id": "category", "value": list(categories)}],
        }
    ]


class OrchestrateCatalogClient:
    """Read-only client for the console catalog service.

    Nothing here installs anything -- installation is a write against the
    instance API and stays behind its own explicit step.
    """

    def __init__(
        self,
        instance_url: str,
        api_key: str | None = None,
        session_cookie: str | None = None,
        csrf_token: str | None = None,
    ) -> None:
        session_cookie = session_cookie or os.environ.get(COOKIE_ENV)
        if not api_key and not session_cookie:
            raise RemoteAPIError(
                "The catalog needs either an API key or a console session cookie "
                f"(set {COOKIE_ENV})."
            )
        self._bases = console_candidates(instance_url)
        self.base = self._bases[0]
        self.page_size = DEFAULT_PAGE_SIZE
        self._session = requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

        if session_cookie:
            self.auth_mode = "session"
            self._session.headers["Cookie"] = session_cookie
            csrf = csrf_token or _csrf_from_cookie(session_cookie)
            if not csrf:
                raise RemoteAPIError(
                    "That cookie header has no `__Secure-fgp` value, so the CSRF token "
                    "can't be derived. Copy the whole Cookie header from a request the "
                    "catalog page made, not just part of it."
                )
            self._session.headers["x-ibm-wo-csrf"] = csrf
            origin = self.base.split("/mfe_catalog")[0]
            self._session.headers.update({"Origin": origin, "Referer": f"{origin}/catalog"})
        else:
            from wheatear.connectors.orchestrate.rest_client import get_iam_token

            self.auth_mode = "iam"
            self._session.headers["Authorization"] = f"Bearer {get_iam_token(api_key or '')}"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        """Issue a request, falling back across candidate console hosts.

        The host is derived from the instance URL, so a deployment that
        arranges things differently should surface as "wrong URL", not as a
        confusing error from whatever else answers at that address.
        """
        last: Exception | None = None
        for base in self._bases:
            url = f"{base}{path}"
            try:
                resp = self._session.request(method, url, timeout=(10, 60), **kwargs)
            except requests.RequestException as exc:
                last = RemoteAPIError(f"Could not reach the Orchestrate catalog at {url}: {exc}")
                continue
            if resp.status_code == 404 and base != self._bases[-1]:
                continue  # not the console; try the next candidate
            self.base = base
            return resp
        raise last or RemoteAPIError(
            f"No Orchestrate catalog answered at any of: {', '.join(self._bases)}. "
            f"Set {CONSOLE_URL_ENV} if your console lives elsewhere."
        )

    def _auth_hint(self, status: int) -> str:
        if self.auth_mode == "iam":
            return (
                f"The catalog rejected the IAM token ({status}). This service authenticates "
                "with a console session rather than an API key. Open the catalog page in a "
                "browser, copy the `Cookie:` header from any /mfe_catalog request "
                f"(F12 -> Network), and pass it via the {COOKIE_ENV} environment variable."
            )
        return (
            f"The catalog rejected the session cookie ({status}). Console sessions expire "
            "within hours -- reload the catalog page and copy a fresh `Cookie:` header."
        )

    def _check(self, resp, what: str) -> dict:
        if resp.status_code in (401, 403):
            raise RemoteAPIError(self._auth_hint(resp.status_code))
        if resp.status_code >= 400:
            detail = " ".join(resp.text.split())[:200]
            if self.auth_mode == "iam" and resp.status_code >= 500:
                # Verified behaviour: the proxy accepts the token and then fails
                # to resolve tenant context. Reporting it as a server fault
                # would send someone chasing an outage that isn't there.
                raise RemoteAPIError(
                    f"The catalog could not authorise the IAM token ({resp.status_code}: "
                    f"{detail}). It authenticates with a console session; set "
                    f"{COOKIE_ENV} to a `Cookie:` header copied from the catalog page."
                )
            raise RemoteAPIError(f"The catalog returned {resp.status_code} for {what}: {detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise RemoteAPIError(f"The catalog returned a non-JSON body for {what}.") from exc

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_categories(self) -> list[str]:
        """Ask the service what categories exist, rather than assuming.

        Falls back to the known set if the filters endpoint is unavailable --
        a filters outage shouldn't cost us the catalog.
        """
        try:
            payload = self._check(self._request("GET", "/artifacts/filters"), "/artifacts/filters")
        except RemoteAPIError:
            return list(FALLBACK_CATEGORIES)

        found: list[str] = []
        for group in payload.get("filters") or []:
            if group.get("id") != "category":
                continue
            for option in group.get("options") or []:
                value = option.get("value")
                if value and value not in found:
                    found.append(value)
        return found or list(FALLBACK_CATEGORIES)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def _post_list(self, criteria: list[dict], limit: int, offset: int) -> dict:
        payload = {
            "limit": limit,
            "offset": offset,
            "select_fields": SELECT_FIELDS,
            # Flat, not grouped: the grouped response keys by category and
            # carries no total, which is exactly what pagination needs.
            "grouped_results": False,
            "sort_by": "name",
            "sort_order": "ASC",
            "search_criteria": [{"filter_groups_operator": "OR", "filter_groups": criteria}],
        }
        resp = self._request("POST", "/artifacts/list", json=payload)

        # The service states its own page-size ceiling on rejection; adopt it
        # and retry rather than carrying a guess about someone else's limit.
        if resp.status_code == 400:
            ceiling = _ceiling_from_error(resp.text)
            if ceiling and ceiling < limit:
                self.page_size = ceiling
                payload["limit"] = ceiling
                resp = self._request("POST", "/artifacts/list", json=payload)

        return self._check(resp, "/artifacts/list")

    def _fetch_all(self, criteria: list[dict]) -> list[dict]:
        artifacts: list[dict] = []
        offset = 0
        while True:
            page = self._post_list(criteria, self.page_size, offset)
            batch = page.get("artifacts") or []
            artifacts.extend(batch)
            total = page.get("total")
            if not batch:
                break
            if isinstance(total, int) and len(artifacts) >= total:
                break
            if offset + len(batch) <= offset:  # no forward progress; refuse to spin
                break
            offset += len(batch)
        return artifacts

    def list_artifacts(self, categories: list[str] | None = None) -> list[dict]:
        """Every artifact in the catalog, across every category it publishes.

        One query rather than one per category: the filter takes a list, and a
        single sweep keeps pagination honest against a single total.
        """
        return self._fetch_all(_category_criteria(categories or self.discover_categories()))

    def list_installable(self, include_agents: bool = False) -> list[dict]:
        """Tools and MCP servers -- the artifacts a tool reference can resolve to.

        Catalog agents are a different migration primitive (a collaborator, not
        a tool), so they're off by default.
        """
        categories = [c for c in self.discover_categories() if c != "agent" or include_agents]
        return self.list_artifacts(categories)

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------

    def get_artifact(self, artifact_id: str) -> dict:
        """Full record for one artifact, including `spec_file`.

        This is where the parameter schema lives -- the list endpoint has none.
        One request per artifact, so it's for shortlisted candidates only.
        """
        return self._check(
            self._request("GET", f"/artifacts/{artifact_id}"), f"/artifacts/{artifact_id}"
        )


# ----------------------------------------------------------------------
# Flattening
# ----------------------------------------------------------------------


def to_artifacts(records: list[dict]) -> list[CatalogArtifact]:
    """Flatten raw catalog records, dropping anything unnameable.

    A record with no name can't be shown to a reviewer or referenced after
    install, so proposing one would only produce a dead end.
    """
    artifacts: list[CatalogArtifact] = []
    for raw in records or []:
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        groups = tuple(
            g.get("name", "") for g in (raw.get("artifact_group") or []) if isinstance(g, dict)
        )
        artifacts.append(
            CatalogArtifact(
                id=raw.get("id") or "",
                name=name,
                description=(raw.get("description") or "").strip(),
                category=raw.get("category") or "tool",
                type=raw.get("type"),
                external_identifier=raw.get("external_identifier"),
                publisher=raw.get("publisher"),
                tags=tuple(raw.get("tags") or []),
                groups=tuple(g for g in groups if g),
            )
        )
    return artifacts


def apply_detail(artifact: CatalogArtifact, detail: dict) -> CatalogArtifact:
    """Fold a `get_artifact` response into an artifact, in place.

    `spec_file` is where the real signature lives: `input_schema` for a tool,
    `mcp.tools` for an MCP server, and `applications` for the connections that
    have to exist before either will run.
    """
    spec = detail.get("spec_file") or {}

    schema = spec.get("input_schema") or {}
    properties = schema.get("properties") or {}
    if properties:
        artifact.params = sorted(properties.keys())
    required = schema.get("required") or []
    if isinstance(required, list):
        artifact.required_params = [str(r) for r in required]

    artifact.connections = [
        app.get("app_id")
        for app in (spec.get("applications") or [])
        if isinstance(app, dict) and app.get("app_id")
    ]

    mcp_tools = ((spec.get("mcp") or {}).get("tools")) or []
    if isinstance(mcp_tools, list):
        artifact.member_tools = [str(t) for t in mcp_tools]

    artifact.enriched = True
    return artifact


def enrich_artifacts(
    client: OrchestrateCatalogClient, artifacts: list[CatalogArtifact]
) -> list[CatalogArtifact]:
    """Fetch full detail for a handful of artifacts.

    Called on a shortlist, never on the catalog: it's one HTTP request each,
    and ~1500 of them to answer a question about eight would be absurd. An
    individual failure leaves that artifact un-enriched rather than sinking the
    batch -- prose-only matching is degraded, not broken.
    """
    for artifact in artifacts:
        if artifact.enriched or not artifact.id:
            continue
        try:
            apply_detail(artifact, client.get_artifact(artifact.id))
        except RemoteAPIError:
            continue
    return artifacts
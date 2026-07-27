"""Deterministic tool-catalog matcher: source connector -> Orchestrate catalog.

A source platform's "built-in connector" (an n8n `n8n-nodes-base.slack` node, a
Copilot `ServiceNow` action) has no universal 1:1 target, but Orchestrate ships
a large curated catalog of prebuilt tools/agents/MCP servers. This module loads
a point-in-time snapshot of that catalog and resolves a source connector to the
best catalog entry by name -- deterministically, no LLM.

It's the concrete fill for `pipeline/map.py`'s previously-empty
`KNOWN_TOOL_MAPPINGS` seam: Map calls `connector_resolver(app, desc)` before
falling back to its manual-rebuild flag. The AI/semantic tier lives separately
(`catalog_semantic.py`) and is never imported here -- this stays deterministic
and auditable.

Catalog shape (each item): id, name, install_ref, category (tool|agent|
mcp_server), type, publisher, description, tags, offerings, params,
required_params, connections, member_tools. Tool names are action phrases that
end in the app ("Accept a Merge Request in GitLab"); offerings name the app too
("Devops and CICD Management with Gitlab"). We mine the app token from both to
build an app -> items index, which is what a bare connector name matches against.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
DEFAULT_CATALOG_PATH = _DATA_DIR / "catalog-snapshot.json"
DEFAULT_N8N_NODE_CATALOG_PATH = _DATA_DIR / "n8n-node-catalog-snapshot.json"

# Confidence floor for a match to be returned at all.
DEFAULT_MIN_CONFIDENCE = 0.7
# At/above this, a match is trusted (not flagged review_required by Map).
HIGH_CONFIDENCE = 0.9

# Category preference when one app maps to several catalog entries. A source
# *connector tool* is best replaced by a hosted MCP server (one endpoint, many
# tools) or a concrete python tool; a prebuilt *agent* bundles behavior and is
# a semantically heavier, last-resort target for a tool reference.
_CATEGORY_RANK = {"mcp_server": 3, "tool": 2, "agent": 1}

_STOPWORDS = {"a", "an", "the", "in", "on", "to", "for", "with", "of", "and", "your"}

# n8n credential-type -> Orchestrate `connections configure --kind`. Sets
# ConnectionRef.auth_type so the human is asked for the right shape of secret.
# Secrets themselves are never stored. Extend as new credential types appear.
N8N_CRED_KIND_MAP: dict[str, str] = {
    "slackApi": "api_key",
    "slackOAuth2Api": "oauth_auth_code_flow",
    "githubApi": "api_key",
    "githubOAuth2Api": "oauth_auth_code_flow",
    "gmailOAuth2": "oauth_auth_code_flow",
    "googleSheetsOAuth2Api": "oauth_auth_code_flow",
    "googlePalmApi": "api_key",
    "httpBasicAuth": "basic",
    "httpHeaderAuth": "api_key",
    "httpBearerAuth": "bearer",
    "postgres": "key_value",
    "mySql": "key_value",
    "openAiApi": "api_key",
    "anthropicApi": "api_key",
}


@dataclass(frozen=True)
class CatalogItem:
    id: str
    name: str
    install_ref: str
    category: str
    type: str | None
    description: str
    tags: tuple[str, ...] = ()
    offerings: tuple[str, ...] = ()
    required_params: tuple[str, ...] = ()
    connections: tuple = ()
    member_tools: tuple[str, ...] = ()

    @property
    def requires_connection(self) -> bool:
        return bool(self.connections or self.required_params)


@dataclass(frozen=True)
class CatalogMatch:
    install_ref: str
    catalog_id: str
    name: str
    category: str
    confidence: float
    tier: str  # "exact" | "app_name" | "app_token"
    member_tools: tuple[str, ...] = ()
    required_connection: bool = False


@dataclass
class Catalog:
    items: list[CatalogItem]
    by_norm_name: dict[str, list[CatalogItem]] = field(default_factory=dict)
    by_install_ref: dict[str, CatalogItem] = field(default_factory=dict)
    # normalized app token -> catalog items that belong to that app
    app_index: dict[str, list[CatalogItem]] = field(default_factory=dict)


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _norm_tokens(text: str) -> list[str]:
    return [t for t in _norm(text).split() if t and t not in _STOPWORDS]


# Every catalog name ends in the app it targets: a tool "Accept a Merge Request
# in GitLab", an agent "Catalog Management with Coupa", "Client Outreach on
# Outlook". We mine the app token from the NAME only -- NOT offerings, which are
# cross-cutting usage groupings ("Write Data in Excel" is listed under a Coupa
# offering) and would cross-contaminate the app index with false matches.
_NAME_TAIL_RE = re.compile(
    r"\b(?:in|on|for|to|from|with)\s+([A-Za-z0-9][\w .&/-]*?)\s*$"
)


def _app_tokens_for(item: CatalogItem) -> set[str]:
    """Extract the app token(s) (normalized) an item belongs to, from its name."""
    tokens: set[str] = set()
    m = _NAME_TAIL_RE.search(item.name)
    if m:
        tokens.add(_norm(m.group(1)))
    # A short mcp_server/agent name may itself be the bare app ("Slack", "Jira").
    if item.category in ("mcp_server", "agent") and len(item.name.split()) <= 2:
        tokens.add(_norm(item.name))
    return {t for t in tokens if t}


@lru_cache(maxsize=2)
def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> Catalog:
    raw = json.loads(Path(path).read_text())
    items: list[CatalogItem] = []
    for r in raw:
        items.append(
            CatalogItem(
                id=r.get("id", ""),
                name=r.get("name", ""),
                install_ref=r.get("install_ref", ""),
                category=r.get("category", ""),
                type=r.get("type"),
                description=r.get("description", ""),
                tags=tuple(r.get("tags") or ()),
                offerings=tuple(r.get("offerings") or ()),
                required_params=tuple(r.get("required_params") or ()),
                connections=tuple(r.get("connections") or ()),
                member_tools=tuple(r.get("member_tools") or ()),
            )
        )

    catalog = Catalog(items=items)
    for item in items:
        catalog.by_norm_name.setdefault(_norm(item.name), []).append(item)
        if item.install_ref:
            catalog.by_install_ref.setdefault(item.install_ref, item)
        for token in _app_tokens_for(item):
            catalog.app_index.setdefault(token, []).append(item)
    return catalog


@lru_cache(maxsize=1)
def load_n8n_node_index(path: Path = DEFAULT_N8N_NODE_CATALOG_PATH) -> dict[str, dict]:
    """n8n node type (install_ref) -> {name, credentials, description}.

    The authoritative source-side map from a node's `type` string to its app
    display name and required credential types. Best-effort: returns {} if the
    snapshot isn't present.
    """
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    index: dict[str, dict] = {}
    for r in raw:
        ref = r.get("install_ref")
        if ref:
            index[ref] = {
                "name": r.get("name", ""),
                "credentials": r.get("credentials") or [],
                "description": r.get("description", ""),
            }
    return index


def _best_of(items: list[CatalogItem]) -> CatalogItem:
    """Pick the preferred item from a group: highest category rank, then the
    shortest name (a general/root tool over a hyper-specific one)."""
    return sorted(
        items, key=lambda i: (_CATEGORY_RANK.get(i.category, 0), -len(i.name)), reverse=True
    )[0]


def match_connector(
    app_name: str,
    description: str | None = None,
    *,
    catalog: Catalog | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> CatalogMatch | None:
    """Resolve a source connector (by app name) to the best Orchestrate catalog
    entry. Deterministic cascade; returns None below `min_confidence`.
    """
    if not app_name:
        return None
    cat = catalog or load_catalog()
    norm = _norm(app_name)
    if not norm:
        return None

    # 1. Exact full-name match (mostly hits mcp_server/agent whose name IS the app).
    exact = cat.by_norm_name.get(norm)
    if exact:
        return _to_match(_best_of(exact), exact, 1.0, "exact")

    # 2. App-token exact key: the connector name equals a mined app token.
    grouped = cat.app_index.get(norm)
    if grouped:
        return _to_match(_best_of(grouped), grouped, 0.9, "app_name")

    # 3. App-token fuzzy: connector name is a whole-word token-subset of an app
    #    key (handles multi-word apps, e.g. "salesforce" ~ "salesforce marketing
    #    cloud"). Requires a clear uniqueness margin to avoid false positives.
    query_tokens = set(_norm_tokens(app_name))
    if query_tokens:
        candidates: list[tuple[str, list[CatalogItem]]] = []
        for key, group in cat.app_index.items():
            key_tokens = set(key.split())
            if query_tokens and query_tokens <= key_tokens:
                candidates.append((key, group))
        if len(candidates) == 1:
            _, group = candidates[0]
            return _to_match(_best_of(group), group, 0.7, "app_token")
        # >1 candidate app => ambiguous; don't guess (below min_confidence).

    return None


def _to_match(
    best: CatalogItem, group: list[CatalogItem], confidence: float, tier: str
) -> CatalogMatch | None:
    if confidence < DEFAULT_MIN_CONFIDENCE:
        return None
    # If the group is a set of python tools under one app, surface them as
    # member_tools so the migration documents the whole toolkit, not one tool.
    members = tuple(sorted({i.name for i in group if i.category == "tool"}))
    return CatalogMatch(
        install_ref=best.install_ref,
        catalog_id=best.id,
        name=best.name,
        category=best.category,
        confidence=confidence,
        tier=tier,
        member_tools=members if len(members) > 1 else (),
        required_connection=best.requires_connection,
    )


def connector_resolver(min_confidence: float = DEFAULT_MIN_CONFIDENCE):
    """Return a `(app_name, description) -> CatalogMatch | None` closure over a
    loaded catalog, suitable to pass to `map_agent(..., connector_resolver=)`.
    """
    cat = load_catalog()

    def resolve(app_name: str, description: str | None = None) -> CatalogMatch | None:
        return match_connector(
            app_name, description, catalog=cat, min_confidence=min_confidence
        )

    return resolve


def auth_kind_for_n8n_credential(cred_type: str) -> str:
    """n8n credential type -> Orchestrate connection kind (auth_type). Unknown
    types default to 'api_key' (the most common) with review always required.
    """
    return N8N_CRED_KIND_MAP.get(cred_type, "api_key")

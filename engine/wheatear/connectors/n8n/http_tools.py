"""n8n `toolHttpRequest` nodes -> a portable HTTP tool description.

An n8n HTTP tool is already a complete API call: a method, a URL, query and
body parameters, and a statement of which of those the *model* supplies rather
than the workflow author. That last part is the whole reason this module
exists. n8n splits every parameter into one of two worlds:

    valueProvider: fieldValue      the author fixed it   -> a constant
    valueProvider: modelRequired   the model fills it in -> a real parameter

and separately allows `{placeholder}` substitution anywhere in the URL, the
query or the headers, defined in `placeholderDefinitions`.

Read naively, an n8n HTTP tool looks like a request with a dozen parameters.
Read correctly, most of those are constants the author baked in, and only the
placeholders and `modelRequired` entries are the tool's actual signature. That
distinction is what makes the difference between migrating a tool the target
model can call and migrating one it cannot:

    sysparm_query = "workflow_state=published^short_descriptionLIKE{searchTerm}"
        -> one parameter, `searchTerm`, not a free-text ServiceNow query

Getting that wrong in either direction breaks the tool. Treat a constant as a
parameter and the model is asked to invent ServiceNow encoded-query syntax it
was never meant to see. Treat a placeholder as a constant and the tool always
searches for the literal string `{searchTerm}`.

What this module deliberately does NOT do is resolve credentials. An n8n node
names a credential (`httpHeaderAuth` -> "ServiceNow - Bearer") but
never contains its value -- n8n encrypts those and the API redacts them. So the
credential travels as a *reference*, and the secret is asked for at
provisioning time. That is the same contract the Copilot corridor uses, and it
is the reason a migration never carries a secret between two platforms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from wheatear.connectors.base import ToolParam

# `{searchTerm}` -- n8n's placeholder syntax, used in the URL, query and headers.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# n8n's two "the model supplies this" providers. Anything else is authored.
_MODEL_PROVIDED = {"modelRequired", "modelOptional"}


@dataclass
class HttpParam:
    """One parameter the target model will have to supply."""

    name: str
    description: str = ""
    required: bool = True
    # Where it goes in the request. Path params come from `{...}` in the URL.
    location: str = "query"  # "query" | "path" | "header"

    def to_tool_param(self) -> ToolParam:
        return ToolParam(name=self.name, description=self.description or None, type="string")


@dataclass
class HttpToolSpec:
    """A single n8n HTTP tool, resolved into something a target can build.

    `base_url` and `path` are split because the target asks the user to confirm
    the base URL at provisioning time -- a migrated tool nearly always points at
    a different instance than the one it was exported from, and an endpoint
    baked into a spec is the most common reason a migrated tool 404s.
    """

    name: str
    description: str = ""
    method: str = "GET"
    base_url: str = ""
    path: str = "/"
    # Author-fixed query parameters, after placeholder substitution is accounted
    # for. Values may still contain `{placeholder}` markers.
    constants: dict[str, str] = field(default_factory=dict)
    params: list[HttpParam] = field(default_factory=list)
    # The n8n credential this node used, by display name. Never a secret.
    credential_ref: str | None = None
    credential_kind: str | None = None  # e.g. "httpHeaderAuth"
    # Set when the node named an auth type we could not classify, so the
    # provisioner asks rather than silently emitting an unauthenticated tool.
    notes: list[str] = field(default_factory=list)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.path}"

    def operation_id(self) -> str:
        """A stable, syntactically valid operation name.

        Derived from the display name because that is what the source author
        chose and what the instructions refer to; an n8n node id would be
        meaningless on the target.
        """
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", self.name).strip("_").lower()
        return slug or "http_tool"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def placeholders_in(*values: object) -> list[str]:
    """Every `{placeholder}` appearing in the given strings, in order, deduped."""
    seen: list[str] = []
    for value in values:
        for match in _PLACEHOLDER.findall(_text(value)):
            if match not in seen:
                seen.append(match)
    return seen


def is_http_tool(node: dict) -> bool:
    return str(node.get("type", "")).endswith(".toolHttpRequest")


def _keypairs(params: object) -> list[dict]:
    """The `values` list out of an n8n fixedCollection, defensively."""
    if not isinstance(params, dict):
        return []
    values = params.get("values")
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def extract(node: dict) -> HttpToolSpec:
    """Turn one `toolHttpRequest` node into an `HttpToolSpec`.

    Total, not partial: a node missing a URL still yields a spec, with the
    problem recorded in `notes`. A migration that silently skipped a malformed
    tool would report a clean run and deploy an agent that cannot do its job.
    """
    params = node.get("parameters") or {}
    url = _text(params.get("url"))
    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}" if split.scheme and split.netloc else ""
    path = split.path or "/"

    spec = HttpToolSpec(
        name=_text(node.get("name")) or "HTTP tool",
        description=" ".join(_text(params.get("toolDescription")).split()),
        method=(_text(params.get("method")) or "GET").upper(),
        base_url=base_url,
        path=path,
    )
    if not base_url:
        spec.notes.append(f"could not read a base URL from {url!r}")

    # --- what the model must supply -------------------------------------
    # Two independent sources, and both matter. `placeholderDefinitions`
    # carries the human-written description, which is the highest-signal
    # thing a target model gets, so it wins where both describe a name.
    described: dict[str, str] = {}
    for entry in _keypairs(params.get("placeholderDefinitions")):
        name = _text(entry.get("name"))
        if name:
            described[name] = " ".join(_text(entry.get("description")).split())

    ordered: list[str] = []

    def note_param(name: str, location: str) -> None:
        if name and not any(p.name == name for p in spec.params):
            spec.params.append(
                HttpParam(name=name, description=described.get(name, ""), location=location)
            )
            ordered.append(name)

    # 1. Placeholders in the URL become path parameters.
    for name in placeholders_in(url):
        note_param(name, "path")

    # 2. Query parameters: authored ones are constants, model ones are params,
    #    and an authored value may still *contain* placeholders.
    for entry in _keypairs(params.get("parametersQuery")):
        name = _text(entry.get("name"))
        if not name:
            continue
        provider = _text(entry.get("valueProvider")) or "modelRequired"
        if provider in _MODEL_PROVIDED:
            note_param(name, "query")
            if provider == "modelOptional":
                for p in spec.params:
                    if p.name == name:
                        p.required = False
            continue
        value = _text(entry.get("value"))
        spec.constants[name] = value
        for inner in placeholders_in(value):
            note_param(inner, "query")

    # 3. Headers, same rule.
    for entry in _keypairs(params.get("parametersHeaders")):
        name = _text(entry.get("name"))
        provider = _text(entry.get("valueProvider")) or "modelRequired"
        if name and provider in _MODEL_PROVIDED:
            note_param(name, "header")

    # Any placeholder that was defined but never referenced is dead weight;
    # any referenced but undefined still has to be sent, just undocumented.
    for name in described:
        if not any(p.name == name for p in spec.params):
            spec.notes.append(f"placeholder {name!r} is defined but never used")

    # --- credential reference (never the secret) -------------------------
    creds = node.get("credentials")
    if isinstance(creds, dict) and creds:
        kind, detail = next(iter(creds.items()))
        spec.credential_kind = kind
        if isinstance(detail, dict):
            spec.credential_ref = _text(detail.get("name")) or None
    auth = _text(params.get("authentication"))
    if auth and auth != "none" and not spec.credential_kind:
        spec.notes.append(f"authentication is {auth!r} but the node names no credential")

    return spec


def to_openapi(specs: list[HttpToolSpec], title: str, server_url: str) -> dict:
    """One OpenAPI 3.0 document covering every tool that shares a base URL.

    One document rather than one per tool because Orchestrate binds credentials
    per imported spec: tools that authenticate the same way and live on the
    same host are one connection on the target, which is also how the source
    had it -- three n8n nodes sharing one credential.
    """
    paths: dict[str, dict] = {}
    for spec in specs:
        parameters = []
        for p in spec.params:
            if p.location == "header":
                continue  # carried by the connection, not the model
            parameters.append(
                {
                    "name": p.name,
                    "in": p.location,
                    "required": True if p.location == "path" else p.required,
                    "description": p.description or f"{p.name} for {spec.name}",
                    "schema": {"type": "string"},
                }
            )
        # Constants are sent as fixed defaults so the target reproduces the
        # source request exactly without asking the model to restate them.
        for name, value in spec.constants.items():
            if _PLACEHOLDER.search(value):
                continue  # templated constants are rebuilt from path params
            parameters.append(
                {
                    "name": name,
                    "in": "query",
                    "required": False,
                    "description": f"Fixed by the source workflow ({spec.name}).",
                    "schema": {"type": "string", "default": value},
                }
            )
        operation = {
            "operationId": spec.operation_id(),
            "summary": spec.name,
            "description": spec.description or spec.name,
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "Successful response",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        }
        entry = paths.setdefault(spec.path, {})
        entry[spec.method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": "1.0.0",
            "description": f"Migrated from n8n HTTP request tools ({len(specs)} operation(s)).",
        },
        "servers": [{"url": server_url.rstrip("/")}],
        "paths": paths,
    }


def group_by_host(specs: list[HttpToolSpec]) -> dict[str, list[HttpToolSpec]]:
    """Tools bucketed by base URL, preserving order within each bucket."""
    grouped: dict[str, list[HttpToolSpec]] = {}
    for spec in specs:
        grouped.setdefault(spec.base_url, []).append(spec)
    return grouped

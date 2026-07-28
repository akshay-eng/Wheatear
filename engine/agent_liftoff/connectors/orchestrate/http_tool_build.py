"""Rebuild a source platform's HTTP tools as watsonx Orchestrate Python tools.

Why Python and not OpenAPI
--------------------------
OpenAPI is the obvious choice and it is the wrong one, for a reason worth
stating because it is easy to rediscover the hard way.

An n8n HTTP tool routinely fixes a parameter to a *template*:

    sysparm_query = "number={recordNumber}"

The tool's real signature is `recordNumber`. The wire format is
`?sysparm_query=number=INC0010864`. OpenAPI has no way to say "take this
parameter, substitute it into that string, then send the result": a parameter
is either sent under its own name or it is not sent. So an OpenAPI translation
has two options, and both are broken --

  * expose `recordNumber` as a query parameter, and send
    `?recordNumber=INC0010864`, which ServiceNow ignores; the tool returns the
    unfiltered table and the agent confidently describes the wrong record.
  * expose `sysparm_query` itself and describe the syntax in prose, which makes
    the target model responsible for inventing ServiceNow encoded-query
    grammar that the source author had already written correctly.

A generated Python function has no such gap. It takes `recordNumber`, builds
the query the source built, and sends it. The signature the target model sees
is exactly the signature the source model saw, which is the only definition of
a faithful tool migration.

Credentials
-----------
Every tool for one host shares one connection, whose auth kind the operator
chooses from the same menu the Copilot corridor offers. That choice decides the
generated code, because each of Orchestrate's credential types is read through
its own accessor and exposes its own fields:

    bearer_token    connections.bearer_token(app)  -> .url, .token
    basic_auth      connections.basic_auth(app)    -> .url, .username, .password
    api_key_auth    connections.api_key_auth(app)  -> .url, .api_key
    key_value_creds connections.key_value(app)     -> a free-form mapping

The typed kinds all carry `url`, which is why the endpoint is read from the
connection rather than frozen into the generated source: a migrated tool almost
never points at the instance it was exported from, and a baked-in endpoint is
the most common reason one 404s on the target.

The auth kind is asked rather than inferred because n8n does not reveal it.
It encrypts credential values and its API redacts them, so a migration knows a
credential existed and what it was called -- never what it held, and not
reliably how it was presented.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path

from agent_liftoff.connectors.n8n.http_tools import HttpToolSpec, placeholders_in

# The `key_value` keys, used only when the operator picks that kind. Named here
# rather than spelled out at each use so the generator, the provisioner and the
# wizard prompt cannot drift apart.
KEY_BASE_URL = "base_url"
KEY_AUTH_HEADER = "auth_header"

_TEMPLATE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# How each auth kind is read at runtime and turned into a request. One table
# rather than a branch per kind in the renderer, so adding a kind is a row.
#
#   accessor     the `connections.*` function to call
#   conn_type    the ConnectionType the @tool decorator must declare
#   base         expression yielding the server URL
#   apply        statements that add authentication to the outgoing request
AUTH_KINDS: dict[str, dict[str, object]] = {
    "bearer_token": {
        "accessor": "bearer_token",
        "conn_type": "BEARER_TOKEN",
        "base": "str(creds.url or '').rstrip('/')",
        "apply": ["    headers['Authorization'] = f'Bearer {creds.token}'"],
    },
    "basic_auth": {
        "accessor": "basic_auth",
        "conn_type": "BASIC_AUTH",
        "base": "str(creds.url or '').rstrip('/')",
        "apply": ["    auth = (creds.username, creds.password)"],
    },
    "api_key_auth": {
        "accessor": "api_key_auth",
        "conn_type": "API_KEY_AUTH",
        # An API key has no single standard home. The Authorization header is
        # the most common and the least surprising; a source that wanted it in
        # a query parameter already encoded that as a constant.
        "base": "str(creds.url or '').rstrip('/')",
        "apply": ["    headers['Authorization'] = str(creds.api_key)"],
    },
    "key_value_creds": {
        "accessor": "key_value",
        "conn_type": "KEY_VALUE",
        "base": f"str(creds[{KEY_BASE_URL!r}]).rstrip('/')",
        "apply": [
            f"    header_value = creds.get({KEY_AUTH_HEADER!r})",
            "    if header_value:",
            "        headers['Authorization'] = str(header_value)",
        ],
    },
}

DEFAULT_AUTH_KIND = "bearer_token"


def safe_identifier(name: str) -> str:
    """A Python identifier that is still recognisably the source name."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]", "_", name).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"p_{cleaned}"
    if keyword.iskeyword(cleaned):
        cleaned = f"{cleaned}_"
    return cleaned


def app_id_for(host: str) -> str:
    """A stable connection id derived from the host.

    Derived from the host rather than the tool name because the connection is
    per-instance, not per-operation: three ServiceNow tools are one credential
    on the source and must be one credential on the target.
    """
    netloc = re.sub(r"^https?://", "", host).split("/")[0]
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", netloc).strip("_").lower()
    return slug or "migrated_http"


def _py(value: str) -> str:
    """A Python string literal, safely quoted."""
    return repr(value)


def _render_tool(spec: HttpToolSpec, app_id: str, auth_kind: str = DEFAULT_AUTH_KIND) -> str:
    """One `@tool` function reproducing one source HTTP tool."""
    auth = AUTH_KINDS.get(auth_kind) or AUTH_KINDS[DEFAULT_AUTH_KIND]
    fn = safe_identifier(spec.operation_id())
    args = [safe_identifier(p.name) for p in spec.params]
    arg_list = ", ".join(f"{a}: str" for a in args) or ""

    # Map the source parameter name to its Python argument, so a templated
    # constant can be rebuilt with the caller's value.
    binding = {p.name: safe_identifier(p.name) for p in spec.params}

    lines: list[str] = []
    lines.append("@tool(")
    lines.append(f"    name={_py(fn)},")
    desc = spec.description or spec.name
    lines.append(f"    description={_py(desc)},")
    lines.append("    permission=ToolPermission.READ_ONLY,")
    lines.append(
        f"    expected_credentials=[{{'app_id': {_py(app_id)}, "
        f"'type': ConnectionType.{auth['conn_type']}}}],"
    )
    lines.append(")")
    lines.append(f"def {fn}({arg_list}) -> dict:")

    # Docstring: the target model reads this, so it carries the parameter
    # descriptions the source author wrote.
    doc = [f'    """{desc}']
    if spec.params:
        doc.append("")
        doc.append("    Args:")
        for p in spec.params:
            text = p.description or f"{p.name} for {spec.name}"
            doc.append(f"        {safe_identifier(p.name)}: {text}")
    doc.append('    """')
    lines.extend(doc)

    lines.append(f"    creds = connections.{auth['accessor']}({_py(app_id)})")
    lines.append(f"    base = {auth['base']}")
    # A connection that exists but has no URL is the failure this whole change
    # is about. Saying so beats `requests` reporting an invalid URL for `/api`.
    lines.append("    if not base:")
    lines.append(
        f"        raise RuntimeError({_py(f'Connection {app_id} has no server URL configured for this environment.')})"
    )

    # Path: substitute `{placeholder}` segments from the arguments.
    path_expr = _py(spec.path)
    for name in placeholders_in(spec.path):
        arg = binding.get(name)
        if arg:
            path_expr = f"{path_expr}.replace({_py('{' + name + '}')}, str({arg}))"
    lines.append(f"    path = {path_expr}")

    # Query: constants first (templated ones rebuilt), then model-supplied.
    lines.append("    params = {}")
    for key, value in spec.constants.items():
        expr = _py(value)
        for name in placeholders_in(value):
            arg = binding.get(name)
            if arg:
                expr = f"{expr}.replace({_py('{' + name + '}')}, str({arg}))"
        lines.append(f"    params[{_py(key)}] = {expr}")
    for p in spec.params:
        if p.location != "query":
            continue
        # A parameter already consumed by a templated constant must not also be
        # sent under its own name -- that is the bug this module exists to
        # avoid, and it is worth being explicit about rather than implicit.
        consumed = any(
            p.name in placeholders_in(v) for v in spec.constants.values()
        )
        if consumed:
            continue
        arg = binding[p.name]
        if p.required:
            lines.append(f"    params[{_py(p.name)}] = {arg}")
        else:
            lines.append(f"    if {arg} is not None:")
            lines.append(f"        params[{_py(p.name)}] = {arg}")

    lines.append("    headers = {'Accept': 'application/json'}")
    lines.append("    auth = None")
    lines.extend(auth["apply"])
    lines.append(
        f"    response = requests.request({_py(spec.method)}, base + path, "
        "params=params, headers=headers, auth=auth, timeout=60)"
    )
    lines.append("    response.raise_for_status()")
    # Not every endpoint returns JSON; a tool that raises on an HTML error page
    # tells the agent nothing useful.
    lines.append("    try:")
    lines.append("        return response.json()")
    lines.append("    except ValueError:")
    lines.append("        return {'status': response.status_code, 'text': response.text[:4000]}")
    return "\n".join(lines)


HEADER = '''"""Tools migrated from n8n HTTP request nodes by Agent Liftoff.

Generated -- edit the source workflow and migrate again rather than editing
here. The endpoint and credentials are read from the `{app_id}` connection at
call time, so pointing these tools at a different instance is a connection
change, not a code change.
"""

import requests

from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool
from ibm_watsonx_orchestrate.run import connections
'''


def render_module(
    specs: list[HttpToolSpec], app_id: str, auth_kind: str = DEFAULT_AUTH_KIND
) -> str:
    """A complete, importable Python tool module for one host's tools."""
    body = "\n\n\n".join(_render_tool(spec, app_id, auth_kind) for spec in specs)
    return f"{HEADER.format(app_id=app_id)}\n\n{body}\n"


def write_module(
    specs: list[HttpToolSpec],
    app_id: str,
    destination: Path,
    auth_kind: str = DEFAULT_AUTH_KIND,
) -> Path:
    """Render and write the module, returning the path written."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_module(specs, app_id, auth_kind))
    return destination


def compiles(source: str) -> tuple[bool, str]:
    """Whether the generated module is at least syntactically valid.

    Cheap, and it catches the whole class of failure where a source tool has a
    name or parameter that does not survive being turned into Python. Without
    it the first sign of trouble is the ADK rejecting the import halfway
    through a migration, with a traceback pointing at generated code the user
    never wrote.
    """
    try:
        compile(source, "<generated>", "exec")
    except SyntaxError as exc:
        return False, f"line {exc.lineno}: {exc.msg}"
    return True, ""

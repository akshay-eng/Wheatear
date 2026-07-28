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
Every tool for one host shares one `key_value` connection holding two entries:

    base_url      where the tool points on the target
    auth_header   the full Authorization header value

`key_value` rather than a typed bearer/basic connection because the source
never reveals which it was -- n8n encrypts credential values and its API
redacts them, so the migration knows a credential *existed* and what it was
called, never what it held. Asking the user for the finished header value
covers bearer, basic, and bare API keys with one prompt and no guessing.

Carrying `base_url` in the connection rather than baking it into the code is
deliberate too: a migrated tool almost never points at the instance it was
exported from, and an endpoint frozen into generated source is the single most
common reason a migrated tool 404s on the target.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path

from wheatear.connectors.n8n.http_tools import HttpToolSpec, placeholders_in

# The connection keys every generated tool reads. Named here rather than
# spelled out at each use so the generator, the provisioner and the wizard
# prompt cannot drift apart.
KEY_BASE_URL = "base_url"
KEY_AUTH_HEADER = "auth_header"

_TEMPLATE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


def _render_tool(spec: HttpToolSpec, app_id: str) -> str:
    """One `@tool` function reproducing one source HTTP tool."""
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
        "'type': ConnectionType.KEY_VALUE}],"
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

    lines.append(f"    creds = connections.key_value({_py(app_id)})")
    lines.append(f"    base = str(creds[{_py(KEY_BASE_URL)}]).rstrip('/')")

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
    lines.append(f"    header_value = creds.get({_py(KEY_AUTH_HEADER)})")
    lines.append("    if header_value:")
    lines.append("        headers['Authorization'] = str(header_value)")
    lines.append(
        f"    response = requests.request({_py(spec.method)}, base + path, "
        "params=params, headers=headers, timeout=60)"
    )
    lines.append("    response.raise_for_status()")
    # Not every endpoint returns JSON; a tool that raises on an HTML error page
    # tells the agent nothing useful.
    lines.append("    try:")
    lines.append("        return response.json()")
    lines.append("    except ValueError:")
    lines.append("        return {'status': response.status_code, 'text': response.text[:4000]}")
    return "\n".join(lines)


HEADER = '''"""Tools migrated from n8n HTTP request nodes by Wheatear.

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


def render_module(specs: list[HttpToolSpec], app_id: str) -> str:
    """A complete, importable Python tool module for one host's tools."""
    body = "\n\n\n".join(_render_tool(spec, app_id) for spec in specs)
    return f"{HEADER.format(app_id=app_id)}\n\n{body}\n"


def write_module(specs: list[HttpToolSpec], app_id: str, destination: Path) -> Path:
    """Render and write the module, returning the path written."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_module(specs, app_id))
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

"""Put rebuilt HTTP tools onto a watsonx Orchestrate tenant.

Split from `http_tool_build` because generating code and changing a tenant are
different kinds of operation with different failure modes. Generation is pure
and testable offline; this module creates connections, stores a secret and
shells out to the ADK. Keeping them apart means the interesting logic -- which
parameters are real, how a templated constant is rebuilt -- can be tested
without a tenant, credentials or a network.

The unit of deployment is the *host*, not the tool. Three ServiceNow tools that
shared one credential in n8n become one connection and one imported module on
the target, because that is what they were: splitting them into three would ask
a person for the same ServiceNow token three times and leave three separate
things to rotate later.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_liftoff.connectors.n8n.http_tools import HttpToolSpec, group_by_host
from agent_liftoff.connectors.orchestrate import provisioning
from agent_liftoff.connectors.orchestrate.http_tool_build import (
    DEFAULT_AUTH_KIND,
    KEY_BASE_URL,
    app_id_for,
    compiles,
    render_module,
    write_module,
)

# `requests` is the only import the generated code makes beyond the ADK's own
# runtime, which is already present wherever a Python tool executes.
REQUIREMENTS = "requests\n"


@dataclass
class HostGroup:
    """One host's worth of tools: what to ask for, and what to deploy."""

    host: str
    app_id: str
    specs: list[HttpToolSpec] = field(default_factory=list)
    # Set once the user has answered; kept separate from the specs so an
    # unanswered group is obviously unanswered rather than silently defaulting.
    base_url: str | None = None
    # Which of Orchestrate's auth kinds the operator chose, and the field values
    # it needs (`token`, or `username`/`password`, ...). The kind decides the
    # generated code, so it is part of the plan rather than a deploy detail.
    auth_kind: str = DEFAULT_AUTH_KIND
    secrets: dict[str, str] = field(default_factory=dict)
    # `member` means each end user signs in and no secret is stored here.
    preference: str = "team"

    @property
    def tool_names(self) -> list[str]:
        return [s.name for s in self.specs]

    @property
    def credential_ref(self) -> str | None:
        """What the source called this credential, for the prompt."""
        for spec in self.specs:
            if spec.credential_ref:
                return spec.credential_ref
        return None

    def summary(self) -> str:
        return f"{self.host} — {len(self.specs)} tool(s): {', '.join(self.tool_names)}"


def plan(specs: list[HttpToolSpec]) -> list[HostGroup]:
    """Group the tools a migration found into per-host deployment units."""
    groups: list[HostGroup] = []
    for host, members in group_by_host(specs).items():
        if not host:
            # No base URL means the source node had no usable endpoint; it is
            # still reported, so the run says what it could not migrate.
            groups.append(HostGroup(host="", app_id="", specs=members))
            continue
        groups.append(HostGroup(host=host, app_id=app_id_for(host), specs=members, base_url=host))
    return groups


def ensure_credentials(group: HostGroup, log=lambda _m: None) -> list[str]:
    """Create the connection, point it at the host and store the credential.

    Configured in every environment, not just `draft`. A deployed agent runs
    against `live`, so a draft-only connection produces a tool that works in
    the builder's preview and fails after deploy -- see `provisioning.provision`.
    """
    request = provisioning.CredentialRequest(
        app_id=group.app_id,
        kind=group.auth_kind,
        preference=group.preference,
        server_url=group.base_url or group.host,
        tools=group.tool_names,
    )
    secrets = dict(group.secrets)
    if group.auth_kind == "key_value_creds":
        # key_value has no `url` field of its own, so the endpoint travels as
        # a key and the generated code reads it from there.
        secrets.setdefault(KEY_BASE_URL, group.base_url or group.host)
    done = provisioning.provision(request, secrets or None)
    for line in done:
        log(line)
    return done


def import_tools(
    group: HostGroup,
    destination: Path,
    orchestrate_cli: str,
    log=lambda _m: None,
) -> tuple[bool, list[str]]:
    """Write the generated module and import it through the ADK.

    Returns (ok, tool names imported). The generated source is written next to
    the migration's other artifacts rather than to a temp dir, so a failed
    import leaves behind exactly the file that failed for somebody to read.
    """
    source = render_module(group.specs, group.app_id, group.auth_kind)
    ok, why = compiles(source)
    if not ok:
        log(f"generated tool module is not valid Python ({why}) — not imported")
        return False, []

    module_path = destination / f"{group.app_id}_tools.py"
    write_module(group.specs, group.app_id, module_path, group.auth_kind)
    requirements = destination / "requirements.txt"
    requirements.write_text(REQUIREMENTS)
    log(f"wrote {module_path.name} ({len(group.specs)} tool(s))")

    command = [
        orchestrate_cli, "tools", "import",
        "-k", "python",
        "-f", str(module_path),
        "-r", str(requirements),
        "-a", group.app_id,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"could not run the ADK ({exc})")
        return False, []

    output = (result.stdout + result.stderr).splitlines()
    lines = [ln for ln in output if ln.strip() and "WARNING" not in ln]
    # The ADK exits 0 on some failures and prints the error instead, the same
    # trap `deploy_spec` documents for agent and knowledge-base imports.
    failed = result.returncode != 0 or any("[ERROR]" in ln for ln in lines)
    for line in lines[-3:]:
        log(f"| {line}")
    if failed:
        return False, []
    return True, [spec.operation_id() for spec in group.specs]


def tool_ids_for(client, names: list[str]) -> list[str]:
    """Resolve imported tool names to the ids an agent spec needs.

    Looked up after the import rather than parsed out of the ADK's output,
    because the output format is not a contract and the tenant is.
    """
    wanted = set(names)
    found: list[str] = []
    for tool in client.list_all_tools():
        name = str(tool.get("name", ""))
        if name in wanted and tool.get("id"):
            found.append(tool["id"])
    return found

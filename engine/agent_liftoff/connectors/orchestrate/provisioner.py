"""Live provision-then-deploy for watsonx Orchestrate.

The plain exporter emits deployable *skeletons* (agents whose tools/knowledge
are left for a human to attach) because the ADK hard-fails an import that
references entities which don't exist yet. This module does the real thing:
it provisions those dependencies on the tenant first, then creates each agent
with the resolved references, in leaf-first order so collaborators exist before
the agents that delegate to them.

Order (the deploy plan): for each agent, leaf-first ->
  1. MCP tools   : reuse an existing toolkit with the same server URL, else
                   register one and wait for its tools to populate.
  2. Knowledge   : import the file-upload KB (documents) via the ADK CLI, wait
                   for indexing to reach 'ready'.
  3. Description : AI-generate a concise agent description (n8n has none).
  4. Create agent: REST POST /agents with resolved tool ids, KB ids,
                   collaborator ids, description, llm.
  5. Validate    : GET the agent back and confirm the wiring resolved.

Only cleanly-resolvable dependencies are attached; anything that still needs a
human (a tool needing real credentials, a KB whose files weren't provided) is
left in the review manifest, exactly as before.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

import yaml

from agent_liftoff.connectors.orchestrate import http_tool_deploy
from agent_liftoff.connectors.orchestrate.exporter import _sanitize_name
from agent_liftoff.connectors.orchestrate.http_tool_deploy import ensure_credentials, import_tools
from agent_liftoff.connectors.orchestrate.rest_client import OrchestrateRestClient
from agent_liftoff.ir.schema import Agent, Workflow


def _adk_cli() -> str:
    """The ADK CLI to import tools with.

    Resolved rather than assumed to be on PATH: in a virtualenv the ADK sits at
    `<venv>/bin/orchestrate`, which a process launched as `.venv/bin/python`
    has nowhere on PATH.
    """
    try:
        from agent_liftoff.model_matrix.target_sources.orchestrate import find_cli

        return find_cli() or "orchestrate"
    except Exception:  # noqa: BLE001
        return "orchestrate"


@dataclass
class AgentDeployReport:
    name: str
    agent_id: str | None = None
    ok: bool = False
    tools: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)
    description: str = ""
    validation: str = ""
    error: str | None = None


# --------------------------------------------------------------------------- #
# MCP toolkits
# --------------------------------------------------------------------------- #

def find_or_create_mcp_toolkit(
    client: OrchestrateRestClient, name: str, server_url: str, transport: str = "sse",
    include_names: list[str] | None = None, log=None, timeout: int = 180,
) -> tuple[str, list[dict]]:
    """Return (toolkit_id, [tool dicts]). Reuses an existing toolkit pointing at
    the same server URL; else registers one via the ADK CLI `toolkits add`,
    which performs the real MCP handshake, enumerates the server's tools, and
    imports the selected ones. (A raw REST POST creates an empty toolkit record
    with zero tools -- it does NOT do the handshake -- which is why we shell out
    to the CLI here.)"""
    def _log(m):
        if log:
            log(m)

    for tk in client.list_toolkits():
        if (tk.get("mcp") or {}).get("server_url") == server_url:
            tools = client.tools_for_toolkit(tk["id"])
            _log(f"reusing existing toolkit '{tk.get('name')}' ({tk['id'][:8]}) — {len(tools)} tool(s) registered")
            return tk["id"], tools

    tk_name = _sanitize_name(name)
    # ADK only accepts "sse" or "streamable_http" for remote transports.
    tk_transport = "sse" if transport not in ("sse", "streamable_http") else transport
    tools_arg = ",".join(include_names) if include_names else "*"
    _log(f"no toolkit for {server_url} yet — registering via ADK (transport={tk_transport}, tools={tools_arg})…")
    result = subprocess.run(
        [_adk_cli(), "toolkits", "add", "--kind", "mcp", "--name", tk_name,
         "--description", f"MCP server migrated from n8n ({server_url})",
         "--url", server_url, "--transport", tk_transport, "--tools", tools_arg],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or ["toolkits add failed"]
        raise RuntimeError(detail[0][:160])
    # Look up the created toolkit + its (now enumerated) tools.
    tk = next((t for t in client.list_toolkits()
               if (t.get("mcp") or {}).get("server_url") == server_url), None)
    if tk is None:
        raise RuntimeError("toolkit not found after `toolkits add`")
    tools = client.tools_for_toolkit(tk["id"])
    _log(f"toolkit '{tk_name}' registered ({tk['id'][:8]}) — {len(tools)} tool(s) imported")
    return tk["id"], tools


def resolve_tool_ids(tools: list[dict], include_names: list[str]) -> list[str]:
    """Filter toolkit tools to the operations the source referenced (by the bare
    name after the 'toolkit:' prefix). Empty include_names -> all tools."""
    if not include_names:
        return [t["id"] for t in tools]
    wanted = set(include_names)
    return [t["id"] for t in tools if t["name"].split(":")[-1] in wanted]


# --------------------------------------------------------------------------- #
# Knowledge bases
# --------------------------------------------------------------------------- #

def _list_knowledge_bases(client: OrchestrateRestClient, retries: int = 4) -> list[dict]:
    """GET /knowledge-bases, tolerating transient 5xx (the endpoint can 500
    while a KB is mid-indexing)."""
    last = None
    for i in range(retries):
        try:
            kbs = client._get("/knowledge-bases")
            return kbs if isinstance(kbs, list) else kbs.get("knowledge_bases", [])
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    if last:
        raise last
    return []


def _existing_kb_id(client: OrchestrateRestClient, kb_name: str) -> str | None:
    match = next((k for k in _list_knowledge_bases(client) if k.get("name") == kb_name), None)
    return match["id"] if match else None


def import_knowledge_base(
    client: OrchestrateRestClient, name: str, description: str, files: list[str],
    poll_seconds: int = 120, log=None,
) -> str | None:
    """Create a built-in KB from uploaded documents via the ADK CLI, wait for
    indexing to reach 'ready', and return its id. None if no files. Idempotent:
    reuses an existing KB with the same name instead of duplicating it."""
    def _log(m):
        if log:
            log(m)

    if not files:
        return None
    kb_name = _sanitize_name(name)
    existing = _existing_kb_id(client, kb_name)
    if existing:
        _log(f"reusing existing knowledge base '{kb_name}' ({existing[:8]})")
        return existing
    _log(f"uploading {len(files)} document(s): {', '.join(Path(f).name for f in files)}")
    spec = {
        "spec_version": "v1",
        "kind": "knowledge_base",
        "name": kb_name,
        "description": description or kb_name,
        "documents": [str(Path(f).expanduser()) for f in files],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(spec, fh, sort_keys=False)
        spec_path = fh.name
    subprocess.run(
        [_adk_cli(), "knowledge-bases", "import", "-f", spec_path],
        capture_output=True, text=True, timeout=180,
    )
    _log("documents uploaded; embedding + indexing (this can take a moment)…")
    # find its id + wait for ready
    deadline = time.time() + poll_seconds
    kb_id = None
    waited = 0
    while time.time() < deadline:
        try:
            match = next((k for k in _list_knowledge_bases(client) if k.get("name") == kb_name), None)
        except Exception:  # noqa: BLE001
            match = None
        if match:
            kb_id = match["id"]
            status = (match.get("vector_index") or {}).get("status")
            if status == "ready":
                _log(f"knowledge base indexed and ready ({kb_id[:8]})")
                return kb_id
        time.sleep(3)
        waited += 3
        if waited % 9 == 0:
            _log(f"  …indexing in progress ({waited}s)")
    return kb_id


def resolve_kb_files(detail: str | None, extra_roots: list[Path] | None = None) -> list[str]:
    """Expand a file-upload KB's recorded selector into real files.

    The recorded path is written from the *source's* point of view, and the
    source is frequently not this machine. n8n in Docker records
    `/home/node/.n8n-files/hr-kb/*.pdf`; the files are on the host under the
    operator's own home. Copilot Studio records a Windows path. Neither
    resolves here, and the consequence is not a missing file -- it is an agent
    that deploys with no knowledge base, answers from the model's memory
    instead of the handbook, and sounds exactly as confident either way.
    (Observed: a migrated HR agent stating a leave allowance that was not the
    one in the PDF.)

    So the literal path is tried first, then the same *tail* under this user's
    home and under any roots the caller offers. Returns [] when nothing
    resolves, which the caller reports as a manual step.
    """
    if not detail:
        return []

    literal = sorted(glob(str(Path(detail).expanduser())))
    if literal:
        return literal

    pattern = Path(detail)
    candidates: list[Path] = []
    # `/home/node/.n8n-files/hr-kb/*.pdf` -> `~/.n8n-files/hr-kb/*.pdf`. Any
    # home-like prefix is swapped for this user's, keeping the rest intact.
    parts = pattern.parts
    for marker in ("home", "Users"):
        if marker in parts:
            index = parts.index(marker)
            # skip the marker and the username that follows it
            tail = parts[index + 2 :]
            if tail:
                candidates.append(Path.home().joinpath(*tail))
    for root in extra_roots or []:
        candidates.append(Path(root) / pattern.name)
        if len(pattern.parts) > 1:
            candidates.append(Path(root).joinpath(*pattern.parts[-2:]))

    for candidate in candidates:
        found = sorted(glob(str(candidate.expanduser())))
        if found:
            return found
    return []


# --------------------------------------------------------------------------- #
# AI description
# --------------------------------------------------------------------------- #

def generate_description(provider, agent: Agent) -> str:
    """AI-generate a concise (1-2 sentence) agent description from its
    instructions. Falls back to the first sentence if no provider / on error."""
    # `instructions` is what the Translate stage produced; `existing_instructions`
    # is what the source actually had. Reading only the former made this a silent
    # no-op for any corridor that deploys without translating first -- the n8n
    # path does -- so every agent got the first line of its own system prompt as
    # its catalog description and no model was ever called.
    source_text = agent.instructions or agent.existing_instructions
    fallback = (agent.description or source_text or agent.name).strip()
    fallback = fallback.split("\n", 1)[0][:240] or agent.name
    if provider is None or not source_text:
        return fallback
    try:
        from pydantic import BaseModel, Field

        class _Desc(BaseModel):
            description: str = Field(description="A concise 1-2 sentence description of what this agent does.")

        prompt = (
            "Write a concise, 1-2 sentence description of what this AI agent does, "
            "for a catalog card. No preamble, just the description.\n\n"
            f"Agent name: {agent.name}\n\nInstructions:\n{source_text[:2000]}"
        )
        out = provider.generate_structured(prompt, _Desc)
        return (out.description or fallback).strip()[:1000]
    except Exception:  # noqa: BLE001
        return fallback


# --------------------------------------------------------------------------- #
# Provision + deploy the whole workflow
# --------------------------------------------------------------------------- #

def provision_and_deploy(
    client: OrchestrateRestClient,
    workflow: Workflow,
    results_by_name: dict,
    llm: str,
    provider=None,
    on_progress=None,
    endpoint_answers: dict[str, dict[str, str]] | None = None,
    tool_output_dir: Path | None = None,
) -> list[AgentDeployReport]:
    """Provision dependencies + create every agent in the workflow, leaf-first.
    `results_by_name` maps agent name -> the ImportResult (for raw MCP/KB refs).

    `endpoint_answers` maps a connection app id to the base URL and auth header
    a person confirmed for it. Passed in rather than prompted for here because
    this module has no terminal: the wizard asks, the CLI can be told, and both
    reach the same code. Absent an answer the source's own endpoint is used and
    the tool is deployed unauthenticated, which fails visibly at call time
    rather than silently deploying a tool nobody knows is broken.

    Returns a per-agent report."""
    tool_output_dir = tool_output_dir or Path.cwd() / "n8n-migration" / "_tools"
    adk_cli = _adk_cli()
    reports: list[AgentDeployReport] = []
    name_to_id: dict[str, str] = {}  # sanitized agent name -> created id

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    for agent in workflow.migration_order():
        # Honor the caller's selection: only deploy agents present in the map
        # (the selected set + their collaborator closure).
        if agent.name not in results_by_name:
            continue
        report = AgentDeployReport(name=agent.name)
        alog = lambda m: log(f"[{agent.name}] {m}")  # noqa: E731
        try:
            log(f"── {agent.name} ──────────────")
            res = results_by_name.get(agent.name)
            tool_ids: list[str] = []
            kb_ids: list[str] = []
            warnings: list[str] = []

            # 1. MCP tools -- a failure here must not stop the agent deploying.
            for raw in (res.raw_tools if res else []):
                if raw.kind == "mcp" and raw.mcp_server_url:
                    try:
                        alog(f"tool: MCP server {raw.mcp_server_url}")
                        tk_id, tools = find_or_create_mcp_toolkit(
                            client, raw.name, raw.mcp_server_url, raw.transport or "sse",
                            include_names=raw.tool_names, log=alog,
                        )
                        # Selection already happened at registration; but if we
                        # reused a toolkit that has more tools, still filter.
                        ids = resolve_tool_ids(tools, raw.tool_names)
                        picked = [t["name"].split(":")[-1] for t in tools if t["id"] in ids]
                        alog(f"tool: attaching {len(ids)} operation(s): {', '.join(picked)}")
                        tool_ids.extend(ids)
                        report.tools.extend([t["name"] for t in tools if t["id"] in ids])
                    except Exception as exc:  # noqa: BLE001
                        alog(f"tool: FAILED to attach '{raw.name}' — {str(exc)[:80]}")
                        warnings.append(f"MCP tool '{raw.name}' not attached: {str(exc)[:80]}")
                elif raw.kind == "http":
                    pass  # rebuilt per host below, not per tool reference
                elif raw.kind != "mcp":
                    alog(f"tool: '{raw.name}' ({raw.kind}) needs manual provisioning — left in review manifest")
                    warnings.append(f"tool '{raw.name}' ({raw.kind}) not auto-provisioned")

            # 1b. HTTP tools the source described completely enough to rebuild.
            #     Grouped by host: tools that shared one credential on the
            #     source share one connection here, so a person is asked for a
            #     given API's token once rather than once per operation.
            for group in http_tool_deploy.plan(res.endpoint_tools if res else []):
                if not group.host:
                    for spec in group.specs:
                        alog(f"tool: '{spec.name}' has no usable endpoint — left in review manifest")
                        warnings.append(f"tool '{spec.name}' had no endpoint to rebuild")
                    continue
                answer = (endpoint_answers or {}).get(group.app_id)
                if answer:
                    group.base_url = answer.get("base_url") or group.base_url
                    group.auth_kind = answer.get("auth_kind") or group.auth_kind
                    group.preference = answer.get("preference") or group.preference
                    group.secrets = dict(answer.get("secrets") or {})
                try:
                    alog(f"tool: rebuilding {len(group.specs)} HTTP tool(s) for {group.host}")
                    ensure_credentials(group, log=alog)
                    ok_import, names = import_tools(
                        group, tool_output_dir / _sanitize_name(agent.name), adk_cli, log=alog
                    )
                    if not ok_import:
                        warnings.append(f"HTTP tools for {group.host} were not imported")
                        continue
                    ids = http_tool_deploy.tool_ids_for(client, names)
                    tool_ids.extend(ids)
                    report.tools.extend(names)
                    alog(f"tool: attached {len(ids)}/{len(names)} rebuilt tool(s): {', '.join(names)}")
                    if len(ids) < len(names):
                        warnings.append(
                            f"{len(names) - len(ids)} rebuilt tool(s) for {group.host} "
                            "imported but could not be found on the tenant"
                        )
                except Exception as exc:  # noqa: BLE001
                    alog(f"tool: FAILED to rebuild tools for {group.host} — {str(exc)[:80]}")
                    warnings.append(f"HTTP tools for {group.host} not attached: {str(exc)[:80]}")

            # 2. Knowledge bases (file uploads) -- likewise non-fatal.
            for kb in (res.raw_knowledge_refs if res else []):
                if kb.is_file_upload:
                    try:
                        files = resolve_kb_files(kb.detail)
                        if files:
                            alog(f"knowledge base: {kb.name}")
                            kb_id = import_knowledge_base(client, kb.name, kb.name, files, log=alog)
                            if kb_id:
                                kb_ids.append(kb_id)
                                report.knowledge.append(kb.name)
                        else:
                            alog(f"knowledge base: no files found at {kb.detail} — left in review manifest")
                            warnings.append(f"KB '{kb.name}': no files found at {kb.detail}")
                    except Exception as exc:  # noqa: BLE001
                        alog(f"knowledge base: FAILED — {str(exc)[:80]}")
                        warnings.append(f"KB '{kb.name}' not attached: {str(exc)[:80]}")

            # 3. Collaborators (already-created ids, leaf-first guarantees this)
            collab_ids = []
            for c in agent.collaborators:
                if c.review_required:
                    continue
                cid = name_to_id.get(_sanitize_name(c.ref))
                if cid:
                    collab_ids.append(cid)
                    report.collaborators.append(c.ref)
            if collab_ids:
                alog(f"collaborators: linking {', '.join(report.collaborators)} (deployed earlier this run)")

            # 4. Description (AI)
            alog("description: generating with the LLM…" if provider else "description: using source-derived summary")
            description = generate_description(provider, agent)
            report.description = description
            alog(f"description: {description[:70]}…")

            # 5. Create the agent (upsert)
            instructions = agent.instructions or agent.existing_instructions or agent.name
            spec = {
                "name": _sanitize_name(agent.name),
                "display_name": agent.name,
                "description": description,
                "llm": llm,
                # ReAct Core is recommended; 'default'/'react' are deprecated.
                "style": agent.agent_style or "react_intrinsic",
                "instructions": instructions,
                "tools": tool_ids,
                "knowledge_base": kb_ids,
                "collaborators": collab_ids,
            }
            sanitized = _sanitize_name(agent.name)
            for existing in client.list_agents():
                if existing.get("name") == sanitized and existing.get("id"):
                    alog("found an existing agent with this name — replacing it (idempotent re-deploy)")
                    client.delete_agent(existing["id"])
            alog(f"creating agent on tenant  (llm={llm}, {len(tool_ids)} tool(s), {len(kb_ids)} KB, {len(collab_ids)} collaborator(s))")
            created = client.create_agent(spec)
            report.agent_id = created.get("id")
            name_to_id[_sanitize_name(agent.name)] = report.agent_id
            alog(f"created ({(report.agent_id or '')[:8]})")

            # 6. Validate wiring
            got = client.get_agent(report.agent_id)
            tools_ok = set(got.get("tools", [])) >= set(tool_ids)
            kb_ok = set(got.get("knowledge_base", [])) >= set(kb_ids)
            collab_ok = set(got.get("collaborators", [])) >= set(collab_ids)
            report.ok = tools_ok and kb_ok and collab_ok
            alog(f"validation: tools {len(got.get('tools',[]))}/{len(tool_ids)} {'✓' if tools_ok else '✗'}, "
                 f"kb {len(got.get('knowledge_base',[]))}/{len(kb_ids)} {'✓' if kb_ok else '✗'}, "
                 f"collaborators {len(got.get('collaborators',[]))}/{len(collab_ids)} {'✓' if collab_ok else '✗'}")
            report.validation = (
                f"tools {'✓' if tools_ok else '✗'} kb {'✓' if kb_ok else '✗'} "
                f"collaborators {'✓' if collab_ok else '✗'}"
            )
            if warnings:
                report.validation += " | " + "; ".join(warnings)
        except Exception as exc:  # noqa: BLE001
            report.error = str(exc)[:200]
        reports.append(report)
    return reports

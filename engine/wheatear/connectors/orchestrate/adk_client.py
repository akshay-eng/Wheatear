"""IBM watsonx Orchestrate connector — REST API backed.

All agent listing and export now goes directly through the Orchestrate REST API
(IAM token exchange + HTTP calls). No ADK CLI or Python library required.

Public surface used by the wizard:
  probe_connection(api_key, instance_url, workspace_id) → (ok, message)
  list_agents(api_key, instance_url, workspace_id)      → list[AgentInfo]
  list_toolkits(api_key, instance_url, workspace_id)    → list[ToolkitInfo]
  export_agent(agent_id, dest, api_key, instance_url, workspace_id) → Path
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

from wheatear.connectors.orchestrate.rest_client import (
    DEFAULT_WORKSPACE,
    OrchestrateRestClient,
    get_iam_token,
)


class AdkError(Exception):
    pass


@dataclass
class AgentInfo:
    name: str           # API name / technical ID
    display_name: str = ""
    description: str = ""
    llm: str = ""
    kind: str = "native"
    agent_id: str = ""  # REST API UUID — used for direct GET /agents/{id}


@dataclass
class ToolkitInfo:
    name: str
    kind: str = ""


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------

def probe_connection(
    api_key: str = "",
    instance_url: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
) -> tuple[bool, str]:
    """Try to authenticate and list agents. Returns (success, message)."""
    if not api_key or not instance_url:
        return False, "API key and instance URL are required."
    try:
        client = OrchestrateRestClient(api_key, instance_url, workspace_id)
        agents = client.list_agents()
        return True, f"{len(agents)} agent(s) found."
    except requests.HTTPError as exc:
        return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    except Exception as exc:
        return False, str(exc)[:300]


# ---------------------------------------------------------------------------
# Agent listing
# ---------------------------------------------------------------------------

def list_agents(
    api_key: str = "",
    instance_url: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
) -> list[AgentInfo]:
    """Return all agents in the Orchestrate instance."""
    try:
        client = OrchestrateRestClient(api_key, instance_url, workspace_id)
        raw = client.list_agents()
    except Exception as exc:
        raise AdkError(f"Could not list agents: {exc}") from exc

    agents: list[AgentInfo] = []
    seen: set[str] = set()
    for a in raw:
        name = a.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        agents.append(AgentInfo(
            name=name,
            display_name=a.get("display_name") or a.get("nickname", ""),
            description=a.get("description", ""),
            llm=a.get("llm", ""),
            kind=a.get("kind", "native"),
            agent_id=a.get("id", ""),
        ))
    return agents


# ---------------------------------------------------------------------------
# Toolkit listing
# ---------------------------------------------------------------------------

def list_toolkits(
    api_key: str = "",
    instance_url: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
) -> list[ToolkitInfo]:
    """Return all toolkits in the Orchestrate instance."""
    try:
        client = OrchestrateRestClient(api_key, instance_url, workspace_id)
        raw = client.list_toolkits()
    except Exception:
        return []

    return [
        ToolkitInfo(
            name=tk.get("name", ""),
            kind="mcp" if (tk.get("mcp") or {}).get("server_url") else "python",
        )
        for tk in raw
        if tk.get("name")
    ]


# ---------------------------------------------------------------------------
# Agent export
# ---------------------------------------------------------------------------

def export_agent(
    agent_id: str,
    dest: Path,
    api_key: str = "",
    instance_url: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
    # legacy kwargs kept for call-site compatibility — ignored in REST path
    agent_name: str = "",
    kind: str = "native",
    display_name: str = "",
) -> Path:
    """Export a single agent to a YAML file at `dest`.

    Uses agent_id for a direct GET /agents/{id} — no name guessing, no retries.
    Falls back to searching by name if agent_id is empty.
    """
    if not agent_id and agent_name:
        agent_id = _resolve_id_by_name(agent_name, api_key, instance_url, workspace_id)

    if not agent_id:
        raise AdkError(
            f"Cannot export agent '{agent_name}': no agent_id and name lookup failed."
        )

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix not in (".yaml", ".yml"):
        dest = dest.with_suffix(".yaml")

    try:
        client = OrchestrateRestClient(api_key, instance_url, workspace_id)
        export = client.export_agent_full(agent_id)
    except requests.HTTPError as exc:
        raise AdkError(
            f"Export failed (HTTP {exc.response.status_code}): {exc.response.text[:200]}"
        ) from exc
    except Exception as exc:
        raise AdkError(f"Export failed: {exc}") from exc

    dest.write_text(
        yaml.dump(export, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return dest


def _resolve_id_by_name(
    name: str,
    api_key: str,
    instance_url: str,
    workspace_id: str,
) -> str:
    """Find an agent's UUID by its API name. Returns empty string if not found."""
    try:
        client = OrchestrateRestClient(api_key, instance_url, workspace_id)
        for a in client.list_agents():
            if a.get("name") == name:
                return a.get("id", "")
    except Exception:
        pass
    return ""

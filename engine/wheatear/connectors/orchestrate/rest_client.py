"""Direct REST API client for IBM watsonx Orchestrate.

Replaces ADK CLI/Python library for agent listing and export.
Auth: IBM IAM token exchange — api_key → short-lived Bearer token.

The instance_url format is:
  https://api.<region>.watson-orchestrate.cloud.ibm.com/instances/<instance-id>
The API base is that URL + /v1/orchestrate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    pass

IAM_URL = "https://iam.cloud.ibm.com/identity/token"
DEFAULT_WORKSPACE = "00000000-0000-0000-0000-000000000001"


def get_iam_token(api_key: str) -> str:
    """Exchange an IBM Cloud API key for a short-lived IAM Bearer token."""
    resp = requests.post(
        IAM_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=f"grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey={api_key}",
        timeout=(10, 60),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


class OrchestrateRestClient:
    """Thin REST wrapper for the watsonx Orchestrate v1 API."""

    def __init__(self, api_key: str, instance_url: str, workspace_id: str = DEFAULT_WORKSPACE) -> None:
        self.workspace_id = workspace_id
        self.base = instance_url.rstrip("/") + "/v1/orchestrate"
        token = get_iam_token(api_key)
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def _get(self, path: str, extra: dict | None = None) -> dict | list:
        params: dict = {"workspace_id": self.workspace_id, "include": "global"}
        if extra:
            params.update(extra)
        resp = self._session.get(f"{self.base}{path}", params=params, timeout=(10, 60))
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Entity accessors
    # ------------------------------------------------------------------

    def list_agents(self) -> list[dict]:
        data = self._get("/agents")
        return data.get("agents", data) if isinstance(data, dict) else data

    def get_agent(self, agent_id: str) -> dict:
        return self._get(f"/agents/{agent_id}")

    def list_toolkits(self) -> list[dict]:
        data = self._get("/toolkits")
        return data if isinstance(data, list) else data.get("toolkits", [])

    def list_all_tools(self) -> list[dict]:
        data = self._get("/tools")
        return data if isinstance(data, list) else data.get("tools", [])

    def get_knowledge_base(self, kb_id: str) -> dict:
        try:
            return self._get(f"/knowledge-bases/{kb_id}")
        except requests.HTTPError as exc:
            return {"id": kb_id, "error": str(exc)}

    # ------------------------------------------------------------------
    # Rich export
    # ------------------------------------------------------------------

    def export_agent_full(self, agent_id: str) -> dict:
        """Return the complete export dict for one agent.

        Structure:
          agent:       core agent fields (instructions, llm, style, guidelines, …)
          toolkits:    each toolkit the agent references, with full tool schemas
          unmapped_tool_ids:  tool UUIDs that couldn't be resolved to a toolkit
          knowledge_bases:    knowledge-base records the agent references
        """
        agent = self.get_agent(agent_id)

        agent_tool_ids: list[str] = agent.get("tools", [])
        kb_ids: list[str] = agent.get("knowledge_base", [])

        toolkits = self.list_toolkits()
        all_tools = self.list_all_tools()

        # toolkit-level tool_id → parent toolkit
        tool_to_toolkit: dict[str, dict] = {}
        for tk in toolkits:
            for tool_id in tk.get("tools", []):
                tool_to_toolkit[tool_id] = tk

        # toolkit_id → [tool detail, …]
        tools_by_toolkit: dict[str, list[dict]] = {}
        for t in all_tools:
            tk_id = t.get("toolkit_id")
            if tk_id:
                tools_by_toolkit.setdefault(tk_id, []).append(t)

        used_toolkits: dict[str, dict] = {}
        unmapped: list[str] = []

        # Strategy 1: match agent tool UUIDs against toolkit tool arrays
        for tool_id in agent_tool_ids:
            tk = tool_to_toolkit.get(tool_id)
            if tk:
                tk_id = tk["id"]
                if tk_id not in used_toolkits:
                    used_toolkits[tk_id] = _toolkit_entry(tk)
                used_toolkits[tk_id]["tool_ids_used_by_agent"].append(tool_id)
            else:
                unmapped.append(tool_id)

        # Strategy 2: infer toolkits from "TOOLKIT_NAME:tool_name" patterns in instructions
        if unmapped:
            for tk in _find_toolkits_in_instructions(agent.get("instructions", ""), toolkits):
                if tk["id"] not in used_toolkits:
                    used_toolkits[tk["id"]] = _toolkit_entry(tk)

        # Strategy 3: guideline tool references (may also be dangling)
        for guideline in agent.get("guidelines", []):
            tool_id = guideline.get("tool")
            if tool_id and tool_id not in tool_to_toolkit and tool_id not in unmapped:
                unmapped.append(tool_id)

        # Enrich each toolkit with current tool schemas
        for tk_id, tk_info in used_toolkits.items():
            tk_info["current_tools"] = [
                {
                    "id": t["id"],
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema"),
                    "output_schema": t.get("output_schema"),
                }
                for t in tools_by_toolkit.get(tk_id, [])
            ]

        knowledge_bases = [self.get_knowledge_base(kb_id) for kb_id in kb_ids]

        return {
            "agent": {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "display_name": agent.get("display_name"),
                "description": agent.get("description"),
                "instructions": agent.get("instructions"),
                "llm": agent.get("llm"),
                "style": agent.get("style"),
                "guidelines": agent.get("guidelines", []),
                "collaborators": agent.get("collaborators", []),
            },
            "toolkits": list(used_toolkits.values()),
            "unmapped_tool_ids": unmapped,
            "knowledge_bases": knowledge_bases,
        }


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _toolkit_entry(tk: dict) -> dict:
    mcp = tk.get("mcp") or {}
    return {
        "id": tk["id"],
        "name": tk["name"],
        "description": tk.get("description", ""),
        "type": "mcp" if mcp.get("server_url") else "python",
        "mcp_server_url": mcp.get("server_url"),
        "transport": mcp.get("transport"),
        "tool_ids_used_by_agent": [],
        "current_tools": [],
    }


_TOOLKIT_REF_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]+):[a-z_]+")


def _find_toolkits_in_instructions(instructions: str, toolkits: list[dict]) -> list[dict]:
    """Return toolkits whose names appear as 'TOOLKIT_NAME:tool' in the instructions."""
    prefixes = {m.group(1) for m in _TOOLKIT_REF_RE.finditer(instructions)}
    return [tk for tk in toolkits if tk["name"] in prefixes]

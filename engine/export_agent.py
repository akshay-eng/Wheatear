#!/usr/bin/env python3
"""
Export an IBM watsonx Orchestrate agent with its tools and knowledge bases.

Usage:
    python export_agent.py
    python export_agent.py --apikey <key> --instance <instance-id> --region us-south
"""

import argparse
import json
import sys
from getpass import getpass
from pathlib import Path

import requests
import yaml

IAM_URL = "https://iam.cloud.ibm.com/identity/token"
DEFAULT_INSTANCE = "df327b39-2104-4b00-a1c2-4746cdf1767e"
DEFAULT_REGION = "us-south"
DEFAULT_WORKSPACE = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_iam_token(api_key: str) -> str:
    resp = requests.post(
        IAM_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=f"grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey={api_key}",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Orchestrate REST helpers
# ---------------------------------------------------------------------------

class OrchestrateClient:
    def __init__(self, token: str, instance_id: str, region: str, workspace_id: str):
        self.token = token
        self.base = (
            f"https://api.{region}.watson-orchestrate.cloud.ibm.com"
            f"/instances/{instance_id}/v1/orchestrate"
        )
        self.workspace_id = workspace_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def _get(self, path: str, extra_params: dict | None = None) -> dict | list:
        params = {"workspace_id": self.workspace_id, "include": "global"}
        if extra_params:
            params.update(extra_params)
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

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
        except requests.HTTPError as e:
            return {"id": kb_id, "error": str(e)}


# ---------------------------------------------------------------------------
# MCP server query
# ---------------------------------------------------------------------------

def query_mcp_tools(server_url: str) -> list[dict]:
    """Call tools/list on the MCP server directly via JSON-RPC."""
    # SSE servers expose /messages for JSON-RPC
    base = server_url.rstrip("/")
    if base.endswith("/sse"):
        base = base[:-4]
    try:
        resp = requests.post(
            f"{base}/messages",
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            timeout=10,
        )
        return resp.json().get("result", {}).get("tools", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Core export logic
# ---------------------------------------------------------------------------

def build_toolkit_map(toolkits: list[dict]) -> dict[str, dict]:
    """Map individual tool UUID → parent toolkit."""
    mapping: dict[str, dict] = {}
    for tk in toolkits:
        for tool_id in tk.get("tools", []):
            mapping[tool_id] = tk
    return mapping


def find_toolkits_in_instructions(instructions: str, toolkits: list[dict]) -> list[dict]:
    """
    Parse instructions for TOOLKIT_NAME:tool_name patterns and return matching toolkits.
    Handles cases where tool UUIDs in the agent don't map to toolkit tool UUIDs.
    """
    import re
    # find all "WORD:" prefixes that look like toolkit references
    prefixes = set(re.findall(r"\b([A-Za-z][A-Za-z0-9_]+):[a-z_]+", instructions))
    matched = []
    for tk in toolkits:
        if tk["name"] in prefixes:
            matched.append(tk)
    return matched


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
        "mcp_tools": [],
    }


def export_agent(client: OrchestrateClient, agent_id: str) -> dict:
    print(f"\n  Fetching agent details...")
    agent = client.get_agent(agent_id)

    agent_tool_ids: list[str] = agent.get("tools", [])
    kb_ids: list[str] = agent.get("knowledge_base", [])

    # --- toolkits + tools -------------------------------------------------
    print("  Fetching toolkits...")
    toolkits = client.list_toolkits()

    print("  Fetching all tools...")
    all_tools = client.list_all_tools()

    # Build lookup maps
    tool_to_toolkit = build_toolkit_map(toolkits)       # toolkit tool_id → toolkit
    tool_detail_map = {t["id"]: t for t in all_tools}   # tool_id → full tool detail
    # also map by toolkit_id → list of tool details
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

    # Strategy 2: if tools remain unmapped, parse instructions for TOOLKIT:tool patterns
    # (handles stale tool IDs from deleted/replaced toolkit registrations)
    if unmapped:
        instructions = agent.get("instructions", "")
        inferred = find_toolkits_in_instructions(instructions, toolkits)
        for tk in inferred:
            if tk["id"] not in used_toolkits:
                print(f"  Inferred toolkit from instructions: {tk['name']}")
                used_toolkits[tk["id"]] = _toolkit_entry(tk)

    # Strategy 3: check guidelines for tool references
    for guideline in agent.get("guidelines", []):
        tool_id = guideline.get("tool")
        if tool_id and tool_id not in tool_to_toolkit and tool_id not in unmapped:
            unmapped.append(tool_id)

    # Enrich each used toolkit with its current tools from all_tools
    for tk_id, tk_info in used_toolkits.items():
        current_tools = tools_by_toolkit.get(tk_id, [])
        tk_info["current_tools"] = [
            {
                "id": t["id"],
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema"),
                "output_schema": t.get("output_schema"),
            }
            for t in current_tools
        ]
        # also try MCP server for live schemas
        url = tk_info.get("mcp_server_url", "")
        if url:
            print(f"  Querying MCP: {tk_info['name']} → {url}")
            tk_info["mcp_tools"] = query_mcp_tools(url)

    # --- knowledge bases --------------------------------------------------
    knowledge_bases: list[dict] = []
    if kb_ids:
        print(f"  Fetching {len(kb_ids)} knowledge base(s)...")
        for kb_id in kb_ids:
            knowledge_bases.append(client.get_knowledge_base(kb_id))

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
            "created_by": agent.get("created_by"),
            "created_on": agent.get("created_on"),
            "updated_at": agent.get("updated_at"),
        },
        "toolkits": list(used_toolkits.values()),
        "unmapped_tool_ids": unmapped,
        "knowledge_bases": knowledge_bases,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_summary(export: dict) -> None:
    agent = export["agent"]
    toolkits = export["toolkits"]
    kbs = export["knowledge_bases"]

    print("\n" + "=" * 60)
    print("EXPORT SUMMARY")
    print("=" * 60)
    print(f"Agent      : {agent['name']}")
    print(f"Display    : {agent['display_name']}")
    print(f"LLM        : {agent['llm']}")
    print(f"Style      : {agent['style']}")
    print(f"Guidelines : {len(agent.get('guidelines', []))}")
    print(f"Collaborators: {len(agent.get('collaborators', []))}")
    print(f"\nToolkits ({len(toolkits)}):")
    for tk in toolkits:
        current = tk.get("current_tools", [])
        mcp = tk.get("mcp_tools", [])
        print(f"  {tk['name']} [{tk.get('type','?')}]")
        print(f"    MCP URL      : {tk['mcp_server_url']}")
        print(f"    Current tools: {len(current)}")
        for t in current:
            print(f"      - {t['name']}: {t.get('description','')[:80]}")
        if mcp:
            print(f"    MCP live tools: {len(mcp)}")
            for t in mcp:
                print(f"      - {t.get('name','?')}: {t.get('description','')[:80]}")
    if export["unmapped_tool_ids"]:
        print(f"\nUnmapped tool IDs ({len(export['unmapped_tool_ids'])}):")
        for uid in export["unmapped_tool_ids"]:
            print(f"  - {uid}")
    print(f"\nKnowledge bases: {len(kbs)}")
    for kb in kbs:
        print(f"  - {kb.get('name', kb.get('id', '?'))}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a watsonx Orchestrate agent")
    parser.add_argument("--apikey", help="WXO API key (prompted if omitted)")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE, help="Orchestrate instance ID")
    parser.add_argument("--region", default=DEFAULT_REGION, help="IBM Cloud region")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="Workspace ID")
    parser.add_argument("--out", default=".", help="Output directory")
    args = parser.parse_args()

    api_key = args.apikey or getpass("Enter WXO API key: ")

    print("\nAuthenticating with IBM IAM...")
    try:
        token = get_iam_token(api_key)
    except Exception as e:
        print(f"Auth failed: {e}")
        sys.exit(1)

    client = OrchestrateClient(token, args.instance, args.region, args.workspace)

    print("Fetching agents...")
    try:
        agents = client.list_agents()
    except Exception as e:
        print(f"Failed to list agents: {e}")
        sys.exit(1)

    if not agents:
        print("No agents found.")
        sys.exit(0)

    # display list
    print(f"\n{'#':<4} {'Name':<45} {'Description':<55} ID")
    print("-" * 140)
    for i, a in enumerate(agents, 1):
        desc = (a.get("description") or "")[:54]
        print(f"{i:<4} {a.get('name',''):<45} {desc:<55} {a.get('id','')}")

    print()
    raw = input("Enter agent number(s) to export [e.g. 1 or 1,3,5 or 'all']: ").strip()

    if raw.lower() == "all":
        selected = agents
    else:
        try:
            indices = [int(x.strip()) - 1 for x in raw.split(",")]
            selected = [agents[i] for i in indices]
        except (ValueError, IndexError):
            print("Invalid selection.")
            sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for agent_meta in selected:
        name = agent_meta["name"]
        agent_id = agent_meta["id"]
        print(f"\nExporting: {name} ({agent_id})")

        try:
            export = export_agent(client, agent_id)
        except Exception as e:
            print(f"  Failed: {e}")
            continue

        json_path = out_dir / f"{name}-export.json"
        yaml_path = out_dir / f"{name}-export.yaml"

        json_path.write_text(json.dumps(export, indent=2, ensure_ascii=False))
        yaml_path.write_text(
            yaml.dump(export, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

        print_summary(export)
        print(f"\n  Saved: {json_path}")
        print(f"  Saved: {yaml_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()

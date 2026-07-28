"""n8n REST API client for live workflow auto-discovery.

Mirrors the public shape of `connectors/orchestrate/adk_client.py` so the wizard
reuses the same discover -> select -> import pattern. Auth is a single API-key
header (`X-N8N-API-KEY`); n8n's public API lives under `/api/v1`.

Public surface:
  probe_connection(base_url, api_key)        -> (ok, message)
  list_workflows(base_url, api_key)          -> list[WorkflowInfo]
  fetch_workflow(base_url, api_key, id)      -> dict   (full workflow JSON)
  fetch_all_workflows(base_url, api_key)     -> list[dict]
"""

from __future__ import annotations

from dataclasses import dataclass

import requests


class N8nError(Exception):
    pass


@dataclass
class WorkflowInfo:
    name: str
    display_name: str = ""
    description: str = ""
    workflow_id: str = ""
    active: bool = False


def _headers(api_key: str) -> dict:
    return {"X-N8N-API-KEY": api_key, "Accept": "application/json"}


def _base(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/v1"


def probe_connection(base_url: str = "", api_key: str = "") -> tuple[bool, str]:
    """Authenticate and count workflows. Returns (success, message)."""
    if not base_url or not api_key:
        return False, "Base URL and API key are required."
    try:
        resp = requests.get(
            f"{_base(base_url)}/workflows",
            headers=_headers(api_key),
            params={"limit": 1},
            timeout=(10, 30),
        )
        resp.raise_for_status()
        return True, "Connected to n8n."
    except requests.HTTPError as exc:
        return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300]


def list_workflows(base_url: str = "", api_key: str = "") -> list[WorkflowInfo]:
    """Return all workflows (paginated) in the n8n instance."""
    out: list[WorkflowInfo] = []
    cursor = None
    try:
        while True:
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(
                f"{_base(base_url)}/workflows",
                headers=_headers(api_key),
                params=params,
                timeout=(10, 60),
            )
            resp.raise_for_status()
            body = resp.json()
            for w in body.get("data", body if isinstance(body, list) else []):
                out.append(
                    WorkflowInfo(
                        name=w.get("name", ""),
                        display_name=w.get("name", ""),
                        description="active" if w.get("active") else "inactive",
                        workflow_id=str(w.get("id", "")),
                        active=bool(w.get("active", False)),
                    )
                )
            cursor = body.get("nextCursor") if isinstance(body, dict) else None
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        raise N8nError(f"Could not list n8n workflows: {exc}") from exc
    return out


def fetch_workflow(base_url: str, api_key: str, workflow_id: str) -> dict:
    """Fetch one workflow's full JSON (nodes + connections)."""
    try:
        resp = requests.get(
            f"{_base(base_url)}/workflows/{workflow_id}",
            headers=_headers(api_key),
            timeout=(10, 60),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise N8nError(f"Could not fetch workflow {workflow_id}: {exc}") from exc


def fetch_all_workflows(base_url: str, api_key: str, workflow_ids: list[str]) -> list[dict]:
    """Fetch the full JSON for each id (feeds the importer's two-pass bundle so
    cross-workflow toolWorkflow references resolve)."""
    return [fetch_workflow(base_url, api_key, wid) for wid in workflow_ids]

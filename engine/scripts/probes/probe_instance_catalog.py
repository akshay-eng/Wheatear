"""/v1/orchestrate/catalog/artifacts answered 400, not 404 -- the route exists
on the instance API, which takes the plain IAM token. If it serves the same
artifacts the console shows, the whole cookie problem disappears.

Read-only: GETs and one search POST.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.rest_client import get_iam_token  # noqa: E402

url = os.environ["IBMUrl"].rstrip("/")
tok = get_iam_token(os.environ["IBMKey"])
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {tok}", "Accept": "application/json"})
WS = "00000000-0000-0000-0000-000000000001"


def show(label, resp):
    body = " ".join(resp.text.split())[:220]
    print(f"  {resp.status_code}  {label}\n        {body}\n")


print("== the 400, in full ==")
show("GET /v1/orchestrate/catalog/artifacts", s.get(f"{url}/v1/orchestrate/catalog/artifacts", timeout=30))

print("== with common params ==")
for params in (
    {"limit": 2},
    {"workspace_id": WS},
    {"workspace_id": WS, "limit": 2},
    {"category": "tool", "limit": 2},
    {"category": "tool", "limit": 2, "workspace_id": WS},
    {"include": "global", "limit": 2},
    {"offset": 0, "limit": 2, "sort_by": "name", "sort_order": "ASC"},
):
    show(f"GET ?{params}", s.get(f"{url}/v1/orchestrate/catalog/artifacts", params=params, timeout=30))

print("== sibling routes ==")
for path in (
    "/v1/orchestrate/catalog",
    "/v1/orchestrate/catalog/artifacts/filters",
    "/v1/orchestrate/catalog/artifacts/list",
    "/v1/orchestrate/catalog/tools",
    "/v1/orchestrate/catalog/agents",
    "/v1/orchestrate/catalog/mcp_servers",
):
    try:
        show(f"GET {path}", s.get(url + path, params={"limit": 2}, timeout=30))
    except Exception as e:
        print(f"  ERR {type(e).__name__} {path}\n")

print("== POST the console-shaped body at the instance API ==")
BODY = {
    "limit": 2, "offset": 0,
    "select_fields": ["name", "id", "description", "category", "external_identifier", "tags", "type"],
    "grouped_results": False, "sort_by": "name", "sort_order": "ASC",
    "search_criteria": [{"filter_groups_operator": "OR", "filter_groups": [
        {"filters_operator": "AND",
         "filters": [{"condition_type": "in", "id": "category", "value": ["tool"]}]}]}],
}
for path in ("/v1/orchestrate/catalog/artifacts/list", "/v1/orchestrate/catalog/artifacts"):
    try:
        r = s.post(url + path, json=BODY, timeout=45)
        show(f"POST {path}", r)
        if r.status_code < 400:
            d = r.json()
            print("        keys:", list(d) if isinstance(d, dict) else type(d))
            print("        sample:", json.dumps(d, indent=1)[:600])
    except Exception as e:
        print(f"  ERR {type(e).__name__} {path}\n")

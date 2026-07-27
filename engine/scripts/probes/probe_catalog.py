"""Read-only probe of the live catalog service.

Answers three questions the design depends on:
  1. Does the catalog accept an IAM bearer token, or only a console session?
  2. Can we ask for a category with NO type filter -- i.e. is "every tool"
     expressible without hardcoding the list of tool types?
  3. What does /artifacts/filters report as the live vocabulary?

Nothing is written or installed.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.catalog_client import console_base  # noqa: E402
from wheatear.connectors.orchestrate.rest_client import get_iam_token  # noqa: E402

url = os.environ["IBMUrl"]
base = console_base(url)
print("catalog base:", base)

tok = get_iam_token(os.environ["IBMKey"])
s = requests.Session()
s.headers.update({
    "Authorization": f"Bearer {tok}",
    "Accept": "application/json",
    "Content-Type": "application/json",
})

FIELDS = ["name", "id", "description", "category", "publisher",
          "external_identifier", "tags", "kind", "type"]


def probe(label, filters, limit=1):
    body = {
        "limit": limit, "offset": 0, "select_fields": FIELDS,
        "grouped_results": False, "sort_by": "name", "sort_order": "ASC",
        "search_criteria": [{"filter_groups_operator": "OR", "filter_groups": filters}],
    }
    try:
        r = s.post(f"{base}/artifacts/list", json=body, timeout=45)
    except Exception as e:
        print(f"  {label:<34} ERR {type(e).__name__}")
        return None
    if r.status_code >= 400:
        print(f"  {label:<34} {r.status_code}  {' '.join(r.text.split())[:110]}")
        return None
    d = r.json()
    print(f"  {label:<34} 200  total={d.get('total')}")
    return d


print("\n-- GET /artifacts/filters (bearer) --")
try:
    r = s.get(f"{base}/artifacts/filters", timeout=45)
    print("  status:", r.status_code)
    if r.status_code < 400:
        print(json.dumps(r.json(), indent=1)[:1200])
except Exception as e:
    print("  ERR", type(e).__name__, e)

print("\n-- POST /artifacts/list (bearer) --")
probe("category=tool + type triple", [{
    "filters_operator": "AND",
    "filters": [
        {"condition_type": "in", "id": "category", "value": ["tool"]},
        {"condition_type": "in", "id": "type", "value": ["python", "openapi", "flow"]},
    ],
}])
probe("category=tool, NO type filter", [{
    "filters_operator": "AND",
    "filters": [{"condition_type": "in", "id": "category", "value": ["tool"]}],
}])
probe("category=mcp_server", [{
    "filters_operator": "AND",
    "filters": [{"condition_type": "in", "id": "category", "value": ["mcp_server"]}],
}])
probe("category=agent", [{
    "filters_operator": "AND",
    "filters": [{"condition_type": "in", "id": "category", "value": ["agent"]}],
}])
probe("NO filters at all", [])

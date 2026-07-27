"""Each constant in catalog_client.py is a guess about someone else's service.
Test which ones the API will hand us instead.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.catalog_client import OrchestrateCatalogClient  # noqa: E402

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
har = json.load(open(HAR))
cookie = next(
    h["value"]
    for e in har["log"]["entries"] if "catalogv3" in e["request"]["url"]
    for h in e["request"]["headers"] if h["name"].lower() == "cookie"
)
c = OrchestrateCatalogClient(os.environ["IBMUrl"], session_cookie=cookie)
H = dict(c._session.headers)
LIST = f"{c.base}/artifacts/list"


def post(body):
    r = requests.post(LIST, json=body, headers=H, timeout=45)
    if r.status_code >= 400:
        return f"{r.status_code} {' '.join(r.text.split())[:90]}", None
    d = r.json()
    return f"200 total={d.get('total')}", d


BASE = {"limit": 1, "offset": 0, "grouped_results": False,
        "search_criteria": [{"filter_groups_operator": "OR", "filter_groups": [
            {"filters_operator": "AND",
             "filters": [{"condition_type": "in", "id": "category", "value": ["tool"]}]}]}]}

print("== can select_fields be omitted? ==")
note, d = post(BASE)
print("  omitted entirely      :", note)
if d and d.get("artifacts"):
    got = sorted(d["artifacts"][0].keys())
    print("  fields returned anyway:", got)

print("\n== can sort_by be omitted? (we only need stable paging) ==")
print("  omitted:", post(BASE)[0])

print("\n== all three categories in ONE query ==")
allcat = dict(BASE, limit=1)
allcat["search_criteria"] = [{"filter_groups_operator": "OR", "filter_groups": [
    {"filters_operator": "AND",
     "filters": [{"condition_type": "in", "id": "category",
                  "value": ["tool", "mcp_server", "agent"]}]}]}]
note, d = post(allcat)
print("  category in [tool,mcp_server,agent]:", note)

print("\n== per-category totals ==")
for cat in ("tool", "mcp_server", "agent"):
    body = dict(BASE)
    body["search_criteria"] = [{"filter_groups_operator": "OR", "filter_groups": [
        {"filters_operator": "AND",
         "filters": [{"condition_type": "in", "id": "category", "value": [cat]}]}]}]
    print(f"  {cat:<12}", post(body)[0])

print("\n== does an unknown type value break the query or narrow it? ==")
body = dict(BASE)
body["search_criteria"] = [{"filter_groups_operator": "OR", "filter_groups": [
    {"filters_operator": "AND", "filters": [
        {"condition_type": "in", "id": "category", "value": ["tool"]},
        {"condition_type": "in", "id": "type", "value": ["python", "openapi", "flow", "wasm"]}]}]}]
print("  with a bogus 'wasm' type:", post(body)[0])

print("\n== page size ceiling ==")
for n in (100, 250, 500, 1200, 2000):
    body = dict(BASE, limit=n)
    note, d = post(body)
    got = len(d.get("artifacts") or []) if d else 0
    print(f"  limit={n:<5} {note}  returned={got}")

print("\n== is there a per-artifact detail route with a schema? ==")
aid = "a35ecf5f-0f5e-4c8f-9172-73e49cf762d5"
for path in (f"/artifacts/{aid}", f"/artifacts/{aid}/details", f"/artifacts/{aid}/spec",
             f"/artifacts/{aid}/tools", f"/artifacts/detail/{aid}"):
    try:
        r = requests.get(c.base + path, headers=H, timeout=30)
        print(f"  {r.status_code}  {path}")
        if r.status_code < 400:
            print("      ", json.dumps(r.json(), indent=1)[:700])
    except Exception as e:
        print(f"  ERR {type(e).__name__} {path}")

"""Verify the real client can reach EVERY artifact in the catalog.

Auth comes from the session cookie in the user's own HAR capture. Read-only:
list queries only, no writes.
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.catalog_client import (  # noqa: E402
    OrchestrateCatalogClient,
    to_artifacts,
)

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
har = json.load(open(HAR))
cookie = None
for e in har["log"]["entries"]:
    if "catalogv3" in e["request"]["url"]:
        cookie = next(h["value"] for h in e["request"]["headers"] if h["name"].lower() == "cookie")
        break

client = OrchestrateCatalogClient(os.environ["IBMUrl"], session_cookie=cookie)
print("base      :", client.base)
print("auth mode :", client.auth_mode)

print("\n-- live filters vocabulary --")
import requests  # noqa: E402
r = requests.get(f"{client.base}/artifacts/filters", headers=dict(client._session.headers), timeout=45)
print("  status:", r.status_code)
if r.status_code < 400:
    for group in r.json().get("filters", []):
        opts = group.get("options") or []
        flat = []
        for o in opts:
            flat.append(o.get("value"))
            for sub in (o.get("options") or []):
                flat.append(f"{o.get('value')}/{sub.get('value')}")
        print(f"  {group.get('id')}: {len(opts)} option(s) -> {flat[:12]}")

print("\n-- full sweep --")
tools = client.list_tools()
print(f"  tools       : {len(tools)}")
mcp = client.list_mcp_servers()
print(f"  mcp servers : {len(mcp)}")

arts = to_artifacts(tools + mcp)
print(f"  flattened   : {len(arts)}")
print("  by type     :", dict(Counter(a.type or a.category for a in arts)))
print("  by publisher:", dict(Counter(a.publisher for a in arts).most_common(6)))
print("  missing external_identifier:", sum(1 for a in arts if not a.external_identifier))
print("  duplicate install refs     :",
      len(arts) - len({a.install_ref for a in arts}))

print("\n  first 3 :", [a.install_ref for a in arts[:3]])
print("  last  3 :", [a.install_ref for a in arts[-3:]])

# Does dropping the type filter reach more than the triple does?
print("\n-- is the hardcoded type list lossy? --")
no_type = client._fetch_all([{
    "filters_operator": "AND",
    "filters": [{"condition_type": "in", "id": "category", "value": ["tool"]}],
}])
print(f"  category=tool, no type filter : {len(no_type)}")
print(f"  category=tool, python/openapi/flow : {len(tools)}")
seen = {t.get("id") for t in tools}
missed = [t for t in no_type if t.get("id") not in seen]
print(f"  reachable ONLY without the type filter : {len(missed)}")
if missed:
    print("  their types:", dict(Counter(t.get("type") for t in missed)))
    print("  examples   :", [t.get("name") for t in missed[:5]])

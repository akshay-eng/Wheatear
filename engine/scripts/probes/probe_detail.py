"""Does GET /artifacts/{id} carry a parameter schema? That's the ceiling on
tier-2 match quality: without params the model judges on prose alone.
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

for aid, label in [
    ("a35ecf5f-0f5e-4c8f-9172-73e49cf762d5", "tool: Accept a Merge Request in GitLab"),
    ("c6a95f1c-5c18-4440-8491-ad4a77af9810", "mcp_server: Athenium Weather Intelligence"),
]:
    r = requests.get(f"{c.base}/artifacts/{aid}", headers=H, timeout=30)
    print("=" * 22, label, "->", r.status_code)
    if r.status_code >= 400:
        print(" ", r.text[:200])
        continue
    d = r.json()
    d.pop("icon", None)
    print("  keys:", sorted(d.keys()))
    schema_keys = [k for k in d if any(w in k.lower() for w in ("schema", "input", "param", "spec", "arg"))]
    print("  schema-ish keys:", schema_keys or "NONE")
    print(json.dumps(d, indent=1)[:1500])
    print()

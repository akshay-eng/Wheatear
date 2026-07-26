"""What exactly is in spec_file? If it carries input schemas, tier-2 matching
stops being prose-only.
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


def strip_icons(obj):
    if isinstance(obj, dict):
        return {k: ("<svg…>" if k == "icon" else strip_icons(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_icons(v) for v in obj]
    return obj


for aid, label in [
    ("a35ecf5f-0f5e-4c8f-9172-73e49cf762d5", "python tool (GitLab)"),
    ("c6a95f1c-5c18-4440-8491-ad4a77af9810", "mcp server (Athenium)"),
]:
    d = requests.get(f"{c.base}/artifacts/{aid}", headers=H, timeout=30).json()
    print("=" * 25, label)
    for key in ("spec_file", "files", "servers", "subscription", "version", "artifact_group"):
        val = strip_icons(d.get(key))
        text = json.dumps(val, indent=1)
        print(f"-- {key} ({len(text)} chars)")
        print(text[:1100])
        print()

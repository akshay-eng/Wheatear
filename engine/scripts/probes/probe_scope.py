"""The instance API serves /v1/orchestrate/catalog/artifacts to a plain IAM
token, but ?category=tool comes back empty. Find the scoping parameter that
makes it return the global catalog, and confirm the per-artifact detail route.

Read-only throughout.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.rest_client import get_iam_token  # noqa: E402

url = os.environ["IBMUrl"].rstrip("/")
ART = f"{url}/v1/orchestrate/catalog/artifacts"
tok = get_iam_token(os.environ["IBMKey"])
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {tok}", "Accept": "application/json"})

# A real artifact id + group id from the browser capture.
KNOWN_ARTIFACT = "a35ecf5f-0f5e-4c8f-9172-73e49cf762d5"   # Accept a Merge Request in GitLab
KNOWN_GROUP = "75aba932-6842-4cfb-a7e6-217f1cfca664"      # its offering


def get(params, path=ART):
    try:
        r = s.get(path, params=params, timeout=30)
    except Exception as e:
        return None, f"ERR {type(e).__name__}"
    body = " ".join(r.text.split())
    if r.status_code < 400:
        try:
            d = r.json()
            if isinstance(d, dict) and "total" in d:
                return d, f"200 total={d['total']} returned={len(d.get('items') or [])}"
        except ValueError:
            pass
    return None, f"{r.status_code} {body[:130]}"


print("== scoping candidates on ?category=tool ==")
for extra in (
    {},
    {"include": "global"},
    {"artifact_origin": "global"},
    {"origin": "global"},
    {"scope": "global"},
    {"visibility": "public"},
    {"kind": "native"},
    {"type": "python"},
    {"published": "true"},
    {"is_global": "true"},
    {"include_global": "true"},
    {"source": "global"},
    {"tenant_scope": "global"},
    {"catalog": "global"},
):
    params = {"category": "tool", "limit": 3, **extra}
    _, note = get(params)
    print(f"  {str(extra) or '(none)':<34} {note}")

print("\n== other categories ==")
for cat in ("tool", "agent", "mcp_server", "model", "knowledge_base"):
    _, note = get({"category": cat, "limit": 3})
    print(f"  category={cat:<16} {note}")

print("\n== by artifact_group_external_id (the other accepted key) ==")
for key in ("artifact_group_external_id",):
    for val in (KNOWN_GROUP, "Devops and CICD Management with Gitlab"):
        _, note = get({key: val, "limit": 3})
        print(f"  {key}={val[:40]:<42} {note}")

print("\n== per-artifact detail route ==")
for path in (f"{ART}/{KNOWN_ARTIFACT}", f"{ART}/{KNOWN_ARTIFACT}/tools"):
    try:
        r = s.get(path, timeout=30)
        print(f"  {r.status_code}  {path.replace(url, '<inst>')}")
        if r.status_code < 400:
            d = r.json()
            print("      keys:", list(d) if isinstance(d, dict) else type(d).__name__)
            print("      ", json.dumps(d, indent=1)[:900])
    except Exception as e:
        print(f"  ERR {type(e).__name__} {path}")

print("\n== POST with category in the body ==")
for body in ({"category": "tool", "limit": 3},
             {"category": "tool", "limit": 3, "include": "global"}):
    try:
        r = s.post(ART, json=body, timeout=30)
        print(f"  {r.status_code}  body={body}\n      {' '.join(r.text.split())[:250]}")
    except Exception as e:
        print(f"  ERR {type(e).__name__}")

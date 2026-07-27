"""The catalog proxy accepts the IAM token but fails with 'Data processing
failed' -- it has no tenant context. The console supplies that via cookies.

Question: can tenant context be *derived* from the API key + instance URL, so
this works headlessly? The IAM token is a JWT carrying the BSS account id, and
the instance id is in the URL, which together are exactly the shape of the
tenant id seen in the capture (<account>_<instance>).
"""
import base64
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from wheatear.connectors.orchestrate.catalog_client import console_base  # noqa: E402
from wheatear.connectors.orchestrate.rest_client import get_iam_token  # noqa: E402

url = os.environ["IBMUrl"].rstrip("/")
base = console_base(url)
instance_id = url.split("/instances/")[-1]
host = base.split("://")[1].split(".")[0]  # region label, e.g. us-south
region = base.split("://")[1].split(".watson-orchestrate")[0]

tok = get_iam_token(os.environ["IBMKey"])
payload = tok.split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
account = (claims.get("account") or {}).get("bss")
print("region      :", region)
print("instance id :", instance_id)
print("bss account :", account)
print("iam claims  :", sorted(claims.keys()))

tenant = f"{account}_{instance_id}"
crn = f"crn:v1:bluemix:public:watsonx-orchestrate:{region}:a/{account}:{instance_id}::"
print("derived tenant:", tenant)
print("derived crn   :", crn)
print("capture tenant:", os.environ.get("WXO_CAPTURE_TENANT", "<set WXO_CAPTURE_TENANT to compare>"))

FIELDS = ["name", "id", "description", "category", "external_identifier", "tags", "type"]
BODY = {
    "limit": 1, "offset": 0, "select_fields": FIELDS, "grouped_results": False,
    "sort_by": "name", "sort_order": "ASC",
    "search_criteria": [{"filter_groups_operator": "OR", "filter_groups": [
        {"filters_operator": "AND",
         "filters": [{"condition_type": "in", "id": "category", "value": ["tool"]}]}]}],
}

variants = {
    "bearer only": {},
    "+ tenant header": {"x-ibm-wo-tenant-id": tenant},
    "+ tenant cookie": {"Cookie": f"x-ibm-wo-tenant-id={tenant}"},
    "+ tenant hdr + crn cookie": {"x-ibm-wo-tenant-id": tenant, "Cookie": f"crn={crn}"},
    "+ tenant & crn cookies": {"Cookie": f"x-ibm-wo-tenant-id={tenant}; crn={crn}"},
    "+ all three": {"x-ibm-wo-tenant-id": tenant, "IAM-API_KEY": "",
                    "Cookie": f"x-ibm-wo-tenant-id={tenant}; crn={crn}"},
}

print("\n-- POST /artifacts/list --")
for label, extra in variants.items():
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/json",
         "Content-Type": "application/json", **extra}
    try:
        r = requests.post(f"{base}/artifacts/list", json=BODY, headers=h, timeout=45)
        note = ""
        if r.status_code < 400:
            note = f"total={r.json().get('total')}"
        else:
            note = " ".join(r.text.split())[:90]
        print(f"  {label:<28} {r.status_code}  {note}")
    except Exception as e:
        print(f"  {label:<28} ERR {type(e).__name__}")

# Is there an instance-API route to the same data? Cheap to rule out.
print("\n-- instance API, for completeness --")
api = requests.Session()
api.headers.update({"Authorization": f"Bearer {tok}", "Accept": "application/json"})
for path in ("/v1/orchestrate/catalog/artifacts", "/v1/catalog/artifacts",
             "/v1/orchestrate/artifacts", "/v1/orchestrate/tools/catalog"):
    try:
        r = api.get(url + path, timeout=25)
        print(f"  {r.status_code}  {path}")
    except Exception as e:
        print(f"  ERR {type(e).__name__}  {path}")

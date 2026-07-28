"""The shortlist put `create_a_record` above everything for a *get* operation.
Either the right tool isn't in the catalog, or the ranking is wrong. Find out
which.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from agent_liftoff.connectors.orchestrate.catalog_client import (  # noqa: E402
    OrchestrateCatalogClient,
    to_artifacts,
)
from agent_liftoff.ir.schema import ToolParameter, ToolRef  # noqa: E402
from agent_liftoff.pipeline.resolve import (  # noqa: E402
    _idf,
    _source_tokens,
    build_marketplace_catalog,
)

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
har = json.load(open(HAR))
os.environ["WXO_CONSOLE_COOKIE"] = next(
    h["value"]
    for e in har["log"]["entries"] if "catalogv3" in e["request"]["url"]
    for h in e["request"]["headers"] if h["name"].lower() == "cookie"
)

client = OrchestrateCatalogClient(os.environ["IBMUrl"])
arts = to_artifacts(client.list_installable())
pool = build_marketplace_catalog(arts)

print("== every ServiceNow artifact whose name mentions 'record' ==")
for a in arts:
    blob = f"{a.name} {a.install_ref}".lower()
    if "record" in blob and ("servicenow" in blob or "service now" in blob or "snow" in blob):
        print(f"  {a.install_ref:<34} {a.name}")

print("\n== anything named like a plain 'get record' ==")
for a in arts:
    n = a.install_ref.lower()
    if n.startswith(("get_a_record", "get_record", "read_record", "retrieve")):
        print(f"  {a.install_ref:<34} {a.name}   [{a.description[:60]}]")

source = ToolRef(
    ref="Get Record", operation_id="GetRecord",
    description="Gets a single ServiceNow record by its sys_id.",
    review_required=True, confidence=0.0,
    inputs=[ToolParameter(name="tableType", description="ServiceNow table name."),
            ToolParameter(name="sysid", description="The record's 32-char hex sys_id.")],
)

idf = _idf(pool)
print("\n== IDF weight of the query's own tokens (higher = more discriminating) ==")
from collections import Counter  # noqa: E402
for tok, n in sorted(Counter(_source_tokens(source)).items(), key=lambda kv: -idf.get(kv[0], 1)):
    print(f"  {tok:<14} idf={idf.get(tok, 1.0):5.2f}  x{n}")

print("\n== score breakdown for a few named candidates ==")
import math  # noqa: E402
wanted = Counter(_source_tokens(source))
for want_ref in ("create_a_record", "get_record_count", "get_a_record",
                 "servicenow_get_record", "update_a_record", "get_table_fields"):
    cand = next((c for c in pool if c.ref == want_ref), None)
    if cand is None:
        print(f"  {want_ref:<26} NOT IN CATALOG")
        continue
    avail = Counter(cand.match_text())
    contrib = {t: min(n, avail[t]) * idf.get(t, 1.0) for t, n in wanted.items() if t in avail}
    overlap = sum(contrib.values())
    print(f"  {want_ref:<26} raw={overlap:6.2f} len={len(avail):3d} "
          f"score={overlap / math.sqrt(len(avail) or 1):6.2f}  {contrib}")

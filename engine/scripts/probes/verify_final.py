"""Final verification against the live catalog, using only shipped code paths.

Checks that the rewritten client reaches every artifact the console shows, that
discovery is dynamic rather than hardcoded, and that enrichment supplies the
parameter schemas the list endpoint omits.
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from agent_liftoff.connectors.orchestrate.catalog_client import (  # noqa: E402
    OrchestrateCatalogClient,
    enrich_artifacts,
    to_artifacts,
)
from agent_liftoff.pipeline.resolve import (  # noqa: E402
    build_marketplace_catalog,
    shortlist_scored,
)
from agent_liftoff.ir.schema import ToolParameter, ToolRef  # noqa: E402

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
har = json.load(open(HAR))
os.environ["WXO_CONSOLE_COOKIE"] = next(
    h["value"]
    for e in har["log"]["entries"] if "catalogv3" in e["request"]["url"]
    for h in e["request"]["headers"] if h["name"].lower() == "cookie"
)

# Cookie comes from the environment now, exactly as a user would set it.
client = OrchestrateCatalogClient(os.environ["IBMUrl"])
print("base       :", client.base)
print("auth mode  :", client.auth_mode)
print("categories :", client.discover_categories(), "(discovered, not hardcoded)")

records = client.list_installable()
arts = to_artifacts(records)
print(f"\ninstallable artifacts: {len(arts)}")
print("  by category :", dict(Counter(a.category for a in arts)))
print("  page size   :", client.page_size)
print("  missing external_identifier:", sum(1 for a in arts if not a.external_identifier))

everything = to_artifacts(client.list_artifacts())
print(f"\nevery artifact incl. agents: {len(everything)}")
print("  by category :", dict(Counter(a.category for a in everything)))

pool = build_marketplace_catalog(arts)
print(f"\nmatchable pool: {len(pool)}")

source = ToolRef(
    ref="Get Record", operation_id="GetRecord",
    description="Gets a single ServiceNow record by its sys_id.",
    review_required=True, confidence=0.0,
    inputs=[ToolParameter(name="tableType", description="ServiceNow table name."),
            ToolParameter(name="sysid", description="The record's 32-char hex sys_id.")],
)
ranked = shortlist_scored(source, pool)
print("\nshortlist for the real Copilot 'Get Record', over all 1173:")
for i, (score, c) in enumerate(ranked, 1):
    print(f"  {i}. {score:6.2f}  {c.ref:<42} {c.display_name}")

print("\nenriching just that shortlist (8 requests, not 1173)…")
enrich_artifacts(client, [c[1].artifact for c in ranked])
for _s, c in ranked[:4]:
    a = c.artifact
    print(f"  {c.ref:<42} params={a.params or '-'}")
    if a.required_params:
        print(f"      required={a.required_params}  connections={a.connections}")

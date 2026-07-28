"""Recall check: for a set of source tools with a known right answer in the
1173-entry catalog, does the deterministic shortlist put that answer in front
of the model at all?

Shortlist precision matters far less than recall -- all 8 candidates go to the
model, which adjudicates. But an answer that never reaches the shortlist can
never be chosen.
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
from agent_liftoff.pipeline.resolve import build_marketplace_catalog, shortlist  # noqa: E402

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
har = json.load(open(HAR))
os.environ["WXO_CONSOLE_COOKIE"] = next(
    h["value"]
    for e in har["log"]["entries"] if "catalogv3" in e["request"]["url"]
    for h in e["request"]["headers"] if h["name"].lower() == "cookie"
)
client = OrchestrateCatalogClient(os.environ["IBMUrl"])
pool = build_marketplace_catalog(to_artifacts(client.list_installable()))
print(f"pool: {len(pool)} artifacts\n")

# (source name, description, inputs, refs that would be a correct answer)
CASES = [
    ("Get Record", "Gets a single ServiceNow record by its sys_id.",
     ["tableType", "sysid"], {"get_records", "get_record_count"}),
    ("List Records", "Gets records of a certain ServiceNow object type like 'Incidents'.",
     [], {"get_records"}),
    ("Create Incident", "Creates a new incident ticket in ServiceNow.",
     ["short_description", "urgency"], {"create_a_record", "create_incident"}),
    ("Send Email", "Sends an email message through Outlook.",
     ["to", "subject", "body"], {"send_email", "send_an_email", "send_email_outlook"}),
    ("Create Calendar Event", "Creates an event on the user's Outlook calendar.",
     ["subject", "start", "end"], {"create_event", "create_an_event", "create_calendar_event"}),
    ("Upload File to SharePoint", "Uploads a document to a SharePoint document library.",
     ["file", "library"], {"upload_file", "upload_a_file"}),
    ("Get Employee Details", "Retrieves an employee record from Workday.",
     ["employee_id"], {"get_worker", "get_workers", "get_employee"}),
    ("Search Knowledge Base", "Searches ServiceNow knowledge articles.",
     ["query"], {"get_knowledge_articles", "search_knowledge"}),
]

hits = 0
for name, desc, inputs, expected in CASES:
    tool = ToolRef(
        ref=name, description=desc, review_required=True, confidence=0.0,
        inputs=[ToolParameter(name=i) for i in inputs],
    )
    ranked = shortlist(tool, pool)
    refs = [c.ref for c in ranked]
    # Which of the expected refs actually exist in this catalog?
    exists = {e for e in expected if any(c.ref == e for c in pool)}
    found = [r for r in refs if r in exists]
    ok = bool(found) or not exists
    hits += ok
    status = "OK " if found else ("n/a" if not exists else "MISS")
    print(f"{status}  {name}")
    print(f"      expected present in catalog: {sorted(exists) or '(none of the guesses exist)'}")
    print(f"      shortlist: {refs[:6]}")
    if exists and not found:
        for c in pool:
            if c.ref in exists:
                print(f"      -> {c.ref} exists but was not shortlisted: {c.display_name}")
    print()

print(f"cases where a known-correct ref reached the shortlist: {hits}/{len(CASES)}")

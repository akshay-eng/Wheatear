"""Quality check for catalog discovery and tool matching.

Run this to confirm the matcher is genuinely reaching the whole catalog rather
than silently degrading to a partial view. Every check states what it proves,
so a FAIL tells you what broke rather than just that something did.

    python scripts/qc_catalog.py                     # all checks
    python scripts/qc_catalog.py --expect-tools 1152 # pin an exact count

Needs IBMUrl and IBMKey; WXO_CONSOLE_COOKIE if the catalog needs a session.
Read-only -- nothing is installed, deployed or written to your instance.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_liftoff.connectors.orchestrate.catalog_client import (  # noqa: E402
    COOKIE_ENV,
    OrchestrateCatalogClient,
    enrich_artifacts,
    to_artifacts,
)
from agent_liftoff.connectors.orchestrate.rest_client import OrchestrateRestClient  # noqa: E402
from agent_liftoff.errors import LiftoffError  # noqa: E402
from agent_liftoff.ir.schema import ToolParameter, ToolRef  # noqa: E402
from agent_liftoff.pipeline.resolve import (  # noqa: E402
    build_catalog,
    build_marketplace_catalog,
    shortlist,
)

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Source tools with a capability that demonstrably exists in the catalog. Each
# lists refs any of which would be a correct answer -- the check is recall (did
# a right answer reach the model at all), not which one ranked first.
RECALL_CASES = [
    ("Get Record", "Gets a single ServiceNow record by its sys_id.",
     ["tableType", "sysid"], {"get_records", "get_record_count"}),
    ("List Records", "Gets records of a certain ServiceNow object type.",
     [], {"get_records"}),
    ("Create Incident", "Creates a new incident ticket in ServiceNow.",
     ["short_description"], {"create_an_incident", "create_a_record"}),
    ("Send Email", "Sends an email message through Outlook.",
     ["to", "subject", "body"], {"send_email", "send_an_email", "send_mail"}),
    ("Upload File", "Uploads a document to a SharePoint library.",
     ["file"], {"upload_file_to_sharepoint", "upload_a_file", "upload_file"}),
]


class Report:
    def __init__(self) -> None:
        self.passed = self.failed = self.warned = 0

    def check(self, ok: bool, label: str, proves: str, detail: str = "") -> bool:
        mark, colour = ("PASS", GREEN) if ok else ("FAIL", RED)
        self.passed += ok
        self.failed += not ok
        print(f"  {colour}{mark}{RESET}  {label}")
        print(f"        {DIM}{proves}{RESET}")
        if detail:
            print(f"        {detail}")
        return ok

    def warn(self, label: str, detail: str) -> None:
        self.warned += 1
        print(f"  {YELLOW}WARN{RESET}  {label}")
        print(f"        {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-tools", type=int, default=None,
                        help="Fail if the tool count differs from this (use the number the UI shows).")
    parser.add_argument("--min-tools", type=int, default=500,
                        help="Fail if fewer than this many tools are reachable.")
    args = parser.parse_args()

    instance_url, api_key = os.environ.get("IBMUrl"), os.environ.get("IBMKey")
    if not instance_url or not api_key:
        print("Set IBMUrl and IBMKey first.", file=sys.stderr)
        return 2

    report = Report()

    # ---------------------------------------------------------------- tier 1
    print("\nInstalled tools (tier 1 -- needs only your API key)")
    try:
        installed_raw = OrchestrateRestClient(api_key, instance_url).list_all_tools()
        installed = build_catalog(installed_raw)
        report.check(
            len(installed) > 0,
            f"read {len(installed)} installed tool(s)",
            "Tier 1 is the pool that is importable today; without it nothing resolves.",
            f"kinds: {dict(Counter(t.kind for t in installed))}",
        )
    except LiftoffError as exc:
        report.check(False, "could not read installed tools", "Tier 1 is unavailable.", str(exc))
        installed = []

    # ---------------------------------------------------------------- tier 2
    print("\nGlobal catalog (tier 2 -- may need a console cookie)")
    cookie = os.environ.get(COOKIE_ENV)
    print(f"  {DIM}auth: {'session cookie' if cookie else 'API key (will try IAM first)'}{RESET}")

    try:
        client = OrchestrateCatalogClient(
            instance_url,
            **({"session_cookie": cookie} if cookie else {"api_key": api_key}),
        )
        started = time.time()
        records = client.list_installable()
        elapsed = time.time() - started
    except LiftoffError as exc:
        report.check(False, "catalog unreachable", "Tier 2 matching is unavailable.", str(exc))
        print(f"\n{YELLOW}Tier 1 still works. Fix the catalog auth and re-run.{RESET}")
        return 1

    artifacts = to_artifacts(records)
    counts = Counter(a.category for a in artifacts)
    tools = counts.get("tool", 0)

    report.check(
        len(artifacts) >= args.min_tools,
        f"read {len(artifacts)} installable artifact(s) in {elapsed:.1f}s",
        "Proves pagination walked the whole catalog, not just the first page.",
        f"by category: {dict(counts)}   page size: {client.page_size}",
    )

    if args.expect_tools is not None:
        report.check(
            tools == args.expect_tools,
            f"tool count is {tools}, expected {args.expect_tools}",
            "Compare against the number the catalog UI shows next to 'Tools'.",
        )
    else:
        print(f"  {DIM}      open the catalog UI and confirm it also says Tools ({tools}){RESET}")

    report.check(
        all(a.external_identifier for a in artifacts),
        "every artifact has an install identifier",
        "Without it a match can't be written into an agent.yaml that imports.",
        f"missing: {sum(1 for a in artifacts if not a.external_identifier)}",
    )

    refs = [a.install_ref for a in artifacts]
    report.check(
        len(refs) == len(set(refs)),
        "install identifiers are unique",
        "A duplicate means a match could resolve to the wrong tool.",
        f"duplicates: {len(refs) - len(set(refs))}",
    )

    categories = client.discover_categories()
    report.check(
        len(categories) >= 2,
        f"categories discovered from the service: {categories}",
        "Proves the query is built from what the service reports, not a hardcoded list.",
    )

    # ------------------------------------------------------------ enrichment
    print("\nDetail enrichment (parameter schemas for shortlisted candidates)")
    sample = artifacts[:3]
    enrich_artifacts(client, sample)
    enriched = [a for a in sample if a.enriched]
    report.check(
        bool(enriched),
        f"fetched detail for {len(enriched)}/{len(sample)} sampled artifact(s)",
        "Without this the model judges catalog candidates on prose alone.",
        "  ".join(f"{a.install_ref}({len(a.params)} params)" for a in enriched) or "none",
    )

    # ----------------------------------------------------------------- recall
    print("\nShortlist recall (does a correct answer reach the model?)")
    pool = build_marketplace_catalog(artifacts)
    available = {c.ref for c in pool}
    for name, description, inputs, expected in RECALL_CASES:
        present = expected & available
        if not present:
            report.warn(name, f"none of {sorted(expected)} exist in this catalog; skipped")
            continue
        tool = ToolRef(
            ref=name, description=description, review_required=True, confidence=0.0,
            inputs=[ToolParameter(name=i) for i in inputs],
        )
        ranked = [c.ref for c in shortlist(tool, pool)]
        hit = [r for r in ranked if r in present]
        report.check(
            bool(hit),
            f"{name} -> {hit[0] if hit else 'MISSED'}",
            "A candidate outside the shortlist can never be chosen, however good it is.",
            f"top 4: {ranked[:4]}",
        )

    # ----------------------------------------------------------------- verdict
    print(f"\n{'=' * 62}")
    print(f"  {GREEN}{report.passed} passed{RESET}   "
          f"{RED if report.failed else DIM}{report.failed} failed{RESET}   "
          f"{YELLOW if report.warned else DIM}{report.warned} skipped{RESET}")
    if not report.failed:
        print(f"  {GREEN}Catalog discovery and matching are working.{RESET}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Dump the full watsonx Orchestrate catalog to a readable text file.

A reference artifact, not part of the pipeline: it exists so a human can read
what the matcher is matching against, and so a catalog snapshot can be diffed
between runs to see what IBM added or withdrew.

    python scripts/dump_catalog.py                    # text, to stdout's sibling file
    python scripts/dump_catalog.py --format json      # machine-readable
    python scripts/dump_catalog.py --enrich           # + parameter schemas (slow)

Reads IBMUrl / IBMKey, and WXO_CONSOLE_COOKIE if the catalog needs a session.
Read-only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_liftoff.connectors.orchestrate.catalog_client import (  # noqa: E402
    COOKIE_ENV,
    OrchestrateCatalogClient,
    enrich_artifacts,
    to_artifacts,
)
from agent_liftoff.errors import LiftoffError  # noqa: E402


def _provenance(instance_url: str) -> str:
    """Region, not the instance URL.

    The catalog is global -- identical for every tenant in a region -- so the
    instance id adds nothing to a snapshot except a tenant identifier that
    would then live in whatever the snapshot gets committed to.
    """
    host = urlsplit(instance_url if "://" in instance_url else f"https://{instance_url}").netloc
    return re.sub(r"^api\.", "", host).split(".watson-orchestrate")[0] or "unknown"


def render_text(artifacts, instance_url: str, enriched: bool) -> str:
    by_category: dict[str, list] = defaultdict(list)
    for artifact in artifacts:
        by_category[artifact.category].append(artifact)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "watsonx Orchestrate catalog snapshot",
        "=" * 78,
        f"taken      : {stamp}",
        f"region     : {_provenance(instance_url)}",
        f"artifacts  : {len(artifacts)}",
        "by category: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_category.items())),
        "",
        "These are INSTALLABLE, not installed. Referencing one from an agent.yaml",
        "fails until it is added to the instance and its connection configured.",
        "The name after '->' is what the tool is called once installed; the title",
        "is what to search for in the catalog UI.",
        "",
    ]

    for category in sorted(by_category):
        entries = sorted(by_category[category], key=lambda a: a.name.lower())
        lines += ["", "=" * 78, f"{category.upper()}  ({len(entries)})", "=" * 78, ""]

        by_group: dict[str, list] = defaultdict(list)
        for artifact in entries:
            by_group[artifact.groups[0] if artifact.groups else "(no offering)"].append(artifact)

        for group in sorted(by_group):
            lines += [f"--- {group} " + "-" * max(0, 74 - len(group)), ""]
            for artifact in sorted(by_group[group], key=lambda a: a.name.lower()):
                lines.append(f"  {artifact.name}")
                lines.append(f"      -> {artifact.install_ref}")
                meta = [f"type={artifact.type or artifact.category}"]
                if artifact.publisher:
                    meta.append(f"publisher={artifact.publisher}")
                if artifact.tags:
                    meta.append(f"tags={','.join(artifact.tags)}")
                lines.append(f"      {'  '.join(meta)}")
                description = " ".join((artifact.description or "").split())
                if description:
                    lines.append(f"      {description[:300]}")
                if enriched and artifact.params:
                    required = set(artifact.required_params)
                    shown = ", ".join(
                        f"{p}*" if p in required else p for p in artifact.params
                    )
                    lines.append(f"      params: {shown}   (* = required)")
                if artifact.connections:
                    lines.append(f"      needs connection: {', '.join(artifact.connections)}")
                if artifact.member_tools:
                    lines.append(
                        f"      exposes {len(artifact.member_tools)} tool(s): "
                        f"{', '.join(artifact.member_tools[:8])}"
                        + (" …" if len(artifact.member_tools) > 8 else "")
                    )
                lines.append("")

    lines += ["", "=" * 78, "INDEX BY PUBLISHER", "=" * 78, ""]
    for publisher, count in Counter(a.publisher or "(unknown)" for a in artifacts).most_common():
        lines.append(f"  {count:>5}  {publisher}")

    lines += ["", "=" * 78, "INDEX BY TAG", "=" * 78, ""]
    tag_counts: Counter = Counter()
    for artifact in artifacts:
        tag_counts.update(artifact.tags or ["(untagged)"])
    for tag, count in tag_counts.most_common():
        lines.append(f"  {count:>5}  {tag}")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("catalog-snapshot.txt"))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Fetch parameter schemas too. One request per artifact -- slow.",
    )
    parser.add_argument(
        "--include-agents", action="store_true", help="Include catalog agents as well."
    )
    args = parser.parse_args()

    instance_url = os.environ.get("IBMUrl")
    if not instance_url:
        print("Set IBMUrl to your Orchestrate instance URL.", file=sys.stderr)
        return 2

    cookie = os.environ.get(COOKIE_ENV)
    auth = {"session_cookie": cookie} if cookie else {"api_key": os.environ.get("IBMKey", "")}

    try:
        client = OrchestrateCatalogClient(instance_url, **auth)
        print(f"reading {client.base} …", file=sys.stderr)
        records = client.list_installable(include_agents=args.include_agents)
    except LiftoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    artifacts = to_artifacts(records)
    print(f"  {len(artifacts)} artifact(s)", file=sys.stderr)

    if args.enrich:
        print(f"  enriching {len(artifacts)} artifact(s), one request each …", file=sys.stderr)
        enrich_artifacts(client, artifacts)
        done = sum(1 for a in artifacts if a.enriched)
        print(f"  {done} enriched, {len(artifacts) - done} unavailable", file=sys.stderr)

    if args.format == "json":
        payload = [
            {
                "id": a.id,
                "name": a.name,
                "install_ref": a.install_ref,
                "category": a.category,
                "type": a.type,
                "publisher": a.publisher,
                "description": a.description,
                "tags": list(a.tags),
                "offerings": list(a.groups),
                "params": a.params,
                "required_params": a.required_params,
                "connections": a.connections,
                "member_tools": a.member_tools,
            }
            for a in sorted(artifacts, key=lambda a: (a.category, a.name.lower()))
        ]
        args.out.write_text(json.dumps(payload, indent=2))
    else:
        args.out.write_text(render_text(artifacts, instance_url, args.enrich))

    print(f"wrote {args.out.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
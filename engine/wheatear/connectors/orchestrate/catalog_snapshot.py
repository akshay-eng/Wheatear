"""Read a captured Orchestrate catalog back into `CatalogArtifact`s.

The global catalog is the same ~1500 artifacts for every Orchestrate customer
alive: it is IBM's marketplace, not a tenant's contents. That makes it the one
part of a migration that genuinely can be captured once and shipped, and the
reason it has to be captured at all is that the endpoint serving it
authenticates with a console session cookie -- an IAM API key gets a 500 from
it, verified. Nobody should need a browser session open to resolve a tool.

So the flow is: `scripts/dump_catalog.py` writes a snapshot when someone with a
session runs it, and everything downstream reads the snapshot. A snapshot goes
stale only when IBM changes the marketplace, which is a job someone runs
deliberately, not a thing every migration re-derives.

This module is the read half. It is deliberately tolerant: a snapshot written
by an older version of the dumper is still worth reading, and one unparseable
record is not worth refusing the other fifteen hundred for.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from wheatear.assets import asset
from wheatear.connectors.orchestrate.catalog_client import CatalogArtifact

# Where a snapshot lives when nobody says otherwise. Beside the engine rather
# than in a data directory: it is a checked-in asset of this repository, the
# same as the IR schema, and it is meant to ship.
DEFAULT_SNAPSHOT = asset("orchestrate", "catalog-snapshot.json")


def snapshot_age_days(path: Path | None = None) -> float | None:
    """How old the snapshot file is, or None if there isn't one."""
    path = path or DEFAULT_SNAPSHOT
    if not path.is_file():
        return None
    captured = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - captured).total_seconds() / 86400


def load_snapshot(path: Path | None = None) -> list[CatalogArtifact]:
    """Load captured catalog artifacts, or an empty list if there is no file.

    Empty rather than an exception: a missing snapshot degrades tool resolution
    to the instance's installed tools, which is a smaller answer and not a
    broken one. A migration that refused to run because a cache file was absent
    would be trading a partial result for no result.
    """
    path = path or DEFAULT_SNAPSHOT
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []

    artifacts: list[CatalogArtifact] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        if not name:
            continue
        artifacts.append(
            CatalogArtifact(
                id=str(record.get("id") or ""),
                name=str(name),
                description=str(record.get("description") or ""),
                category=str(record.get("category") or "tool"),
                type=record.get("type"),
                # The dumper writes the resolved `install_ref`, not the raw
                # `external_identifier` it came from. Restoring it here means
                # `CatalogArtifact.install_ref` returns the same string the
                # snapshot recorded rather than falling back to the display
                # name, which is not referenceable from an agent.yaml.
                external_identifier=record.get("install_ref") or None,
                publisher=record.get("publisher"),
                tags=tuple(record.get("tags") or ()),
                groups=tuple(record.get("offerings") or ()),
                params=list(record.get("params") or []),
                required_params=list(record.get("required_params") or []),
                connections=list(record.get("connections") or []),
                member_tools=list(record.get("member_tools") or []),
                # Params present means the dumper had already enriched it.
                enriched=bool(record.get("params")),
            )
        )
    return artifacts


def tools_only(artifacts: list[CatalogArtifact]) -> list[CatalogArtifact]:
    """Just the installable tools.

    The catalog also carries agents and MCP servers. They matter -- an MCP
    server is often the better answer for a source tool than any single
    installable -- but they are resolved differently, and mixing them into the
    tool shortlist means a source tool can "match" a whole agent.
    """
    return [a for a in artifacts if a.category == "tool"]

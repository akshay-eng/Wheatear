"""What every probe source shares: its inputs, its output, and the one
function that turns raw records into an `EntitySchema`.

`observe` is small and does three things in an order that matters. It redacts
before it infers, so no credential reaches the corpus or the fingerprint. It
infers over every record it was given, so the statistics (`required`,
`occurrence`, enum detection) are as good as the sample allows. And it stores
only a capped prefix of those records, because samples are test fixtures and a
cache entry should not grow with a tenant's size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from wheatear.foundry import redact, shape
from wheatear.foundry.types import EntityKind, EntitySchema, ProbeGap, ProbeOrigin

# How many records are kept as samples. Enough to write meaningful positive
# test cases from, few enough that a corpus stays a file you can open.
MAX_STORED_SAMPLES = 25

# A single record larger than this is almost always an embedded document (a
# base64 attachment, a bundled knowledge file) rather than agent structure.
# Keeping it would bloat the store without teaching the mapping anything.
MAX_SAMPLE_CHARS = 200_000


@dataclass
class ProbeContext:
    """Everything a probe might need to reach a platform.

    Deliberately one flat bag rather than a class per platform: the inspector
    hands the same context to every source, and a source takes what it
    recognises. `extra` is the escape hatch for material only one platform
    needs -- a tenant id, a CSRF token, a captured HAR path -- so that adding a
    platform never means changing this type.

    Nothing here is persisted. Credentials live in the process, in the
    environment, or in the OS keychain via `creds.py`, exactly as everywhere
    else in Wheatear.
    """

    platform: str
    export_path: Path | None = None
    instance_url: str | None = None
    api_key: str | None = None
    # A browser session `Cookie:` header, for endpoints that will not accept a
    # token. The catalog is the known case (see connectors/orchestrate/
    # catalog_client.py); the user pastes it and it expires within hours.
    session_cookie: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    # Off for a fully offline probe. The structural pass never needs it; the
    # hydration passes are skipped and record a gap instead of failing.
    allow_network: bool = True

    def has_live_access(self) -> bool:
        return bool(self.allow_network and self.instance_url and (self.api_key or self.session_cookie))


@dataclass
class ProbeResult:
    """One source's contribution to a corpus."""

    entities: list[EntitySchema] = field(default_factory=list)
    gaps: list[ProbeGap] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    platform_version: str | None = None


class ProbeSource(Protocol):
    """A way of finding out what a platform's records look like."""

    name: str

    def probe(self, context: ProbeContext) -> ProbeResult: ...


def observe(
    kind: EntityKind,
    name: str,
    origin: ProbeOrigin,
    records: list[dict],
    notes: list[str] | None = None,
) -> EntitySchema | None:
    """Turn raw records into an `EntitySchema`, redacting first.

    Returns None for an empty record set rather than an empty schema: "we
    looked and there were none" and "we could not look" are different facts,
    and the second one belongs in a `ProbeGap`, not in a field-less entity
    that would later read as a platform with no agents.
    """
    usable = [r for r in records if isinstance(r, dict) and r]
    if not usable:
        return None

    skipped = 0
    safe: list[dict] = []
    for record in usable:
        cleaned = redact.redact(record)
        if len(str(cleaned)) > MAX_SAMPLE_CHARS:
            skipped += 1
            continue
        safe.append(cleaned)
    if not safe:
        return None

    all_notes = list(notes or [])
    if skipped:
        all_notes.append(
            f"{skipped} record(s) over {MAX_SAMPLE_CHARS} characters were excluded as "
            "embedded content rather than structure."
        )
    if len(safe) > MAX_STORED_SAMPLES:
        all_notes.append(
            f"Shape inferred from all {len(safe)} record(s); "
            f"{MAX_STORED_SAMPLES} kept as samples."
        )

    return EntitySchema(
        kind=kind,
        name=name,
        origin=origin,
        sample_count=len(safe),
        fields=shape.infer_fields(safe),
        samples=safe[:MAX_STORED_SAMPLES],
        notes=all_notes,
    )

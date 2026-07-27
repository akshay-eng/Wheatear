"""Agent 1, the Inspector: find out what a platform's records actually look like.

It runs every probe source available for a platform and merges their answers
into one `SchemaCorpus`. Two passes, in this order and for this reason:

  **Structural first.** The export archive is free, offline, and authoritative
  about nesting and vocabulary. It establishes the skeleton.

  **Live second.** A live API fills in what the export strips -- tool schemas,
  connector endpoints, the fields a vendor considers server-side. It runs
  second so that a probe with no credentials degrades the corpus rather than
  emptying it.

Merging is where the care goes. Two sources describing the same entity kind
produce one schema, not two, because a mapping wants one answer to "what is an
Orchestrate tool" -- but a field only one source saw must not come out looking
mandatory. So a field is `required` only when every contributing source both
saw it and saw it in every record, and `occurrence` carries the real number
either way.

The other output is the gap list. "This platform has no triggers" and "we
could not see this platform's triggers" are different facts with different
consequences, and the inspector never collapses the second into the first.
"""

from __future__ import annotations

from wheatear.foundry.probes.base import MAX_STORED_SAMPLES, ProbeContext, ProbeSource
from wheatear.foundry.shape import schema_from_model
from wheatear.foundry.types import (
    EntityKind,
    EntitySchema,
    FieldNode,
    GapReason,
    ProbeGap,
    ProbeOrigin,
    SchemaCorpus,
)
from wheatear.ir.schema import (
    IR_SPEC_VERSION,
    Agent,
    ConnectionRef,
    KnowledgeRef,
    Topic,
    ToolRef,
    Workflow,
)

# Which IR model is the counterpart of each entity kind. This is the target
# vocabulary every import adapter maps onto, and the source vocabulary every
# export adapter maps from.
IR_MODELS: dict[EntityKind, type] = {
    EntityKind.AGENT: Agent,
    EntityKind.TOOL: ToolRef,
    EntityKind.KNOWLEDGE: KnowledgeRef,
    EntityKind.CONNECTION: ConnectionRef,
    EntityKind.TOPIC: Topic,
    EntityKind.WORKFLOW: Workflow,
}

# IR subtrees that are *composed*, not mapped: they are filled by other
# adapters' output or by a later pipeline stage, never by a field on the record
# being converted. An Orchestrate agent record does not contain its tools; it
# references them, and the tool adapter produces those separately.
#
# Without this the agent mapping reports forty "no equivalent" flags for fields
# that were never its job, which buries the handful that are real.
IR_COMPOSED: dict[EntityKind, tuple[str, ...]] = {
    EntityKind.AGENT: (
        "tools",
        "knowledge",
        "connections",
        "collaborators",
        "topics",
        # Guidelines are synthesised by the Translate stage from an agent's
        # guardrails, not carried from a source field.
        "guidelines",
    ),
    EntityKind.WORKFLOW: ("agents",),
    EntityKind.TOPIC: ("nodes",),
}

# Entity kinds a platform can have that the IR has no primitive for. Recorded
# as a gap on the IR corpus rather than left to be discovered as an empty
# mapping: a trigger has nowhere to land today, and that is a fact about
# Wheatear, not about the source platform.
IR_MISSING: dict[EntityKind, str] = {
    EntityKind.TRIGGER: (
        "The IR has no trigger primitive. Source triggers can be described in an agent's "
        "instructions but not reproduced as a scheduled or event-driven binding."
    ),
}

IR_PLATFORM = "wheatear-ir"


def composed_prefixes(entity_kind: EntityKind) -> tuple[str, ...]:
    """IR paths for `entity_kind` that no single record maps onto."""
    return IR_COMPOSED.get(entity_kind, ())


def default_probes(platform: str) -> list[ProbeSource]:
    """The probe sources for a platform, structural pass first.

    A platform with no live probe still gets the structural one, so an export
    from a platform Wheatear has never integrated with still produces a corpus.
    """
    from wheatear.foundry.probes.export_scan import ExportScan

    from wheatear.foundry.probes.write_model import DataverseWriteModel, OrchestrateWriteModel

    sources: list[ProbeSource] = [ExportScan()]
    if platform == "orchestrate":
        from wheatear.foundry.probes.orchestrate import OrchestrateProbe

        sources.append(OrchestrateProbe())
        sources.append(OrchestrateWriteModel())
    elif platform == "copilot-studio":
        from wheatear.foundry.probes.copilot_studio import DataverseProbe

        sources.append(DataverseProbe())
        sources.append(DataverseWriteModel())
    return sources


# ----------------------------------------------------------------------
# Merging
# ----------------------------------------------------------------------


def _merge_field(
    path: str, seen: list[tuple[FieldNode, int]], contributors: int, total_records: int
) -> FieldNode:
    """Combine one path's observations from several sources.

    `required` is the conservative reading: every source that could have seen
    this field must have seen it, in every record. A field the live API always
    returns and the export never mentions is optional, and an adapter that
    treated it as mandatory would reject every exported record.

    `occurrence` is weighted by how many records each source actually
    contributed, not averaged across sources. Averaging reports a field present
    in 33 of 35 records as "50%" purely because one of two sources lacked it,
    which is the sort of number that makes a reviewer distrust the whole
    corpus -- correctly, since it is wrong.
    """
    types: list[str] = []
    enum: list[str] = []
    examples: list[str] = []
    description = None
    for node, _ in seen:
        types.extend(t for t in node.types if t not in types)
        enum.extend(v for v in node.enum if v not in enum)
        examples.extend(e for e in node.examples if e not in examples)
        description = description or node.description

    # Writability: silence is not a denial. A field the GET response showed
    # and the write model never mentioned is unknown, not read-only -- so only
    # an explicit declaration counts, and any source saying "writable" wins.
    declarations = [node for node, _ in seen if node.writable is not None]
    writable = any(node.writable for node in declarations) if declarations else None

    weighted = sum(node.occurrence * count for node, count in seen)
    return FieldNode(
        path=path,
        types=sorted(types),
        # A declared write model states required-ness outright, and that is a
        # fact about the platform -- not something to be voted down by an
        # export archive that simply never mentioned the field. Only when
        # nothing declared does the conservative observed rule apply.
        required=(
            any(node.required for node in declarations)
            if declarations
            else len(seen) == contributors and all(node.required for node, _ in seen)
        ),
        occurrence=round(weighted / total_records, 4) if total_records else 0.0,
        enum=sorted(enum),
        examples=examples[:3],
        description=description,
        writable=writable,
        container=any(node.container for node, _ in seen),
    )


def _interleave(samples: list[list[dict]], limit: int) -> list[dict]:
    """Take samples round-robin across sources, up to `limit`.

    Round-robin rather than concatenation so that a source with 200 records
    cannot crowd out the one with 3 -- the small one is usually the live API,
    and its records are the richer ones.
    """
    merged: list[dict] = []
    index = 0
    while len(merged) < limit and any(index < len(group) for group in samples):
        for group in samples:
            if index < len(group):
                merged.append(group[index])
                if len(merged) >= limit:
                    break
        index += 1
    return merged


def merge_entities(entities: list[EntitySchema]) -> list[EntitySchema]:
    """Collapse entities of the same kind into one schema per kind."""
    by_kind: dict[EntityKind, list[EntitySchema]] = {}
    for entity in entities:
        by_kind.setdefault(entity.kind, []).append(entity)

    merged: list[EntitySchema] = []
    for kind, group in sorted(by_kind.items(), key=lambda item: item[0].value):
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Each observation carries the record count it came from, so occurrence
        # can be weighted rather than averaged across sources.
        by_path: dict[str, list[tuple[FieldNode, int]]] = {}
        for entity in group:
            for node in entity.fields:
                by_path.setdefault(node.path, []).append((node, entity.sample_count))
        total_records = sum(entity.sample_count for entity in group)

        primary = max(group, key=lambda e: e.sample_count)
        notes = [
            f"Merged {len(group)} source(s): "
            + ", ".join(f"{e.name} ({e.origin.value}, {e.sample_count} record(s))" for e in group)
        ]
        for entity in group:
            notes.extend(entity.notes)

        merged.append(
            EntitySchema(
                kind=kind,
                name=primary.name,
                origin=primary.origin,
                sample_count=sum(e.sample_count for e in group),
                fields=sorted(
                    (
                        _merge_field(path, seen, len(group), total_records)
                        for path, seen in by_path.items()
                    ),
                    key=lambda f: f.path,
                ),
                samples=_interleave([e.samples for e in group], MAX_STORED_SAMPLES),
                notes=notes,
            )
        )
    return merged


# ----------------------------------------------------------------------
# Inspection
# ----------------------------------------------------------------------


def inspect(
    context: ProbeContext,
    sources: list[ProbeSource] | None = None,
    platform_version: str | None = None,
) -> SchemaCorpus:
    """Probe a platform and return everything found, merged.

    A source that raises is recorded as a gap rather than allowed to sink the
    probe: with two passes and several endpoints, something being unreachable
    is the normal case, and a partial corpus is worth far more than none.
    """
    sources = sources if sources is not None else default_probes(context.platform)

    entities: list[EntitySchema] = []
    gaps: list[ProbeGap] = []
    notes: list[str] = []
    version = platform_version

    for source in sources:
        try:
            result = source.probe(context)
        except Exception as exc:  # noqa: BLE001 - a broken probe is a gap, not a crash
            gaps.append(
                ProbeGap(
                    what=f"probe source `{getattr(source, 'name', type(source).__name__)}`",
                    reason=GapReason.API_REFUSED,
                    detail=f"{type(exc).__name__}: {exc}",
                    remedy="Re-run the probe; if it persists this source needs a fix.",
                )
            )
            continue
        entities.extend(result.entities)
        gaps.extend(result.gaps)
        notes.extend(f"[{getattr(source, 'name', '?')}] {n}" for n in result.notes)
        version = version or result.platform_version

    from wheatear.foundry.conformance import declared_versions  # noqa: PLC0415 - avoids a cycle

    return SchemaCorpus(
        platform=context.platform,
        platform_version=version,
        declared_versions=declared_versions(context.platform),
        entities=merge_entities(entities),
        gaps=gaps,
        notes=notes,
    )


def ir_corpus() -> SchemaCorpus:
    """The IR's own shape, read off `ir/schema.py`.

    The hub side of every mapping. It is not probed -- it is a declared
    contract in this repository -- so deriving it from the pydantic models
    means the target vocabulary cannot drift from the models without the
    corpus fingerprint changing, which is exactly when cached adapters should
    stop being reused.
    """
    entities = [
        EntitySchema(
            kind=kind,
            name=model.__name__,
            origin=ProbeOrigin.MODEL,
            sample_count=0,
            fields=schema_from_model(model),
            notes=[" ".join((model.__doc__ or "").split())[:400]],
        )
        for kind, model in sorted(IR_MODELS.items(), key=lambda item: item[0].value)
    ]
    gaps = [
        ProbeGap(
            what=f"IR primitive for `{kind.value}`",
            reason=GapReason.UNSUPPORTED,
            detail=detail,
            remedy=f"Add a `{kind.value}` model to ir/schema.py to carry these across.",
        )
        for kind, detail in sorted(IR_MISSING.items(), key=lambda item: item[0].value)
    ]
    return SchemaCorpus(
        platform=IR_PLATFORM,
        platform_version=IR_SPEC_VERSION,
        entities=entities,
        gaps=gaps,
        notes=["Read from wheatear.ir.schema; not probed."],
    )

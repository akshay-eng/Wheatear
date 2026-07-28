"""Agent 2, the Translator: turn two probed schemas into one field mapping.

This is the only agent that reasons about meaning, and it is deliberately the
only one. Everything before it is observation and everything after it is
compilation; the judgement -- "Copilot's `gptCapabilities.webBrowsing` is the
IR's `web_search`" -- lives here, is written down as a `MappingSpec`, and is
reviewable as a single artifact.

It runs in two passes, the same shape as the Resolve stage:

  1. `align` settles every field whose correspondence is unambiguous. On two
     agent schemas that is most of them, and each one costs nothing and is
     reproducible.
  2. The model adjudicates the rest, seeing a shortlist of eight plausible
     source fields per unresolved target rather than the whole inventory.

Three rules keep it honest, and they are the difference between a mapping and
a plausible-looking one:

  * **A returned path that isn't in the inventory is discarded.** A model that
    invents `agent.displayName` produces code that reads a field no record has
    and writes null into ten thousand agents.
  * **Unmapped is a legitimate answer.** A field with no counterpart becomes a
    `ReviewFlag`, not a forced match. This is the "flag it and move on"
    contract: flags never block the build and never disappear either.
  * **Without a provider it still produces a spec.** The deterministic pass
    alone yields a real, usable mapping for the obvious fields, with everything
    ambiguous flagged. A migration that declines to use a model gets less
    coverage, not an error.

Only field *metadata* is sent to the model -- paths, types, enum values,
descriptions and truncated redacted examples. Whole records never leave the
machine here. That keeps a customer's agent content out of a model provider's
logs while giving up nothing the correlation actually needs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_liftoff.foundry import align as align_mod
from agent_liftoff.foundry import redact
from agent_liftoff.foundry.types import (
    Direction,
    EntityKind,
    EntitySchema,
    FieldMapping,
    FieldNode,
    FlagReason,
    MappingSpec,
    ReviewFlag,
    TransformKind,
)
from agent_liftoff.ir.schema import IR_SPEC_VERSION
from agent_liftoff.llm.base import LLMProvider

# Unresolved target fields per model call. Small enough that the model attends
# to each one; large enough that a 60-field agent schema is a handful of calls
# rather than sixty.
BATCH_SIZE = 12

# Ceiling on how many ambiguous fields one adapter will spend model calls on.
#
# Without it the cost of a build is set by how rich the target platform's read
# model happens to be, which is the wrong thing to be governed by: a live
# Orchestrate tool record has 592 fields, most of them server-generated
# response metadata (ids, timestamps, tenant ids) that no IR field could ever
# produce. Adjudicating all of them is ~50 model calls to answer "none" 500
# times.
#
# What is skipped is not lost -- it is flagged exactly as any other unmapped
# field, and the spec records how many were left. Raising the ceiling is a
# deliberate choice with a visible price, which is the right shape for this.
MAX_ADJUDICATED = 60

# Confidence assigned to a match the deterministic pass accepted. High, but
# not 1.0: the leaf names agreed and nothing else contradicted, which is strong
# evidence and not proof.
CERTAIN_CONFIDENCE = 0.9
CERTAIN_COERCE_CONFIDENCE = 0.8

# Examples shown per field. Enough to disambiguate a `status` from a `state`;
# few enough that the prompt stays about schema rather than data.
MAX_EXAMPLES = 2


class EnumPair(BaseModel):
    """One value-level translation, as a pair rather than a map entry.

    A `dict[str, str]` would be the obvious modelling, and it is unusable: an
    open-ended object becomes `additionalProperties` in JSON Schema, which the
    Gemini Developer API rejects outright ("additionalProperties is only
    supported in Gemini Enterprise Agent Platform mode"). Every response schema
    in the foundry therefore uses closed shapes, which cost one conversion here
    and work on every provider.
    """

    source_value: str
    target_value: str


class FieldDecision(BaseModel):
    """The model's verdict on one target field."""

    target_path: str = Field(description="Exact target path from the list, copied verbatim.")
    verdict: Literal["mapped", "none"] = Field(
        description=(
            "'mapped' = a source field (or fields) genuinely produces this. "
            "'none' = nothing in the candidate list does; the field has no counterpart."
        )
    )
    source_paths: list[str] = Field(
        default_factory=list,
        description="Exact source paths from the candidates, verbatim. Empty when verdict is 'none'.",
    )
    transform: TransformKind = Field(
        default=TransformKind.RENAME,
        description=(
            "copy/rename for a straight carry-over; enum_map when the two sides use "
            "different vocabularies; coerce for a type change; join for several sources "
            "into one; collect for array-of-item mapping; derive when real logic is needed."
        ),
    )
    enum_map: list[EnumPair] = Field(
        default_factory=list,
        description="Value translations. Required when transform is enum_map.",
    )
    constant: str | int | float | bool | None = Field(
        default=None, description="The fixed value, when transform is constant."
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", description="One sentence. Why this, or why nothing.")


class FieldDecisions(BaseModel):
    decisions: list[FieldDecision] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Prompting
# ----------------------------------------------------------------------


def _describe_field(node: FieldNode, with_examples: bool = True) -> str:
    parts = [f"  - {node.path}: {'|'.join(node.types) or 'unknown'}"]
    if node.required:
        parts.append(" (required)")
    elif node.occurrence and node.occurrence < 1.0:
        parts.append(f" (in {node.occurrence:.0%} of records)")
    if node.enum:
        parts.append(f" enum[{', '.join(node.enum[:8])}]")
    if node.description:
        parts.append(f" -- {' '.join(node.description.split())[:180]}")
    if with_examples and node.examples:
        shown = ", ".join(e[:60] for e in node.examples[:MAX_EXAMPLES])
        parts.append(f" e.g. {shown}")
    return "".join(parts)


def build_prompt(
    batch: list[align_mod.Alignment],
    source_name: str,
    target_name: str,
    entity_kind: EntityKind,
) -> str:
    """One batch of unresolved target fields, each with its shortlist."""
    blocks = []
    for alignment in batch:
        blocks.append(f"TARGET FIELD\n{_describe_field(alignment.target, with_examples=False)}")
        if alignment.candidates:
            blocks.append("  candidate source fields:")
            blocks.extend(
                f"  {_describe_field(candidate.field)}" for candidate in alignment.candidates
            )
        else:
            blocks.append("  candidate source fields: (none ranked; answer 'none' unless you "
                          "recognise a correspondence the ranking missed)")
        blocks.append("")

    return f"""You are mapping the fields of one AI-agent platform's `{entity_kind.value}` records
onto another's, so that a deterministic converter can be generated from your answer.

SOURCE SHAPE: {source_name}
TARGET SHAPE: {target_name}

For each TARGET FIELD below, decide which source field (if any) produces it.

{chr(10).join(blocks)}
Rules:
- target_path and every source_path MUST be copied verbatim from the lists above.
  Never invent a path. A path that does not appear above will be discarded.
- 'none' is a correct and expected answer. Many fields have no counterpart across
  platforms, and a wrong mapping is far more costly than a missing one: it compiles
  into code that runs over every record in the migration.
- Use enum_map when both sides have a closed vocabulary but different values, and
  give the full mapping. Values you cannot map should simply be left out of it.
- Use derive only when the target genuinely needs computation from the source
  (concatenation of several fields, parsing, a conditional). Say what, in rationale.
- Judge on meaning, not on name similarity. Two fields called `type` on different
  platforms usually mean different things.
"""


# ----------------------------------------------------------------------
# Translating
# ----------------------------------------------------------------------


def _deterministic_mapping(alignment: align_mod.Alignment) -> FieldMapping:
    confidence = (
        CERTAIN_COERCE_CONFIDENCE
        if alignment.transform is TransformKind.COERCE
        else CERTAIN_CONFIDENCE
    )
    assert alignment.source is not None  # guaranteed by Alignment.resolved
    return FieldMapping(
        target_path=alignment.target.path,
        source_paths=[alignment.source.path],
        transform=alignment.transform,
        required=alignment.target.required,
        confidence=confidence,
        rationale=(
            f"Field names agree ({alignment.source.path} -> {alignment.target.path}) with "
            f"compatible types and no competing candidate."
        ),
    )


# Transforms whose renderer reads exactly one source path. A decision naming
# several of them is a decision that would lose all but the first.
_SINGLE_SOURCE = frozenset(
    {
        TransformKind.COPY,
        TransformKind.RENAME,
        TransformKind.COERCE,
        TransformKind.ENUM_MAP,
    }
)


def _apply_decision(
    decision: FieldDecision,
    alignment: align_mod.Alignment,
    source_paths: set[str],
) -> tuple[FieldMapping | None, str | None]:
    """Validate one decision and turn it into a mapping.

    Returns (mapping, complaint). A complaint is recorded in the spec's notes
    so that a discarded hallucination is visible rather than silent -- if a
    provider starts inventing paths, the spec says so.
    """
    if decision.verdict == "none":
        return None, None

    if decision.transform is TransformKind.CONSTANT:
        # A constant is the one transform with no source: `source_platform` is
        # "copilot-studio" for every record this adapter will ever see. Treating
        # an empty source list as "no match" would silently drop exactly the
        # fields the IR marks required and no source record carries.
        if decision.constant is None:
            return None, (
                f"Discarded a mapping for `{decision.target_path}`: it asked for a constant "
                "but supplied no value."
            )
    elif not decision.source_paths:
        return None, (
            f"Discarded a mapping for `{decision.target_path}`: it claims a match but names "
            "no source field."
        )

    unknown = [p for p in decision.source_paths if p not in source_paths]
    if unknown:
        return None, (
            f"Discarded a mapping for `{decision.target_path}`: it referenced source "
            f"path(s) {', '.join(unknown)}, which are not in the schema."
        )

    if decision.transform is not TransformKind.DERIVE:
        if "[]" not in decision.target_path:
            plural = [p for p in decision.source_paths if "[]" in p]
            if plural:
                return None, (
                    f"Discarded a mapping for `{decision.target_path}`: {', '.join(plural)} "
                    "maps over an array and yields a list, which cannot land on a scalar "
                    "field. Collapsing several values into one needs a derive."
                )
        else:
            singular = [p for p in decision.source_paths if "[]" not in p]
            if singular:
                return None, (
                    f"Discarded a mapping for `{decision.target_path}`: {', '.join(singular)} "
                    "is a single value being broadcast to every element of an array. "
                    "Repeating one value across a list needs a derive."
                )

    if decision.transform is TransformKind.ENUM_MAP and not decision.enum_map:
        return None, (
            f"Discarded a mapping for `{decision.target_path}`: it asked for an enum "
            "translation but supplied no value mapping."
        )

    transform = decision.transform
    if len(decision.source_paths) > 1 and transform in _SINGLE_SOURCE:
        # The model named several sources for a transform that reads one. The
        # renderer would silently use the first and discard the rest, which is
        # the worst of the three available answers -- it looks mapped, and it
        # is absent for every record that carries the field somewhere else.
        # Coalescing is what the model almost always meant.
        transform = TransformKind.COALESCE

    return (
        FieldMapping(
            target_path=decision.target_path,
            source_paths=list(decision.source_paths),
            transform=transform,
            constant=decision.constant,
            enum_map={pair.source_value: pair.target_value for pair in decision.enum_map},
            required=alignment.target.required,
            confidence=decision.confidence,
            rationale=decision.rationale.strip(),
        ),
        None,
    )


def _adjudicate(
    batch: list[align_mod.Alignment],
    source_entity: EntitySchema,
    target_entity: EntitySchema,
    entity_kind: EntityKind,
    provider: LLMProvider,
    source_paths: set[str],
) -> tuple[list[FieldMapping], list[str]]:
    prompt = build_prompt(batch, source_entity.name, target_entity.name, entity_kind)
    try:
        answer = provider.generate_structured(prompt, FieldDecisions)
    except Exception as exc:  # noqa: BLE001 - a provider failure degrades, never sinks
        return [], [
            f"The model could not adjudicate {len(batch)} field(s) "
            f"({type(exc).__name__}); they are unmapped and flagged."
        ]

    by_target = {a.target.path: a for a in batch}
    mappings: list[FieldMapping] = []
    notes: list[str] = []
    for decision in answer.decisions:
        alignment = by_target.get(decision.target_path)
        if alignment is None:
            notes.append(
                f"Discarded a decision for `{decision.target_path}`: not a target field in "
                "this batch."
            )
            continue
        mapping, complaint = _apply_decision(decision, alignment, source_paths)
        if complaint:
            notes.append(complaint)
        elif mapping is not None:
            mappings.append(mapping)
    return mappings, notes


def _flags(
    source_entity: EntitySchema,
    target_entity: EntitySchema,
    mappings: list[FieldMapping],
    alignments: list[align_mod.Alignment],
) -> list[ReviewFlag]:
    """Everything the mapping could not carry, stated explicitly."""
    mapped_targets = {m.target_path for m in mappings}
    mapped_sources = {path for m in mappings for path in m.source_paths}
    flags: list[ReviewFlag] = []

    for alignment in alignments:
        target = alignment.target
        if target.path in mapped_targets:
            continue
        # A required target with no source is the serious one: the record the
        # adapter produces will be structurally incomplete. It does not stop
        # the build -- flag and move on -- but it is marked as blocking so a
        # reviewer cannot miss it.
        flags.append(
            ReviewFlag(
                path=target.path,
                reason=FlagReason.NO_TARGET_EQUIVALENT,
                detail=(
                    f"No source field produces `{target.path}`; it will be left at its "
                    "default."
                ),
                severity="block" if target.required else "info",
            )
        )

    for node in source_entity.leaves():
        if node.path in mapped_sources:
            continue
        if redact.is_secret_key(node.leaf_name):
            # A credential field is not a mapping failure -- Agent Liftoff never
            # carries secrets across platforms by design -- but the connection
            # it belongs to still has to be configured by hand on the target.
            flags.append(
                ReviewFlag(
                    path=node.path,
                    reason=FlagReason.REQUIRES_AUTH,
                    detail=(
                        f"`{node.path}` holds credential material. It is never migrated; "
                        "the equivalent connection must be configured on the target."
                    ),
                    severity="warn",
                )
            )
            continue
        flags.append(
            ReviewFlag(
                path=node.path,
                reason=FlagReason.LOSSY,
                detail=f"`{node.path}` has no counterpart on the target and is dropped.",
                severity="warn" if node.required else "info",
            )
        )

    for mapping in mappings:
        if mapping.transform is TransformKind.DERIVE:
            flags.append(
                ReviewFlag(
                    path=mapping.target_path,
                    reason=FlagReason.SEMANTIC_AMBIGUITY,
                    detail=(
                        f"`{mapping.target_path}` is derived rather than carried over: "
                        f"{mapping.rationale or 'no rationale given'}"
                    ),
                    severity="warn",
                )
            )
    return flags


def _self_evident(
    platform: str, direction: Direction, target_fields: list[FieldNode]
) -> list[FieldMapping]:
    """Target fields the adapter can fill from what it *is*, not what it reads.

    Two of them, and they are the two that were failing every import: the IR
    records which platform a record came from, and which version of the IR it
    was written against. Neither is a property of the record -- no Copilot
    Studio bot has a field saying "I am a Copilot Studio bot" -- so no ranking
    and no model will ever find a source for them, and both are required.

    Left to the aligner these become "no counterpart" flags on fields that have
    exactly one possible correct value, which is a bad trade twice: the field
    is wrong *and* the flag is noise in the list a reviewer reads.
    """
    if direction is not Direction.IMPORT:
        return []
    constants: dict[str, str] = {"source_platform": platform, "spec_version": IR_SPEC_VERSION}
    present = {node.path for node in target_fields}
    return [
        FieldMapping(
            target_path=path,
            source_paths=[],
            transform=TransformKind.CONSTANT,
            constant=value,
            required=True,
            confidence=1.0,
            rationale=f"Every record this adapter converts came from {platform}."
            if path == "source_platform"
            else f"The IR revision this adapter was compiled against ({value}).",
        )
        for path, value in constants.items()
        if path in present
    ]


def writable_only(nodes: list[FieldNode]) -> tuple[list[FieldNode], int]:
    """Restrict an export target to fields the platform accepts on create.

    Applied only when a declared write model actually contributed -- i.e. some
    field says `writable=True`. Without that, silence means nobody asked, and
    filtering on it would empty the target vocabulary.

    This is what stops an export adapter proposing `created_on`, `tenant_id`
    and `id`: it is not that those fields look server-generated, it is that the
    platform's own metadata says a create rejects them.
    """
    if not any(node.writable for node in nodes):
        return nodes, 0
    kept = [node for node in nodes if node.writable is not False]
    return kept, len(nodes) - len(kept)


def _outside(prefixes: tuple[str, ...], nodes: list[FieldNode]) -> list[FieldNode]:
    """Fields not under any composed prefix."""
    if not prefixes:
        return nodes
    return [
        node
        for node in nodes
        if node.path.split(".", 1)[0].rstrip("[]") not in prefixes
    ]


def translate(
    source_entity: EntitySchema,
    target_entity: EntitySchema,
    platform: str,
    direction: Direction,
    schema_fingerprint: str,
    provider: LLMProvider | None = None,
    batch_size: int = BATCH_SIZE,
    skip_source_prefixes: tuple[str, ...] = (),
    skip_target_prefixes: tuple[str, ...] = (),
) -> MappingSpec:
    """Correlate two entity schemas into a reviewable field mapping.

    `platform` is always the non-IR side, whichever direction this runs in --
    an adapter belongs to a platform, and the IR is the hub both directions
    meet at.

    The skip prefixes exclude subtrees that are composed rather than mapped
    (see `inspector.IR_COMPOSED`). Excluding them is not the same as failing to
    map them: an agent's `tools[]` is produced by the *tool* adapter, so
    flagging it here would bury the flags that describe real losses under
    dozens that describe the architecture.
    """
    entity_kind = target_entity.kind if direction is Direction.EXPORT else source_entity.kind
    source_fields = _outside(skip_source_prefixes, source_entity.fields)
    target_fields = _outside(skip_target_prefixes, target_entity.fields)
    source_entity = source_entity.model_copy(update={"fields": source_fields})

    read_only_dropped = 0
    if direction is Direction.EXPORT:
        target_fields, read_only_dropped = writable_only(target_fields)

    alignments = align_mod.align(target_fields, source_fields)
    source_paths = {node.path for node in source_fields}

    mappings: list[FieldMapping] = []
    unresolved: list[align_mod.Alignment] = []
    notes: list[str] = []

    seeded = _self_evident(platform, direction, target_fields)
    known = {mapping.target_path for mapping in seeded}
    mappings.extend(seeded)
    if seeded:
        notes.append(
            "Seeded "
            + ", ".join(f"`{m.target_path}` = {m.constant!r}" for m in seeded)
            + ": the adapter knows these without reading a record."
        )

    for alignment in alignments:
        if alignment.target.path in known:
            continue
        if alignment.resolved and alignment.transform is not TransformKind.ENUM_MAP:
            mappings.append(_deterministic_mapping(alignment))
        else:
            unresolved.append(alignment)

    notes.append(
        f"Deterministic alignment settled {len(mappings) - len(seeded)} of "
        f"{len(alignments) - len(seeded)} target field(s)."
    )

    # A target the deterministic ranking found no plausible source for at all
    # is not an ambiguity a model can settle -- there is nothing to choose
    # between. Asking anyway is a call that can only return "none".
    worth_asking = [a for a in unresolved if a.candidates]
    skipped_empty = len(unresolved) - len(worth_asking)
    if skipped_empty:
        notes.append(
            f"{skipped_empty} target field(s) had no candidate source at all and were "
            "flagged without a model call."
        )

    # Required fields first, then the ones that actually appear in records:
    # if there is a budget, it should be spent on the fields whose absence
    # would make the output invalid.
    worth_asking.sort(key=lambda a: (not a.target.required, -a.target.occurrence, a.target.path))
    if len(worth_asking) > MAX_ADJUDICATED:
        notes.append(
            f"{len(worth_asking) - MAX_ADJUDICATED} ambiguous field(s) were left unmapped: "
            f"the per-adapter adjudication ceiling is {MAX_ADJUDICATED}. They are flagged "
            "like any other unmapped field; raise MAX_ADJUDICATED to spend more."
        )
        worth_asking = worth_asking[:MAX_ADJUDICATED]

    generator = "deterministic"
    if provider is not None and worth_asking:
        generator = type(provider).__name__
        for start in range(0, len(worth_asking), batch_size):
            batch = worth_asking[start : start + batch_size]
            decided, complaints = _adjudicate(
                batch, source_entity, target_entity, entity_kind, provider, source_paths
            )
            mappings.extend(decided)
            notes.extend(complaints)
        notes.append(f"The model adjudicated {len(worth_asking)} remaining field(s).")
    elif worth_asking:
        notes.append(
            f"No model provider: {len(worth_asking)} ambiguous field(s) were left unmapped "
            "and flagged rather than guessed."
        )

    if read_only_dropped:
        notes.append(
            f"{read_only_dropped} target field(s) were excluded: the platform's own metadata "
            "says a create does not accept them (server-generated ids, timestamps, ownership)."
        )
    for prefix in skip_target_prefixes + skip_source_prefixes:
        notes.append(
            f"`{prefix}` was excluded: it is composed from other entity kinds' adapters, "
            "not mapped from a field on this record."
        )

    mappings.sort(key=lambda m: m.target_path)
    return MappingSpec(
        platform=platform,
        direction=direction,
        entity_kind=entity_kind,
        schema_fingerprint=schema_fingerprint,
        mappings=mappings,
        flags=_flags(source_entity, target_entity, mappings, alignments),
        notes=notes,
        generator=generator,
    )

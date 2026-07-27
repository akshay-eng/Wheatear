"""The typed contracts the four foundry agents hand each other.

Every boundary in the foundry is a pydantic model, for the same reason the IR
is: these objects get written to a cache, read back weeks later by a different
version of the code, and -- in the case of `MappingSpec` -- produced directly
by a language model. A dict would let all three of those fail silently.

The chain is linear and each link is independently inspectable on disk:

    SchemaCorpus  (inspector)  what a platform's records actually look like
        |
    MappingSpec   (translator) how its fields correspond to the IR's
        |
    TestCase[]    (cases)      what the compiled mapping must do
        |
    AdapterArtifact (engineer) the code, its tests, and the sandbox report
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from wheatear.ir.schema import IR_SPEC_VERSION

FOUNDRY_SPEC_VERSION = "wheatear.foundry/v1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Direction(str, Enum):
    """Which way an adapter runs.

    There is no "source platform to target platform" direction on purpose:
    every corridor is a pair of adapters meeting at the IR, so a platform's
    IMPORT adapter is reused by every corridor that starts there.
    """

    IMPORT = "import"  # platform record -> IR
    EXPORT = "export"  # IR -> platform record


class EntityKind(str, Enum):
    """The migratable primitives. Both platforms have all of these under
    different names -- a Copilot "topic" and an Orchestrate "guideline" are
    both conversational routing -- and the mapping is inferred per kind,
    because a tool schema has nothing to say about an agent schema.
    """

    AGENT = "agent"
    TOOL = "tool"
    WORKFLOW = "workflow"
    TRIGGER = "trigger"
    TOPIC = "topic"
    KNOWLEDGE = "knowledge"
    CONNECTION = "connection"


class ProbeOrigin(str, Enum):
    """Where a schema observation came from. Kept per-entity because it
    determines how much to trust it: an export archive is authoritative about
    structure and silent about endpoints, while a live API is authoritative
    about both and only shows you what your credentials can see.
    """

    EXPORT = "export"  # parsed out of a downloaded export archive
    API = "api"  # a live, token-authenticated platform API
    SESSION = "session"  # a live endpoint reachable only with a browser session
    MODEL = "model"  # read off Wheatear's own pydantic IR definitions
    # The platform's own declaration of its write model -- Dataverse
    # attribute metadata, an SDK's request models, an OpenAPI schema.
    # Authoritative in a way an observed GET response is not: it says what
    # a *create* accepts, which is what a migration actually has to send.
    SCHEMA = "schema"
    FIXTURE = "fixture"  # a checked-in sample, used when nothing live is available


class GapReason(str, Enum):
    NO_CREDENTIALS = "no_credentials"
    NOT_IN_EXPORT = "not_in_export"
    REQUIRES_SESSION = "requires_session"
    API_REFUSED = "api_refused"
    UNSUPPORTED = "unsupported"


class FieldNode(BaseModel):
    """One field in a platform's record shape, addressed by a dotted path.

    Flat with dotted paths rather than a nested tree, because both consumers
    want it flat: the correlation step compares two path inventories, and
    generated code walks a path to read or write a value. `[]` marks an array
    level, so `topics[].triggers[].phrase` is one field however deep it sits.
    """

    path: str
    # Every JSON type observed at this path across the samples, not just the
    # first. Real exports are heterogeneous -- a field that is a string in
    # 900 records and null in 100 is a fact the generated code has to handle,
    # and collapsing it to "string" is how you get a crash on record 901.
    types: list[str] = Field(default_factory=list)
    # Present in every sample. Genuinely different from "the platform's schema
    # marks it required" -- this is observed, so it is only as good as the
    # sample -- which is why `occurrence` is carried alongside it.
    required: bool = False
    occurrence: float = 0.0
    # Populated only when the observed values form a small closed set, which
    # is the signal that a field is an enum rather than free text. An enum is
    # the one thing that reliably needs a value-level translation between
    # platforms, not just a rename.
    enum: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    description: str | None = None
    # Whether the platform accepts this field on create. None means nobody
    # said -- an observed GET response cannot tell you. False is a positive
    # declaration that a create will reject it, which is what keeps an export
    # adapter from mapping onto `created_on` and `tenant_id`.
    writable: bool | None = None
    # True for the object/array nodes on the way to a leaf. Carried so the
    # correlation step can ignore containers, which correspond structurally
    # and carry no data.
    container: bool = False

    @property
    def leaf_name(self) -> str:
        """The last path segment, array markers stripped."""
        return self.path.replace("[]", "").rsplit(".", 1)[-1]


class EntitySchema(BaseModel):
    """Everything known about one entity kind on one platform."""

    kind: EntityKind
    # The platform's own name for this thing: "botcomponent", "agent.yaml",
    # "toolkit". Kept verbatim because it is what the user will recognise, and
    # what an error message has to say to be actionable.
    name: str
    origin: ProbeOrigin
    sample_count: int = 0
    fields: list[FieldNode] = Field(default_factory=list)
    # Redacted, capped sample records. These are the positive test cases the
    # compiled adapter is judged against, so a corpus with no samples can
    # still produce a mapping but not a trustworthy one.
    samples: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def paths(self) -> set[str]:
        return {f.path for f in self.fields}

    def leaves(self) -> list[FieldNode]:
        return [f for f in self.fields if not f.container]

    def field(self, path: str) -> FieldNode | None:
        return next((f for f in self.fields if f.path == path), None)


class ProbeGap(BaseModel):
    """Something the inspector could not reach, recorded rather than guessed.

    A gap is not a failure. It is the difference between "this platform has no
    triggers" and "we could not see this platform's triggers", and conflating
    those two produces an adapter that silently drops every trigger.
    """

    what: str
    reason: GapReason
    detail: str = ""
    # What the user could do to close it, when there is something. This is the
    # text that ends up in front of a human, so it names the concrete action
    # ("paste a console session cookie"), not the abstract problem.
    remedy: str | None = None


class SchemaCorpus(BaseModel):
    """One platform's complete probed shape: the inspector's output.

    `fingerprint()` is what makes the whole cache work, so what goes into it
    matters. It covers structure -- entity kinds, field paths, types,
    required-ness, enum values -- and deliberately excludes samples, capture
    time, tenant, and gap list. Two tenants on the same platform version
    therefore fingerprint identically and share a compiled adapter, while a
    platform that adds a field does not.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: str = FOUNDRY_SPEC_VERSION
    platform: str
    platform_version: str | None = None
    # The versions of the *declared* contracts this build was made against --
    # the ADK for Orchestrate, the Dataverse API revision for Copilot Studio,
    # the IR for both. A build is meant to serve every customer, so it has to
    # carry the claim it is making about what it describes; `conformance.check`
    # is what tests that claim on the machine actually running the migration.
    declared_versions: dict[str, str] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=_utcnow)
    entities: list[EntitySchema] = Field(default_factory=list)
    gaps: list[ProbeGap] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def entity(self, kind: EntityKind) -> EntitySchema | None:
        return next((e for e in self.entities if e.kind == kind), None)

    def kinds(self) -> list[EntityKind]:
        return [e.kind for e in self.entities]

    def structure(self) -> dict:
        """The canonical projection the fingerprint is taken over.

        Sorted at every level so that dict ordering, probe ordering and sample
        ordering can never change the hash -- an unstable fingerprint would
        silently disable the cache instead of failing loudly.
        """
        return {
            "spec": self.spec_version,
            "platform": self.platform,
            "version": self.platform_version,
            "entities": sorted(
                (
                    {
                        "kind": entity.kind.value,
                        "name": entity.name,
                        "fields": sorted(
                            (
                                {
                                    "path": field.path,
                                    "types": sorted(field.types),
                                    "required": field.required,
                                    "enum": sorted(field.enum),
                                }
                                for field in entity.fields
                            ),
                            key=lambda f: f["path"],
                        ),
                    }
                    for entity in self.entities
                ),
                key=lambda e: (e["kind"], e["name"]),
            ),
        }

    def fingerprint(self) -> str:
        blob = json.dumps(self.structure(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def entity_fingerprint(self, kind: EntityKind) -> str:
        """Fingerprint of one entity's shape alone.

        Adapters are compiled per entity kind, so keying them on the whole
        corpus would throw away every tool adapter the day a platform added a
        field to its agent schema.
        """
        entity = self.entity(kind)
        if entity is None:
            return hashlib.sha256(f"{self.platform}:{kind.value}:absent".encode()).hexdigest()
        structure = next(
            e for e in self.structure()["entities"] if e["kind"] == kind.value and e["name"] == entity.name
        )
        blob = json.dumps(
            {"platform": self.platform, "version": self.platform_version, "entity": structure},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()


class TransformKind(str, Enum):
    """How one target field is produced from the source record.

    A closed set on purpose. Anything the translator cannot express as one of
    these becomes `DERIVE`, which is the explicit "this needs real code"
    escape hatch -- and because it is explicit, a reviewer can find every one
    of them instead of discovering them in a diff.
    """

    COPY = "copy"  # same path, same value
    RENAME = "rename"  # different path, same value
    CONSTANT = "constant"  # target always gets a fixed value
    ENUM_MAP = "enum_map"  # value-level translation via `enum_map`
    COERCE = "coerce"  # same meaning, different type (str <-> int, scalar -> list)
    JOIN = "join"  # several source paths concatenated into one target
    COALESCE = "coalesce"  # several source paths, first one present wins
    SPLIT = "split"  # one source path parsed into a structured target
    COLLECT = "collect"  # array of source items -> array of target items
    DERIVE = "derive"  # needs logic; `rationale` states what, the Engineer writes it


class FlagReason(str, Enum):
    NO_TARGET_EQUIVALENT = "no_target_equivalent"
    REQUIRES_AUTH = "requires_auth"
    REQUIRES_MANUAL_SETUP = "requires_manual_setup"
    SEMANTIC_AMBIGUITY = "semantic_ambiguity"
    PLATFORM_SPECIFIC = "platform_specific"
    LOSSY = "lossy"


class ReviewFlag(BaseModel):
    """A field the translator could not carry across, surfaced instead of dropped.

    This is the "flag them and move on" contract: a flag never stops the build,
    and it never silently disappears either -- it rides in the MappingSpec, is
    re-emitted by the compiled adapter at runtime, and lands in the review
    manifest a human reads.
    """

    path: str
    reason: FlagReason
    detail: str = ""
    severity: str = "warn"  # info | warn | block


class FieldMapping(BaseModel):
    """One target field and how to produce it.

    Keyed by target rather than source because that is the direction code runs
    in: the adapter builds a target record field by field, and two source
    fields feeding one target (JOIN) is common while the reverse is rare.
    """

    target_path: str
    source_paths: list[str] = Field(default_factory=list)
    transform: TransformKind = TransformKind.COPY
    constant: Any = None
    enum_map: dict[str, str] = Field(default_factory=dict)
    default: Any = None
    required: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""

    @property
    def source_path(self) -> str | None:
        return self.source_paths[0] if self.source_paths else None


class MappingSpec(BaseModel):
    """The translator's output: a complete, reviewable field mapping.

    This is the artifact worth reading. The generated code is derived from it
    and can be regenerated at will; the spec is the decision.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: str = FOUNDRY_SPEC_VERSION
    platform: str
    direction: Direction
    entity_kind: EntityKind
    # The corpus shape this was inferred from. If the platform's schema drifts,
    # this stops matching and the stored adapter is correctly treated as stale.
    schema_fingerprint: str
    ir_version: str = IR_SPEC_VERSION
    mappings: list[FieldMapping] = Field(default_factory=list)
    flags: list[ReviewFlag] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # Name of the model that produced this, or "deterministic" when the
    # alignment pass alone was enough. Recorded so a spec can be re-judged
    # later against what produced it.
    generator: str = "deterministic"

    def key(self) -> AdapterKey:
        return AdapterKey(
            platform=self.platform,
            direction=self.direction,
            entity_kind=self.entity_kind,
            schema_fingerprint=self.schema_fingerprint,
            ir_version=self.ir_version,
        )

    def required_targets(self) -> list[str]:
        return [m.target_path for m in self.mappings if m.required]

    def blocking_flags(self) -> list[ReviewFlag]:
        return [f for f in self.flags if f.severity == "block"]


class AdapterKey(BaseModel):
    """What a compiled adapter is cached under.

    Frozen and hashable so it can key a dict; `slug()` is its path on disk.
    The fingerprint and the IR version are both in the key because an adapter
    is only valid for the pair of shapes it was compiled against -- changing
    either side has to miss the cache, not quietly reuse stale code.
    """

    model_config = ConfigDict(frozen=True)

    platform: str
    direction: Direction
    entity_kind: EntityKind
    schema_fingerprint: str
    ir_version: str = IR_SPEC_VERSION

    def slug(self) -> str:
        return "/".join(
            (
                self.platform,
                self.direction.value,
                self.entity_kind.value,
                self.schema_fingerprint[:16],
            )
        )

    def family(self) -> str:
        """The key without the fingerprint: identifies every generation of an
        adapter for this platform/direction/entity, across schema versions.
        """
        return "/".join((self.platform, self.direction.value, self.entity_kind.value))


class CaseKind(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    EDGE = "edge"


class TestCase(BaseModel):
    """One case the compiled adapter has to satisfy.

    Every case, of every kind, asserts the adapter does not raise. That is not
    a stylistic preference: an adapter runs unattended over ten thousand
    records, and one that throws on a malformed record halts the batch instead
    of flagging the record and continuing.
    """

    name: str
    kind: CaseKind
    # Deliberately `Any`, not `dict`: "what happens when this is handed a list,
    # or None, or a string" is one of the cases worth having, and a type that
    # made it unrepresentable would quietly delete the test.
    record: Any = Field(default_factory=dict)
    # Target path -> expected value. A subset assertion, not an equality
    # assertion on the whole output: a case about `name` should not fail
    # because an unrelated field also got populated.
    expect_paths: dict[str, Any] = Field(default_factory=dict)
    # Target paths that must be absent or None. This is how "do not invent a
    # value for a field the source did not have" is actually enforced.
    expect_absent: list[str] = Field(default_factory=list)
    # Weaker assertions, for cases where the value can't be predicted from the
    # spec alone but its shape can -- a collected array whose element mapping
    # is derived still has to come out the same length as its source.
    expect_types: dict[str, str] = Field(default_factory=dict)
    expect_lengths: dict[str, int] = Field(default_factory=dict)
    rationale: str = ""


class CaseFailure(BaseModel):
    name: str
    message: str


class SandboxResult(BaseModel):
    """What came back from running the tests in isolation."""

    ok: bool = False
    runner: str = ""
    exit_code: int = -1
    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_s: float = 0.0
    failures: list[CaseFailure] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"{self.passed} passed in {self.duration_s:.2f}s ({self.runner})"
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.errors:
            parts.append(f"{self.errors} errored")
        return ", ".join(parts) + f" ({self.runner})"

    def feedback(self, limit: int = 12) -> str:
        """The failure text handed back to the model for a repair attempt.

        Trimmed hard: a full pytest-style dump is mostly noise, and a model
        given 200 lines of traceback fixates on the last one rather than the
        pattern across them.
        """
        if self.failures:
            lines = [f"- {f.name}: {' '.join(f.message.split())[:400]}" for f in self.failures[:limit]]
            return "\n".join(lines)
        tail = (self.stderr or self.stdout or "").strip().splitlines()[-limit:]
        return "\n".join(tail) or "The test run produced no output."


class AdapterArtifact(BaseModel):
    """A compiled, tested adapter -- the thing the cache stores and the thing
    a migration actually executes.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: str = FOUNDRY_SPEC_VERSION
    key: AdapterKey
    code: str
    tests: str
    spec: MappingSpec
    report: SandboxResult
    attempts: int = 1
    generator: str = "deterministic"
    created_at: datetime = Field(default_factory=_utcnow)

    @property
    def verified(self) -> bool:
        """True only if the tests actually ran and passed somewhere isolated.

        A stored artifact whose tests never ran is still useful to a human, so
        it is not thrown away -- but nothing should execute it over ten
        thousand records without saying so first.
        """
        return self.report.ok and self.report.passed > 0

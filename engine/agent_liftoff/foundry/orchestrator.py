"""The Orchestrator: owns the cache, and decides whether anything else runs.

Every other agent in the foundry is expensive -- the Inspector costs API calls
against a customer's tenant, the Translator costs model calls, the Engineer
costs container runs. This one is cheap and runs first, and its whole job is to
find out that none of them are needed.

    ensure_adapter(...)
        |
        +-- store.find(platform, direction, entity, fingerprint)
        |       hit   -> return it. No probe, no model, no container.
        |       stale -> the schema moved; say so, rebuild from the old spec.
        |       miss  -> Translator -> Engineer -> store.put
        |
        +-- migrate(records) -> runtime.convert_all

The fingerprint is what makes the hit real rather than optimistic. It is
computed from the *shape* the inspector found, not from a tenant or a
timestamp, so the second customer on the same platform version hits the cache
the first one filled -- and a vendor who adds a field misses it, correctly,
instead of running last quarter's mapping over this quarter's records.

A corridor is two adapters meeting at the IR, never one. `corridor()` builds
both halves and reports them together, which is also why migrating a new
platform *to* Orchestrate only costs the import half: the export half already
exists from every corridor that ended there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from agent_liftoff.foundry import inspector, runtime, translator
from agent_liftoff.foundry.engineer import build_adapter
from agent_liftoff.foundry.probes.base import ProbeContext
from agent_liftoff.foundry.sandbox import Sandbox, default_sandbox
from agent_liftoff.foundry.store import AdapterLookup, FoundryStore
from agent_liftoff.foundry.types import (
    AdapterArtifact,
    Direction,
    EntityKind,
    SchemaCorpus,
)
from agent_liftoff.llm.base import LLMProvider

# Entity kinds a corridor builds adapters for, in the order a migration needs
# them: tools and knowledge before the agents that reference them.
DEFAULT_KINDS: tuple[EntityKind, ...] = (
    EntityKind.TOOL,
    EntityKind.KNOWLEDGE,
    EntityKind.CONNECTION,
    EntityKind.TOPIC,
    EntityKind.AGENT,
)

# Kinds that are *found* on the target rather than *constructed* from source
# fields, so an export adapter for them would be mapping into a shape nothing
# will ever be created from.
#
# An Orchestrate tool comes from the catalog, an MCP server, or an OpenAPI
# import -- never from a Copilot connector's `data.inputs[].propertyName`. Its
# migration is a lookup (`pipeline/resolve.py`, against the instance and the
# catalog snapshot), and what the *import* adapter contributes is the search
# query: the ref, operation id, description and parameter names that lookup
# ranks on. A knowledge base is the same shape of thing -- you hand Orchestrate
# the documents and it indexes them; there is no field mapping to write.
#
# So the import half is built and the export half is not. Building it produced
# an adapter with 138 mappings, 715 review flags and no consumer.
LOOKUP_RESOLVED: frozenset[EntityKind] = frozenset(
    {EntityKind.TOOL, EntityKind.KNOWLEDGE, EntityKind.CONNECTION}
)

Origin = Literal["cache", "built", "rebuilt"]


@dataclass
class AdapterResult:
    """One adapter, and how we came to have it."""

    artifact: AdapterArtifact | None
    origin: Origin | Literal["unavailable"]
    lookup: AdapterLookup
    reason: str = ""

    @property
    def from_cache(self) -> bool:
        return self.origin == "cache"

    @property
    def usable(self) -> bool:
        return self.artifact is not None and self.artifact.verified

    def describe(self) -> str:
        if self.artifact is None:
            return f"unavailable: {self.reason}"
        key = self.artifact.key
        return (
            f"{key.family()} [{self.origin}] {self.artifact.report.summary()}, "
            f"{len(self.artifact.spec.mappings)} mapping(s), "
            f"{len(self.artifact.spec.flags)} flag(s)"
        )


@dataclass
class CorridorResult:
    """Both halves of a corridor, plus what neither half could carry."""

    source_platform: str
    target_platform: str
    imports: dict[EntityKind, AdapterResult] = field(default_factory=dict)
    exports: dict[EntityKind, AdapterResult] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def all_results(self) -> list[AdapterResult]:
        return list(self.imports.values()) + list(self.exports.values())

    def cache_hits(self) -> int:
        return sum(1 for result in self.all_results() if result.from_cache)

    def model_calls_avoided(self) -> int:
        """How many adapter builds the cache saved on this run."""
        return self.cache_hits()

    def blocking_flags(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for result in self.all_results():
            if result.artifact is None:
                continue
            for flag in result.artifact.spec.blocking_flags():
                found.append((result.artifact.key.family(), f"{flag.path}: {flag.detail}"))
        return found


class Orchestrator:
    """Cache-first controller for the foundry."""

    def __init__(
        self,
        store: FoundryStore | None = None,
        sandbox: Sandbox | None = None,
        provider: LLMProvider | None = None,
        max_attempts: int = 4,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store or FoundryStore()
        # Building a corridor is minutes of sequential model calls. A command
        # that prints nothing until it finishes is indistinguishable from one
        # that has hung, so the orchestrator says what it is starting before it
        # starts it rather than only what it finished.
        self.on_progress = on_progress or (lambda _message: None)
        # Constructed lazily so that a run which hits the cache for everything
        # never touches a container runtime, and never fails on a machine
        # without one.
        self._sandbox = sandbox
        self.provider = provider
        self.max_attempts = max_attempts

    @property
    def sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = default_sandbox()
        return self._sandbox

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    def corpus_for(
        self,
        context: ProbeContext,
        reuse_within: timedelta | None = None,
        force_probe: bool = False,
    ) -> tuple[SchemaCorpus, bool]:
        """A corpus for a platform, from the store if a recent one is there.

        Returns (corpus, probed). This is step one of the user-facing flow --
        "is there a recent pull for this platform?" -- and the answer being yes
        skips a full sweep of a customer's tenant, which is the slowest and
        most intrusive part of a migration.
        """
        if not force_probe and reuse_within is not None:
            cached = self.store.latest_corpus(context.platform, max_age=reuse_within)
            if cached is not None:
                return cached, False
        corpus = inspector.inspect(context)
        self.store.put_corpus(corpus)
        return corpus, True

    # ------------------------------------------------------------------
    # Adapters
    # ------------------------------------------------------------------

    def ensure_adapter(
        self,
        platform_corpus: SchemaCorpus,
        direction: Direction,
        entity_kind: EntityKind,
        rebuild: bool = False,
    ) -> AdapterResult:
        """Return a compiled adapter, building one only if the cache can't answer."""
        fingerprint = platform_corpus.entity_fingerprint(entity_kind)
        lookup = self.store.find(
            platform_corpus.platform, direction, entity_kind, fingerprint
        )

        if lookup.usable and not rebuild:
            return AdapterResult(lookup.artifact, "cache", lookup, lookup.reason)

        platform_entity = platform_corpus.entity(entity_kind)
        if platform_entity is None:
            return AdapterResult(
                None,
                "unavailable",
                lookup,
                f"The probe found no `{entity_kind.value}` records on "
                f"{platform_corpus.platform}, so there is nothing to map.",
            )

        ir = inspector.ir_corpus()
        ir_entity = ir.entity(entity_kind)
        if ir_entity is None:
            return AdapterResult(
                None,
                "unavailable",
                lookup,
                f"The IR has no `{entity_kind.value}` primitive, so this cannot be carried "
                "across. See ir_corpus().gaps.",
            )

        composed = inspector.composed_prefixes(entity_kind)
        if direction is Direction.IMPORT:
            source_entity, target_entity = platform_entity, ir_entity
            skip_source, skip_target = (), composed
        else:
            source_entity, target_entity = ir_entity, platform_entity
            skip_source, skip_target = composed, ()

        spec = translator.translate(
            source_entity=source_entity,
            target_entity=target_entity,
            platform=platform_corpus.platform,
            direction=direction,
            schema_fingerprint=fingerprint,
            provider=self.provider,
            skip_source_prefixes=skip_source,
            skip_target_prefixes=skip_target,
        )
        artifact = build_adapter(
            spec,
            sandbox=self.sandbox,
            source_entity=source_entity,
            target_entity=target_entity,
            provider=self.provider,
            max_attempts=self.max_attempts,
        )
        self.store.put(artifact)
        origin: Origin = "rebuilt" if lookup.status == "stale" else "built"
        return AdapterResult(artifact, origin, lookup, lookup.reason)

    def corridor(
        self,
        source_corpus: SchemaCorpus,
        target_corpus: SchemaCorpus,
        entity_kinds: Iterable[EntityKind] = DEFAULT_KINDS,
        rebuild: bool = False,
    ) -> CorridorResult:
        """Build (or find) both halves of a corridor.

        Both halves, always. The import adapter belongs to the source platform
        and the export adapter to the target, and keeping them separate is what
        makes them reusable: the next corridor into this target reuses the
        export half untouched.
        """
        result = CorridorResult(
            source_platform=source_corpus.platform, target_platform=target_corpus.platform
        )
        planned = [
            (corpus, direction, kind)
            for kind in entity_kinds
            for corpus, direction in (
                (source_corpus, Direction.IMPORT),
                (target_corpus, Direction.EXPORT),
            )
            if corpus.entity(kind) is not None
            and not (direction is Direction.EXPORT and kind in LOOKUP_RESOLVED)
        ]
        skipped = sorted(
            k.value
            for k in entity_kinds
            if k in LOOKUP_RESOLVED and target_corpus.entity(k) is not None
        )
        if skipped:
            result.notes.append(
                f"No export adapter built for {', '.join(skipped)}: on the target these are "
                "found, not constructed -- a tool comes from the catalog or an MCP server, a "
                "knowledge base from the documents you hand it. They migrate by lookup."
            )
        for index, (corpus, direction, kind) in enumerate(planned, start=1):
            family = f"{corpus.platform}/{direction.value}/{kind.value}"
            self.on_progress(f"[{index}/{len(planned)}] {family}")
            adapter = self.ensure_adapter(corpus, direction, kind, rebuild)
            self.on_progress(f"[{index}/{len(planned)}] {family}: {adapter.describe()}")
            group = result.imports if direction is Direction.IMPORT else result.exports
            group[kind] = adapter

        hits = result.cache_hits()
        built = len(result.all_results()) - hits
        result.notes.append(
            f"{hits} adapter(s) came from the cache; {built} were compiled this run."
        )
        for gap in source_corpus.gaps + target_corpus.gaps:
            result.notes.append(f"gap: {gap.what} -- {gap.detail}")
        return result

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def migrate(
        self,
        records: Iterable[Any],
        import_adapter: AdapterResult,
        export_adapter: AdapterResult | None = None,
    ) -> tuple[list[dict], list[runtime.MigrationRun]]:
        """Run records through the corridor: source -> IR, then IR -> target.

        Composed rather than fused. Running the two halves separately means the
        intermediate IR records exist and can be inspected, validated against
        `ir/schema.py`, or handed to the rest of the pipeline -- which is the
        entire reason for having a hub in the first place.
        """
        if import_adapter.artifact is None:
            raise ValueError(f"No import adapter available: {import_adapter.reason}")

        loaded = runtime.load(import_adapter.artifact)
        intermediate, first = runtime.convert_all(loaded, records)
        if export_adapter is None or export_adapter.artifact is None:
            return intermediate, [first]

        exporter = runtime.load(export_adapter.artifact)
        final, second = runtime.convert_all(exporter, intermediate)
        return final, [first, second]


def entity_kinds_in(corpus: SchemaCorpus) -> list[EntityKind]:
    """The kinds a corpus actually has, in the corridor's build order."""
    present = set(corpus.kinds())
    return [kind for kind in DEFAULT_KINDS if kind in present]

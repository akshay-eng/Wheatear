"""The memory, the build loop, and the thing that decides whether either runs.

The property the whole design turns on is here: probing a second tenant of the
same platform version must hit the cache and make no model call at all, while a
platform whose schema has actually moved must miss it. Both are asserted
against a real store on a real temporary directory, because a cache that is
right in principle and wrong on disk is worth nothing.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wheatear.foundry import engineer, inspector
from wheatear.foundry.orchestrator import Orchestrator
from wheatear.foundry.probes.base import ProbeContext
from wheatear.foundry.sandbox import SubprocessSandbox
from wheatear.foundry.store import FoundryStore
from wheatear.foundry.types import (
    AdapterArtifact,
    AdapterKey,
    CaseFailure,
    Direction,
    EntityKind,
    EntitySchema,
    FieldMapping,
    FieldNode,
    MappingSpec,
    ProbeOrigin,
    SandboxResult,
    SchemaCorpus,
    TransformKind,
)

FIXTURES = Path(__file__).resolve().parents[1] / "wheatear/connectors/copilot_studio/fixtures"
SOLUTION = FIXTURES / "sample_solution_agent"


# ----------------------------------------------------------------------
# Fixtures and doubles
# ----------------------------------------------------------------------


def _corpus(platform="acme", extra_field=None) -> SchemaCorpus:
    fields = [
        FieldNode(path="bot.name", types=["string"], required=True, occurrence=1.0),
        FieldNode(path="bot.description", types=["string"], required=False, occurrence=0.5),
    ]
    if extra_field:
        fields.append(FieldNode(path=extra_field, types=["string"], occurrence=1.0))
    return SchemaCorpus(
        platform=platform,
        entities=[
            EntitySchema(
                kind=EntityKind.AGENT,
                name="bot",
                origin=ProbeOrigin.EXPORT,
                sample_count=2,
                fields=fields,
                samples=[{"bot": {"name": "HR", "description": "d"}}, {"bot": {"name": "IT"}}],
            )
        ],
    )


def _spec(platform="acme", fingerprint="fp") -> MappingSpec:
    return MappingSpec(
        platform=platform,
        direction=Direction.IMPORT,
        entity_kind=EntityKind.AGENT,
        schema_fingerprint=fingerprint,
        mappings=[
            FieldMapping(
                target_path="name", source_paths=["bot.name"],
                transform=TransformKind.RENAME, required=True, confidence=0.9,
            )
        ],
    )


def _artifact(spec=None, ok=True) -> AdapterArtifact:
    spec = spec or _spec()
    return AdapterArtifact(
        key=spec.key(),
        code="def transform(record):\n    return {}\n\n\ndef flags(record):\n    return []\n",
        tests="# none",
        spec=spec,
        report=SandboxResult(ok=ok, passed=3 if ok else 0, runner="test"),
    )


class GreenSandbox:
    """Reports success without running anything, for testing the loop's shape."""

    name = "green"

    def __init__(self):
        self.runs = 0

    def available(self):
        return True, "green"

    def run(self, files):
        self.runs += 1
        return SandboxResult(ok=True, runner=self.name, passed=len(files), exit_code=0)


class RedSandbox:
    """Always fails, with one named failure the repair loop can read."""

    name = "red"

    def __init__(self, failure="test_positive_sample_0"):
        self.runs = 0
        self.failure = failure

    def available(self):
        return True, "red"

    def run(self, files):
        self.runs += 1
        return SandboxResult(
            ok=False, runner=self.name, failed=1, exit_code=1,
            failures=[CaseFailure(name=self.failure, message="expected 'x', got None")],
        )


class CountingProvider:
    """Counts calls and answers each schema with a scripted reply."""

    def __init__(self, replies=None):
        self.calls = 0
        self.replies = replies or {}

    def generate_structured(self, prompt, schema):
        self.calls += 1
        reply = self.replies.get(schema.__name__)
        if reply is not None:
            return reply
        return schema()


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


def test_a_corpus_round_trips_through_the_store(tmp_path):
    store = FoundryStore(tmp_path)
    corpus = _corpus()
    store.put_corpus(corpus)
    back = store.get_corpus("acme", corpus.fingerprint())
    assert back is not None
    assert back.fingerprint() == corpus.fingerprint()


def test_reprobing_an_unchanged_schema_rewrites_one_file(tmp_path):
    """The interesting axis is platform versions. Keeping a copy per probe
    would turn the store into a log of tenants.
    """
    store = FoundryStore(tmp_path)
    store.put_corpus(_corpus())
    store.put_corpus(_corpus())
    assert len(store.list_corpora("acme")) == 1


def test_a_recent_probe_can_be_reused_and_a_stale_one_cannot(tmp_path):
    store = FoundryStore(tmp_path)
    old = _corpus()
    old.captured_at = datetime.now(timezone.utc) - timedelta(days=30)
    store.put_corpus(old)

    assert store.latest_corpus("acme", max_age=timedelta(days=90)) is not None
    assert store.latest_corpus("acme", max_age=timedelta(days=1)) is None
    assert store.latest_corpus("acme") is not None  # no limit accepts any age


def test_an_adapter_is_stored_with_readable_sidecars(tmp_path):
    """The generated code is meant to be reviewed and diffed by a person, and
    nobody reviews code embedded in a JSON string.
    """
    store = FoundryStore(tmp_path)
    artifact = _artifact()
    directory = store.put(artifact)
    assert (directory / "adapter.py").read_text() == artifact.code
    assert (directory / "spec.json").exists()
    assert store.get(artifact.key) is not None


def test_an_adapter_compiled_for_this_exact_schema_is_a_hit(tmp_path):
    store = FoundryStore(tmp_path)
    store.put(_artifact(_spec(fingerprint="abc")))
    lookup = store.find("acme", Direction.IMPORT, EntityKind.AGENT, "abc")
    assert lookup.status == "hit"
    assert lookup.usable


def test_an_adapter_for_a_different_schema_is_stale_not_a_hit(tmp_path):
    """Running it would apply last quarter's field mapping to this quarter's
    records. It is also not nothing: it is the best starting point for a
    rebuild.
    """
    store = FoundryStore(tmp_path)
    store.put(_artifact(_spec(fingerprint="abc")))
    lookup = store.find("acme", Direction.IMPORT, EntityKind.AGENT, "xyz")
    assert lookup.status == "stale"
    assert lookup.artifact is not None
    assert "schema moved" in lookup.reason


def test_an_adapter_compiled_against_a_different_ir_version_is_stale(tmp_path):
    store = FoundryStore(tmp_path)
    spec = _spec(fingerprint="abc")
    spec.ir_version = "wheatear/v0"
    store.put(_artifact(spec))
    lookup = store.find("acme", Direction.IMPORT, EntityKind.AGENT, "abc")
    assert lookup.status == "stale"
    assert "IR" in lookup.reason


def test_an_adapter_whose_tests_failed_is_never_a_hit(tmp_path):
    store = FoundryStore(tmp_path)
    store.put(_artifact(_spec(fingerprint="abc"), ok=False))
    lookup = store.find("acme", Direction.IMPORT, EntityKind.AGENT, "abc")
    assert lookup.status == "stale"
    assert not lookup.usable


def test_nothing_stored_is_a_miss(tmp_path):
    lookup = FoundryStore(tmp_path).find("acme", Direction.IMPORT, EntityKind.AGENT, "abc")
    assert lookup.status == "miss"
    assert lookup.artifact is None


def test_a_corrupt_cache_entry_reads_as_absence(tmp_path):
    """One bad file must not block every migration until somebody finds and
    deletes it. Recompiling is a much cheaper failure.
    """
    store = FoundryStore(tmp_path)
    artifact = _artifact()
    store.put(artifact)
    (store.adapter_dir(artifact.key) / "artifact.json").write_text("{ truncated")
    assert store.get(artifact.key) is None
    assert store.list_adapters("acme") == []


def test_forgetting_an_adapter_forces_a_rebuild(tmp_path):
    store = FoundryStore(tmp_path)
    artifact = _artifact()
    store.put(artifact)
    assert store.forget(artifact.key) is True
    assert store.forget(artifact.key) is False
    assert store.get(artifact.key) is None


def test_a_platform_key_cannot_write_outside_the_store(tmp_path):
    store = FoundryStore(tmp_path)
    corpus = _corpus(platform="../../etc")
    path = store.put_corpus(corpus)
    assert tmp_path in path.parents


# ----------------------------------------------------------------------
# Fingerprinting
# ----------------------------------------------------------------------


def test_the_fingerprint_covers_shape_and_ignores_data():
    """Two tenants on the same platform version must share a compiled adapter;
    a vendor who adds a field must not.
    """
    a = _corpus()
    b = _corpus()
    b.entities[0].samples = [{"bot": {"name": "completely different"}}]
    b.captured_at = datetime.now(timezone.utc) + timedelta(days=5)
    assert a.fingerprint() == b.fingerprint()

    moved = _corpus(extra_field="bot.newField")
    assert moved.fingerprint() != a.fingerprint()


def test_each_entity_kind_is_fingerprinted_separately():
    """Keying adapters on the whole corpus would throw away every tool adapter
    the day a platform added a field to its agent schema.
    """
    a = _corpus()
    b = _corpus(extra_field="bot.newField")
    assert a.entity_fingerprint(EntityKind.AGENT) != b.entity_fingerprint(EntityKind.AGENT)
    assert a.entity_fingerprint(EntityKind.TOOL) == b.entity_fingerprint(EntityKind.TOOL)


# ----------------------------------------------------------------------
# The Engineer's loop
# ----------------------------------------------------------------------


def test_a_mapping_with_no_holes_needs_no_model_at_all():
    """The common case, and the one worth optimising for: every transform the
    translator named has exactly one correct rendering.
    """
    box = GreenSandbox()
    artifact = engineer.build_adapter(_spec(), sandbox=box, provider=None)
    assert artifact.verified
    assert box.runs == 1
    assert "transform" in artifact.code


def test_a_derived_field_is_handed_to_the_model_and_spliced_in():
    spec = _spec()
    spec.mappings.append(
        FieldMapping(target_path="web_search", source_paths=["caps.browsing"],
                     transform=TransformKind.DERIVE, rationale="true when browsing is on")
    )
    provider = CountingProvider(
        {
            "DerivedFunctions": engineer.DerivedFunctions(
                functions=[
                    engineer.DerivedFunction(
                        target_path="web_search",
                        source=(
                            "def _derive_web_search(record):\n"
                            "    v = _get(record, 'caps.browsing')\n"
                            "    return bool(v) if v is not _MISSING else _MISSING\n"
                        ),
                    )
                ]
            )
        }
    )
    artifact = engineer.build_adapter(spec, sandbox=GreenSandbox(), provider=provider)
    assert "_derive_web_search" in artifact.code
    assert "NotImplementedError" not in artifact.code
    assert "Written by the Engineer" in artifact.code


def test_a_function_with_the_wrong_name_is_rejected_before_it_is_spliced():
    """A mismatched name produces a module that imports cleanly and raises
    NameError on the first record.
    """
    source, complaint = engineer.validate_function(
        "def something_else(record):\n    return 1\n", "_derive_web_search"
    )
    assert source is None
    assert "expected exactly one function" in complaint


def test_a_function_that_reaches_outside_the_allowlist_is_rejected():
    source, complaint = engineer.validate_function(
        "def _derive_x(record):\n    import os\n    return os.getcwd()\n", "_derive_x"
    )
    assert source is None
    assert "safety check" in complaint


def test_a_guard_violation_short_circuits_the_sandbox():
    """There is no point starting a container for code we have already decided
    not to run.
    """
    box = GreenSandbox()
    result, report = engineer._attempt("import os\n" + _artifact().code, "", box)
    assert box.runs == 0
    assert result.runner == "guard"
    assert not report.ok


def test_a_failing_build_is_still_stored_but_not_marked_verified():
    """The code, the failures and the reason are exactly what a human needs to
    finish it by hand. `verified` is what stops it running unattended.
    """
    artifact = engineer.build_adapter(_spec(), sandbox=RedSandbox(), provider=None)
    assert artifact.verified is False
    assert artifact.report.failed == 1
    assert any("No model available to repair" in note for note in artifact.spec.notes)


def test_the_repair_loop_stops_at_max_attempts():
    spec = _spec()
    spec.mappings.append(
        FieldMapping(target_path="derived", source_paths=["x"], transform=TransformKind.DERIVE)
    )
    box = RedSandbox()
    provider = CountingProvider(
        {
            "DerivedFunctions": engineer.DerivedFunctions(
                functions=[
                    engineer.DerivedFunction(
                        target_path="derived",
                        source="def _derive_derived(record):\n    return _MISSING\n",
                    )
                ]
            )
        }
    )
    artifact = engineer.build_adapter(
        spec, sandbox=box, provider=provider, max_attempts=3, allow_rewrite=False
    )
    assert artifact.verified is False
    assert box.runs == 3
    assert artifact.attempts == 3


def test_cases_the_model_proposed_are_validated_against_real_target_paths():
    kept, complaints = engineer.validate_cases(
        [
            engineer.ProposedCase(
                name="good",
                record_json='{"bot": {"name": "x"}}',
                expect_paths=[engineer.ExpectedValue(path="name", value="x")],
            ),
            engineer.ProposedCase(
                name="bad", expect_paths=[engineer.ExpectedValue(path="not_a_field", value=1)]
            ),
            engineer.ProposedCase(name="has spaces and $!"),
            engineer.ProposedCase(name="malformed", record_json="not json at all"),
        ],
        known_targets={"name"},
        existing=set(),
    )
    assert [case.name for case in kept] == ["proposed_good"]
    assert len(complaints) == 3


def test_a_failure_made_only_of_model_proposed_cases_drops_those_cases():
    """A model that invents an expectation its own code cannot meet has more
    likely written a bad test than a bad adapter. Letting that fail the build
    would block a correct adapter on a wrong assertion.
    """

    class Fussy:
        """Fails while the proposed case is present, passes once it is gone."""

        name = "fussy"

        def __init__(self):
            self.runs = 0

        def available(self):
            return True, "fussy"

        def run(self, files):
            self.runs += 1
            if "proposed_odd" in files.get("test_adapter.py", ""):
                return SandboxResult(
                    ok=False, runner=self.name, failed=1,
                    failures=[CaseFailure(name="test_proposed_odd", message="nope")],
                )
            return SandboxResult(ok=True, runner=self.name, passed=5)

    provider = CountingProvider(
        {
            "ProposedCases": engineer.ProposedCases(
                cases=[engineer.ProposedCase(name="odd", record={}, expect_absent=["name"])]
            )
        }
    )
    box = Fussy()
    artifact = engineer.build_adapter(
        _spec(), sandbox=box, provider=provider, max_attempts=1, allow_rewrite=False
    )
    assert artifact.verified is True
    assert any("dropped as unreliable" in note for note in artifact.spec.notes)


def test_a_rewritten_module_that_fails_the_guard_is_not_accepted():
    provider = CountingProvider(
        {"FullAdapter": engineer.FullAdapter(source="import socket\ndef transform(r): return {}\n")}
    )
    artifact = engineer.build_adapter(
        _spec(), sandbox=RedSandbox(), provider=provider, max_attempts=2
    )
    assert "import socket" not in artifact.code
    assert any("rejected by the safety check" in note for note in artifact.spec.notes)


# ----------------------------------------------------------------------
# The Orchestrator
# ----------------------------------------------------------------------


def test_the_second_build_of_an_unchanged_schema_makes_no_model_call(tmp_path):
    """This is the property the whole design exists for. Migrating ten thousand
    agents, or a second customer on the same platform version, must not pay the
    inference cost again.
    """
    provider = CountingProvider()
    orchestrator = Orchestrator(
        store=FoundryStore(tmp_path), sandbox=GreenSandbox(), provider=provider
    )
    corpus = _corpus()

    first = orchestrator.ensure_adapter(corpus, Direction.IMPORT, EntityKind.AGENT)
    assert first.origin == "built"
    calls_after_build = provider.calls
    assert calls_after_build > 0

    second = orchestrator.ensure_adapter(corpus, Direction.IMPORT, EntityKind.AGENT)
    assert second.from_cache
    assert provider.calls == calls_after_build


def test_a_schema_that_moved_forces_a_rebuild(tmp_path):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    orchestrator.ensure_adapter(_corpus(), Direction.IMPORT, EntityKind.AGENT)
    moved = orchestrator.ensure_adapter(
        _corpus(extra_field="bot.newField"), Direction.IMPORT, EntityKind.AGENT
    )
    assert moved.origin == "rebuilt"
    assert not moved.from_cache


def test_rebuild_ignores_a_perfectly_good_cache_entry(tmp_path):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    corpus = _corpus()
    orchestrator.ensure_adapter(corpus, Direction.IMPORT, EntityKind.AGENT)
    forced = orchestrator.ensure_adapter(
        corpus, Direction.IMPORT, EntityKind.AGENT, rebuild=True
    )
    assert forced.origin == "built"


def test_an_entity_kind_the_platform_does_not_have_is_reported_not_invented(tmp_path):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    result = orchestrator.ensure_adapter(_corpus(), Direction.IMPORT, EntityKind.WORKFLOW)
    assert result.artifact is None
    assert "found no `workflow` records" in result.reason


def test_an_entity_kind_the_ir_cannot_represent_is_reported(tmp_path):
    corpus = _corpus()
    corpus.entities.append(
        EntitySchema(
            kind=EntityKind.TRIGGER, name="trigger", origin=ProbeOrigin.EXPORT,
            sample_count=1, fields=[FieldNode(path="cron", types=["string"])],
            samples=[{"cron": "0 9 * * *"}],
        )
    )
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    result = orchestrator.ensure_adapter(corpus, Direction.IMPORT, EntityKind.TRIGGER)
    assert result.artifact is None
    assert "no `trigger` primitive" in result.reason


def test_a_recent_corpus_is_reused_instead_of_reprobing(tmp_path):
    """Step one of the user-facing flow. A hit skips a full sweep of a
    customer's tenant, which is the slowest and most intrusive part.
    """
    store = FoundryStore(tmp_path)
    orchestrator = Orchestrator(store=store, sandbox=GreenSandbox())
    context = ProbeContext(platform="copilot-studio", export_path=SOLUTION, allow_network=False)

    _, probed_first = orchestrator.corpus_for(context, reuse_within=timedelta(days=7))
    assert probed_first is True

    _, probed_again = orchestrator.corpus_for(context, reuse_within=timedelta(days=7))
    assert probed_again is False

    _, forced = orchestrator.corpus_for(context, reuse_within=timedelta(days=7), force_probe=True)
    assert forced is True


def test_a_corridor_builds_both_halves_and_reports_the_cache(tmp_path):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    source, target = _corpus("copilot-studio"), _corpus("orchestrate")

    first = orchestrator.corridor(source, target, entity_kinds=[EntityKind.AGENT])
    assert set(first.imports) == {EntityKind.AGENT}
    assert set(first.exports) == {EntityKind.AGENT}
    assert first.cache_hits() == 0

    again = orchestrator.corridor(source, target, entity_kinds=[EntityKind.AGENT])
    assert again.cache_hits() == 2
    assert again.model_calls_avoided() == 2


def test_composed_ir_subtrees_are_excluded_when_building_an_agent_adapter(tmp_path):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    result = orchestrator.ensure_adapter(_corpus(), Direction.IMPORT, EntityKind.AGENT)
    assert result.artifact is not None
    flagged = {flag.path for flag in result.artifact.spec.flags}
    assert not any(path.startswith("tools") for path in flagged)


# ----------------------------------------------------------------------
# End to end, on real fixtures
# ----------------------------------------------------------------------


def test_a_real_export_becomes_a_tested_adapter_that_produces_valid_ir(tmp_path):
    """The whole chain, deterministically and with no model: probe the Copilot
    fixtures, correlate against the IR, compile, run the generated tests for
    real, then convert a batch and validate the result as IR.
    """
    from wheatear.foundry import runtime

    store = FoundryStore(tmp_path)
    orchestrator = Orchestrator(store=store, sandbox=SubprocessSandbox(timeout_s=60))

    context = ProbeContext(platform="copilot-studio", export_path=SOLUTION, allow_network=False)
    corpus, _ = orchestrator.corpus_for(context)
    assert EntityKind.AGENT in corpus.kinds()

    result = orchestrator.ensure_adapter(corpus, Direction.IMPORT, EntityKind.AGENT)
    assert result.artifact is not None
    assert result.artifact.verified, result.artifact.report.feedback()

    adapter = runtime.load(result.artifact)
    records = corpus.entity(EntityKind.AGENT).samples * 200
    converted, report = runtime.convert_all(adapter, records)
    assert report.total == len(records)
    assert report.failed == 0

    # The mapping found the agent's system prompt, which is the field that
    # matters most in this corridor.
    assert converted[0].get("instructions")
    ir = runtime.to_ir(
        EntityKind.AGENT, {**converted[0], "name": "HR", "source_platform": "copilot-studio"}
    )
    assert ir.ok, ir.errors


def test_the_ir_side_of_a_mapping_is_read_from_the_models_not_probed(tmp_path):
    corpus = inspector.ir_corpus()
    assert corpus.platform == inspector.IR_PLATFORM
    # Reading it twice gives the same fingerprint: it is a contract, not a
    # sample, so nothing about the machine or the moment can change it.
    assert corpus.fingerprint() == inspector.ir_corpus().fingerprint()


@pytest.mark.parametrize("direction", [Direction.IMPORT, Direction.EXPORT])
def test_both_directions_of_a_platform_are_keyed_separately(tmp_path, direction):
    orchestrator = Orchestrator(store=FoundryStore(tmp_path), sandbox=GreenSandbox())
    result = orchestrator.ensure_adapter(_corpus(), direction, EntityKind.AGENT)
    assert result.artifact is not None
    assert result.artifact.key.direction is direction
    assert result.artifact.key.family() == f"acme/{direction.value}/agent"


def test_an_adapter_key_is_hashable_so_it_can_index_a_cache():
    key = AdapterKey(
        platform="acme", direction=Direction.IMPORT, entity_kind=EntityKind.AGENT,
        schema_fingerprint="abc",
    )
    assert {key: 1}[key] == 1
    assert key.slug() == "acme/import/agent/abc"


def test_a_rewrite_that_also_fails_is_discarded_for_the_generated_module():
    """Observed on a real corridor: the model rewrote a knowledge adapter,
    silently dropped five `constant` mappings, and still failed its tests --
    and the Engineer kept the rewrite. A rewrite that does not achieve a green
    run is strictly worse than the emitted module: it is not reproducible, not
    derived from the spec, and demonstrably lossy.
    """
    rewritten_source = (
        "ADAPTER_META = {}\n"
        "REVIEW_FLAGS = []\n"
        "def transform(record):\n"
        "    return {}\n"
        "\n\n"
        "def flags(record):\n"
        "    return []\n"
    )
    provider = CountingProvider({"FullAdapter": engineer.FullAdapter(source=rewritten_source)})
    artifact = engineer.build_adapter(
        _spec(), sandbox=RedSandbox(), provider=provider, max_attempts=3
    )
    assert artifact.verified is False
    assert artifact.code != rewritten_source, "a failing rewrite must not replace the emitted code"
    # The mapping the spec actually asked for is still in the module.
    assert "bot.name" in artifact.code
    assert any("discarded and the generated one kept" in n for n in artifact.spec.notes)


def test_a_rewrite_that_passes_is_kept():
    """The escape hatch still works when it earns its keep."""

    class GreenAfterRewrite:
        name = "green-after-rewrite"

        def __init__(self):
            self.runs = 0

        def available(self):
            return True, "ok"

        def run(self, files):
            self.runs += 1
            if "REWRITTEN" in files["adapter.py"]:
                return SandboxResult(ok=True, runner=self.name, passed=4)
            return SandboxResult(
                ok=False, runner=self.name, failed=1,
                failures=[CaseFailure(name="test_x", message="nope")],
            )

    source = (
        "# REWRITTEN\n"
        "ADAPTER_META = {}\n"
        "REVIEW_FLAGS = []\n"
        "def transform(record):\n"
        "    return {}\n"
        "\n\n"
        "def flags(record):\n"
        "    return []\n"
    )
    provider = CountingProvider({"FullAdapter": engineer.FullAdapter(source=source)})
    artifact = engineer.build_adapter(
        _spec(), sandbox=GreenAfterRewrite(), provider=provider, max_attempts=3
    )
    assert artifact.verified is True
    assert "REWRITTEN" in artifact.code
    assert artifact.generator == "rewritten"


def test_no_export_adapter_is_built_for_a_kind_the_target_looks_up(tmp_path):
    """An Orchestrate tool comes from the catalog or an MCP server, never from
    a Copilot connector's fields. An export adapter for it maps into a shape
    nothing will ever be created from -- 138 mappings, 715 flags, no consumer.
    """
    from wheatear.foundry.orchestrator import LOOKUP_RESOLVED

    assert EntityKind.TOOL in LOOKUP_RESOLVED
    assert EntityKind.KNOWLEDGE in LOOKUP_RESOLVED
    # The import half is what feeds the lookup its search query, so it stays.
    assert EntityKind.AGENT not in LOOKUP_RESOLVED
    assert EntityKind.TOPIC not in LOOKUP_RESOLVED


def test_the_stored_report_always_describes_the_stored_code():
    """The repair loop must not replace the module on its final pass and then
    fall out, because the artifact would then ship one adapter and a report
    about another -- a claim about code that was never run. Observed on a real
    corridor: the stored module passed every case its own report said failed.
    """

    class RecordingSandbox:
        """Fails everything, and remembers each module it was handed."""

        name = "recording"

        def __init__(self):
            self.seen: list[str] = []

        def available(self):
            return True, "recording"

        def run(self, files):
            self.seen.append(files["adapter.py"])
            return SandboxResult(
                ok=False, runner=self.name, failed=1, exit_code=1,
                failures=[CaseFailure(name="test_mapping_web_search", message="boom")],
            )

    spec = _spec()
    spec.mappings.append(
        FieldMapping(target_path="web_search", source_paths=["caps.browsing"],
                     transform=TransformKind.DERIVE, rationale="true when browsing is on")
    )
    # Answers every repair request with a fresh, differently-named body, so the
    # emitted module genuinely changes on each pass.
    class ShiftingProvider:
        def __init__(self):
            self.calls = 0

        def generate_structured(self, prompt, schema):
            self.calls += 1
            if schema.__name__ != "DerivedFunctions":
                return schema()
            return engineer.DerivedFunctions(
                functions=[
                    engineer.DerivedFunction(
                        target_path="web_search",
                        source=(
                            "def _derive_web_search(record):\n"
                            f"    # revision {self.calls}\n"
                            "    return _MISSING\n"
                        ),
                    )
                ]
            )

    box = RecordingSandbox()
    artifact = engineer.build_adapter(
        spec, sandbox=box, provider=ShiftingProvider(), max_attempts=3
    )
    assert not artifact.verified
    assert artifact.code == box.seen[-1], "the artifact shipped code the sandbox never ran"

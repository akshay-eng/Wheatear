"""Probing: reading a platform's shape out of an export, and merging sources.

The structural pass runs against the real Copilot Studio fixtures in this
repository rather than synthetic data, because the thing being tested is
whether the classification rules match the layouts vendors actually ship --
which invented fixtures cannot tell you.
"""

import zipfile
from pathlib import Path

import pytest

from agent_liftoff.foundry import inspector
from agent_liftoff.foundry.probes import export_scan
from agent_liftoff.foundry.probes.base import ProbeContext, ProbeResult, observe
from agent_liftoff.foundry.probes.export_scan import classify, scan_export
from agent_liftoff.foundry.types import EntityKind, GapReason, ProbeGap, ProbeOrigin

FIXTURES = Path(__file__).resolve().parents[1] / "agent_liftoff/connectors/copilot_studio/fixtures"
SOLUTION = FIXTURES / "sample_solution_agent"
CLONE = FIXTURES / "sample_agent"


def _kinds(result: ProbeResult) -> set[EntityKind]:
    return {entity.kind for entity in result.entities}


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("botcomponents/ai_Bot.topic.Greeting/data", EntityKind.TOPIC),
        ("botcomponents/ai_Bot.knowledge.Policies/data", EntityKind.KNOWLEDGE),
        ("botcomponents/ai_Bot.gpt.default/data", EntityKind.AGENT),
        ("bots/ai_Bot/bot.xml", EntityKind.AGENT),
        ("topics/order_status.mcs.yml", EntityKind.TOPIC),
        ("agent.mcs.yaml", EntityKind.AGENT),
        ("agent.yaml", EntityKind.AGENT),
        ("workflows/nightly.json", EntityKind.WORKFLOW),
        ("triggers/on_message.json", EntityKind.TRIGGER),
        ("readme.md", None),
    ],
)
def test_files_are_classified_by_where_they_sit_in_the_archive(path, expected):
    assert classify(path) == expected


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------


def test_a_copilot_solution_export_yields_agents_topics_and_knowledge():
    result = scan_export(SOLUTION)
    assert {EntityKind.AGENT, EntityKind.TOPIC, EntityKind.KNOWLEDGE} <= _kinds(result)


def test_a_component_metadata_file_and_its_payload_become_one_record():
    """Copilot writes a component's XML metadata and its YAML payload as two
    files in one directory. Treating them as two records would produce two
    half-schemas, neither of which describes anything real.
    """
    result = scan_export(SOLUTION)
    topic = next(e for e in result.entities if e.kind is EntityKind.TOPIC)
    paths = topic.paths()
    assert "botcomponent.componenttype" in paths  # from the XML
    assert any(p.startswith("data.") for p in paths)  # from the payload


def test_an_agent_split_across_two_directories_becomes_one_record():
    """A Copilot agent is written to the archive twice: `bots/X/` holds the
    container, `botcomponents/X.gpt.default/` holds the instructions and model.
    Left as two records they are two agents, each missing what the other has,
    and no single mapping can serve both.
    """
    result = scan_export(SOLUTION)
    agent = next(e for e in result.entities if e.kind is EntityKind.AGENT)
    paths = agent.paths()
    assert "bot.name" in paths  # from bots/X/bot.xml
    assert "gpt.data.instructions" in paths  # grafted from the gpt component
    assert "gpt.botcomponent.componenttype" in paths


def test_a_grafted_child_whose_parent_is_absent_stays_a_record_of_its_own(tmp_path):
    """Half an agent in the corpus is worth more than none: the shape it
    contributes is real whether or not its container shipped with it.
    """
    component = tmp_path / "botcomponents" / "orphan_Bot.gpt.default"
    component.mkdir(parents=True)
    (component / "botcomponent.xml").write_text(
        '<botcomponent schemaname="orphan_Bot.gpt.default">'
        "<parentbotid><schemaname>nowhere</schemaname></parentbotid>"
        "</botcomponent>"
    )
    (component / "data").write_text("instructions: be helpful\n")
    result = scan_export(tmp_path)
    agent = next(e for e in result.entities if e.kind is EntityKind.AGENT)
    assert "data.instructions" in agent.paths()


def test_a_connected_agent_action_lands_as_a_collaborator_on_its_caller():
    """The delegation graph appears in exactly one place in the archive. Lose
    the connected-agent components and a supervisor migrates as a lone agent
    that silently answers everything itself.
    """
    result = scan_export(SOLUTION)
    agent = next(e for e in result.entities if e.kind is EntityKind.AGENT)
    assert any(p.startswith("collaborators[]") for p in agent.paths())


def test_sibling_files_that_are_separate_entities_stay_separate():
    """`topics/` holds one file per topic. Bundling them the way component
    directories are bundled would merge every topic in an agent into one
    record and destroy the shape entirely.
    """
    result = scan_export(CLONE)
    topics = next(e for e in result.entities if e.kind is EntityKind.TOPIC)
    assert topics.sample_count >= 2


def test_files_that_match_no_rule_are_counted_as_a_gap_not_dropped(tmp_path):
    """A corridor where 40% of the archive went unclassified is a corridor
    whose rules need work, and the only way anyone finds that out is if the
    scan says so.
    """
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "a.json").write_text('{"name": "ok"}')
    (tmp_path / "mystery.json").write_text('{"whatever": 1}')
    result = scan_export(tmp_path)
    gap = next(g for g in result.gaps if g.reason is GapReason.UNSUPPORTED)
    assert "mystery.json" in gap.detail


def test_a_recognised_solution_export_leaves_nothing_unclassified():
    """Packaging the vendor wraps a solution in is not an unrecognised entity.
    Counting it as one buries the files that genuinely need a rule under noise
    a reviewer learns to skip past.
    """
    result = scan_export(SOLUTION)
    assert not [g for g in result.gaps if g.reason is GapReason.UNSUPPORTED]


def test_scanning_something_that_is_not_an_export_says_so(tmp_path):
    (tmp_path / "notes.txt").write_text("not an export")
    result = scan_export(tmp_path)
    assert result.entities == []
    assert any(gap.reason is GapReason.NOT_IN_EXPORT for gap in result.gaps)


def test_a_missing_path_is_a_gap_rather_than_an_exception(tmp_path):
    result = scan_export(tmp_path / "nowhere")
    assert any("does not exist" in gap.detail for gap in result.gaps)


def test_an_unparseable_file_does_not_sink_the_scan(tmp_path):
    """An export routinely contains one malformed file. Refusing to read the
    other four thousand because of it would be strictly worse.
    """
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "good.json").write_text('{"name": "ok"}')
    (tmp_path / "agents" / "bad.json").write_text("{not json at all,,,")
    result = scan_export(tmp_path)
    assert _kinds(result) == {EntityKind.AGENT}
    assert any("could not be parsed" in note for note in result.notes)


def test_a_zip_export_is_read_without_leaving_anything_behind(tmp_path):
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("agents/one.json", '{"name": "a", "description": "d"}')
        zf.writestr("agents/two.json", '{"name": "b", "description": "e"}')
    result = scan_export(archive)
    assert _kinds(result) == {EntityKind.AGENT}
    assert not list(tmp_path.glob("agent_liftoff-scan-*"))


def test_an_archive_entry_that_escapes_its_root_is_refused(tmp_path):
    """An export is a file a user downloaded from a vendor, which makes it
    untrusted input. A `../../` entry costs nothing to write and everything to
    extract.
    """
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.json", "{}")
    with pytest.raises(ValueError, match="escapes the archive root"):
        scan_export(archive)


def test_xml_attributes_cannot_collide_with_child_elements():
    result = scan_export(SOLUTION)
    topic = next(e for e in result.entities if e.kind is EntityKind.TOPIC)
    assert "botcomponent.@schemaname" in topic.paths()


# ----------------------------------------------------------------------
# observe()
# ----------------------------------------------------------------------


def test_observe_redacts_before_it_infers():
    """Redaction has to happen at the probe boundary, not at each of the three
    places a corpus could leak from.
    """
    entity = observe(
        EntityKind.TOOL,
        "tool",
        ProbeOrigin.API,
        [{"name": "t", "client_secret": "hunter2"}],
    )
    assert entity is not None
    assert "hunter2" not in str(entity.samples)


def test_observe_returns_nothing_for_an_empty_record_set():
    """"We looked and there were none" belongs in a gap, not in a field-less
    entity that would later read as a platform with no agents.
    """
    assert observe(EntityKind.AGENT, "agent", ProbeOrigin.API, []) is None
    assert observe(EntityKind.AGENT, "agent", ProbeOrigin.API, [{}, None]) is None


def test_observe_caps_stored_samples_but_infers_over_all_of_them():
    records = [{"name": f"n{i}", "extra": i} for i in range(80)]
    entity = observe(EntityKind.AGENT, "agent", ProbeOrigin.API, records)
    assert entity is not None
    assert entity.sample_count == 80
    assert len(entity.samples) <= 25
    assert entity.field("name").required is True


# ----------------------------------------------------------------------
# Inspector
# ----------------------------------------------------------------------


def test_inspecting_an_export_offline_needs_no_credentials():
    context = ProbeContext(platform="copilot-studio", export_path=SOLUTION, allow_network=False)
    corpus = inspector.inspect(context)
    assert corpus.platform == "copilot-studio"
    assert EntityKind.AGENT in corpus.kinds()
    assert any(gap.reason is GapReason.NO_CREDENTIALS for gap in corpus.gaps)


def test_a_probe_source_that_raises_becomes_a_gap():
    """With two passes and several endpoints, something being unreachable is
    the normal case. A partial corpus is worth far more than none.
    """

    class Exploding:
        name = "exploding"

        def probe(self, context):
            raise RuntimeError("the tenant said no")

    corpus = inspector.inspect(ProbeContext(platform="x"), sources=[Exploding()])
    assert any("the tenant said no" in gap.detail for gap in corpus.gaps)


def test_two_sources_describing_one_kind_produce_one_schema():
    from agent_liftoff.foundry.types import EntitySchema, FieldNode

    export = EntitySchema(
        kind=EntityKind.AGENT,
        name="export",
        origin=ProbeOrigin.EXPORT,
        sample_count=3,
        fields=[FieldNode(path="name", types=["string"], required=True, occurrence=1.0)],
        samples=[{"name": "a"}],
    )
    api = EntitySchema(
        kind=EntityKind.AGENT,
        name="api",
        origin=ProbeOrigin.API,
        sample_count=2,
        fields=[
            FieldNode(path="name", types=["string"], required=True, occurrence=1.0),
            FieldNode(path="llm", types=["string"], required=True, occurrence=1.0),
        ],
        samples=[{"name": "b", "llm": "x"}],
    )
    merged = inspector.merge_entities([export, api])
    assert len(merged) == 1
    fields = {f.path: f for f in merged[0].fields}
    assert fields["name"].required is True
    # Seen by one source only: an adapter that treated it as mandatory would
    # reject every record the other source produces.
    assert fields["llm"].required is False
    assert merged[0].sample_count == 5


def test_merged_samples_are_taken_round_robin():
    """A source with 200 records must not crowd out the one with 3 -- the
    small one is usually the live API, and its records are the richer ones.
    """
    from agent_liftoff.foundry.types import EntitySchema

    big = EntitySchema(
        kind=EntityKind.TOOL, name="big", origin=ProbeOrigin.EXPORT, sample_count=40,
        samples=[{"src": "big", "n": i} for i in range(40)],
    )
    small = EntitySchema(
        kind=EntityKind.TOOL, name="small", origin=ProbeOrigin.API, sample_count=2,
        samples=[{"src": "small", "n": i} for i in range(2)],
    )
    merged = inspector.merge_entities([big, small])[0]
    assert any(sample["src"] == "small" for sample in merged.samples)


# ----------------------------------------------------------------------
# The IR side
# ----------------------------------------------------------------------


def test_the_ir_corpus_is_read_from_the_models():
    corpus = inspector.ir_corpus()
    assert corpus.platform == inspector.IR_PLATFORM
    assert EntityKind.AGENT in corpus.kinds()
    agent = corpus.entity(EntityKind.AGENT)
    assert agent is not None
    assert any(field.path == "instructions" for field in agent.fields)


def test_the_ir_declares_what_it_has_no_primitive_for():
    """A trigger has nowhere to land today. That is a fact about Agent Liftoff, not
    about the source platform, and it belongs in the record rather than being
    discovered later as an empty mapping.
    """
    gaps = inspector.ir_corpus().gaps
    assert any("trigger" in gap.what for gap in gaps)


def test_composed_ir_subtrees_are_named_so_they_can_be_excluded():
    """An agent record does not contain its tools; it references them, and the
    tool adapter produces those. Flagging `tools[]` as unmappable on the agent
    adapter would bury the flags that describe real losses.
    """
    composed = inspector.composed_prefixes(EntityKind.AGENT)
    assert "tools" in composed
    assert "collaborators" in composed
    assert inspector.composed_prefixes(EntityKind.CONNECTION) == ()


# ----------------------------------------------------------------------
# Live probes, without the network
# ----------------------------------------------------------------------


def test_the_orchestrate_probe_reports_a_gap_rather_than_failing_without_credentials():
    from agent_liftoff.foundry.probes.orchestrate import OrchestrateProbe

    result = OrchestrateProbe().probe(ProbeContext(platform="orchestrate"))
    assert result.entities == []
    assert any(gap.reason is GapReason.NO_CREDENTIALS for gap in result.gaps)
    assert all(gap.remedy for gap in result.gaps)


def test_the_dataverse_probe_names_the_credential_it_wanted():
    from agent_liftoff.foundry.probes.copilot_studio import DataverseProbe

    result = DataverseProbe().probe(ProbeContext(platform="copilot-studio"))
    assert any("token" in (gap.remedy or "") for gap in result.gaps)


def test_offline_mode_makes_no_network_call_and_says_why():
    from agent_liftoff.foundry.probes.orchestrate import OrchestrateProbe

    context = ProbeContext(
        platform="orchestrate", instance_url="https://example.invalid",
        api_key="k", allow_network=False,
    )
    result = OrchestrateProbe().probe(context)
    assert result.entities == []
    assert any("disabled" in gap.detail for gap in result.gaps)


def test_a_copilot_component_type_of_nine_is_split_by_its_payload_kind():
    """Copilot stores topics and connector actions in the same table under the
    same type; only the payload's `kind` tells them apart. Mapping them to one
    kind would produce a topic adapter that had to cope with tool records.
    """
    from agent_liftoff.foundry.probes.copilot_studio import _component_kind

    assert _component_kind({"componenttype": 9, "data": {"kind": "TaskDialog"}}) is EntityKind.TOOL
    assert _component_kind({"componenttype": 9, "data": {"kind": "AdaptiveDialog"}}) is EntityKind.TOPIC
    assert _component_kind({"componenttype": 15}) is EntityKind.AGENT
    assert _component_kind({"componenttype": 999}) is None


def test_probe_gaps_always_say_what_would_close_them():
    context = ProbeContext(platform="copilot-studio", export_path=SOLUTION, allow_network=False)
    corpus = inspector.inspect(context)
    assert corpus.gaps
    for gap in corpus.gaps:
        assert isinstance(gap, ProbeGap)
        assert gap.remedy, f"{gap.what} has no remedy"


def test_export_scan_module_declares_its_limits():
    assert export_scan.MAX_FILES > 0
    assert export_scan.MAX_ARCHIVE_BYTES > 0


def test_merged_occurrence_is_weighted_by_records_not_averaged_across_sources():
    """A field in 33 of 35 records is not "50%" just because one of two sources
    lacked it. Averaging across sources produces numbers that make a reviewer
    distrust the corpus -- correctly, because they are wrong.
    """
    from agent_liftoff.foundry.types import EntitySchema, FieldNode

    api = EntitySchema(
        kind=EntityKind.AGENT, name="api", origin=ProbeOrigin.API, sample_count=33,
        fields=[FieldNode(path="name", types=["string"], required=True, occurrence=1.0)],
    )
    export = EntitySchema(
        kind=EntityKind.AGENT, name="export", origin=ProbeOrigin.EXPORT, sample_count=2,
        fields=[FieldNode(path="agent.name", types=["string"], required=True, occurrence=1.0)],
    )
    merged = inspector.merge_entities([api, export])[0]
    fields = {f.path: f for f in merged.fields}
    assert fields["name"].occurrence == round(33 / 35, 4)
    assert fields["agent.name"].occurrence == round(2 / 35, 4)
    # Neither is required: each was invisible to the other source.
    assert fields["name"].required is False
    assert fields["agent.name"].required is False

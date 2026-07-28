"""Generating the adapter, and the two things that stand in front of running it.

The emitted module is the artifact a migration actually executes ten thousand
times, so these tests are about its behaviour rather than its text: what it
does with an absent field, a null, a wrong type, an unmapped enum value. The
guard tests are about what it is not allowed to contain.

The generated code is executed in-process here, which is safe because it never
leaves this repository's own emitter -- and because `runtime.load` applies the
same guard before compiling it that the sandbox path does.
"""

import pytest

from agent_liftoff.foundry import cases, emit, guard, runtime
from agent_liftoff.foundry.shape import MISSING
from agent_liftoff.foundry.types import (
    AdapterArtifact,
    EntityKind,
    EntitySchema,
    FieldMapping,
    FieldNode,
    FlagReason,
    MappingSpec,
    ProbeOrigin,
    ReviewFlag,
    SandboxResult,
    TransformKind,
    Direction,
)


def _spec(*mappings, flags=(), platform="acme", kind=EntityKind.AGENT) -> MappingSpec:
    return MappingSpec(
        platform=platform,
        direction=Direction.IMPORT,
        entity_kind=kind,
        schema_fingerprint="fp0123456789abcdef",
        mappings=list(mappings),
        flags=list(flags),
    )


def _targets(*specs) -> EntitySchema:
    return EntitySchema(
        kind=EntityKind.AGENT,
        name="Agent",
        origin=ProbeOrigin.MODEL,
        fields=[FieldNode(path=path, types=list(types)) for path, types in specs],
    )


def _load(code: str):
    """Compile emitted code through the same path a cached adapter takes."""
    artifact = AdapterArtifact(
        key=_spec().key(),
        code=code,
        tests="",
        spec=_spec(),
        report=SandboxResult(ok=False),
    )
    return runtime.load(artifact, verified_only=False)


def _emit(spec, targets=None, implementations=None):
    code, holes = emit.emit_adapter(spec, targets, implementations)
    assert guard.check_source(code).ok, guard.check_source(code).summary()
    return _load(code), holes


# ----------------------------------------------------------------------
# What the generated code does
# ----------------------------------------------------------------------


def test_a_straight_rename_carries_the_value():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="name", source_paths=["bot.name"],
                           transform=TransformKind.RENAME))
    )
    assert adapter.transform({"bot": {"name": "HR Agent"}}) == {"name": "HR Agent"}


def test_a_field_with_no_source_is_omitted_rather_than_nulled():
    """Null is a value; a missing field is a missing field. Conflating them is
    how a migration silently overwrites data on the target.
    """
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="name", source_paths=["bot.name"],
                           transform=TransformKind.RENAME))
    )
    assert adapter.transform({}) == {}
    assert adapter.transform({"bot": {}}) == {}
    assert adapter.transform({"bot": {"name": None}}) == {}


def test_a_default_is_written_only_when_the_source_was_absent():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="style", source_paths=["mode"],
                           transform=TransformKind.RENAME, default="default"))
    )
    assert adapter.transform({}) == {"style": "default"}
    assert adapter.transform({"mode": "react"}) == {"style": "react"}


def test_a_constant_is_always_written():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="source_platform", transform=TransformKind.CONSTANT,
                           constant="copilot-studio"))
    )
    assert adapter.transform({}) == {"source_platform": "copilot-studio"}


def test_an_enum_value_with_no_translation_is_dropped_not_passed_through():
    """Passing an unrecognised value straight into a closed target vocabulary
    is how an agent imports cleanly and then behaves wrongly.
    """
    adapter, _ = _emit(
        _spec(
            FieldMapping(
                target_path="style",
                source_paths=["mode"],
                transform=TransformKind.ENUM_MAP,
                enum_map={"Reactive": "react", "Standard": "default"},
            )
        )
    )
    assert adapter.transform({"mode": "Reactive"}) == {"style": "react"}
    assert adapter.transform({"mode": "SomethingNew"}) == {}


def test_coercion_follows_the_targets_declared_type():
    adapter, _ = _emit(
        _spec(
            FieldMapping(target_path="tags", source_paths=["tag"], transform=TransformKind.COERCE),
            FieldMapping(target_path="count", source_paths=["n"], transform=TransformKind.COERCE),
            FieldMapping(target_path="label", source_paths=["x"], transform=TransformKind.COERCE),
        ),
        _targets(("tags", ["array"]), ("count", ["integer"]), ("label", ["string"])),
    )
    assert adapter.transform({"tag": "one"})["tags"] == ["one"]
    assert adapter.transform({"n": "42"})["count"] == 42
    assert adapter.transform({"x": True})["label"] == "true"


def test_a_coercion_that_cannot_be_done_omits_the_field():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="count", source_paths=["n"],
                           transform=TransformKind.COERCE)),
        _targets(("count", ["integer"])),
    )
    assert adapter.transform({"n": "not a number"}) == {}
    assert adapter.transform({"n": {"nested": 1}}) == {}


def test_joining_several_sources_skips_the_ones_that_are_absent():
    adapter, _ = _emit(
        _spec(
            FieldMapping(
                target_path="instructions",
                source_paths=["greeting", "body", "footer"],
                transform=TransformKind.JOIN,
            )
        )
    )
    assert adapter.transform({"greeting": "Hi.", "footer": "Bye."})["instructions"] == "Hi. Bye."
    assert adapter.transform({}) == {}


def _coalesce_adapter():
    adapter, _ = _emit(
        _spec(
            FieldMapping(
                target_path="name",
                source_paths=["bot.name", "botcomponent.name", "name"],
                transform=TransformKind.COALESCE,
            )
        )
    )
    return adapter


def test_coalesce_takes_the_first_source_that_is_present():
    """The transform for a corpus assembled from more than one shape of the
    same thing: an export and a live API both describe an agent, and they put
    its name in different places.
    """
    adapter = _coalesce_adapter()
    assert adapter.transform({"bot": {"name": "from the export"}}) == {"name": "from the export"}
    assert adapter.transform({"name": "from the api"}) == {"name": "from the api"}
    assert adapter.transform({"botcomponent": {"name": "from the component"}}) == {
        "name": "from the component"
    }


def test_coalesce_prefers_an_earlier_source_over_a_later_one():
    adapter = _coalesce_adapter()
    record = {"bot": {"name": "first"}, "botcomponent": {"name": "second"}, "name": "third"}
    assert adapter.transform(record) == {"name": "first"}


def test_coalesce_falls_through_an_empty_value_rather_than_settling_for_it():
    """An export that writes `<name/>` yields an empty string, not an absent
    field. Stopping there would carry the emptiness and discard the real name.
    """
    adapter = _coalesce_adapter()
    assert adapter.transform({"bot": {"name": ""}, "name": "real"}) == {"name": "real"}
    assert adapter.transform({"bot": {"name": None}, "name": "real"}) == {"name": "real"}


def test_coalesce_with_nothing_present_omits_the_field():
    assert _coalesce_adapter().transform({"unrelated": 1}) == {}


def test_an_array_of_items_is_collected_element_by_element():
    adapter, _ = _emit(
        _spec(
            FieldMapping(target_path="inputs[].name", source_paths=["schema.props[].id"],
                         transform=TransformKind.RENAME),
            FieldMapping(target_path="inputs[].description", source_paths=["schema.props[].help"],
                         transform=TransformKind.RENAME),
        )
    )
    out = adapter.transform(
        {"schema": {"props": [{"id": "table", "help": "which table"}, {"id": "sys_id"}]}}
    )
    assert out == {"inputs": [{"name": "table", "description": "which table"}, {"name": "sys_id"}]}


def test_an_empty_source_array_produces_an_empty_target_array():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="inputs[].name", source_paths=["props[].id"],
                           transform=TransformKind.RENAME))
    )
    assert adapter.transform({"props": []}) == {"inputs": []}


def test_a_whole_array_of_scalars_is_carried_as_a_list():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="starter_prompts", source_paths=["suggestions[]"],
                           transform=TransformKind.RENAME))
    )
    assert adapter.transform({"suggestions": ["a", "b"]}) == {"starter_prompts": ["a", "b"]}


def test_transform_never_raises_whatever_it_is_given():
    """An adapter runs unattended over ten thousand records. One that throws on
    record 4,001 halts the batch instead of flagging the record.
    """
    adapter, _ = _emit(
        _spec(
            FieldMapping(target_path="a", source_paths=["x.y.z"], transform=TransformKind.RENAME),
            FieldMapping(target_path="b", source_paths=["items[].k"],
                         transform=TransformKind.RENAME),
        )
    )
    for record in (None, [], "text", 42, {"x": "flat"}, {"items": "not-a-list"}, {"x": {"y": []}}):
        assert isinstance(adapter.transform(record), dict)


def test_transform_does_not_mutate_the_record_it_was_given():
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="name", source_paths=["bot.name"],
                           transform=TransformKind.RENAME))
    )
    record = {"bot": {"name": "x"}, "other": [1, 2]}
    adapter.transform(record)
    assert record == {"bot": {"name": "x"}, "other": [1, 2]}


def test_a_derived_field_with_no_implementation_raises_a_stub_and_is_omitted():
    """A stub that raises is the point: it fails the tests loudly, which drives
    the repair loop. A stub returning None would silently drop the field.
    """
    spec = _spec(
        FieldMapping(target_path="web_search", source_paths=["caps.browsing"],
                     transform=TransformKind.DERIVE, rationale="true when browsing is on"),
        FieldMapping(target_path="name", source_paths=["n"], transform=TransformKind.RENAME),
    )
    adapter, holes = _emit(spec)
    assert [h.target_path for h in holes] == ["web_search"]
    out = adapter.transform({"caps": {"browsing": True}, "n": "x"})
    assert out == {"name": "x"}  # the hole is omitted; the rest still works


def test_a_supplied_implementation_replaces_the_stub():
    spec = _spec(
        FieldMapping(target_path="web_search", source_paths=["caps.browsing"],
                     transform=TransformKind.DERIVE)
    )
    body = (
        "def _derive_web_search(record):\n"
        "    value = _get(record, 'caps.browsing')\n"
        "    return bool(value) if value is not _MISSING else _MISSING\n"
    )
    adapter, _ = _emit(spec, implementations={"web_search": body})
    assert adapter.transform({"caps": {"browsing": True}}) == {"web_search": True}
    assert adapter.transform({}) == {}


def test_deeply_nested_arrays_become_a_hole_rather_than_a_wrong_loop():
    """One loop over reads flattened across two levels would produce one long
    list where the target expects a list of lists. Admitting it is better.
    """
    spec = _spec(
        FieldMapping(target_path="topics[].nodes[].text", source_paths=["t[].n[].msg"],
                     transform=TransformKind.RENAME)
    )
    _, holes = _emit(spec)
    assert [h.target_path for h in holes] == ["topics[].nodes[].text"]


def test_two_different_source_arrays_feeding_one_target_becomes_a_hole():
    spec = _spec(
        FieldMapping(target_path="inputs[].name", source_paths=["a[].id"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="inputs[].type", source_paths=["b[].kind"],
                     transform=TransformKind.RENAME),
    )
    _, holes = _emit(spec)
    assert {h.target_path for h in holes} == {"inputs[].name", "inputs[].type"}


def test_per_record_flags_fire_only_when_the_field_is_actually_present():
    spec = _spec(
        FieldMapping(target_path="name", source_paths=["n"], transform=TransformKind.RENAME),
        flags=[
            ReviewFlag(path="secret", reason=FlagReason.REQUIRES_AUTH,
                       detail="configure this connection by hand", severity="warn"),
            ReviewFlag(path="legacy", reason=FlagReason.LOSSY, detail="dropped", severity="warn"),
        ],
    )
    adapter, _ = _emit(spec)
    assert adapter.flags({"n": "x"}) == []
    found = adapter.flags({"n": "x", "secret": "abc"})
    assert [f["path"] for f in found] == ["secret"]
    assert found[0]["reason"] == "requires_auth"
    # An empty value is not a finding: nothing was lost.
    assert adapter.flags({"legacy": ""}) == []


def test_static_review_flags_travel_with_the_module():
    spec = _spec(
        FieldMapping(target_path="name", source_paths=["n"], transform=TransformKind.RENAME),
        flags=[ReviewFlag(path="x", reason=FlagReason.LOSSY, detail="d", severity="info")],
    )
    adapter, _ = _emit(spec)
    assert adapter.review_flags[0]["path"] == "x"
    assert adapter.meta["schema_fingerprint"] == "fp0123456789abcdef"


def test_generating_the_same_spec_twice_produces_the_same_bytes():
    """Reproducibility is the reason codegen is deterministic rather than a
    model's job: two runs must be diffable, not merely equivalent.
    """
    spec = _spec(
        FieldMapping(target_path="b", source_paths=["y"], transform=TransformKind.RENAME),
        FieldMapping(target_path="a", source_paths=["x"], transform=TransformKind.RENAME),
    )
    first, _ = emit.emit_adapter(spec)
    second, _ = emit.emit_adapter(spec)
    assert first == second


def test_the_generated_module_imports_nothing():
    """That is what lets it run in a stock container with no packages, no
    network and no filesystem.
    """
    code, _ = emit.emit_adapter(
        _spec(FieldMapping(target_path="a", source_paths=["x"], transform=TransformKind.RENAME))
    )
    assert "\nimport " not in code
    assert "\nfrom " not in code


# ----------------------------------------------------------------------
# Reference semantics
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,want",
    [
        ("text", "string"), (3, "string"), (True, "string"), (None, "string"),
        (["a", "b"], "string"), ({"a": 1}, "string"),
        ("42", "integer"), ("4.5", "integer"), (7, "integer"), (True, "integer"),
        ("1.5", "number"), (2, "number"),
        ("yes", "boolean"), ("off", "boolean"), ("maybe", "boolean"), (1, "boolean"),
        ("x", "array"), (["x"], "array"), (None, "array"),
    ],
)
def test_the_test_expectations_agree_with_the_generated_code(value, want):
    """`cases.py` computes what the adapter should produce, and `emit.py`
    writes the code that produces it. They are separate implementations on
    purpose -- a reference that imported the thing under test would be no
    reference -- so the duplication is pinned here rather than hoped about.
    """
    adapter, _ = _emit(
        _spec(FieldMapping(target_path="out", source_paths=["v"], transform=TransformKind.COERCE)),
        _targets(("out", [want])),
    )
    produced = adapter.transform({"v": value})
    reference = cases.ref_coerce(value, want)
    if reference is MISSING or reference is None:
        assert produced == {}, f"{value!r} -> {want}: emitted {produced}, reference omits"
    else:
        assert produced == {"out": reference}


# ----------------------------------------------------------------------
# Derived cases
# ----------------------------------------------------------------------


def _case_pair():
    spec = _spec(
        FieldMapping(target_path="name", source_paths=["bot.name"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="source_platform", transform=TransformKind.CONSTANT,
                     constant="acme"),
    )
    source = EntitySchema(
        kind=EntityKind.AGENT, name="bot", origin=ProbeOrigin.EXPORT, sample_count=1,
        samples=[{"bot": {"name": "HR Agent"}, "extra": "kept"}],
    )
    return spec, source


def test_positive_cases_come_from_real_probed_records():
    spec, source = _case_pair()
    derived = cases.derive_cases(spec, source)
    positive = next(c for c in derived if c.name == "positive_sample_0")
    assert positive.expect_paths["name"] == "HR Agent"
    assert positive.expect_paths["source_platform"] == "acme"


def test_an_empty_record_expects_everything_optional_to_be_absent():
    spec, source = _case_pair()
    empty = next(c for c in cases.derive_cases(spec, source) if c.name == "negative_empty_record")
    assert "name" in empty.expect_absent
    # A constant does not depend on the source, so it is still expected.
    assert empty.expect_paths["source_platform"] == "acme"


def test_non_record_inputs_are_covered():
    spec, source = _case_pair()
    names = {c.name for c in cases.derive_cases(spec, source)}
    assert {"negative_not_a_dict_none", "negative_not_a_dict_list",
            "negative_not_a_dict_string", "negative_not_a_dict_number"} <= names


def test_edge_cases_cover_unicode_length_and_unknown_keys():
    spec, source = _case_pair()
    names = {c.name for c in cases.derive_cases(spec, source)}
    assert {"edge_unicode", "edge_very_long_strings", "edge_unknown_extra_keys",
            "edge_empty_values"} <= names


def test_derived_fields_get_no_value_expectation():
    """The spec does not say what a derived field should produce, only that it
    needs logic. Asserting a value would be inventing a requirement.
    """
    spec = _spec(
        FieldMapping(target_path="web_search", source_paths=["c.b"],
                     transform=TransformKind.DERIVE)
    )
    source = EntitySchema(kind=EntityKind.AGENT, name="s", origin=ProbeOrigin.EXPORT,
                          sample_count=1, samples=[{"c": {"b": True}}])
    for case in cases.derive_cases(spec, source):
        assert "web_search" not in case.expect_paths


def test_the_rendered_harness_is_a_runnable_module():
    spec, source = _case_pair()
    rendered = cases.render_tests(cases.derive_cases(spec, source), spec)
    compile(rendered, "test_adapter.py", "exec")
    assert "import adapter" in rendered
    assert "transform() mutated the record it was given" in rendered


# ----------------------------------------------------------------------
# The guard
# ----------------------------------------------------------------------


VALID = "def transform(record):\n    return {}\n\n\ndef flags(record):\n    return []\n"


def test_a_plain_mapping_module_is_clean():
    assert guard.check_source(VALID).ok


@pytest.mark.parametrize(
    "snippet",
    [
        "import os",
        "import socket",
        "import subprocess",
        "import requests",
        "from pathlib import Path",
        "import urllib.request",
        "from urllib.request import urlopen",
        "import importlib",
        "import ctypes",
        "import pickle",
    ],
)
def test_modules_an_adapter_has_no_business_importing_are_refused(snippet):
    report = guard.check_source(f"{snippet}\n{VALID}")
    assert not report.ok
    assert "not allowed" in report.summary()


@pytest.mark.parametrize("module", ["re", "json", "math", "datetime", "urllib.parse", "itertools"])
def test_pure_modules_a_derived_field_may_need_are_allowed(module):
    """`emit.py` writes code that imports nothing. This allowlist exists for
    the derived-field logic a model writes, where parsing a date genuinely
    needs help.
    """
    assert guard.check_source(f"import {module}\n{VALID}").ok


@pytest.mark.parametrize(
    "snippet",
    [
        "def transform(record):\n    return eval('1')\n",
        "def transform(record):\n    return open('/etc/passwd').read()\n",
        "def transform(record):\n    return exec('x=1')\n",
        "def transform(record):\n    return __import__('os')\n",
        "def transform(record):\n    return globals()\n",
        "def transform(record):\n    return compile('1', '', 'eval')\n",
    ],
)
def test_builtins_that_turn_data_into_code_or_io_are_refused(snippet):
    assert not guard.check_source(snippet + "\ndef flags(record):\n    return []\n").ok


def test_dunder_attribute_access_is_refused():
    """`().__class__.__base__.__subclasses__()` is the standard route from an
    allowed object to a forbidden one. No field mapping needs a dunder, so all
    of them go rather than a blocklist of known escape chains.
    """
    code = "def transform(record):\n    return type(record).__subclasses__()\n"
    report = guard.check_source(code + "\ndef flags(record):\n    return []\n")
    assert not report.ok
    assert "__subclasses__" in report.summary()


def test_with_blocks_and_async_are_refused():
    """A pure mapping has no resource to manage and nothing to await; their
    presence means something else is going on.
    """
    assert not guard.check_source(
        "def transform(record):\n    with record:\n        return {}\n"
        "\ndef flags(record):\n    return []\n"
    ).ok
    assert not guard.check_source(
        "async def transform(record):\n    return {}\n\ndef flags(record):\n    return []\n"
    ).ok


def test_an_adapter_that_does_not_answer_to_the_contract_is_refused():
    report = guard.check_source("def transform(record):\n    return {}\n")
    assert not report.ok
    assert "flags()" in report.summary()


def test_code_that_does_not_parse_is_reported_as_such_not_crashed_on():
    report = guard.check_source("def transform(record:\n")
    assert not report.ok
    assert "does not parse" in report.summary()


def test_an_oversized_generation_is_refused_before_it_is_parsed():
    report = guard.check_source("x = 1\n" * 200_000)
    assert not report.ok
    assert "exceeds" in report.summary()


def test_check_and_raise_is_the_load_path_gate():
    with pytest.raises(ValueError, match="failed the safety check"):
        guard.check_and_raise("import os\n" + VALID)


# ----------------------------------------------------------------------
# Per-mapping coverage
#
# Added after a real failure: a model rewrote an adapter, silently dropped five
# `constant` mappings, and the suite still passed -- because the sample records
# happened not to distinguish the two behaviours.
# ----------------------------------------------------------------------


def test_every_declared_mapping_gets_its_own_case():
    """Cases derived from samples test what the data exercises. These test what
    the spec declares, which is what the adapter is supposed to implement.
    """
    spec = _spec(
        FieldMapping(target_path="name", source_paths=["bot.name"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="vector_index.chunk_size", transform=TransformKind.CONSTANT,
                     constant=400),
        FieldMapping(target_path="style", source_paths=["mode"],
                     transform=TransformKind.ENUM_MAP, enum_map={"Reactive": "react"}),
    )
    names = {case.name for case in cases.derive_cases(spec)}
    assert {"mapping_name", "mapping_vector_index_chunk_size", "mapping_style"} <= names


def test_a_dropped_constant_mapping_fails_the_suite():
    """The exact regression: an adapter that ignores its `constant` mappings.

    The sample-derived cases pass it happily; the per-mapping case does not.
    """
    spec = _spec(
        FieldMapping(target_path="vector_index.chunk_size", transform=TransformKind.CONSTANT,
                     constant=400),
        FieldMapping(target_path="vector_index.top_k", transform=TransformKind.CONSTANT,
                     constant=10),
    )
    derived = cases.derive_cases(spec)
    coverage = [c for c in derived if c.name.startswith("mapping_")]
    assert len(coverage) == 2
    for case in coverage:
        assert case.expect_paths, f"{case.name} asserts nothing"

    # The correct adapter satisfies them...
    good, _ = _emit(spec)
    for case in coverage:
        out = good.transform(case.record)
        for path, want in case.expect_paths.items():
            assert cases.resolve_path(out, path) == want

    # ...and one that drops the constants does not.
    dropped = _load(
        "ADAPTER_META = {'schema_fingerprint': 'fp0123456789abcdef'}\n"
        "REVIEW_FLAGS = []\n"
        "def transform(record):\n"
        "    return {}\n"
        "\n\n"
        "def flags(record):\n"
        "    return []\n"
    )
    failures = [
        case.name
        for case in coverage
        if any(
            cases.resolve_path(dropped.transform(case.record), path) != want
            for path, want in case.expect_paths.items()
        )
    ]
    assert len(failures) == 2, "a dropped mapping must fail its own case"


def test_a_coverage_case_seeds_only_the_field_it_is_about():
    """A failure has to name one mapping, so the record must contain exactly
    that mapping's sources and nothing else.
    """
    spec = _spec(
        FieldMapping(target_path="name", source_paths=["bot.profile.name"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="description", source_paths=["bot.about"],
                     transform=TransformKind.RENAME),
    )
    by_name = {c.name: c for c in cases.derive_cases(spec)}
    assert by_name["mapping_name"].record == {"bot": {"profile": {"name": cases.PROBE_TEXT}}}
    assert by_name["mapping_description"].record == {"bot": {"about": cases.PROBE_TEXT}}


def test_a_coverage_case_covers_an_array_element_mapping():
    spec = _spec(
        FieldMapping(target_path="inputs[].name", source_paths=["schema.props[].id"],
                     transform=TransformKind.RENAME)
    )
    case = next(c for c in cases.derive_cases(spec) if c.name == "mapping_inputs_name")
    assert case.record == {"schema": {"props": [{"id": cases.PROBE_TEXT}]}}
    assert case.expect_paths == {"inputs[].name": [cases.PROBE_TEXT]}
    adapter, _ = _emit(spec)
    assert adapter.transform(case.record) == {"inputs": [{"name": cases.PROBE_TEXT}]}


def test_a_coverage_case_probes_an_enum_with_a_value_the_map_translates():
    """Probing with a value outside the map would assert nothing: an unmapped
    value is correctly dropped, so the case would pass on any adapter.
    """
    spec = _spec(
        FieldMapping(target_path="style", source_paths=["mode"],
                     transform=TransformKind.ENUM_MAP,
                     enum_map={"Reactive": "react", "Standard": "default"})
    )
    case = next(c for c in cases.derive_cases(spec) if c.name == "mapping_style")
    assert case.record == {"mode": "Reactive"}
    assert case.expect_paths == {"style": "react"}


def test_seeding_builds_the_minimum_structure_for_a_path():
    assert cases.seed_record("a", 1) == {"a": 1}
    assert cases.seed_record("a.b", 1) == {"a": {"b": 1}}
    assert cases.seed_record("tags[]", "x") == {"tags": ["x"]}
    assert cases.seed_record("a.b[].c", 1) == {"a": {"b": [{"c": 1}]}}
    assert cases.seed_record("a[].b[].c", 1) == {"a": [{"b": [{"c": 1}]}]}


def test_a_cross_wired_array_mapping_does_not_cost_the_whole_group():
    """`outputs[].description <- inputs[].description` is one wrong answer.
    Taking the group to hand-written holes because of it would lose the
    correct mappings beside it, which is a worse trade than rendering the
    array the name agrees with.
    """
    spec = _spec(
        FieldMapping(target_path="outputs[].name", source_paths=["data.outputs[].name"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="outputs[].description",
                     source_paths=["data.inputs[].description"],
                     transform=TransformKind.RENAME),
    )
    adapter, holes = _emit(spec)
    assert [h.target_path for h in holes] == ["outputs[].description"]
    out = adapter.transform({"data": {"outputs": [{"name": "result"}]}})
    assert out == {"outputs": [{"name": "result"}]}


def test_two_unrelated_source_arrays_still_have_nothing_to_choose_between():
    """Neither `a[]` nor `b[]` shares a word with `inputs[]`. A tiebreak that
    picked one anyway would be a coin flip compiled into a loop.
    """
    spec = _spec(
        FieldMapping(target_path="inputs[].name", source_paths=["a[].id"],
                     transform=TransformKind.RENAME),
        FieldMapping(target_path="inputs[].type", source_paths=["b[].kind"],
                     transform=TransformKind.RENAME),
    )
    _, holes = _emit(spec)
    assert {h.target_path for h in holes} == {"inputs[].name", "inputs[].type"}


# ----------------------------------------------------------------------
# Unfilled IR fields
# ----------------------------------------------------------------------


def _fill_targets():
    return EntitySchema(
        kind=EntityKind.AGENT,
        name="Agent",
        origin=ProbeOrigin.MODEL,
        fields=[
            FieldNode(path="name", types=["string"]),
            FieldNode(path="description", types=["null", "string"]),
            FieldNode(path="web_search", types=["boolean"]),
            FieldNode(path="starter_prompts", types=["array"]),
            FieldNode(path="style", types=["string"], enum=["react", "default"]),
        ],
    )


def _fill_spec(direction=Direction.IMPORT):
    return MappingSpec(
        platform="acme",
        direction=direction,
        entity_kind=EntityKind.AGENT,
        schema_fingerprint="fp0123456789abcdef",
        mappings=[
            FieldMapping(target_path="name", source_paths=["n"], transform=TransformKind.RENAME)
        ],
    )


def test_an_ir_field_with_no_counterpart_gets_its_types_empty_value():
    """One key must mean one thing whatever platform the record came from. A
    key that disappears on one platform and not another is not that.
    """
    adapter, _ = _emit(_fill_spec(), _fill_targets())
    out = adapter.transform({"n": "HR"})
    assert out == {"name": "HR", "description": "", "web_search": False, "starter_prompts": []}


def test_a_closed_vocabulary_is_never_filled_with_an_empty_string():
    """`""` is not one of `style`'s allowed values, so filling it would produce
    exactly the validation error the filling exists to avoid.
    """
    adapter, _ = _emit(_fill_spec(), _fill_targets())
    assert "style" not in adapter.transform({"n": "HR"})


def test_a_mapped_field_absent_from_this_record_still_says_absent():
    """That is a gap in the data, not a gap in the schema. Papering over it
    would deploy an agent named "".
    """
    adapter, _ = _emit(_fill_spec(), _fill_targets())
    assert "name" not in adapter.transform({})


def test_nothing_is_filled_on_the_way_out_of_the_ir():
    """A create call reads an empty string as "set this field to empty", so
    filling unmapped columns would overwrite the target's own defaults.
    """
    adapter, _ = _emit(_fill_spec(Direction.EXPORT), _fill_targets())
    assert adapter.transform({"n": "HR"}) == {"name": "HR"}


def test_a_mapping_the_emitter_could_not_render_gets_no_value_assertion():
    """The spec says `rename`; the code calls a hand-written function. The spec
    is no longer the authority on what comes out, so asserting its value would
    fail an adapter for doing the only thing it could.
    """
    spec = _spec(
        FieldMapping(target_path="label", source_paths=["a[].x"], transform=TransformKind.RENAME),
    )
    with_hole = cases.derive_cases(spec, holes={"label"})
    without = cases.derive_cases(spec)
    named = next(c for c in with_hole if c.name == "mapping_label")
    assert named.expect_paths == {}
    assert next(c for c in without if c.name == "mapping_label").expect_paths != {}

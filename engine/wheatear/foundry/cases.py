"""Derive the test suite a compiled adapter has to pass, from the spec itself.

The expectations here are *not* produced by running a reference implementation
and recording what it did -- that would only prove the adapter agrees with
itself. They are restatements of the mapping spec as assertions: "the spec says
`name` comes from `bot.name`, so given a record whose `bot.name` is X, the
output's `name` must be X". That is checkable independently of how the adapter
was written, which is what makes it a test rather than a snapshot.

Three families, all mechanically derived:

  **positive** -- the real records the inspector kept. The most valuable cases
  by a distance, because they are the actual shapes this corridor will see.
  **negative** -- empty, null, wrong-typed and non-dict input. These encode the
  contract that an adapter over ten thousand records flags and continues
  rather than raising and halting the batch.
  **edge** -- empty strings and arrays, unicode, very long values, unknown
  extra keys. The cases that separate an adapter that works from one that
  worked on the sample.

Derived mappings (`DERIVE`) get no value expectation, because the spec does not
say what they should produce -- only that they need logic. They still get the
no-raise and no-mutation assertions, and the Engineer can add real expectations
for them alongside the code it writes.

Nothing here calls a model, does I/O, or imports the generated adapter. It
produces `TestCase` objects and a rendered `unittest` module; running it is
`sandbox.py`'s job.
"""

from __future__ import annotations

import json
import re
from typing import Any

from wheatear.foundry.emit import array_prefix, container_path
from wheatear.foundry.shape import MISSING, resolve_path
from wheatear.foundry.types import (
    CaseKind,
    EntitySchema,
    FieldMapping,
    FieldNode,
    MappingSpec,
    TestCase,
    TransformKind,
)

# Sample records turned into positive cases. Beyond a handful the marginal
# value drops sharply -- they are variations on one shape -- while prompt and
# runtime cost keep rising.
MAX_POSITIVE = 8

LONG_STRING = "w" * 10_000
UNICODE_VALUE = "日本語 café 🌾 ​ naïve"

# Transforms whose output the spec fully determines, and which can therefore
# carry a value assertion.
PREDICTABLE = frozenset(
    {
        TransformKind.COPY,
        TransformKind.RENAME,
        TransformKind.CONSTANT,
        TransformKind.ENUM_MAP,
        TransformKind.COERCE,
        TransformKind.JOIN,
        TransformKind.COALESCE,
    }
)


# ----------------------------------------------------------------------
# Reference semantics
#
# These mirror the helpers `emit.py` writes into every adapter. The duplication
# is deliberate and is pinned by a test that runs both over the same table: a
# reference implementation that imported the thing under test would be no
# reference at all.
# ----------------------------------------------------------------------


def ref_text(value: Any) -> Any:
    if value is None:
        return MISSING
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        kept = [p for p in (ref_text(v) for v in value) if p is not MISSING]
        return ", ".join(kept) if kept else MISSING
    return MISSING


def ref_number(value: Any, want_int: bool) -> Any:
    if isinstance(value, bool) or value is None:
        return MISSING
    if isinstance(value, (int, float)):
        return int(value) if want_int else float(value)
    if isinstance(value, str):
        try:
            return int(value.strip()) if want_int else float(value.strip())
        except ValueError:
            return MISSING
    return MISSING


def ref_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True
        if lowered in ("false", "no", "0", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return MISSING


def ref_listed(value: Any) -> Any:
    if value is None:
        return MISSING
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def ref_coerce(value: Any, want: str | None) -> Any:
    if want == "array":
        return ref_listed(value)
    if want == "string":
        return ref_text(value)
    if want == "integer":
        return ref_number(value, True)
    if want == "number":
        return ref_number(value, False)
    if want == "boolean":
        return ref_boolean(value)
    return value if value is not None else MISSING


def _target_type(node: FieldNode | None) -> str | None:
    if node is None:
        return None
    concrete = [t for t in node.types if t != "null"]
    return concrete[0] if len(concrete) == 1 else None


def expected(mapping: FieldMapping, record: Any, target: FieldNode | None) -> Any:
    """What the spec says this mapping produces, or MISSING for omitted."""
    if mapping.transform is TransformKind.CONSTANT:
        return mapping.constant

    if mapping.transform is TransformKind.JOIN:
        parts = []
        for path in mapping.source_paths:
            text = ref_text(resolve_path(record, path, MISSING))
            if text is not MISSING and text != "":
                parts.append(text)
        return " ".join(parts) if parts else MISSING

    if mapping.transform is TransformKind.COALESCE:
        found: Any = MISSING
        for path in mapping.source_paths:
            if found is not MISSING and found is not None and found != "":
                break
            candidate = resolve_path(record, path, MISSING)
            if candidate is not MISSING:
                found = candidate
        return found

    source = mapping.source_paths[0] if mapping.source_paths else ""
    value = resolve_path(record, source, MISSING)
    if value is MISSING:
        return MISSING

    if mapping.transform is TransformKind.ENUM_MAP:
        key = ref_text(value)
        return mapping.enum_map.get(key, MISSING) if key is not MISSING else MISSING

    if mapping.transform is TransformKind.COERCE:
        want = _target_type(target)
        return ref_coerce(value, want) if want else value

    return value


# ----------------------------------------------------------------------
# Case derivation
# ----------------------------------------------------------------------


def _predictable(spec: MappingSpec) -> list[FieldMapping]:
    """Scalar mappings whose output the spec determines.

    Array-element mappings are excluded from value assertions: their target
    path addresses a position inside a list, and asserting on it would mean
    reimplementing the collection loop here.
    """
    return [
        m
        for m in spec.mappings
        if m.transform in PREDICTABLE and "[]" not in m.target_path.rstrip("[]")
    ]


def _settable(path: str) -> str:
    return path[:-2] if path.endswith("[]") else path


def _case_from_record(
    name: str,
    kind: CaseKind,
    record: Any,
    spec: MappingSpec,
    targets: dict[str, FieldNode],
    rationale: str,
    assert_values: bool = True,
) -> TestCase:
    """One case, with expectations computed from the spec for this record."""
    case = TestCase(name=name, kind=kind, record=record, rationale=rationale)
    if not assert_values:
        return case

    for mapping in _predictable(spec):
        value = expected(mapping, record, targets.get(mapping.target_path))
        target = _settable(mapping.target_path)
        if value is MISSING or value is None:
            case.expect_absent.append(target)
        else:
            case.expect_paths[target] = value
    return case


def _blank(record: Any) -> Any:
    """A copy of `record` with every leaf value replaced by None."""
    if isinstance(record, dict):
        return {k: _blank(v) for k, v in record.items()}
    if isinstance(record, list):
        return [_blank(v) for v in record]
    return None


def _retype(record: Any) -> Any:
    """A copy of `record` with every leaf replaced by an unexpected type.

    A dict is the useful wrong value: it is the one thing none of the coercion
    helpers can turn into anything, so it exercises every fallback path at once.
    """
    if isinstance(record, dict):
        return {k: _retype(v) for k, v in record.items()}
    if isinstance(record, list):
        return {"unexpected": "object where an array was"}
    return {"unexpected": True}


def _stretch(record: Any) -> Any:
    if isinstance(record, dict):
        return {k: _stretch(v) for k, v in record.items()}
    if isinstance(record, list):
        return [_stretch(v) for v in record]
    if isinstance(record, str):
        return LONG_STRING
    return record


def _unicode(record: Any) -> Any:
    if isinstance(record, dict):
        return {k: _unicode(v) for k, v in record.items()}
    if isinstance(record, list):
        return [_unicode(v) for v in record]
    if isinstance(record, str):
        return UNICODE_VALUE
    return record


def _empty(record: Any) -> Any:
    if isinstance(record, dict):
        return {k: _empty(v) for k, v in record.items()}
    if isinstance(record, list):
        return []
    if isinstance(record, str):
        return ""
    return record


# The value a synthetic probe writes into a source field. Distinctive enough
# that seeing it in an output identifies which case produced it.
PROBE_TEXT = "__wheatear_probe__"

_PROBE_BY_TYPE: dict[str, Any] = {
    "string": PROBE_TEXT,
    "integer": 4242,
    "number": 42.5,
    "boolean": True,
    "object": {"probe": PROBE_TEXT},
}


def seed_record(path: str, value: Any) -> Any:
    """The smallest record holding `value` at `path`.

    Built inside out, so `a.b[].c` becomes `{"a": {"b": [{"c": value}]}}` --
    the minimum structure that makes exactly one path resolvable and nothing
    else.
    """
    node = value
    for segment in reversed(path.split(".")):
        collect = segment.endswith("[]")
        key = segment[:-2] if collect else segment
        if collect:
            node = [node]
        if key:
            node = {key: node}
    return node


def _merge(left: Any, right: Any) -> Any:
    """Deep-merge two seeded records, for a mapping with several sources."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return right
    merged = dict(left)
    for key, value in right.items():
        merged[key] = _merge(merged[key], value) if key in merged else value
    return merged


def _probe_value(node: FieldNode | None, path: str) -> Any:
    """A value the source field could plausibly hold.

    Chosen from the field's own observed type so a coercion is exercised
    honestly: probing an integer field with a string would test the wrong
    thing. A path ending in `[]` gets a scalar, because `seed_record` supplies
    the list around it.
    """
    if path.endswith("[]"):
        return PROBE_TEXT
    concrete = [t for t in (node.types if node else []) if t != "null"]
    if concrete and concrete[0] == "array":
        return [PROBE_TEXT]
    return _PROBE_BY_TYPE.get(concrete[0] if concrete else "string", PROBE_TEXT)


def _case_name(target_path: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", target_path.lower()).strip("_")
    return f"mapping_{slug}"


def synthetic_cases(
    spec: MappingSpec,
    source_entity: EntitySchema | None = None,
    target_entity: EntitySchema | None = None,
    holes: set[str] | None = None,
) -> list[TestCase]:
    """One case per declared mapping, on a record built to exercise just it.

    This is the coverage guarantee, and it exists because of a real failure:
    a model rewrote an adapter, silently dropped five `constant` mappings, and
    the suite still passed -- because the sample records happened not to
    distinguish the two behaviours. Cases derived from samples test what the
    data exercises; these test what the *spec declares*, which is the thing the
    adapter is supposed to implement.

    Every mapping gets a record containing exactly its own source fields and
    nothing else, so a failure names precisely which mapping stopped working.
    """
    sources = {node.path: node for node in (source_entity.fields if source_entity else [])}
    targets = {node.path: node for node in (target_entity.fields if target_entity else [])}
    cases: list[TestCase] = []

    for mapping in spec.mappings:
        record: Any = {}
        if mapping.transform is TransformKind.ENUM_MAP and mapping.enum_map and mapping.source_paths:
            # Probe with a value the map actually translates; anything else is
            # correctly dropped and would assert nothing.
            record = seed_record(mapping.source_paths[0], sorted(mapping.enum_map)[0])
        elif mapping.transform is TransformKind.COALESCE and mapping.source_paths:
            # Seed only the *last* alternative. Seeding all of them would let an
            # adapter that reads nothing but the first source pass, which is
            # precisely the regression this transform exists to prevent.
            last = mapping.source_paths[-1]
            record = seed_record(last, _probe_value(sources.get(last), last))
        elif mapping.transform is not TransformKind.CONSTANT:
            for path in mapping.source_paths:
                record = _merge(record, seed_record(path, _probe_value(sources.get(path), path)))

        case = TestCase(
            name=_case_name(mapping.target_path),
            kind=CaseKind.POSITIVE,
            record=record,
            rationale=(
                f"The spec declares {mapping.target_path} <- "
                f"{', '.join(mapping.source_paths) or 'a constant'} "
                f"({mapping.transform.value}); this proves the adapter still does it."
            ),
        )

        prefix = array_prefix(mapping.target_path)
        # A mapping the emitter could not render mechanically does not do what
        # the spec's transform says -- it calls a hand-written function. The
        # spec is no longer the authority on its output, so the case proves the
        # adapter survives the record and asserts nothing about the value.
        predictable = mapping.transform in PREDICTABLE and mapping.target_path not in (holes or set())
        if predictable and prefix is None:
            value = expected(mapping, record, targets.get(mapping.target_path))
            if value is not MISSING and value is not None:
                case.expect_paths[container_path(mapping.target_path)] = value
        elif prefix is not None and predictable:
            # An array-element mapping: the harness reads `inputs[].name` as
            # every element's name, so a one-element seed expects a one-element
            # answer.
            value = _element_expectation(mapping, record, targets.get(mapping.target_path))
            if value is not MISSING and value is not None:
                case.expect_paths[mapping.target_path] = [value]
        cases.append(case)

    return cases


def _element_expectation(mapping: FieldMapping, record: Any, target: FieldNode | None) -> Any:
    """What one collected array element should hold.

    The generated loop reads each element with paths relative to the array, so
    the expectation has to be computed the same way: lift the seeded element
    out, and rebase the mapping's source paths onto it. Passing the full path
    against an element resolves to nothing and would silently assert nothing.
    """
    source_prefix = array_prefix(mapping.source_path or "")
    if source_prefix is None:
        return MISSING
    items = resolve_path(record, source_prefix, [])
    element = items[0] if isinstance(items, list) and items else {}
    rebased = mapping.model_copy(
        update={
            "source_paths": [
                path[len(source_prefix) :].lstrip(".") if path.startswith(source_prefix) else path
                for path in mapping.source_paths
            ]
        }
    )
    return expected(rebased, element, target)


def derive_cases(
    spec: MappingSpec,
    source_entity: EntitySchema | None = None,
    target_entity: EntitySchema | None = None,
    holes: set[str] | None = None,
) -> list[TestCase]:
    """The full derived suite for one mapping spec.

    `holes` names the mappings the emitter could not render from the template.
    They still get a coverage case -- the field must not crash the adapter --
    but no value assertion, because what they produce is whatever the Engineer
    wrote, not what the spec's transform describes.
    """
    targets = {node.path: node for node in (target_entity.fields if target_entity else [])}
    samples = list(source_entity.samples) if source_entity else []
    # Coverage first: one case per declared mapping, so no mapping can quietly
    # stop working just because no sample record happened to exercise it.
    cases: list[TestCase] = synthetic_cases(spec, source_entity, target_entity, holes)

    for index, sample in enumerate(samples[:MAX_POSITIVE]):
        cases.append(
            _case_from_record(
                f"positive_sample_{index}",
                CaseKind.POSITIVE,
                sample,
                spec,
                targets,
                "A real record from the probed platform.",
            )
        )

    # --- negative -----------------------------------------------------
    cases.append(
        _case_from_record(
            "negative_empty_record",
            CaseKind.NEGATIVE,
            {},
            spec,
            targets,
            "An empty record: every non-constant target must be omitted, not nulled.",
        )
    )
    for record, label in (
        (None, "none"),
        ([], "list"),
        ("a string", "string"),
        (42, "number"),
    ):
        cases.append(
            TestCase(
                name=f"negative_not_a_dict_{label}",
                kind=CaseKind.NEGATIVE,
                record=record,
                rationale="A non-record input must return an empty dict, not raise.",
                expect_absent=[_settable(m.target_path) for m in _predictable(spec)],
            )
        )

    if samples:
        base = samples[0]
        cases.append(
            _case_from_record(
                "negative_all_values_null",
                CaseKind.NEGATIVE,
                _blank(base),
                spec,
                targets,
                "Every field present but null: targets must be omitted, not set to null.",
            )
        )
        cases.append(
            _case_from_record(
                "negative_wrong_types",
                CaseKind.NEGATIVE,
                _retype(base),
                spec,
                targets,
                "Every leaf replaced by an object. Must not raise.",
                assert_values=False,
            )
        )

        # --- edge -----------------------------------------------------
        cases.append(
            _case_from_record(
                "edge_empty_values",
                CaseKind.EDGE,
                _empty(base),
                spec,
                targets,
                "Empty strings and empty arrays: an empty value is a value, not an absence.",
            )
        )
        cases.append(
            _case_from_record(
                "edge_unicode",
                CaseKind.EDGE,
                _unicode(base),
                spec,
                targets,
                "Non-ASCII, emoji and a zero-width space must survive unchanged.",
            )
        )
        cases.append(
            _case_from_record(
                "edge_very_long_strings",
                CaseKind.EDGE,
                _stretch(base),
                spec,
                targets,
                "10k-character values must not be truncated or refused.",
            )
        )
        extra = dict(base) if isinstance(base, dict) else {}
        extra["__wheatear_unknown__"] = {"added": ["by", "a", "newer", "platform", "version"]}
        cases.append(
            _case_from_record(
                "edge_unknown_extra_keys",
                CaseKind.EDGE,
                extra,
                spec,
                targets,
                "A field the platform added since the probe must be ignored, not fatal.",
            )
        )

    return cases


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

HARNESS = '''"""Generated tests for a Wheatear foundry adapter.

Every case asserts three things beyond its own expectations: that transform()
does not raise, that it returns a dict, and that it does not mutate the record
it was given. Those three are the contract that makes an adapter safe to run
unattended over a whole tenant.
"""

import copy
import json
import unittest

import adapter

MISSING = object()

CASES = json.loads({cases})
FINGERPRINT = {fingerprint}


def get(record, path):
    if not path:
        return record
    current = record
    for segment in path.split("."):
        collect = segment.endswith("[]")
        key = segment[:-2] if collect else segment
        if key:
            if isinstance(current, list):
                current = [i.get(key) for i in current if isinstance(i, dict)]
            elif isinstance(current, dict):
                if key not in current:
                    return MISSING
                current = current[key]
            else:
                return MISSING
        if collect:
            if not isinstance(current, list):
                return MISSING
            flat = []
            for item in current:
                flat.extend(item) if isinstance(item, list) else flat.append(item)
            current = flat
    return current


TYPE_NAMES = {{
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}}


class AdapterContract(unittest.TestCase):
    """Properties that hold for every record, whatever the mapping says."""

    def test_metadata_matches_the_spec_it_was_built_from(self):
        self.assertEqual(adapter.ADAPTER_META.get("schema_fingerprint"), FINGERPRINT)

    def test_non_records_return_an_empty_dict(self):
        for garbage in (None, [], "text", 42, 3.5, True):
            self.assertEqual(adapter.transform(garbage), {{}})

    def test_flags_always_returns_a_list(self):
        for garbage in (None, [], "text", 42, {{}}):
            self.assertIsInstance(adapter.flags(garbage), list)


class GeneratedCases(unittest.TestCase):
    def _run_case(self, case):
        record = case["record"]
        before = json.dumps(record, sort_keys=True, default=str)
        try:
            out = adapter.transform(record)
        except Exception as exc:
            self.fail("transform() raised %s: %s" % (type(exc).__name__, exc))
        self.assertIsInstance(out, dict, "transform() must return a dict")

        after = json.dumps(record, sort_keys=True, default=str)
        self.assertEqual(before, after, "transform() mutated the record it was given")

        for path, want in case["expect_paths"].items():
            actual = get(out, path)
            self.assertIsNot(actual, MISSING, "%s should be present, output was %r" % (path, out))
            self.assertEqual(actual, want, "%s: expected %r, got %r" % (path, want, actual))

        for path in case["expect_absent"]:
            actual = get(out, path)
            self.assertTrue(
                actual is MISSING or actual is None,
                "%s should be absent (the source had no value), got %r" % (path, actual),
            )

        for path, name in case.get("expect_types", {{}}).items():
            actual = get(out, path)
            self.assertIsNot(actual, MISSING, "%s should be present" % path)
            self.assertIsInstance(actual, TYPE_NAMES[name], "%s should be a %s" % (path, name))

        for path, length in case.get("expect_lengths", {{}}).items():
            actual = get(out, path)
            self.assertIsNot(actual, MISSING, "%s should be present" % path)
            self.assertEqual(len(actual), length, "%s should hold %d item(s)" % (path, length))

        try:
            found = adapter.flags(record)
        except Exception as exc:
            self.fail("flags() raised %s: %s" % (type(exc).__name__, exc))
        self.assertIsInstance(found, list)
        for entry in found:
            self.assertIn("path", entry)
            self.assertIn("severity", entry)


def _bind(index, case):
    def test(self):
        self._run_case(copy.deepcopy(CASES[index]))

    test.__name__ = "test_" + case["name"]
    test.__doc__ = case.get("rationale") or case["name"]
    return test


for _index, _case in enumerate(CASES):
    setattr(GeneratedCases, "test_" + _case["name"], _bind(_index, _case))


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''


def render_tests(cases: list[TestCase], spec: MappingSpec) -> str:
    """Render the derived cases as a runnable stdlib `unittest` module.

    The harness is generated by Wheatear rather than by a model, even when a
    model contributed cases. Cases are data -- a record and its expectations --
    and keeping the code that runs them ours means a model can extend the suite
    without being able to weaken it.
    """
    payload = json.dumps(
        [case.model_dump(mode="json") for case in cases], ensure_ascii=False, sort_keys=True
    )
    return HARNESS.format(cases=repr(payload), fingerprint=repr(spec.schema_fingerprint))

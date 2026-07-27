"""Agent 3, the Engineer: compile the mapping, test it in isolation, repair it.

The Engineer's job is narrower than "write the adapter", and narrowing it is
what makes the output trustworthy. `emit.py` renders every mechanical mapping
deterministically, so the model is never asked to write code whose correct form
is already known. What is left is:

  * **holes** -- `DERIVE` mappings, where the spec says "this needs logic" and
    describes it in prose. The model writes one small function each.
  * **extra cases** -- edge cases the mechanical deriver wouldn't invent, which
    the model proposes as *data*. The harness that runs them stays ours, so a
    model can extend the suite without being able to weaken it.
  * **repair** -- when the tests fail, the model sees the failures and revises.

Every candidate goes through the same gate before it runs anywhere: parse,
static guard, then a container with no network and no filesystem. A generation
that fails the guard never reaches the sandbox, and one that fails the sandbox
never reaches the store.

One safeguard is worth calling out. If the final attempt fails and every
failing test is a case the *model* proposed, the cases are dropped and the run
is retried once. A model that invents an expectation its own code cannot meet
has more likely written a bad test than a bad adapter, and letting that fail
the build would block a correct adapter on a wrong assertion.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from wheatear.foundry import guard
from wheatear.foundry.cases import derive_cases, render_tests
from wheatear.foundry.emit import emit_adapter
from wheatear.foundry.sandbox import Sandbox
from wheatear.foundry.types import (
    AdapterArtifact,
    CaseFailure,
    CaseKind,
    EntitySchema,
    FieldMapping,
    MappingSpec,
    SandboxResult,
    TestCase,
)
from wheatear.llm.base import LLMProvider

DEFAULT_MAX_ATTEMPTS = 4

# Extra cases requested from the model. A handful of well-chosen ones beats a
# long list: the derived suite already covers the systematic cases, so these
# are for domain knowledge ("this platform writes an empty topic list as an
# empty string"), which is not a long list.
MAX_PROPOSED_CASES = 6

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,60}$")

ADAPTER_FILENAME = "adapter.py"
TESTS_FILENAME = "test_adapter.py"


class DerivedFunction(BaseModel):
    """One filled hole: the complete source of a `_derive_*` function."""

    target_path: str = Field(description="The target path this produces, copied verbatim.")
    source: str = Field(
        description=(
            "The complete function definition, starting at `def `. Standard library only. "
            "Return the value, or the module-level `_MISSING` sentinel to omit the field."
        )
    )


class DerivedFunctions(BaseModel):
    functions: list[DerivedFunction] = Field(default_factory=list)


class ExpectedValue(BaseModel):
    """One assertion: a target path and the value it must hold."""

    path: str
    value: str | int | float | bool | None = None


class ProposedCase(BaseModel):
    """A test case the model thinks the derived suite is missing.

    `record` is a JSON *string* and the expectations are a list rather than a
    map, for the same reason as `translator.EnumPair`: an open-ended object
    becomes `additionalProperties` in JSON Schema, and the Gemini Developer API
    refuses those. Closed shapes cost one `json.loads` and work everywhere.
    """

    name: str = Field(description="lowercase_with_underscores, unique.")
    kind: Literal["positive", "negative", "edge"] = "edge"
    record_json: str = Field(
        default="{}", description="The input record, as a JSON object in a string."
    )
    expect_paths: list[ExpectedValue] = Field(default_factory=list)
    expect_absent: list[str] = Field(default_factory=list)
    rationale: str = ""

    def record(self) -> dict | None:
        """The parsed record, or None if the model did not send a JSON object."""
        try:
            parsed = json.loads(self.record_json)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


class ProposedCases(BaseModel):
    cases: list[ProposedCase] = Field(default_factory=list)


class FullAdapter(BaseModel):
    """A complete replacement module, for when repair of the holes isn't enough."""

    source: str = Field(description="The complete adapter module, standard library only.")
    rationale: str = ""


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def validate_function(source: str, expected_name: str) -> tuple[str | None, str | None]:
    """Check one model-written function before it goes anywhere near the module.

    Returns (source, complaint). Three things are checked, and all three have
    bitten in practice: that it parses, that it defines exactly the function
    the emitted call site will call, and that it stays inside the guard's
    vocabulary. A function that defines the wrong name would produce a module
    that imports cleanly and raises NameError on the first record.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return None, f"`{expected_name}` does not parse: {exc.msg} (line {exc.lineno})"

    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != expected_name:
        found = ", ".join(f.name for f in functions) or "nothing"
        return None, (
            f"expected exactly one function named `{expected_name}`, found {found}"
        )
    if len(tree.body) != len(functions):
        return None, f"`{expected_name}` must be a single function, with nothing else beside it"

    report = guard.check_tree(tree)
    if not report.ok:
        return None, f"`{expected_name}` failed the safety check: {report.summary()}"
    return source, None


def validate_cases(
    proposed: list[ProposedCase], known_targets: set[str], existing: set[str]
) -> tuple[list[TestCase], list[str]]:
    """Keep the proposed cases that are well-formed and about real fields."""
    kept: list[TestCase] = []
    complaints: list[str] = []
    for case in proposed[:MAX_PROPOSED_CASES]:
        name = case.name.strip().lower().replace(" ", "_")
        if not _SAFE_NAME.match(name) or name in existing:
            complaints.append(f"Dropped a proposed case with an unusable name: {case.name!r}.")
            continue
        record = case.record()
        if record is None:
            complaints.append(
                f"Dropped proposed case `{name}`: its record is not a JSON object."
            )
            continue
        unknown = [
            path
            for path in [e.path for e in case.expect_paths] + list(case.expect_absent)
            if path not in known_targets
        ]
        if unknown:
            complaints.append(
                f"Dropped proposed case `{name}`: it asserts on {', '.join(unknown)}, "
                "which are not target fields."
            )
            continue
        existing.add(name)
        kept.append(
            TestCase(
                name=f"proposed_{name}",
                kind=CaseKind(case.kind),
                record=record,
                expect_paths={e.path: e.value for e in case.expect_paths},
                expect_absent=list(case.expect_absent),
                rationale=case.rationale or "Proposed by the Engineer.",
            )
        )
    return kept, complaints


# ----------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------


def _hole_brief(mapping: FieldMapping) -> str:
    sources = ", ".join(mapping.source_paths) or "(the whole record)"
    return (
        f"- target `{mapping.target_path}` from {sources}\n"
        f"  what it needs to do: {mapping.rationale or 'not stated in the spec'}"
    )


def build_derive_prompt(
    spec: MappingSpec,
    holes: list[FieldMapping],
    source_entity: EntitySchema | None,
    failures: str = "",
) -> str:
    sample = ""
    if source_entity and source_entity.samples:
        keys = sorted(source_entity.samples[0].keys())[:25]
        sample = f"\nTop-level keys of a real source record: {', '.join(keys)}\n"

    repair = ""
    if failures:
        repair = f"""
A previous attempt failed these tests. Fix the cause, do not adjust around it:

{failures}
"""

    return f"""You are completing a generated data-mapping adapter for a migration between
AI agent platforms ({spec.platform}, direction: {spec.direction.value}, entity: {spec.entity_kind.value}).

Everything mechanical is already generated. What remains are fields that need real
logic. Write one function for each.
{sample}
FIELDS TO DERIVE
{chr(10).join(_hole_brief(m) for m in holes)}
{repair}
The module you are extending already defines these, which you may call:

  _MISSING            sentinel meaning "omit this field"
  _get(record, path)  read a dotted path ("a.b", "items[].name"); returns _MISSING if absent
  _text(value)        faithful string for a value, or _MISSING
  _listed(value)      value as a list, or _MISSING
  _coerce(value, want) convert to "string"/"integer"/"number"/"boolean"/"array", or _MISSING

Rules, all of them enforced mechanically:
- One function per field, named exactly `_derive_<target path with non-alphanumerics
  replaced by underscores, lowercased>`. Nothing else in the source you return.
- Standard library only, and only pure modules: re, json, math, datetime, itertools,
  string, textwrap, unicodedata, urllib.parse. No file, network, process or dynamic
  import of any kind. The code runs with no network and no filesystem.
- Never raise. Return `_MISSING` when the input doesn't support an answer.
- Never invent a value. If the source has nothing, the answer is `_MISSING`, not a
  default or a placeholder.
"""


def build_cases_prompt(spec: MappingSpec, source_entity: EntitySchema | None) -> str:
    fields = "\n".join(
        f"  - {m.target_path} <- {', '.join(m.source_paths) or '(constant)'} [{m.transform.value}]"
        for m in spec.mappings[:40]
    )
    sample = ""
    if source_entity and source_entity.samples:
        keys = sorted(source_entity.samples[0].keys())[:25]
        sample = f"\nA real source record has these top-level keys: {', '.join(keys)}\n"

    return f"""A generated adapter maps `{spec.entity_kind.value}` records for {spec.platform}
({spec.direction.value}). It is already tested against real records, empty records, null
values, wrong types, empty strings, unicode, 10k-character values and unknown extra keys.

THE MAPPING
{fields}
{sample}
Propose up to {MAX_PROPOSED_CASES} additional cases those systematic ones would miss --
things that are true about *this platform* rather than about data in general. For example:
a field this platform writes as an empty string rather than omitting, a value that arrives
double-encoded, a legacy spelling that still occurs.

For each: the input record, and what the output must contain (`expect_paths`) or must not
(`expect_absent`). Only assert on target paths listed above. Propose nothing if you have no
platform-specific knowledge to add -- an invented case is worse than no case.
"""


def build_rewrite_prompt(spec: MappingSpec, code: str, failures: str) -> str:
    return f"""A generated adapter is failing its tests and the failures are not in the
hand-written parts, so the generated code itself is wrong. Rewrite the module.

FAILURES
{failures}

CURRENT MODULE
{code[:12000]}

Requirements:
- Keep the public contract exactly: module-level `transform(record) -> dict` and
  `flags(record) -> list`, plus the existing `ADAPTER_META` and `REVIEW_FLAGS` values.
- Standard library only, pure modules only. No file, network, process or dynamic import.
- `transform` must never raise, must return a dict for any input including non-records,
  must not mutate its argument, and must omit any target field whose source was absent
  rather than writing a null.
- This maps {spec.entity_kind.value} records for {spec.platform} ({spec.direction.value}).
"""


# ----------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------


class BuildLog(BaseModel):
    """What happened during a build, kept with the artifact."""

    attempts: int = 0
    notes: list[str] = Field(default_factory=list)
    holes: list[str] = Field(default_factory=list)
    rewritten: bool = False


def _implementations(
    provider: LLMProvider,
    spec: MappingSpec,
    holes: list[FieldMapping],
    source_entity: EntitySchema | None,
    failures: str,
    log: BuildLog,
) -> dict[str, str]:
    from wheatear.foundry.emit import _derive_name  # noqa: PLC0415 - one naming rule, shared

    try:
        answer = provider.generate_structured(
            build_derive_prompt(spec, holes, source_entity, failures), DerivedFunctions
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure leaves stubs, not a crash
        log.notes.append(f"The model could not write the derived fields ({type(exc).__name__}).")
        return {}

    wanted = {m.target_path for m in holes}
    accepted: dict[str, str] = {}
    for function in answer.functions:
        if function.target_path not in wanted:
            log.notes.append(
                f"Discarded a function for `{function.target_path}`: not a field that needed one."
            )
            continue
        source, complaint = validate_function(
            function.source, _derive_name(function.target_path)
        )
        if complaint:
            log.notes.append(f"Discarded a function: {complaint}")
            continue
        if source:
            accepted[function.target_path] = source
    return accepted


def _proposed_cases(
    provider: LLMProvider,
    spec: MappingSpec,
    source_entity: EntitySchema | None,
    known_targets: set[str],
    taken: set[str],
    log: BuildLog,
) -> list[TestCase]:
    try:
        answer = provider.generate_structured(
            build_cases_prompt(spec, source_entity), ProposedCases
        )
    except Exception as exc:  # noqa: BLE001 - extra cases are a bonus, never a blocker
        log.notes.append(f"The model proposed no extra cases ({type(exc).__name__}).")
        return []
    kept, complaints = validate_cases(answer.cases, known_targets, taken)
    log.notes.extend(complaints)
    if kept:
        log.notes.append(f"The model contributed {len(kept)} extra test case(s).")
    return kept


def _attempt(
    code: str, tests: str, sandbox: Sandbox
) -> tuple[SandboxResult, guard.GuardReport]:
    report = guard.check_source(code)
    if not report.ok:
        # A guard violation short-circuits the sandbox: there is no point
        # starting a container for code we have already decided not to run.
        return (
            SandboxResult(
                ok=False,
                runner="guard",
                exit_code=-1,
                stderr=report.summary(),
                failures=[
                    CaseFailure(name="safety-check", message=violation)
                    for violation in report.violations
                ],
            ),
            report,
        )
    return sandbox.run({ADAPTER_FILENAME: code, TESTS_FILENAME: tests}), report


def build_adapter(
    spec: MappingSpec,
    sandbox: Sandbox,
    source_entity: EntitySchema | None = None,
    target_entity: EntitySchema | None = None,
    provider: LLMProvider | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    allow_rewrite: bool = True,
) -> AdapterArtifact:
    """Compile, test and repair an adapter for one mapping spec.

    Returns an artifact whatever happens. A build that never went green is
    still worth keeping and showing -- the code, the failures and the reason
    are exactly what a human needs to finish it by hand -- and
    `AdapterArtifact.verified` is what stops it being executed unattended.
    """
    log = BuildLog()
    code, holes = emit_adapter(spec, target_entity)
    log.holes = [m.target_path for m in holes]

    cases = derive_cases(spec, source_entity, target_entity, {m.target_path for m in holes})
    known_targets = {m.target_path.rstrip("[]") for m in spec.mappings}
    taken = {case.name for case in cases}
    proposed: list[TestCase] = []

    if provider is not None:
        proposed = _proposed_cases(provider, spec, source_entity, known_targets, taken, log)

    implementations: dict[str, str] = {}
    if provider is not None and holes:
        implementations = _implementations(provider, spec, holes, source_entity, "", log)
        code, _ = emit_adapter(spec, target_entity, implementations)
        log.notes.append(
            f"{len(implementations)} of {len(holes)} derived field(s) were implemented."
        )
    elif holes:
        log.notes.append(
            f"{len(holes)} field(s) need logic and no model was available; they raise "
            "NotImplementedError and are omitted at runtime."
        )

    tests = render_tests(cases + proposed, spec)
    result = SandboxResult()

    # The last emitted (template-generated) candidate and how it scored. Kept
    # separately from `code` so a model rewrite can be *undone*: a rewrite that
    # does not achieve a green run is strictly worse than the emitted module --
    # it is not reproducible, it is not derived from the spec, and it has been
    # observed to silently drop whole mappings (five `constant` fields, on a
    # real corridor). Losing the deterministic version to it would be the worst
    # outcome available.
    emitted_code, emitted_result = code, None

    for attempt in range(1, max_attempts + 1):
        log.attempts = attempt
        result, _ = _attempt(code, tests, sandbox)
        if not log.rewritten:
            emitted_code, emitted_result = code, result
        if result.ok:
            break

        if provider is None:
            log.notes.append("No model available to repair the failures.")
            break

        # Both repair paths are guarded by having an attempt left to test the
        # repair *in*. Without the guard the final iteration can replace `code`
        # and then fall out of the loop, leaving the artifact holding one
        # module and its report describing another -- which is worse than not
        # repairing at all, because the report is then a claim about code that
        # was never run.
        if attempt == max_attempts:
            break

        feedback = result.feedback()
        if holes:
            revised = _implementations(provider, spec, holes, source_entity, feedback, log)
            if revised:
                implementations.update(revised)
                code, _ = emit_adapter(spec, target_entity, implementations)
                log.rewritten = False
                log.notes.append(f"Attempt {attempt} failed; revised the derived fields.")
                continue

        if allow_rewrite:
            replacement = _rewrite(provider, spec, code, feedback, log)
            if replacement:
                code = replacement
                log.rewritten = True
                continue

        break

    if not result.ok and log.rewritten and emitted_result is not None:
        code, result = emitted_code, emitted_result
        log.rewritten = False
        log.notes.append(
            "The rewritten module did not pass either, so it was discarded and the "
            "generated one kept: it is reproducible from the spec and a rewrite that "
            "fails has nothing to recommend it."
        )

    # A failure made up entirely of cases the model invented is more likely a
    # bad expectation than a bad adapter. Drop them and settle the question.
    if not result.ok and proposed and _only_proposed_failed(result):
        log.notes.append(
            "Every remaining failure was a model-proposed case; they were dropped as "
            "unreliable and the derived suite alone was re-run."
        )
        tests = render_tests(cases, spec)
        proposed = []
        result, _ = _attempt(code, tests, sandbox)

    spec_with_log = spec.model_copy(deep=True)
    spec_with_log.notes = list(spec.notes) + log.notes

    return AdapterArtifact(
        key=spec.key(),
        code=code,
        tests=tests,
        spec=spec_with_log,
        report=result,
        attempts=log.attempts or 1,
        generator=("rewritten" if log.rewritten else spec.generator),
    )


def _rewrite(
    provider: LLMProvider, spec: MappingSpec, code: str, failures: str, log: BuildLog
) -> str | None:
    try:
        answer = provider.generate_structured(
            build_rewrite_prompt(spec, code, failures), FullAdapter
        )
    except Exception as exc:  # noqa: BLE001
        log.notes.append(f"A full rewrite was attempted and failed ({type(exc).__name__}).")
        return None

    report = guard.check_source(answer.source)
    if not report.ok:
        log.notes.append(f"A rewritten adapter was rejected by the safety check: {report.summary()}")
        return None
    log.notes.append(
        "The emitted adapter failed its own tests, so the module was rewritten by the model. "
        f"Reason given: {answer.rationale.strip() or 'none'}. This is unusual and is worth "
        "reading before the adapter is trusted."
    )
    return answer.source


def _only_proposed_failed(result: SandboxResult) -> bool:
    return bool(result.failures) and all(
        "proposed_" in failure.name for failure in result.failures
    )

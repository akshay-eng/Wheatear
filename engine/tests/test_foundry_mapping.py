"""Correlating two schemas: the deterministic pass, and the model's part of it.

The interesting property throughout is restraint. The aligner accepts a match
only when it is unambiguous, the translator discards anything the model
returns that isn't in the schema, and "no counterpart" is a first-class answer
rather than a failure -- because a wrong mapping compiles into code that runs
over every record in the migration.
"""

from wheatear.foundry import align, translator
from wheatear.foundry.translator import FieldDecision, FieldDecisions
from wheatear.ir.schema import IR_SPEC_VERSION
from wheatear.foundry.types import (
    Direction,
    EntityKind,
    EntitySchema,
    FieldNode,
    FlagReason,
    ProbeOrigin,
    TransformKind,
)


def _fields(*specs) -> list[FieldNode]:
    out = []
    for spec in specs:
        path, types = spec[0], spec[1]
        required = spec[2] if len(spec) > 2 else False
        enum = spec[3] if len(spec) > 3 else []
        out.append(
            FieldNode(
                path=path,
                types=list(types),
                required=required,
                occurrence=1.0,
                enum=list(enum),
                container=path.endswith("[]") is False and "array" in types,
            )
        )
    return out


def _entity(kind, name, fields, samples=None) -> EntitySchema:
    return EntitySchema(
        kind=kind,
        name=name,
        origin=ProbeOrigin.EXPORT,
        sample_count=len(samples or []),
        fields=fields,
        samples=list(samples or []),
    )


class FakeProvider:
    """Returns scripted decisions and records the prompts it was given."""

    def __init__(self, decisions=None):
        self.decisions = decisions or []
        self.prompts: list[str] = []

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return FieldDecisions(decisions=list(self.decisions))


# ----------------------------------------------------------------------
# Alignment
# ----------------------------------------------------------------------


def test_an_unambiguous_leaf_name_match_needs_no_model():
    """Most of two agent schemas correspond obviously. Spending a model call on
    `description` -> `description` would be pure cost.
    """
    sources = _fields(("data.description", ["string"]), ("data.owner", ["string"]))
    targets = _fields(("description", ["string"]))
    result = align.align(targets, sources)[0]
    assert result.certain is True
    assert result.source is not None
    assert result.source.path == "data.description"
    assert result.transform is TransformKind.RENAME


def test_two_candidates_with_the_same_leaf_name_are_left_to_the_model():
    """`bot.name` and `botcomponent.name` both claim `name` equally. Picking
    whichever sorted first would be a coin flip written into generated code.
    """
    sources = _fields(("bot.name", ["string"]), ("botcomponent.name", ["string"]))
    targets = _fields(("name", ["string"], True))
    result = align.align(targets, sources)[0]
    assert result.certain is False
    assert len(result.candidates) == 2


def test_plurals_fold_onto_their_singular():
    """The two platforms pluralise the same capability inconsistently."""
    assert align.leaf_tokens("data.tools") == align.leaf_tokens("tool")
    assert align.tokenize("Trigger Phrases") == align.tokenize("trigger_phrase")


def test_singularising_does_not_corrupt_words_that_end_in_s():
    assert align.tokenize("status") == ["status"]
    assert align.tokenize("address") == ["address"]
    assert align.tokenize("analysis") == ["analysis"]


def test_a_type_mismatch_costs_a_candidate_without_disqualifying_it():
    """A platform storing a count as a string is ordinary. It is evidence, not
    a veto.
    """
    string_source = _fields(("count", ["string"]))
    target = _fields(("count", ["integer"]))[0]
    idf = align._idf(string_source)
    scored = align.rank(target, string_source, idf, 8)
    assert scored, "a type mismatch must not remove the candidate entirely"
    assert align.types_compatible(string_source[0], target) is True  # both scalar


def test_an_array_and_a_boolean_are_not_compatible():
    assert (
        align.types_compatible(_fields(("a", ["boolean"]))[0], _fields(("b", ["array"]))[0])
        is False
    )


def test_container_fields_are_never_aligned():
    """An object node carries no value of its own; mapping it would overwrite
    the fields nested under it.
    """
    targets = [FieldNode(path="profile", types=["object"], container=True)]
    targets += _fields(("profile.name", ["string"]))
    sources = _fields(("name", ["string"]))
    aligned = align.align(targets, sources)
    assert [a.target.path for a in aligned] == ["profile.name"]


def test_differing_enum_vocabularies_ask_for_a_value_mapping():
    source = _fields(("state", ["string"], False, ["Low", "High"]))[0]
    target = _fields(("level", ["string"], False, ["low", "high"]))[0]
    assert align.infer_transform(source, target) is TransformKind.ENUM_MAP


# ----------------------------------------------------------------------
# Translation, deterministically
# ----------------------------------------------------------------------


def _simple_pair():
    source = _entity(
        EntityKind.AGENT,
        "bot",
        _fields(
            ("displayName", ["string"], True),
            ("description", ["string"]),
            ("agentInstructionText", ["string"]),
            ("clientSecret", ["string"]),
            ("legacyFlag", ["boolean"]),
        ),
        samples=[
            {
                "displayName": "HR",
                "description": "d",
                "agentInstructionText": "be helpful",
                "clientSecret": "x",
                "legacyFlag": True,
            }
        ],
    )
    target = _entity(
        EntityKind.AGENT,
        "Agent",
        _fields(
            ("display_name", ["string"], True),
            ("description", ["null", "string"]),
            ("instructions", ["string"], True),
        ),
    )
    return source, target


def test_the_deterministic_pass_alone_produces_a_real_mapping():
    """A migration that declines to use a model gets less coverage, not an
    error.
    """
    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp")
    mapped = {m.target_path: m.source_paths[0] for m in spec.mappings}
    assert mapped == {"description": "description", "display_name": "displayName"}
    assert spec.generator == "deterministic"
    assert any("left unmapped and flagged" in note for note in spec.notes)


def test_a_required_target_with_no_source_is_flagged_as_blocking():
    """The record the adapter produces will be structurally incomplete. That
    does not stop the build -- flag and move on -- but it must not be missable.
    """
    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp")
    blocking = {flag.path for flag in spec.blocking_flags()}
    assert "instructions" in blocking


def test_a_credential_field_is_flagged_as_needing_manual_setup():
    """Wheatear never carries secrets across platforms. The connection they
    belong to still has to be configured by hand on the target, and that is the
    thing a reviewer needs told.
    """
    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp")
    auth = [f for f in spec.flags if f.reason is FlagReason.REQUIRES_AUTH]
    assert [f.path for f in auth] == ["clientSecret"]


def test_a_source_field_with_no_target_is_reported_as_a_loss():
    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp")
    lossy = {f.path for f in spec.flags if f.reason is FlagReason.LOSSY}
    assert "legacyFlag" in lossy


def test_composed_subtrees_are_excluded_rather_than_flagged():
    """`Agent.tools[]` is produced by the tool adapter. Flagging forty of those
    on the agent mapping buries the handful of flags that are real.
    """
    source, target = _simple_pair()
    target.fields.extend(_fields(("tools[].ref", ["string"], True), ("tools[].kind", ["string"])))
    spec = translator.translate(
        source, target, "acme", Direction.IMPORT, "fp", skip_target_prefixes=("tools",)
    )
    assert not any(flag.path.startswith("tools") for flag in spec.flags)
    assert any("`tools` was excluded" in note for note in spec.notes)


# ----------------------------------------------------------------------
# Translation, with a model
# ----------------------------------------------------------------------


def test_a_model_decision_about_a_real_field_is_accepted():
    source, target = _simple_pair()
    provider = FakeProvider(
        [
            FieldDecision(
                target_path="instructions",
                verdict="mapped",
                source_paths=["description"],
                transform=TransformKind.RENAME,
                confidence=0.7,
                rationale="the description doubles as the prompt on this platform",
            )
        ]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    mapping = next(m for m in spec.mappings if m.target_path == "instructions")
    assert mapping.source_paths == ["description"]
    assert mapping.confidence == 0.7


def test_a_source_path_that_is_not_in_the_schema_is_discarded_and_recorded():
    """A model that invents `agent.displayName` produces code that reads a
    field no record has and writes null into ten thousand agents. Discarding it
    silently would hide that the provider is doing this at all.
    """
    source, target = _simple_pair()
    provider = FakeProvider(
        [
            FieldDecision(
                target_path="instructions",
                verdict="mapped",
                source_paths=["agent.doesNotExist"],
                confidence=0.99,
            )
        ]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    assert not any(m.target_path == "instructions" for m in spec.mappings)
    assert any("not in the schema" in note for note in spec.notes)


def test_a_constant_needs_no_source_field():
    """`source_platform` is "copilot-studio" for every record this adapter will
    ever see. Treating an empty source list as "no match" would silently drop
    exactly the fields the IR marks required and no source record carries.
    """
    source, target = _simple_pair()
    provider = FakeProvider(
        [
            FieldDecision(
                target_path="instructions",
                verdict="mapped",
                source_paths=[],
                transform=TransformKind.CONSTANT,
                constant="fixed text",
                confidence=1.0,
            )
        ]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    mapping = next(m for m in spec.mappings if m.target_path == "instructions")
    assert mapping.transform is TransformKind.CONSTANT
    assert mapping.constant == "fixed text"
    assert not spec.blocking_flags()


def test_a_match_that_names_no_source_and_no_constant_is_discarded():
    source, target = _simple_pair()
    provider = FakeProvider(
        [FieldDecision(target_path="instructions", verdict="mapped", source_paths=[])]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    assert not any(m.target_path == "instructions" for m in spec.mappings)
    assert any("names no source field" in note for note in spec.notes)


def test_an_enum_translation_with_no_value_map_is_discarded():
    source, target = _simple_pair()
    provider = FakeProvider(
        [
            FieldDecision(
                target_path="instructions",
                verdict="mapped",
                source_paths=["description"],
                transform=TransformKind.ENUM_MAP,
                enum_map=[],
                confidence=0.9,
            )
        ]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    assert not any(m.target_path == "instructions" for m in spec.mappings)
    assert any("no value mapping" in note for note in spec.notes)


def test_a_decision_about_a_field_that_was_not_asked_about_is_discarded():
    source, target = _simple_pair()
    provider = FakeProvider(
        [FieldDecision(target_path="something_else", verdict="mapped", source_paths=["description"])]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    assert any("not a target field" in note for note in spec.notes)


def test_a_provider_that_fails_degrades_instead_of_sinking_the_mapping():
    class Broken:
        def generate_structured(self, prompt, schema):
            raise RuntimeError("rate limited")

    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=Broken())
    # The deterministic matches still landed.
    assert any(m.target_path == "description" for m in spec.mappings)
    assert any("could not adjudicate" in note for note in spec.notes)


def test_whole_records_are_never_sent_to_the_model():
    """Only field metadata leaves the machine: paths, types, enum values and
    truncated redacted examples. A customer's agent content staying out of a
    provider's logs costs the correlation nothing.
    """
    source, target = _simple_pair()
    source.samples = [{"displayName": "HR", "description": "CONFIDENTIAL BOARD MINUTES"}]
    provider = FakeProvider([])
    translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    assert provider.prompts
    assert all("CONFIDENTIAL BOARD MINUTES" not in prompt for prompt in provider.prompts)


def test_the_prompt_names_the_only_paths_the_model_may_answer_with():
    source, target = _simple_pair()
    provider = FakeProvider([])
    translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    prompt = provider.prompts[0]
    assert "copied verbatim" in prompt
    assert "'none' is a correct and expected answer" in prompt


def test_the_reasoning_stages_import_nothing_that_does_io():
    """The foundry's I/O lives in `probes/`, `store.py` and `sandbox.py`, and
    nowhere else. Correlation, code generation and case derivation are pure --
    which is what makes their output reproducible enough to hash and cache. If
    someone adds a direct fetch to one of them, this fails.
    """
    import ast
    import inspect

    from wheatear.foundry import cases, emit, guard, redact, shape
    from wheatear.foundry import align as align_module
    from wheatear.foundry import translator as translator_module

    forbidden = {"requests", "httpx", "urllib", "subprocess", "socket", "shutil", "tempfile"}
    for module in (shape, redact, align_module, translator_module, emit, cases, guard):
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        leaked = imported & forbidden
        assert not leaked, f"{module.__name__} imported I/O modules: {leaked}"
        assert not any(name.startswith("wheatear.connectors") for name in imported)


def test_the_spec_key_carries_the_schema_it_was_built_against():
    """This is what makes a cache hit safe: an adapter compiled for one shape
    must not be reused for another.
    """
    source, target = _simple_pair()
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fingerprint-abc")
    key = spec.key()
    assert key.platform == "acme"
    assert key.direction is Direction.IMPORT
    assert key.schema_fingerprint == "fingerprint-abc"
    assert key.family() == "acme/import/agent"


def test_no_response_schema_uses_an_open_dict():
    """Every schema handed to a model must be a closed shape.

    `dict[str, str]` is the obvious modelling for a value map and is unusable:
    pydantic emits `additionalProperties`, and the Gemini Developer API rejects
    that outright -- which failed silently as "the model could not adjudicate"
    against a real key, with the deterministic mappings quietly standing in.
    Closed shapes cost one conversion and work on every provider.
    """
    from wheatear.foundry.engineer import DerivedFunctions, FullAdapter, ProposedCases

    def open_dicts(node, trail="") -> list[str]:
        found = []
        if isinstance(node, dict):
            extra = node.get("additionalProperties")
            if extra not in (None, False):
                found.append(trail or "<root>")
            for key, value in node.items():
                found += open_dicts(value, f"{trail}.{key}" if trail else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found += open_dicts(value, f"{trail}[{index}]")
        return found

    for schema in (FieldDecisions, DerivedFunctions, ProposedCases, FullAdapter):
        offenders = open_dicts(schema.model_json_schema())
        assert not offenders, f"{schema.__name__} has open dict(s) at: {offenders}"


def test_enum_pairs_become_the_value_map_the_generated_code_uses():
    """The wire shape is a list of pairs (closed, portable); the spec stores a
    dict. The conversion happens here so nothing downstream has to know why.
    """
    from wheatear.foundry.translator import EnumPair

    source, target = _simple_pair()
    provider = FakeProvider(
        [
            FieldDecision(
                target_path="instructions",
                verdict="mapped",
                source_paths=["description"],
                transform=TransformKind.ENUM_MAP,
                enum_map=[
                    EnumPair(source_value="Low", target_value="permissive"),
                    EnumPair(source_value="High", target_value="strict"),
                ],
                confidence=0.9,
            )
        ]
    )
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    mapping = next(m for m in spec.mappings if m.target_path == "instructions")
    assert mapping.enum_map == {"Low": "permissive", "High": "strict"}


def test_a_field_with_no_candidate_costs_no_model_call():
    """The deterministic ranking found nothing plausible, so there is nothing
    for a model to choose between. Asking anyway is a call that can only come
    back "none" -- and on a rich target schema there are hundreds of them.
    """
    source = _entity(EntityKind.AGENT, "src", _fields(("displayName", ["string"])))
    target = _entity(
        EntityKind.AGENT,
        "Agent",
        _fields(
            ("display_name", ["string"]),
            ("zzz_unrelated_alpha", ["string"]),
            ("zzz_unrelated_beta", ["string"]),
        ),
    )
    provider = FakeProvider([])
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp", provider=provider)
    asked = "".join(provider.prompts)
    assert "zzz_unrelated" not in asked, "fields with no candidate must not be asked about"
    assert "display_name" in asked, "a field with a real candidate still goes to the model"
    assert any("no candidate source at all" in note for note in spec.notes)
    # They are still flagged -- skipped is not the same as ignored.
    assert {"zzz_unrelated_alpha", "zzz_unrelated_beta"} <= {f.path for f in spec.flags}


def test_adjudication_is_bounded_and_spends_the_budget_on_required_fields():
    """A live Orchestrate tool record has 592 fields. Without a ceiling the cost
    of a build is set by how rich the target platform's read model happens to
    be, which is the wrong thing to be governed by.
    """
    source = _entity(
        EntityKind.AGENT, "src",
        _fields(*[(f"note_s{n:03d}", ["string"]) for n in range(80)]),
    )
    # Every target shares the `note` token with every source, so all of them
    # rank candidates and none of them is an exact leaf match: maximally
    # ambiguous, which is exactly the case the ceiling exists for.
    targets = [("note_required", ["string"], True)]
    targets += [(f"note_t{n:03d}", ["string"]) for n in range(80)]
    target = _entity(EntityKind.AGENT, "Agent", _fields(*targets))

    provider = FakeProvider([])
    spec = translator.translate(
        source, target, "acme", Direction.IMPORT, "fp", provider=provider, batch_size=100
    )
    asked = sum(prompt.count("TARGET FIELD\n  - ") for prompt in provider.prompts)
    assert asked <= translator.MAX_ADJUDICATED
    assert any("adjudication ceiling" in note for note in spec.notes)
    # The required field is never the one dropped for budget.
    assert "note_required" in "".join(provider.prompts)


# ----------------------------------------------------------------------
# Fields the adapter fills from what it is, not what it reads
# ----------------------------------------------------------------------


def _ir_shaped_target():
    return _entity(
        EntityKind.AGENT,
        "Agent",
        _fields(
            ("name", ["string"], True),
            ("source_platform", ["string"], True),
            ("spec_version", ["string"], True),
        ),
    )


def test_the_platform_a_record_came_from_is_seeded_not_hunted_for():
    """No Copilot Studio bot has a field saying "I am a Copilot Studio bot".
    Left to the aligner, `source_platform` is a "no counterpart" flag on a
    required field with exactly one possible correct value.
    """
    source, _ = _simple_pair()
    spec = translator.translate(
        source, _ir_shaped_target(), "copilot-studio", Direction.IMPORT, "fp"
    )
    seeded = {m.target_path: m.constant for m in spec.mappings if m.transform is TransformKind.CONSTANT}
    assert seeded["source_platform"] == "copilot-studio"
    assert seeded["spec_version"] == IR_SPEC_VERSION
    assert "source_platform" not in {flag.path for flag in spec.blocking_flags()}


def test_nothing_is_seeded_on_the_way_out_of_the_ir():
    """`source_platform` is a fact about where a record came from. Writing one
    into a target platform's payload would be inventing a field.
    """
    source, _ = _simple_pair()
    spec = translator.translate(
        _ir_shaped_target(), source, "acme", Direction.EXPORT, "fp"
    )
    assert not [m for m in spec.mappings if m.transform is TransformKind.CONSTANT]


# ----------------------------------------------------------------------
# One value where one value is wanted
# ----------------------------------------------------------------------


def test_an_array_mapped_source_is_not_offered_to_a_scalar_target():
    """`collaborators[].description` reads as a list. An agent with no
    description of its own scores it very highly, and the mapping is wrong for
    every record it ever sees.
    """
    source = _entity(
        EntityKind.AGENT,
        "bot",
        _fields(("collaborators[].description", ["string"]), ("summary", ["string"])),
    )
    target = _entity(EntityKind.AGENT, "Agent", _fields(("description", ["null", "string"])))
    spec = translator.translate(source, target, "acme", Direction.IMPORT, "fp")
    assert not [m for m in spec.mappings if m.target_path == "description"]


def test_an_array_mapped_source_still_serves_a_target_inside_an_array():
    """Both sides sit under an array, so the loop pairs them element by
    element and the arity filter must not stand in the way.
    """
    source = _fields(("collaborators[].description", ["string"]))
    target = _fields(("agents[].description", ["string"]))
    assert align.arity_compatible(source[0], target[0])
    assert align.align(target, source)[0].candidates


def test_an_array_mapped_source_is_never_a_candidate_for_a_scalar():
    source = _fields(("collaborators[].description", ["string"]))
    target = _fields(("description", ["string"]))
    assert not align.arity_compatible(source[0], target[0])
    assert align.align(target, source)[0].candidates == []


def test_an_array_mapped_source_may_still_fill_a_list_valued_field():
    source = _fields(("topics[].label", ["string"]))
    target = _fields(("labels", ["array"]))
    assert align.arity_compatible(source[0], target[0])


def _decide(decision, target_path, source_paths, target_types=("string",)):
    """Put one model decision through the validation the translator applies."""
    target = _fields((target_path, list(target_types)))[0]
    return translator._apply_decision(
        decision, align.Alignment(target=target), set(source_paths)
    )


def test_a_model_that_points_a_scalar_at_an_array_is_refused_and_recorded():
    mapping, complaint = _decide(
        FieldDecision(
            target_path="title",
            verdict="mapped",
            source_paths=["topics[].label"],
            transform=TransformKind.RENAME,
            confidence=0.9,
        ),
        "title",
        ["topics[].label"],
    )
    assert mapping is None
    assert "cannot land on a scalar" in complaint


def test_a_derive_may_collapse_an_array_because_it_is_written_by_hand():
    mapping, complaint = _decide(
        FieldDecision(
            target_path="title",
            verdict="mapped",
            source_paths=["topics[].label"],
            transform=TransformKind.DERIVE,
            confidence=0.9,
        ),
        "title",
        ["topics[].label"],
    )
    assert complaint is None
    assert mapping.transform is TransformKind.DERIVE


# ----------------------------------------------------------------------
# Several sources, one field
# ----------------------------------------------------------------------


def test_several_sources_for_a_single_source_transform_become_a_coalesce():
    """The renderer reads one path. A decision naming three would silently use
    the first and be absent for every record carrying the field elsewhere --
    which looks mapped and is not.
    """
    mapping, complaint = _decide(
        FieldDecision(
            target_path="name",
            verdict="mapped",
            source_paths=["bot.name", "botcomponent.name"],
            transform=TransformKind.RENAME,
            confidence=0.9,
        ),
        "name",
        ["bot.name", "botcomponent.name"],
    )
    assert complaint is None
    assert mapping.transform is TransformKind.COALESCE
    assert mapping.source_paths == ["bot.name", "botcomponent.name"]


def test_a_join_of_several_sources_is_left_alone():
    """Concatenation is a real answer for prose fields, and coalescing it would
    throw away everything after the first part.
    """
    mapping, _ = _decide(
        FieldDecision(
            target_path="instructions",
            verdict="mapped",
            source_paths=["intro", "body"],
            transform=TransformKind.JOIN,
            confidence=0.9,
        ),
        "instructions",
        ["intro", "body"],
    )
    assert mapping.transform is TransformKind.JOIN

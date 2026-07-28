"""Schema inference and redaction: the two things everything else is built on.

Inference is what the adapter cache is keyed by, so its output has to be stable
and its judgements (required, enum, type) have to be conservative -- an
over-confident schema produces an adapter that rejects valid records. Redaction
runs before all of it, because the records being inferred over came off a live
customer tenant.
"""

from agent_liftoff.foundry import redact, shape
from agent_liftoff.foundry.shape import MISSING, has_path, infer_fields, read_path, resolve_path
from agent_liftoff.ir.schema import Agent, DialogNode, Topic


def _by_path(fields):
    return {field.path: field for field in fields}


# ----------------------------------------------------------------------
# Inference
# ----------------------------------------------------------------------


def test_a_field_missing_from_one_record_is_not_required():
    """`required` drives whether generated code may assume a field is there.

    Nine records out of ten is an optional field, and an adapter that treated
    it as mandatory would reject the tenth -- so presence has to be unanimous,
    and `occurrence` carries the real number either way.
    """
    fields = _by_path(
        infer_fields([{"name": "a", "note": "x"}, {"name": "b"}, {"name": "c"}])
    )
    assert fields["name"].required is True
    assert fields["note"].required is False
    assert fields["note"].occurrence == round(1 / 3, 4)


def test_array_elements_collapse_onto_one_positional_path():
    """`topics[0].name` and `topics[7].name` are the same field.

    Indexing them separately would make a schema's size a function of how much
    data happened to be in the sample, and would make the fingerprint change
    every time a tenant added a topic.
    """
    fields = _by_path(
        infer_fields([{"topics": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}])
    )
    assert "topics[].name" in fields
    assert fields["topics"].container is True
    assert fields["topics"].types == ["array"]
    assert fields["topics[].name"].container is False


def test_nested_arrays_keep_both_levels_in_the_path():
    fields = _by_path(
        infer_fields([{"topics": [{"nodes": [{"text": "hello"}, {"text": "bye"}]}]}])
    )
    assert "topics[].nodes[].text" in fields


def test_every_observed_type_is_recorded_not_just_the_first():
    """A field that is a string in most records and null in some is both.

    Collapsing it to "string" is how a generated adapter ends up calling a
    string method on None four thousand records in.
    """
    fields = _by_path(infer_fields([{"x": "text"}, {"x": None}, {"x": 3}]))
    assert fields["x"].types == ["integer", "null", "string"]


def test_a_small_repeating_vocabulary_is_detected_as_an_enum():
    """Enums are the one thing that needs a *value* translation across
    platforms, not just a rename, so they have to be found.
    """
    records = [{"state": s} for s in ["open", "closed", "open", "closed", "open", "open"]]
    assert _by_path(infer_fields(records))["state"].enum == ["closed", "open"]


def test_values_that_never_repeat_are_not_an_enum():
    """Identifiers and free text have high cardinality by nature. Calling them
    an enum would put customer data into the corpus fingerprint.
    """
    records = [{"id": f"guid-{n}"} for n in range(10)]
    assert _by_path(infer_fields(records))["id"].enum == []


def test_redacted_values_are_never_treated_as_an_enum():
    """Every value redacted to the same marker looks exactly like a
    one-value enum. Letting that through would write the marker into the
    fingerprint, so a tenant with secrets and one without would stop sharing a
    cached adapter.
    """
    records = [redact.redact({"api_key": f"secret-value-{n}"}) for n in range(8)]
    assert _by_path(infer_fields(records))["api_key"].enum == []


def test_inference_is_order_independent():
    """The fingerprint is taken over this output, so two probes that saw the
    same records in a different order must produce the same schema.
    """
    a = [{"x": 1, "y": "p"}, {"y": "q", "x": 2}]
    b = [{"y": "q", "x": 2}, {"x": 1, "y": "p"}]
    assert [f.model_dump() for f in infer_fields(a)] == [f.model_dump() for f in infer_fields(b)]


def test_no_samples_yields_no_fields():
    assert infer_fields([]) == []


# ----------------------------------------------------------------------
# Reading a shape off the IR
# ----------------------------------------------------------------------


def test_the_ir_shape_comes_from_the_models_not_a_restatement():
    fields = _by_path(shape.schema_from_model(Agent))
    assert fields["name"].required is True
    assert fields["description"].types == ["null", "string"]
    assert fields["tools"].container is True
    assert "tools[].ref" in fields
    assert "tools[].inputs[].name" in fields


def test_ir_enums_become_the_target_vocabulary():
    """`ToolKind`, `BridgeStrategy` and friends are closed sets the mapping has
    to land inside. Surfacing their members is what lets a translator produce
    an enum_map instead of passing an unrecognised value straight through.
    """
    fields = _by_path(shape.schema_from_model(Agent))
    assert "mcp" in fields["tools[].kind"].enum
    assert "manual" in fields["tools[].bridge"].enum


def test_a_self_referential_model_terminates():
    """`DialogNode.children` is a list of DialogNode. Without a cycle guard
    this recurses until the interpreter gives up.
    """
    fields = shape.schema_from_model(DialogNode)
    assert any(f.path == "children[].text" for f in fields)
    assert len(fields) < 100


def test_model_docstrings_travel_as_field_descriptions():
    """The IR carries its semantics in prose above each model, and that prose
    is the most useful thing a correlating model can be told about the target.
    """
    fields = _by_path(shape.schema_from_model(Topic))
    assert fields["nodes[]"].description
    assert "dialog graph" in fields["nodes[]"].description


# ----------------------------------------------------------------------
# Path access
# ----------------------------------------------------------------------


def test_reading_a_path_across_an_array_maps_rather_than_indexes():
    record = {"topics": [{"name": "a"}, {"name": "b"}]}
    assert read_path(record, "topics[].name") == ["a", "b"]


def test_reading_across_two_array_levels_flattens():
    record = {"t": [{"n": [{"x": 1}, {"x": 2}]}, {"n": [{"x": 3}]}]}
    assert read_path(record, "t[].n[].x") == [1, 2, 3]


def test_a_missing_path_is_distinguishable_from_a_null_one():
    """The generated code omits a target whose source was absent but carries
    one whose source was explicitly null, so the two cannot be the same answer.
    """
    assert resolve_path({"a": None}, "a") is None
    assert resolve_path({}, "a") is MISSING
    assert has_path({"a": None}, "a") is True
    assert has_path({}, "a") is False


def test_path_access_never_raises_on_a_malformed_record():
    """An accessor used to assert on adapter output must not itself explode.

    An empty list reads as an empty list rather than as absence, which is the
    same answer `{"topics": []}` gives for `topics[].name` -- mapping over
    nothing produces nothing, and that is different from the key not being
    there at all.
    """
    for record in (None, "text", 42, {"a": "not-a-dict"}):
        assert read_path(record, "a.b.c") is None
    assert read_path([], "a.b.c") == []
    assert read_path({"topics": []}, "topics[].name") == []


# ----------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------


def test_credentials_are_removed_by_key_name():
    cleaned = redact.redact(
        {"client_secret": "hunter2", "apiKey": "abc", "Authorization": "xyz", "name": "keep"}
    )
    assert cleaned["client_secret"].startswith("<redacted")
    assert cleaned["apiKey"].startswith("<redacted")
    assert cleaned["Authorization"].startswith("<redacted")
    assert cleaned["name"] == "keep"


def test_configuration_that_merely_mentions_tokens_survives():
    """`max_tokens` is a model's context limit, not a credential. Redacting it
    would produce an adapter that silently drops it from every agent.
    """
    cleaned = redact.redact({"max_tokens": 4096, "tokenizer": "cl100k", "top_p": 0.9})
    assert cleaned == {"max_tokens": 4096, "tokenizer": "cl100k", "top_p": 0.9}


def test_credential_shaped_values_are_removed_whatever_the_key_is_called():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
    cleaned = redact.redact({"notes": f"paste this: {jwt} then continue"})
    assert jwt not in cleaned["notes"]
    assert "paste this:" in cleaned["notes"]


def test_a_url_with_embedded_credentials_keeps_the_url():
    """An endpoint is evidence worth keeping; the password in it is not."""
    cleaned = redact.redact({"endpoint": "https://admin:s3cr3t@example.invalid/api"})
    assert "s3cr3t" not in cleaned["endpoint"]
    assert "example.invalid/api" in cleaned["endpoint"]


def test_session_cookies_are_removed_but_the_cookie_name_is_kept():
    cleaned = redact.redact({"captured": "__Secure-fgp=9f8a7b6c5d4e; other=1"})
    assert "9f8a7b6c5d4e" not in cleaned["captured"]
    assert "__Secure-fgp=" in cleaned["captured"]


def test_prose_is_not_mistaken_for_a_secret():
    """Instructions are the single most important field in an agent. A blob
    rule that ate them would gut the corpus.
    """
    prose = "You are a helpful assistant. Answer questions about HR policy in under 200 words."
    assert redact.redact({"instructions": prose})["instructions"] == prose


def test_redaction_preserves_json_type_so_the_inferred_shape_is_unchanged():
    """The corpus stays accurate about what a platform's records look like
    while being wrong, deliberately, about what was in them.
    """
    original = [{"token": "abc", "name": "x"}, {"token": "def", "name": "y"}]
    cleaned = redact.redact_all(original)
    assert infer_fields(cleaned)[1].types == infer_fields(original)[1].types


def test_redaction_does_not_mutate_its_input():
    """A probe may hand the same response to more than one consumer."""
    original = {"password": "hunter2", "nested": {"secret": "x"}}
    redact.redact(original)
    assert original == {"password": "hunter2", "nested": {"secret": "x"}}

"""Deterministic schema inference -- the part of the inspector that does not
need a model, and the part the correlation step is only as good as.

Two sources, one output type. A platform's shape is *observed* from records it
handed us; the IR's shape is *read* off Agent Liftoff's own pydantic definitions,
where it is already declared. Both produce `list[FieldNode]`, which is what
lets the translator compare them without caring which side is which.

Paths are flat and dotted, with `[]` marking an array level:

    name
    topics[].trigger_phrases[]
    tools[].inputs[].description

Flat because both consumers want it flat. The correlation step compares two
path inventories, and generated code walks a path to read or write a value --
neither has any use for a tree. And the array marker is positional rather than
indexed on purpose: `topics[0].name` and `topics[7].name` are the same field,
and treating them as different ones would make a schema's size a function of
how much data happened to be in the sample.

Nothing here calls a model or does I/O. It is pure, so its output is stable
enough to hash -- which is what the whole adapter cache is keyed on.
"""

from __future__ import annotations

import re

import types as pytypes
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from agent_liftoff.foundry.types import FieldNode

# How many distinct values a field may take before it stops looking like an
# enum and starts looking like free text.
ENUM_MAX_DISTINCT = 12
# Below this many observations, a small distinct-value count means nothing --
# three records with three different strings is not evidence of anything.
ENUM_MIN_OBSERVATIONS = 4
# A value longer than this is prose, not a token, whatever its cardinality.
ENUM_MAX_VALUE_LENGTH = 64

MAX_EXAMPLES = 3
EXAMPLE_LENGTH = 120

# Depth limit for both walks. Deep enough for every real agent schema seen so
# far; bounded so a self-referential model (IR `DialogNode.children`) or a
# pathological export terminates instead of recursing forever.
MAX_DEPTH = 6


def json_type(value: Any) -> str:
    """The JSON type name for a Python value.

    `bool` is checked before `int` because it is a subclass of it, and a flag
    that reports itself as an integer is exactly the kind of thing that
    produces a generated mapping writing `True` into a count field.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


@dataclass
class _Observation:
    """Everything seen at one path while walking the samples."""

    types: set[str] = field(default_factory=set)
    values: list[Any] = field(default_factory=list)
    present_in: int = 0
    container: bool = False


def _walk(value: Any, path: str, into: dict[str, _Observation], depth: int) -> None:
    obs = into.setdefault(path, _Observation())
    obs.types.add(json_type(value))

    if depth >= MAX_DEPTH:
        return

    if isinstance(value, dict):
        obs.container = True
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            _walk(child, child_path, into, depth + 1)
    elif isinstance(value, (list, tuple)):
        obs.container = True
        for item in value:
            _walk(item, f"{path}[]", into, depth + 1)
    elif value is not None:
        obs.values.append(value)


def _looks_like_enum(obs: _Observation) -> list[str]:
    """Decide whether a field's observed values form a closed set.

    Worth getting right because an enum is the one thing that reliably needs a
    *value* translation across platforms, not just a rename -- Copilot's
    "Low"/"High" moderation and Orchestrate's absence of one are not a
    field-name problem. Over-detecting is the cheaper mistake here: a spurious
    enum shows up in the spec as a reviewable list, while a missed one becomes
    a value silently passed through unmapped.
    """
    if obs.container or len(obs.values) < ENUM_MIN_OBSERVATIONS:
        return []
    if not all(isinstance(v, (str, bool, int)) and not isinstance(v, float) for v in obs.values):
        return []
    rendered = [str(v) for v in obs.values]
    if any(len(v) > ENUM_MAX_VALUE_LENGTH for v in rendered):
        return []
    # A field whose values were all redacted looks exactly like an enum: one
    # repeated token. Letting that through would write the redaction marker
    # into the corpus fingerprint, so a tenant with secrets and a tenant
    # without would stop sharing a cached adapter.
    if any(v.startswith("<redacted") for v in rendered):
        return []
    distinct = sorted(set(rendered))
    if len(distinct) > ENUM_MAX_DISTINCT:
        return []
    # Repetition is the actual evidence: values that recur are a vocabulary,
    # values that never repeat are identifiers or free text.
    if len(distinct) > max(2, len(rendered) // 2):
        return []
    return distinct


def _examples(obs: _Observation) -> list[str]:
    rendered = {str(v)[:EXAMPLE_LENGTH] for v in obs.values if v != ""}
    return sorted(rendered)[:MAX_EXAMPLES]


# A path segment that is plainly an identifier rather than a field name.
# `agent_mapping.4e96a4fc-7ee8-45fb-bbff-f2944a845fc1` is not a field called
# `4e96a4fc-...`; it is one entry in a map keyed by agent id.
_ID_KEY = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{24,}|\d+)$"
)

# Below this, the keys under a path are too unstable to be field names.
# A record has the same keys in every sample; a map does not.
MAP_KEY_STABILITY = 0.5


def _map_paths(samples: list[dict]) -> set[str]:
    """Paths whose children are map entries rather than fields.

    Two signals, either of which is enough:

      * a child key that is plainly an identifier -- a GUID, a long hex blob;
      * child keys that barely repeat across records. A record has the same
        keys every time. `binding.python.connections` does not: it is keyed by
        whichever app connections *that tenant* configured.

    Getting this wrong is not only a privacy problem, though it is that -- it
    put tenant agent ids and connection names into generated adapter code. It
    is a correctness problem: an adapter compiled against one tenant's
    connection names has no mapping for the next tenant's, which defeats the
    point of compiling it once for everyone.
    """
    seen: dict[str, list[set[str]]] = {}

    def walk(value: Any, path: str, depth: int) -> None:
        if depth >= MAX_DEPTH:
            return
        if isinstance(value, dict):
            seen.setdefault(path, []).append(set(map(str, value.keys())))
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, f"{path}[]", depth + 1)

    for sample in samples:
        if isinstance(sample, dict):
            walk(sample, "", 0)

    maps: set[str] = set()
    for path, key_sets in seen.items():
        if not path:
            continue
        keys = set().union(*key_sets)
        if not keys:
            continue
        if any(_ID_KEY.match(k) for k in keys):
            maps.add(path)
            continue
        # How often a key recurs across the records that had this path.
        shared = set.intersection(*key_sets) if len(key_sets) > 1 else keys
        if len(keys) > 2 and len(shared) / len(keys) < MAP_KEY_STABILITY:
            maps.add(path)
    return maps


def infer_fields(samples: list[dict]) -> list[FieldNode]:
    """Infer a flat field inventory from sample records.

    `required` here means "present in every sample we saw", which is weaker
    than a declared schema and is why `occurrence` travels with it: a field in
    9 of 10 samples is almost certainly optional, and a field in 10 of 10 when
    there were only 2 samples is not evidence of much.
    """
    if not samples:
        return []

    maps = _map_paths(samples)

    def canonical(path: str) -> str:
        """Collapse map entries onto the map: `conns.snow_ibm_1` -> `conns.*`."""
        for parent in maps:
            if path == parent or not path.startswith(parent + "."):
                continue
            rest = path[len(parent) + 1 :]
            head, _, tail = rest.partition(".")
            if head:
                return f"{parent}.*{('.' + tail) if tail else ''}"
        return path

    merged: dict[str, _Observation] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        per_sample: dict[str, _Observation] = {}
        _walk(sample, "", per_sample, 0)
        per_sample.pop("", None)  # the root itself is not a field
        per_sample = {canonical(p): o for p, o in per_sample.items()}
        for path, obs in per_sample.items():
            target = merged.setdefault(path, _Observation())
            target.types |= obs.types
            target.values.extend(obs.values)
            target.container = target.container or obs.container
            target.present_in += 1

    total = sum(1 for s in samples if isinstance(s, dict)) or 1
    fields = [
        FieldNode(
            path=path,
            types=sorted(obs.types),
            required=obs.present_in == total,
            occurrence=round(obs.present_in / total, 4),
            enum=_looks_like_enum(obs),
            examples=[] if obs.container else _examples(obs),
            container=obs.container,
        )
        for path, obs in merged.items()
    ]
    fields.sort(key=lambda f: f.path)
    return fields


# ----------------------------------------------------------------------
# Reading a shape off a pydantic model
# ----------------------------------------------------------------------


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip `| None` off an annotation, reporting whether it was there.

    Nullability is a real fact about a field -- it is the difference between
    "the adapter may omit this" and "the adapter must produce it" -- so it is
    preserved as a `null` entry in `types` rather than discarded.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is pytypes.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        nullable = len(args) != len(get_args(annotation))
        if len(args) == 1:
            return args[0], nullable
        return annotation, nullable
    return annotation, False


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin in (list, set, tuple):
        return "array"
    if origin is dict:
        return "object"
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return "object"
        if issubclass(annotation, Enum):
            return "string"
        return {
            bool: "boolean",
            int: "integer",
            float: "number",
            str: "string",
        }.get(annotation, annotation.__name__)
    return "string"


def _enum_values(annotation: Any) -> list[str]:
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return sorted(str(member.value) for member in annotation)
    return []


def _describe(annotation: Any) -> str | None:
    """A nested model's own docstring, used as the field's description.

    Agent Liftoff's IR carries its semantics in prose right above each model, and
    that prose is the most useful thing a correlating model can be given about
    the target vocabulary -- far more than a type name. It is available at
    runtime as `__doc__`, so it costs nothing to pass along.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        doc = (annotation.__doc__ or "").strip()
        return " ".join(doc.split())[:400] or None
    return None


def _model_fields(
    model: type[BaseModel], prefix: str, depth: int, stack: tuple[type, ...]
) -> list[FieldNode]:
    # One level of self-nesting is allowed, then the recursion stops. The IR's
    # `DialogNode.children` is a list of DialogNode: refusing to descend at all
    # would report that a child node has no fields, while descending freely
    # would not terminate. One level says "children look like nodes" and ends.
    if depth >= MAX_DEPTH or stack.count(model) >= 2:
        return []

    nodes: list[FieldNode] = []
    for name, info in model.model_fields.items():
        annotation, nullable = _unwrap_optional(info.annotation)
        path = f"{prefix}.{name}" if prefix else name
        origin = get_origin(annotation)

        if origin in (list, set, tuple):
            item = next(iter(get_args(annotation)), Any)
            item, item_nullable = _unwrap_optional(item)
            nodes.append(
                FieldNode(
                    path=path,
                    types=sorted(["array"] + (["null"] if nullable else [])),
                    required=info.is_required(),
                    occurrence=1.0,
                    description=info.description,
                    container=True,
                )
            )
            item_path = f"{path}[]"
            if isinstance(item, type) and issubclass(item, BaseModel):
                nodes.append(
                    FieldNode(
                        path=item_path,
                        types=["object"],
                        required=False,
                        occurrence=1.0,
                        description=_describe(item),
                        container=True,
                    )
                )
                nodes.extend(_model_fields(item, item_path, depth + 1, stack + (model,)))
            else:
                nodes.append(
                    FieldNode(
                        path=item_path,
                        types=sorted([_type_name(item)] + (["null"] if item_nullable else [])),
                        required=False,
                        occurrence=1.0,
                        enum=_enum_values(item),
                    )
                )
            continue

        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            nodes.append(
                FieldNode(
                    path=path,
                    types=sorted(["object"] + (["null"] if nullable else [])),
                    required=info.is_required(),
                    occurrence=1.0,
                    description=info.description or _describe(annotation),
                    container=True,
                )
            )
            nodes.extend(_model_fields(annotation, path, depth + 1, stack + (model,)))
            continue

        nodes.append(
            FieldNode(
                path=path,
                types=sorted([_type_name(annotation)] + (["null"] if nullable else [])),
                required=info.is_required(),
                occurrence=1.0,
                enum=_enum_values(annotation),
                description=info.description,
                container=get_origin(annotation) is dict,
            )
        )
    return nodes


def schema_from_model(model: type[BaseModel], writable: bool | None = None) -> list[FieldNode]:
    """Read a flat field inventory off a pydantic model.

    Used for the IR side of every mapping. The IR does not need probing -- it
    is a declared contract in this repository -- and deriving it from the
    models rather than restating it means the target vocabulary cannot drift
    away from `ir/schema.py` without the fingerprint changing.
    """
    nodes = _model_fields(model, "", 0, ())
    if writable is not None:
        for node in nodes:
            node.writable = writable
    nodes.sort(key=lambda f: f.path)
    return nodes


# ----------------------------------------------------------------------
# Path access
# ----------------------------------------------------------------------


MISSING = object()


def resolve_path(record: Any, path: str, missing: Any = MISSING) -> Any:
    """Walk a dotted path, returning `missing` if any step isn't there.

    Array segments map across the whole array rather than indexing into it, so
    `topics[].name` returns every topic's name and `topics[].triggers[].phrase`
    returns every phrase, flattened. That is the only reading consistent with
    how the paths were inferred, where `topics[0]` and `topics[7]` are the same
    field.

    Never raises. It is used to assert on adapter output and to feed the
    review manifest, and an accessor that can itself explode on a malformed
    record would defeat the point of the whole exercise.
    """
    if not path:
        return record
    current: Any = record
    for segment in path.split("."):
        collect = segment.endswith("[]")
        key = segment[:-2] if collect else segment
        if key:
            if isinstance(current, list):
                current = [item.get(key) for item in current if isinstance(item, dict)]
            elif isinstance(current, dict):
                if key not in current:
                    return missing
                current = current[key]
            else:
                return missing
        if collect:
            if not isinstance(current, list):
                return missing
            flat: list[Any] = []
            for item in current:
                flat.extend(item) if isinstance(item, list) else flat.append(item)
            current = flat
    return current


def read_path(record: Any, path: str) -> Any:
    """Read a dotted path, with a missing path reading as None."""
    value = resolve_path(record, path, None)
    return value


def has_path(record: Any, path: str) -> bool:
    """Whether a path exists at all, as opposed to existing and holding None."""
    return resolve_path(record, path) is not MISSING

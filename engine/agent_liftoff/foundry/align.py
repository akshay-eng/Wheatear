"""Deterministic field alignment: the part of the correlation a model isn't
needed for.

The same two-part shape as the Resolve stage, for the same reason. A cheap,
reproducible ranking does the obvious work and narrows the rest to a handful
of candidates; the model adjudicates only what is genuinely a judgement call.
Between two agent schemas that is most of the job -- `description` is
`description` on every platform anyone has shipped -- and every field settled
here is a field whose mapping is reproducible, reviewable, and free.

Ranking is IDF-weighted token overlap over the path, with the leaf segment
weighted up. Both parts matter. IDF is what stops `id` scoring alike against
forty target paths that all contain `id`, and the leaf weighting is what makes
`tools[].description` prefer `description` over `tools`.

Acceptance without a model is deliberately narrow: the leaf names must agree
exactly after tokenisation, the types must be compatible, and no other
candidate may have an equal claim. That last condition is what keeps `name`
from binding to whichever of `name`, `display_name` and `schema_name` happened
to sort first -- an ambiguous field is exactly the kind a model should decide,
so it is passed on rather than guessed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from agent_liftoff.foundry.types import FieldNode, TransformKind

# Shortlist size handed to the model per unresolved field. Same reasoning as
# pipeline/resolve.py: large enough that the answer is nearly always present,
# small enough that precision doesn't fall off.
SHORTLIST_SIZE = 8

# Below this, an unambiguous leaf-name match is still not worth acting on
# without a model -- it means the paths agree on almost nothing else.
MIN_AUTO_SCORE = 1.0

# How much more a match on the last path segment counts than one further up.
# The leaf is the field; everything before it is where the field lives.
LEAF_WEIGHT = 3

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Structural words that appear in so many paths they say nothing about which
# field this is. Deliberately short: IDF already discounts common terms in
# proportion to how often they occur in *this* inventory, which adapts to a
# platform's conventions better than any fixed list.
_STOPWORDS = frozenset("a an the of for to in on is it and or value".split())

_KEEP_TRAILING_S = ("ss", "us", "is", "os")

# Scalar types that convert into one another without losing meaning. An
# integer id read as a string is the same id; a string read as a boolean is
# not, which is why `boolean` is not in here with the rest.
_SCALARS = {"string", "integer", "number"}


def _singular(token: str) -> str:
    """Fold a plural onto its singular so `tools` matches `tool`.

    The same handful of rules as the Resolve stage rather than a stemmer: this
    runs on identifier fragments, where over-stemming (silently aligning two
    unrelated fields) is a worse failure than under-stemming (asking the model).
    """
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(_KEEP_TRAILING_S):
        return token[:-1]
    return token


def tokenize(text: str | None) -> list[str]:
    """Lowercase word tokens, splitting snake_case, camelCase and dotted names."""
    if not text:
        return []
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [_singular(t) for t in _TOKEN_RE.findall(spaced.lower()) if t not in _STOPWORDS]


def leaf_tokens(path: str) -> list[str]:
    """Tokens of the final path segment -- the field's own name."""
    return tokenize(path.replace("[]", "").rsplit(".", 1)[-1])


def path_tokens(path: str) -> list[str]:
    """Every token in a path, with the leaf segment repeated for weight."""
    return tokenize(path.replace("[]", " ")) + leaf_tokens(path) * (LEAF_WEIGHT - 1)


def _field_tokens(node: FieldNode) -> list[str]:
    """Everything about a field that carries matching signal.

    The description is included at single weight because on the IR side it is
    the model docstring -- real prose about what the field means -- and on the
    platform side it is usually absent. Enum values are included because a
    shared vocabulary ("react", "default") is strong evidence two fields are
    the same field even when their names disagree.
    """
    tokens = path_tokens(node.path)
    for value in node.enum:
        tokens += tokenize(value)
    tokens += tokenize(node.description)
    return tokens


def _idf(fields: list[FieldNode]) -> dict[str, float]:
    total = len(fields) or 1
    seen: Counter[str] = Counter()
    for node in fields:
        seen.update(set(_field_tokens(node)))
    return {token: math.log(total / (1 + count)) + 1.0 for token, count in seen.items()}


def types_compatible(source: FieldNode, target: FieldNode) -> bool:
    """Whether a value at `source` can land at `target` without reinterpretation."""
    a = {t for t in source.types if t != "null"}
    b = {t for t in target.types if t != "null"}
    if not a or not b:
        return True  # nothing observed at one end; not evidence against
    if a & b:
        return True
    return bool(a & _SCALARS) and bool(b & _SCALARS)


def arity_compatible(source: FieldNode, target: FieldNode) -> bool:
    """Whether reading `source` yields one value where `target` wants one.

    A path containing `[]` maps over an array, so reading it produces a list --
    one entry per element. Landing that on a scalar target is not a type
    mismatch a coercion repairs; it is the wrong *number* of things, and the
    result is a field whose value is `["a", "b"]` where the schema promised a
    string.

    Worth a hard filter rather than a score penalty because the token overlap
    that suggests these matches is often excellent: an agent with no
    description of its own scores `collaborators[].description` very highly,
    and the mapping it produces is wrong for every record.
    """
    if "[]" in source.path and "[]" in target.path:
        return True  # both sit under an array; the loop pairs them element-wise
    if "[]" in source.path:
        return "array" in target.types
    if "[]" in target.path:
        # A scalar source for an array-element target broadcasts one value to
        # every element. That is a guess dressed as a mapping, and it costs
        # more than itself: a group of array mappings is only renderable when
        # they share a single source array, so one broadcast among them sends
        # every mapping in the group to a hand-written hole.
        return False
    return True


def infer_transform(source: FieldNode, target: FieldNode) -> TransformKind:
    """The cheapest transform that gets `source` to `target`.

    Only ever suggests the mechanical ones. Anything needing real logic is
    left for the model to call `DERIVE`, because a wrong guess here compiles
    into code that runs ten thousand times.
    """
    if source.enum and target.enum and set(source.enum) != set(target.enum):
        return TransformKind.ENUM_MAP
    a = {t for t in source.types if t != "null"}
    b = {t for t in target.types if t != "null"}
    if a and b and not (a & b):
        return TransformKind.COERCE
    if "array" in b and "array" not in a:
        return TransformKind.COERCE
    return TransformKind.COPY if source.path == target.path else TransformKind.RENAME


@dataclass
class Candidate:
    score: float
    field: FieldNode


@dataclass
class Alignment:
    """One target field and the best the deterministic pass could do for it."""

    target: FieldNode
    source: FieldNode | None = None
    score: float = 0.0
    transform: TransformKind = TransformKind.COPY
    # True when the match is unambiguous enough to accept with no model call.
    certain: bool = False
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.certain and self.source is not None


def rank(target: FieldNode, sources: list[FieldNode], idf: dict[str, float], limit: int) -> list[Candidate]:
    """Rank source fields against one target field, best first."""
    wanted = Counter(_field_tokens(target))
    if not wanted:
        return []

    scored: list[tuple[float, int, FieldNode]] = []
    for index, source in enumerate(sources):
        if not arity_compatible(source, target):
            continue
        available = Counter(_field_tokens(source))
        overlap = sum(
            min(count, available[token]) * idf.get(token, 1.0)
            for token, count in wanted.items()
            if token in available
        )
        if overlap <= 0:
            continue
        # Normalise by the candidate's own length so a long, sprawling path
        # can't outscore a precise one just by covering more tokens.
        score = overlap / math.sqrt(sum(available.values()) or 1)
        if not types_compatible(source, target):
            # Not disqualifying -- a platform storing a count as a string is
            # ordinary -- but it is evidence, so it costs.
            score *= 0.6
        scored.append((score, -index, source))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [Candidate(score=score, field=node) for score, _, node in scored[:limit]]


def align(
    targets: list[FieldNode],
    sources: list[FieldNode],
    limit: int = SHORTLIST_SIZE,
) -> list[Alignment]:
    """Align every target field against the source inventory.

    Containers are skipped on the target side: an object or array node carries
    no value of its own, and mapping it would produce code that overwrote the
    fields nested under it.
    """
    leaves = [node for node in targets if not node.container]
    source_leaves = [node for node in sources if not node.container]
    if not source_leaves:
        return [Alignment(target=node) for node in leaves]

    idf = _idf(source_leaves)
    alignments: list[Alignment] = []
    for target in leaves:
        candidates = rank(target, source_leaves, idf, limit)
        alignment = Alignment(target=target, candidates=candidates)
        if candidates:
            best = candidates[0]
            alignment.source = best.field
            alignment.score = best.score
            alignment.transform = infer_transform(best.field, target)
            alignment.certain = _is_certain(target, candidates)
        alignments.append(alignment)
    return alignments


def _is_certain(target: FieldNode, candidates: list[Candidate]) -> bool:
    """Whether the top candidate can be accepted with no model call.

    Three conditions, all of them necessary:

      the leaf names agree exactly after tokenisation -- `display_name` and
      `displayName` are the same field, `display_name` and `name` are not;
      the types are compatible;
      and no runner-up shares the leaf name, because two source fields with
      equal claim is precisely the ambiguity a model exists to settle.
    """
    best = candidates[0]
    if best.score < MIN_AUTO_SCORE:
        return False
    target_leaf = leaf_tokens(target.path)
    if not target_leaf or leaf_tokens(best.field.path) != target_leaf:
        return False
    if not types_compatible(best.field, target):
        return False
    return not any(leaf_tokens(other.field.path) == target_leaf for other in candidates[1:])


def unmatched_sources(sources: list[FieldNode], alignments: list[Alignment]) -> list[FieldNode]:
    """Source fields no target claimed -- candidates for a "no equivalent" flag."""
    claimed = {a.source.path for a in alignments if a.source is not None and a.certain}
    return [node for node in sources if not node.container and node.path not in claimed]

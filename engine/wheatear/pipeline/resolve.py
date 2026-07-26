"""Resolve stage: unmapped source tools -> real tools in the target catalog.

Map is deliberately LLM-free (see pipeline/map.py) because anything touching
schemas or credentials should stay mechanical and auditable. But "does this
Copilot connector operation already exist as a tool on this Orchestrate
instance?" is a judgement call about meaning, not a lookup: the source calls
it `Get Record` with a `sysid`, the target calls it `SNOWMCPALL:get_record`
with a `sys_id`, and no string comparison gets you there.

So this stage sits between Map and Translate and is explicitly AI-assisted,
in two parts:

  1. A deterministic shortlist ranks the whole catalog lexically. Cheap, and
     it means the model sees ~8 plausible candidates instead of 150, which is
     both cheaper and more accurate than asking it to scan everything.
  2. The model adjudicates the shortlist against the actual parameter
     schemas and returns a structured verdict.

That runs over two pools, in order, because they are not interchangeable:

  * **Installed** -- the tools present on the target instance. A match here is
    importable today; agent.yaml can reference it as it stands.
  * **Catalog** -- the ~1150 tools IBM and partners publish globally but which
    are not on this instance. A match here is real but not yet usable: the
    tool has to be added to the instance first, and its connection configured,
    so it always carries an install step into the review manifest.

Installed is searched first and wins ties, because a working answer beats a
better-sounding one. The pools are also ranked separately rather than merged:
catalog records carry no parameter schema, so scoring them against installed
tools in one list would systematically favour whichever pool happened to have
more text.

Two rules keep the model honest:

  * A returned ref that isn't in the catalog is discarded and downgraded to
    "no match". A hallucinated tool name is worse than an honest miss -- it
    produces an agent.yaml that imports cleanly and then fails at runtime.
  * Only an `exact` verdict clears review_required. Everything else still
    lands in the review manifest for a human, with the model's reasoning
    attached so the reviewer can see *why* it was proposed.

With no provider configured the stage still runs: it records the shortlist as
candidate suggestions and changes nothing else, so a deterministic migration
stays deterministic.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from wheatear.ir.schema import Agent, BridgeStrategy, ToolRef
from wheatear.llm.base import LLMProvider

# How many catalog entries the model is asked to consider. Large enough that
# the right answer is nearly always present, small enough to keep the prompt
# focused -- precision drops when a model has to scan a long list.
SHORTLIST_SIZE = 8

# Below this the model's own confidence isn't worth acting on, even for a
# verdict it called "exact".
MIN_ACCEPT_CONFIDENCE = 0.5

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Given the catalog artifacts behind a shortlist, fetch their full detail and
# write parameters onto them. Injected by the caller so this stage does no I/O.
EnrichHook = Callable[[list], None]

# Grammatical function words only. Verbs are deliberately absent: in a tool
# catalog the verb is the most discriminating part of a name -- `create_record`,
# `get_record` and `update_record` differ by nothing else, and a matcher that
# can't tell them apart could answer a read with a write. Common verbs are not a
# problem to leave in, because IDF discounts every term in proportion to how
# often it actually occurs in this catalog. Domain nouns ("servicenow") are
# absent for the same reason.
_STOPWORDS = frozenset(
    """a an and are as at be by for from in into is it of on or that the this to
    with your you given specific single new existing args arg parameter
    parameters value values""".split()
)

# Endings where stripping the plural `s` would corrupt the word: `status` is not
# `statu`, `address` is not `addres`, `analysis` is not `analysi`.
_KEEP_TRAILING_S = ("ss", "us", "is", "os")


def _singular(token: str) -> str:
    """Fold a plural onto its singular so `records` matches `record`.

    Deliberately a few rules rather than a stemmer: this has to be exact on
    identifier fragments, and the failure mode of over-stemming (matching two
    unrelated tools) is worse than the failure mode of under-stemming.
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


def _tokens(text: str | None) -> list[str]:
    """Split into lowercase word tokens, also breaking snake_case, camelCase
    and dotted/colon-separated names apart: `SNOWMCPALL:get_record` has to
    yield {snowmcpall, get, record} for `Get Record` to reach it.

    Tokens are singularised, because the two platforms pluralise the same
    capability inconsistently -- Copilot's `List Records` has to reach
    Orchestrate's `get_records` *and* a singular `get_record`, and `record` is
    the highest-signal term in either.
    """
    if not text:
        return []
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [
        _singular(t) for t in _TOKEN_RE.findall(spaced.lower()) if t not in _STOPWORDS
    ]


@dataclass
class CatalogTool:
    """One tool the target platform can offer, flattened to what matching
    actually needs.

    Covers both pools. `origin` is the difference that matters downstream:
    "installed" is referenceable now, "catalog" needs an install step first.
    """

    ref: str
    description: str = ""
    params: list[str] = field(default_factory=list)
    toolkit_id: str | None = None
    kind: str = "unknown"
    origin: Literal["installed", "catalog"] = "installed"
    # Catalog entries have a human title ("Add a comment in Google Drive")
    # distinct from the `ref` an installed copy answers to
    # ("add_a_comment_google_drive"). Both carry signal, so both are matched on.
    display_name: str | None = None
    # Catalog-only context: tags, the offering a tool ships in, its publisher.
    # Weak signal individually, but "Devops and CICD Management with Gitlab"
    # tells you what `accept_a_merge_request` is for when its one-line
    # description doesn't.
    context: list[str] = field(default_factory=list)
    # The source `CatalogArtifact`, kept so the enrich hook can fetch its
    # detail and write parameters back. Untyped to keep this stage free of a
    # connector import.
    artifact: object | None = None

    @property
    def installed(self) -> bool:
        return self.origin == "installed"

    def connections(self) -> list[str]:
        """App connections that must exist before this tool will run.

        Only ever populated for enriched catalog entries; an installed tool's
        connection is already configured or the tool wouldn't be there.
        """
        return list(getattr(self.artifact, "connections", []) or [])

    def match_text(self) -> list[str]:
        """Tokens describing this tool. Names are repeated because a name
        collision is far stronger evidence than a description collision --
        descriptions in a real catalog share boilerplate ("Args:", the product
        name) that the name does not.
        """
        tokens = _tokens(self.ref) * 2
        if self.display_name:
            tokens += _tokens(self.display_name) * 2
        tokens += _tokens(self.description)
        tokens += _tokens(" ".join(self.params))
        tokens += _tokens(" ".join(self.context))
        return tokens


def build_catalog(raw_tools: list[dict]) -> list[CatalogTool]:
    """Flatten `GET /tools` output -- the tools installed on the instance.

    Tools with no name are skipped: they can't be referenced from an
    agent.yaml, so proposing one would produce an unimportable spec.
    """
    catalog: list[CatalogTool] = []
    for raw in raw_tools or []:
        ref = raw.get("name")
        if not ref:
            continue
        schema = raw.get("input_schema") or {}
        properties = schema.get("properties") or {}
        binding = raw.get("binding") or {}
        catalog.append(
            CatalogTool(
                ref=ref,
                description=(raw.get("description") or "").strip(),
                params=sorted(properties.keys()),
                toolkit_id=raw.get("toolkit_id"),
                kind=next(iter(binding), "unknown"),
                origin="installed",
            )
        )
    return catalog


def build_marketplace_catalog(artifacts: list) -> list[CatalogTool]:
    """Flatten `CatalogArtifact`s -- the global, not-yet-installed catalog.

    Takes the dataclass from `connectors.orchestrate.catalog_client` rather
    than raw dicts, so the one place that knows the catalog's wire format stays
    the connector, not this pipeline stage.

    `params` is empty until the artifact has been enriched: the catalog's list
    endpoint returns no schema, and fetching one for every entry would be
    thousands of requests. The `enrich` hook on `resolve_agent_tools` fills
    them in for shortlisted candidates only.
    """
    catalog: list[CatalogTool] = []
    for artifact in artifacts or []:
        ref = artifact.install_ref
        if not ref:
            continue
        catalog.append(
            CatalogTool(
                ref=ref,
                description=artifact.description,
                params=list(getattr(artifact, "params", []) or []),
                toolkit_id=None,
                kind=artifact.type or artifact.category or "unknown",
                origin="catalog",
                display_name=artifact.name,
                context=[*artifact.tags, *artifact.groups],
                artifact=artifact,
            )
        )
    return catalog


def _idf(catalog: list[CatalogTool]) -> dict[str, float]:
    """Inverse document frequency across the catalog.

    Without this, every ServiceNow tool scores alike on a ServiceNow query --
    61 of the 150 tools in the calibration instance mention it, so the word is
    nearly worthless for telling them apart, while `sys_id` is decisive.
    """
    total = len(catalog) or 1
    seen: Counter[str] = Counter()
    for tool in catalog:
        seen.update(set(tool.match_text()))
    return {token: math.log(total / (1 + count)) + 1.0 for token, count in seen.items()}


def _source_tokens(tool: ToolRef) -> list[str]:
    tokens = _tokens(tool.ref) * 2
    tokens += _tokens(tool.operation_id) * 2
    tokens += _tokens(tool.description)
    for param in tool.inputs:
        tokens += _tokens(param.name)
        tokens += _tokens(param.description)
    return tokens


def shortlist_scored(
    tool: ToolRef, catalog: list[CatalogTool], limit: int = SHORTLIST_SIZE
) -> list[tuple[float, CatalogTool]]:
    """Rank the catalog against one source tool, best first, with scores.

    Deterministic and dependency-free on purpose: this is the part that has to
    be reproducible and testable, so the only non-determinism in the stage is
    the model's adjudication of a fixed, inspectable candidate list. Scores are
    returned so the ranking can be inspected directly (see `wheatear
    match-tools`) rather than only judged by its end result.
    """
    if not catalog:
        return []

    idf = _idf(catalog)
    wanted = Counter(_source_tokens(tool))
    if not wanted:
        return []

    scored: list[tuple[float, int, CatalogTool]] = []
    for index, candidate in enumerate(catalog):
        available = Counter(candidate.match_text())
        overlap = sum(
            min(count, available[token]) * idf.get(token, 1.0)
            for token, count in wanted.items()
            if token in available
        )
        if overlap > 0:
            # Normalize by candidate length so a sprawling description can't
            # out-score a precise name match just by covering more words.
            score = overlap / math.sqrt(len(available) or 1)
            # Index breaks ties deterministically; catalog order is stable.
            scored.append((score, -index, candidate))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [(score, candidate) for score, _, candidate in scored[:limit]]


def shortlist(
    tool: ToolRef, catalog: list[CatalogTool], limit: int = SHORTLIST_SIZE
) -> list[CatalogTool]:
    """Rank the catalog against one source tool, best first."""
    return [candidate for _, candidate in shortlist_scored(tool, catalog, limit)]


class ToolMatch(BaseModel):
    """The model's verdict on one source tool."""

    target_ref: str | None = Field(
        default=None,
        description="Exact `ref` of the chosen catalog tool, copied verbatim. Null if none fit.",
    )
    verdict: Literal["exact", "near", "none"] = Field(
        description=(
            "'exact' = same capability, any parameter renaming is mechanical. "
            "'near' = related but not a drop-in (missing parameters, broader or "
            "narrower scope). 'none' = nothing in the list does this job."
        )
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", description="One or two sentences justifying the verdict.")


def _format_candidates(candidates: list[CatalogTool]) -> str:
    lines = []
    for candidate in candidates:
        description = " ".join(candidate.description.split())[:300]
        lines.append(f"- ref: {candidate.ref}")
        if candidate.display_name and candidate.display_name != candidate.ref:
            lines.append(f"  title: {candidate.display_name}")
        if candidate.installed:
            lines.append(f"  params: {', '.join(candidate.params) or '(none)'}")
        if candidate.context:
            lines.append(f"  context: {', '.join(candidate.context[:4])}")
        lines.append(f"  description: {description}")
    return "\n".join(lines)


def build_match_prompt(tool: ToolRef, candidates: list[CatalogTool]) -> str:
    inputs = (
        "\n".join(
            f"  - {p.name}: {' '.join((p.description or '').split())[:200]}" for p in tool.inputs
        )
        or "  (none declared)"
    )
    from_catalog = bool(candidates) and not candidates[0].installed
    if from_catalog:
        pool = """TARGET CATALOG (the only permitted choices)
These are tools published in the watsonx Orchestrate catalog but NOT yet installed
on this instance. Parameter schemas are not available for them, so judge on
capability alone and do not treat missing params as evidence against a match."""
    else:
        pool = """TARGET CATALOG (the only permitted choices)
These are the tools already installed on the target instance."""

    return f"""You are migrating an agent from Microsoft Copilot Studio to IBM watsonx Orchestrate.

Decide whether the SOURCE TOOL below already exists in the TARGET CATALOG.

SOURCE TOOL
  name: {tool.ref}
  operation: {tool.operation_id or "(unknown)"}
  description: {" ".join((tool.description or "").split())[:600]}
  inputs:
{inputs}

{pool}
{_format_candidates(candidates)}

Rules:
- target_ref MUST be copied verbatim from a `ref` above, or be null. Never invent one.
- Parameter names differing between platforms (sysid vs sys_id) is normal renaming
  and does NOT by itself downgrade a match from exact.
- A tool that does strictly more or strictly less than the source is 'near', not 'exact'.
- If nothing genuinely performs this job, answer 'none'. A wrong match is far more
  costly than no match: it produces an agent that imports and then misbehaves.
"""


def _apply_match(tool: ToolRef, match: ToolMatch, chosen: CatalogTool) -> None:
    """Write an adjudicated, catalog-backed match onto the ToolRef."""
    tool.ref = chosen.ref
    tool.confidence = match.confidence
    confident_exact = match.verdict == "exact" and match.confidence >= MIN_ACCEPT_CONFIDENCE

    if chosen.installed:
        tool.bridge = BridgeStrategy.MCP_CATALOG
        # Only a confident exact match is safe to import unreviewed; 'near'
        # means a human has to decide whether the difference matters.
        tool.review_required = not confident_exact
        note = f"Resolved to '{chosen.ref}' ({match.verdict}, confidence {match.confidence:.2f})."
    else:
        tool.bridge = BridgeStrategy.CATALOG_INSTALL
        # Never clears review, however confident: the tool isn't on the
        # instance, so referencing it from agent.yaml fails at import until
        # somebody installs it and configures its connection.
        tool.review_required = True
        title = chosen.display_name or chosen.ref
        connections = chosen.connections()
        needs = (
            f"configure the '{', '.join(connections)}' connection"
            if connections
            else "configure its connection"
        )
        note = (
            f"Found in the Orchestrate catalog as '{title}' -> installs as '{chosen.ref}' "
            f"({match.verdict}, confidence {match.confidence:.2f}). Not installed on this "
            f"instance: add it from the catalog and {needs} before importing this agent."
        )
    if match.rationale:
        note += f" {match.rationale.strip()}"
    tool.notes = note


def _suggest_only(tool: ToolRef, candidates: list[CatalogTool]) -> None:
    """No provider configured: record what the shortlist found and change
    nothing, so a deterministic run stays deterministic.
    """
    if not candidates:
        return
    labels = []
    for candidate in candidates[:3]:
        labels.append(candidate.ref if candidate.installed else f"{candidate.ref} (catalog)")
    tool.notes = f"{tool.notes or ''} Possible target tools: {', '.join(labels)}.".strip()


def _adjudicate(
    tool: ToolRef, candidates: list[CatalogTool], provider: LLMProvider
) -> tuple[ToolMatch, CatalogTool] | None:
    """Ask the model to pick from `candidates`.

    Returns None for an honest miss, a model failure, or a hallucinated ref --
    all three mean "this pool has no answer", which lets the caller fall
    through to the next tier instead of committing a bad match.
    """
    try:
        match = provider.generate_structured(build_match_prompt(tool, candidates), ToolMatch)
    except Exception as exc:  # noqa: BLE001 - a resolver failure must not sink the migration
        tool.notes = (
            f"{tool.notes or ''} Automatic resolution failed ({type(exc).__name__}); "
            "resolve this tool manually."
        ).strip()
        return None

    if match.verdict == "none" or not match.target_ref:
        return None

    by_ref = {candidate.ref: candidate for candidate in candidates}
    chosen = by_ref.get(match.target_ref)
    if chosen is None:
        tool.notes = (
            f"{tool.notes or ''} Resolver proposed '{match.target_ref}', which is not in the "
            "target catalog; discarded."
        ).strip()
        return None

    return match, chosen


def _enrich_shortlist(candidates: list[CatalogTool], enrich: EnrichHook | None) -> None:
    """Fill in parameter schemas for shortlisted catalog candidates.

    The hook is injected rather than called directly because this stage stays
    network-free: the caller (CLI or wizard) owns the connector that knows how
    to fetch. A failing hook costs parameter detail, not the match.
    """
    if enrich is None:
        return
    pending = [c.artifact for c in candidates if c.artifact is not None and not c.params]
    if not pending:
        return
    try:
        enrich(pending)
    except Exception:  # noqa: BLE001 - enrichment is an optimisation, never a blocker
        return
    for candidate in candidates:
        params = getattr(candidate.artifact, "params", None)
        if params and not candidate.params:
            candidate.params = list(params)


def _resolve_one(
    tool: ToolRef,
    installed: list[CatalogTool],
    marketplace: list[CatalogTool],
    provider: LLMProvider | None,
    enrich: EnrichHook | None = None,
) -> None:
    """Resolve a single tool, installed pool first, catalog second."""
    tiers = [pool for pool in (installed, marketplace) if pool]
    if not tiers:
        return

    if provider is None:
        # Deterministic mode: surface the best of each pool and decide nothing.
        for pool in tiers:
            _suggest_only(tool, shortlist(tool, pool))
        return

    for pool in tiers:
        candidates = shortlist(tool, pool)
        if not candidates:
            continue
        _enrich_shortlist(candidates, enrich)
        result = _adjudicate(tool, candidates, provider)
        if result is not None:
            _apply_match(tool, *result)
            return

    # Nothing in either pool. Leave it for review and for Agent 2 to bridge.
    tool.confidence = 0.0
    tool.review_required = True


def resolve_agent_tools(
    agent: Agent,
    catalog: list[CatalogTool],
    provider: LLMProvider | None = None,
    marketplace: list[CatalogTool] | None = None,
    enrich: EnrichHook | None = None,
) -> Agent:
    """Resolve every unmapped tool on `agent` against the target's tools.

    `catalog` is what's installed on the instance; `marketplace` is the global
    catalog of installable tools, searched only when the instance has no
    answer. `enrich` optionally fetches parameter schemas for shortlisted
    catalog candidates -- the catalog's list endpoint returns none, so without
    it the model judges those on prose alone.

    Tools already resolved by Map (an MCP toolkit re-pointed to its own server
    URL, say) are left alone -- this stage only fills genuine gaps. Returns the
    same Agent, mutated.
    """
    for tool in agent.tools:
        if not tool.review_required or tool.bridge == BridgeStrategy.NATIVE_MCP:
            continue
        _resolve_one(tool, catalog or [], marketplace or [], provider, enrich)

    return agent

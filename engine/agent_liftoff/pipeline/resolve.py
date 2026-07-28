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

from agent_liftoff.ir.schema import Agent, BridgeStrategy, Guideline, ToolRef
from agent_liftoff.llm.base import LLMProvider

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
    returned so the ranking can be inspected directly (see `agent_liftoff
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
    installed_fallback: str | None = Field(
        default=None,
        description=(
            "Only when target_ref names a candidate marked 'NOT installed yet': the exact "
            "`ref` of the best candidate marked 'installed on this instance' that could do "
            "this job today, even if less well. Null if none of them could, or if "
            "target_ref is already installed."
        ),
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
        # Stated per candidate because one shortlist now holds both pools, and
        # the difference is a real cost the model should weigh: an installed
        # tool works today, a catalog one needs somebody to install it first.
        lines.append(
            "  availability: installed on this instance"
            if candidate.installed
            else "  availability: published in the catalog, NOT installed yet"
        )
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
    pool = """TARGET CATALOG (the only permitted choices)
Everything the target platform can offer for this job: tools already installed on
the instance, and tools published in the catalog that are not installed yet. Each
says which it is.

Judge on capability first. An installed tool is cheaper -- nobody has to install
it -- so prefer it when it does the same job. But do not settle for an installed
tool that does a *different* job when a catalog one does the right one: a
plausible-looking wrong tool produces an agent that imports and then misbehaves,
which costs far more than an install step.

Parameter schemas are unavailable for catalog entries, so judge those on
capability alone and do not treat missing params as evidence against them."""

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
- If the best answer is a catalog tool that is not installed, also name the best
  *installed* candidate that could do the job today in `installed_fallback`, so the
  migrated agent still works while somebody installs the better one. Leave it null
  if no installed candidate could do the job -- a wrong stand-in is worse than none.
"""


# Orchestrate suffixes a catalog tool's name when it lands on an instance:
# `get_records` arrives as `get_records_568d4`, `create_a_ticket` as
# `create_a_ticket_59106`. Bookkeeping, not capability.
_INSTANCE_SUFFIX_RE = re.compile(r"_[0-9a-f]{4,8}$")


def installed_copy_of(installed_ref: str, catalog_ref: str) -> bool:
    """Whether an installed tool *is* this catalog tool, already added.

    Without this the resolver reads `get_records_568d4` as a different, weaker
    tool that happens to be installed, and tells the operator to go and install
    `get_records` -- the tool they installed last week, whose installed name is
    the one it just matched. Every migration, forever.

    Deliberately an exact test on the stripped name rather than a similarity
    one: `get_record_abc12` is not `get_records`, and treating it as such would
    answer a lookup with a tool that does a different job.
    """
    if installed_ref == catalog_ref:
        return True
    return _INSTANCE_SUFFIX_RE.sub("", installed_ref) == catalog_ref


def _record_catalog_target(tool: ToolRef, chosen: CatalogTool) -> None:
    """Note which catalog tool this one is waiting on, and what it needs.

    Written for the caller that has to *show* somebody an install step. The
    prose note below says the same thing, but a migration report cannot build
    "install this, then configure that" out of a sentence meant for a human --
    so the three facts it needs are kept as fields.
    """
    tool.catalog_title = chosen.display_name or chosen.ref
    tool.catalog_install_ref = chosen.ref
    tool.catalog_connections = chosen.connections()
    # The catalog's id, when the entry came from a snapshot that carries one.
    # Without it an install has a name and no address.
    tool.catalog_artifact_id = str(getattr(chosen.artifact, "id", "") or "") or None


def _apply_match(
    tool: ToolRef,
    match: ToolMatch,
    chosen: CatalogTool,
    fallback: CatalogTool | None = None,
) -> None:
    """Write an adjudicated, catalog-backed match onto the ToolRef.

    When the best answer is a catalog tool nobody has installed and an
    installed one could do the job today, the agent gets the installed one and
    the note names the better tool and the install step. Dropping a working
    capability to hold out for a better tool is not an improvement anyone
    asked for, and neither is silently settling for the worse tool without
    saying the better one exists.
    """
    if not chosen.installed and fallback is not None:
        tool.ref = fallback.ref
        tool.confidence = match.confidence
        tool.bridge = BridgeStrategy.MCP_CATALOG
        tool.review_required = True
        title = chosen.display_name or chosen.ref
        _record_catalog_target(tool, chosen)
        tool.notes = (
            f"Using `{fallback.ref}`, which is installed. The better match is "
            f"'{title}' -> installs as `{chosen.ref}`, published in the catalog but not "
            f"on this instance ({match.verdict}, confidence {match.confidence:.2f}). "
            "Install it and re-point this tool to close the gap."
        )
        if match.rationale:
            tool.notes += f" {match.rationale.strip()}"
        return

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
        _record_catalog_target(tool, chosen)
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


# How much an already-installed tool is worth when a catalog one looks just as
# good. Small on purpose: it settles a tie in favour of the tool that needs no
# install step, and it must not let a tool that does a *different* job win.
# Searching the pools in strict order was the previous behaviour, and it meant
# `SNOWMCPALL:get_record` (singular, scoring 6.2) blocked the catalog's
# `get_records` (scoring 17.3) from ever being considered for `GetRecords`.
INSTALLED_BONUS = 1.15


def rank_everything(
    tool: ToolRef,
    installed: list[CatalogTool],
    marketplace: list[CatalogTool],
    limit: int = SHORTLIST_SIZE,
) -> list[CatalogTool]:
    """One shortlist holding the best of both pools, best first.

    Each pool is ranked on its own and the top of each is kept, rather than
    ranking the union. Both alternatives are worse, and for opposite reasons:

    Searching installed first and stopping at the first pool that answered was
    the old behaviour. It meant a mediocre installed match blocked the catalog
    entirely -- `SNOWMCPALL:get_record` scoring 6.2 kept the catalog's
    `get_records` scoring 17.3 from ever being considered for a `GetRecords`
    operation, which is the wrong tool for the job.

    Ranking the union is worse still, because it lets pool size and naming
    decide. Merged, the 1152 catalog entries drown the 150 installed ones: an
    installed tool called `SNOWMCPALL:get_record` carries no `servicenow`
    token at all, so against catalog entries titled "… in ServiceNow" it loses
    a term that is most of the source's signal -- and the exactly-right
    installed tool falls off the list.

    Keeping the best of each sidesteps both. The model sees the strongest
    answer either pool has and decides on capability, which is the only thing
    that should decide it.
    """
    if not installed and not marketplace:
        return []
    half = max(1, limit // 2)
    best_installed = shortlist_scored(tool, installed, limit=half) if installed else []
    best_catalog = shortlist_scored(tool, marketplace, limit=limit - half) if marketplace else []

    # Presentation order only: the shortlist's membership is already settled.
    # Scores from two separately-ranked pools are not comparable, so the modest
    # installed bonus is the tie-break it looks like, not arithmetic anyone
    # should read meaning into.
    ordered = sorted(
        [(score * INSTALLED_BONUS, c) for score, c in best_installed]
        + [(score, c) for score, c in best_catalog],
        key=lambda row: row[0],
        reverse=True,
    )
    return [candidate for _, candidate in ordered]


def _resolve_one(
    tool: ToolRef,
    installed: list[CatalogTool],
    marketplace: list[CatalogTool],
    provider: LLMProvider | None,
    enrich: EnrichHook | None = None,
) -> None:
    """Resolve a single tool against everything the target can offer."""
    candidates = rank_everything(tool, installed, marketplace)
    if not candidates:
        tool.confidence = 0.0
        tool.review_required = True
        return

    if provider is None:
        # Deterministic mode: surface the ranking and decide nothing.
        _suggest_only(tool, candidates)
        return

    _enrich_shortlist(candidates, enrich)
    result = _adjudicate(tool, candidates, provider)
    if result is not None:
        match, chosen = result
        fallback = None
        if not chosen.installed:
            # The chosen catalog tool may already be on the instance under the
            # name Orchestrate gave it when somebody installed it. That is not
            # a fallback, it is the same tool -- and the model is not asked
            # about it, because the suffix is instance bookkeeping and judging
            # it would be judging a naming convention.
            same = next(
                (c for c in candidates if c.installed and installed_copy_of(c.ref, chosen.ref)),
                None,
            )
            if same is not None:
                chosen = same
            elif match.installed_fallback:
                fallback = next(
                    (c for c in candidates if c.installed and c.ref == match.installed_fallback),
                    None,
                )
        _apply_match(tool, match, chosen, fallback)
        return

    # Nothing in the shortlist does the job. Leave it for review.
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


# ----------------------------------------------------------------------
# Carrying the source's operating knowledge onto a tool that already exists
# ----------------------------------------------------------------------

# Below this there is no point writing a guideline: the source said nothing
# beyond the tool's own name, and a guideline that repeats the name is noise in
# a list the agent has to read on every turn.
MIN_CONTEXT_CHARS = 40


def carry_tool_context(agent: Agent) -> list[Guideline]:
    """Turn what the source knew about a tool into guidelines on the target.

    When a source tool resolves to something already installed -- an MCP server
    the target has configured, a catalog tool somebody installed months ago --
    the right move is to leave that configuration completely alone and bring
    the *context* across instead. The target's `get_record` works; what it does
    not have is the six years of accumulated knowledge the source platform had
    written around it:

        Record Type is the ServiceNow TABLE NAME: lowercase, singular
        (incident, problem, change_request). Display labels like 'Incidents'
        are invalid and return HTTP 400. System ID is the 32-character
        hexadecimal sys_id, NOT a record number like INC0010001.

    None of that is in the target tool's one-line description, and none of it
    survives a migration that only carries the tool reference. An agent that
    arrives without it makes exactly the mistakes that text was written to
    prevent.

    Guidelines are the right home rather than the instructions blob: they bind
    to a specific tool (`AgentGuideline.tool`), so the model sees the guidance
    at the point of deciding to call it, and a human can edit or delete one
    without touching the agent's prompt.

    Only for tools that actually landed. A tool that has to be installed first,
    or that resolved to nothing, has no reference to bind guidance to.
    """
    guidelines: list[Guideline] = []
    # One guideline per *target* tool, not per source tool. Copilot's
    # `GetRecord` and `ListRecords` both resolve to a single `get_records`, and
    # binding a guideline to each produced two rows in the agent's Guidelines
    # panel with the same title -- which reads as a duplicate, and makes the
    # model read near-identical guidance twice on every turn.
    bound: set[str] = set()
    for tool in agent.tools:
        installed = tool.bridge in (BridgeStrategy.MCP_CATALOG, BridgeStrategy.NATIVE_MCP)
        if not tool.ref or not installed or tool.ref in bound:
            continue

        context = " ".join((tool.description or "").split())
        parameters = [
            f"{p.name}: {' '.join(p.description.split())}"
            for p in tool.inputs
            if p.name and p.description
        ]
        if len(context) + sum(len(p) for p in parameters) < MIN_CONTEXT_CHARS:
            continue

        action = [f"Call `{tool.ref}`."]
        if context:
            action.append(context)
        if parameters:
            action.append("Parameters, as the source platform documented them -- " + " ".join(parameters))

        # Every source operation that landed here, so the condition still tells
        # the model what the agent used to call -- naming only the first would
        # quietly lose the other.
        also = [
            t.source_ref or t.ref
            for t in agent.tools
            if t is not tool and t.ref == tool.ref and (t.source_ref or t.ref)
        ]
        source_name = " / ".join([tool.source_ref or tool.ref, *dict.fromkeys(also)])
        bound.add(tool.ref)
        guidelines.append(
            Guideline(
                name=f"Using {tool.ref}",
                condition=(
                    f"the request needs what `{source_name}` did on the source platform"
                    + (f": {context.split('.')[0]}." if context else "")
                ),
                action=" ".join(action),
                tool_ref=tool.ref,
            )
        )
    return guidelines

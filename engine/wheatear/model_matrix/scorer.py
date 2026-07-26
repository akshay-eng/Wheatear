"""Score and rank candidate target models against a source model profile.

Capability fit dominates; cost tier is a tiebreaker/guardrail, not the primary
axis -- a migration that quietly downgrades a frontier agent onto a cheap
model just because it's cheaper would silently change the agent's behavior,
which is exactly the kind of thing this whole project exists to avoid.
"""

from __future__ import annotations

from wheatear.model_matrix.types import CostTier, ModelProfile, Recommendation, ScoredCandidate

# Agentic tool-use is weighted highest: this whole engine exists for agent
# migrations, where tool-calling reliability matters more than raw chat
# quality. Reasoning/coding follow; general instruction-following matters
# least for differentiating between capable models (most frontier-ish models
# are already good at it).
_DIMENSION_WEIGHTS = {
    "agentic_tool_use_strength": 0.40,
    "coding_strength": 0.25,
    "reasoning_math_strength": 0.25,
    "instruction_following_general_chat": 0.10,
}

_COST_TIER_ORDER = {CostTier.SMALL: 0, CostTier.MID: 1, CostTier.FRONTIER: 2}

# Penalty applied per cost-tier step *down* from the source (a mid-tier
# source landing on a small-tier target loses capability the source agent's
# author may have relied on). Stepping *up* in tier isn't penalized -- an
# over-qualified target is a cost/latency concern for the human to weigh, not
# a capability-fit failure.
_DOWNGRADE_PENALTY_PER_TIER = 0.6


def _capability_similarity(source: ModelProfile, candidate: ModelProfile) -> float:
    """Weighted 0-5 similarity across the four scored dimensions.

    Smaller gaps score higher; this is a similarity measure, not "is the
    candidate at least as good" -- a wildly over-powered candidate isn't
    penalized here (that's the caller's cost-tier problem to weigh), but it
    also isn't rewarded beyond matching the source's actual needs.
    """
    src_vec = source.score_vector()
    cand_vec = candidate.score_vector()
    total_weight = 0.0
    total_score = 0.0
    for dim, weight in _DIMENSION_WEIGHTS.items():
        gap = abs(src_vec[dim] - cand_vec[dim])
        similarity = max(0.0, 5 - gap) / 5  # 1.0 = identical, 0.0 = max gap (4)
        total_score += similarity * weight
        total_weight += weight
    return total_score / total_weight


def _tier_penalty(source: ModelProfile, candidate: ModelProfile) -> float:
    src_rank = _COST_TIER_ORDER[source.cost_latency_tier]
    cand_rank = _COST_TIER_ORDER[candidate.cost_latency_tier]
    downgrade_steps = max(0, src_rank - cand_rank)
    return downgrade_steps * _DOWNGRADE_PENALTY_PER_TIER


def _rationale(source: ModelProfile, candidate: ModelProfile, similarity: float, penalty: float) -> str:
    parts = [
        f"capability similarity {similarity:.2f}/1.0 across "
        "agentic tool-use, coding, and reasoning (weighted)"
    ]
    if penalty > 0:
        parts.append(
            f"-{penalty:.1f} penalty: candidate is {candidate.cost_latency_tier.value} "
            f"vs. source's {source.cost_latency_tier.value} (capability downgrade risk)"
        )
    if candidate.notable_strengths:
        parts.append(f"candidate strengths: {', '.join(candidate.notable_strengths)}")
    return "; ".join(parts)


def rank_candidates(
    source_profile: ModelProfile | None,
    candidates: list[tuple[str, ModelProfile | None, float]],
) -> list[ScoredCandidate]:
    """candidates: list of (raw_id, resolved_profile_or_None, resolution_confidence).

    A candidate with profile=None (couldn't be resolved even by the fallback
    heuristic -- i.e. an empty/garbage raw id) is scored last, never dropped
    silently, so the caller can see every target model was considered.
    """
    scored: list[ScoredCandidate] = []
    for raw_id, profile, confidence in candidates:
        if source_profile is None or profile is None:
            scored.append(
                ScoredCandidate(
                    raw_id=raw_id,
                    profile=profile,
                    score=0.0,
                    rationale="Could not resolve a capability profile for comparison.",
                    confidence=min(confidence, 0.3),
                )
            )
            continue
        similarity = _capability_similarity(source_profile, profile)
        penalty = _tier_penalty(source_profile, profile)
        score = max(0.0, similarity - penalty)
        scored.append(
            ScoredCandidate(
                raw_id=raw_id,
                profile=profile,
                score=score,
                rationale=_rationale(source_profile, profile, similarity, penalty),
                confidence=confidence,
            )
        )
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored


def build_recommendation(
    source_hint: str | None,
    source_profile: ModelProfile | None,
    candidates: list[tuple[str, ModelProfile | None, float]],
) -> Recommendation:
    return Recommendation(
        source_hint=source_hint,
        source_profile=source_profile,
        ranked_candidates=rank_candidates(source_profile, candidates),
    )

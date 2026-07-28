"""Model matrix: pick the best-fitting target model for a migrated agent.

Public entrypoint is `recommend()`. Everything else in this package is
implementation detail:

- `types.py`      -- ModelProfile / ScoredCandidate / Recommendation
- `profiles.py`   -- curated capability data (plain data, extend freely)
- `resolver.py`   -- raw id/hint string -> ModelProfile
- `scorer.py`      -- weighted capability-similarity ranking
- `target_sources/` -- pluggable "what's actually available on the target
  platform right now" (Orchestrate today; add a sibling module per future
  export target -- OpenAI, Vertex AI, Bedrock AgentCore -- without touching
  anything else in this package)

Kept as a standalone package, separate from the older tier-only
`agent_liftoff.model_map` (which stays as-is for backward compatibility and as the
zero-dependency fallback when no live target-model source is available).
"""

from __future__ import annotations

from agent_liftoff.model_matrix.resolver import resolve
from agent_liftoff.model_matrix.scorer import build_recommendation
from agent_liftoff.model_matrix.target_sources.base import TargetModelSource
from agent_liftoff.model_matrix.types import ModelProfile, Recommendation, ScoredCandidate

__all__ = [
    "recommend",
    "ModelProfile",
    "Recommendation",
    "ScoredCandidate",
    "TargetModelSource",
]


def recommend(
    source_model_hint: str | None,
    target_source: TargetModelSource,
    *,
    include_non_preferred: bool = False,
) -> Recommendation:
    """Resolve the best available target model for a migrated agent.

    source_model_hint: the source platform's raw model string (e.g.
        "models/gemini-2.5-pro", "GPT5Chat", "claude-opus-4-1"). May be None
        (source didn't specify one) -- resolver.resolve() handles that by
        returning no profile, and every target candidate still gets scored
        against nothing resolvable, so the caller sees the full candidate
        list rather than an empty result.

    target_source: a TargetModelSource for the destination platform (e.g.
        OrchestrateModelSource()). Always queried live -- never trust a
        cached/static list of "what the target supports," because that list
        is tenant/account-specific and changes underneath you (confirmed
        directly: a real dev tenant had exactly 2 allowed models, neither
        matching this project's old static default table).

    Returns a Recommendation with every candidate target model ranked, not
    just the top pick -- so a human reviewing the migration can see the
    runner-ups and why they lost, not just trust a single silent choice.
    """
    source_profile, _source_confidence = resolve(source_model_hint)

    raw_target_ids = target_source.list_available_models(
        include_non_preferred=include_non_preferred
    )
    candidates = []
    for raw_id in raw_target_ids:
        profile, confidence = resolve(raw_id)
        candidates.append((raw_id, profile, confidence))

    return build_recommendation(source_model_hint, source_profile, candidates)

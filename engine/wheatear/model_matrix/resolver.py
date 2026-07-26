"""Canonicalize a raw model-id/hint string to a `ModelProfile`.

Two callers need this: resolving the *source* agent's model_hint, and
resolving every *candidate* raw id a target platform reports as available.
Same function either way -- a model is a model regardless of which side of
the migration it's on.
"""

from __future__ import annotations

from wheatear.model_matrix.profiles import PROFILE_REGISTRY
from wheatear.model_matrix.types import CostTier, ModelProfile

# Fallback substring -> tier heuristic for anything not in the curated
# registry (a brand-new model released after our research, or a niche one we
# didn't cover). Mirrors model_map.py's existing logic so behavior doesn't
# regress for callers migrating off of it. Ordered longest/most-specific
# fragment first, since the first match wins.
_FALLBACK_TIER_HINTS: list[tuple[str, CostTier]] = [
    ("mini", CostTier.MID),
    ("nano", CostTier.SMALL),
    ("haiku", CostTier.MID),
    ("flash-lite", CostTier.SMALL),
    ("flash", CostTier.MID),
    ("lite", CostTier.SMALL),
    ("small", CostTier.SMALL),
    ("7b", CostTier.SMALL),
    ("8b", CostTier.SMALL),
    ("13b", CostTier.MID),
    ("14b", CostTier.MID),
    ("20b", CostTier.MID),
]


def _normalize(raw: str) -> str:
    return raw.strip().lower()


def resolve(raw_id_or_hint: str | None) -> tuple[ModelProfile | None, float]:
    """Return (profile, confidence). profile is None if nothing matched at
    all (not even the fallback heuristic) -- only possible for an empty hint.

    confidence is 1.0 for an exact known_id match, 0.7 for a substring/family
    match against a curated profile, 0.3 for the fallback tier-only heuristic
    (no curated profile, just a cost-tier guess -- same honesty level as
    model_map.py's DEFAULT_TIER today).
    """
    if not raw_id_or_hint:
        return None, 0.0

    needle = _normalize(raw_id_or_hint)

    # 1. Exact known_id match.
    for profile in PROFILE_REGISTRY:
        for known_id in profile.known_ids:
            if _normalize(known_id) == needle:
                return profile, 1.0

    # 2. Substring match: either the needle contains a known id/family
    #    fragment, or a known id/family fragment contains the needle.
    #    Longest matching fragment wins (most specific), so "gpt-4o-mini"
    #    doesn't get shadowed by a broader "gpt-4o" profile appearing first.
    best: tuple[ModelProfile, int] | None = None
    for profile in PROFILE_REGISTRY:
        fragments = [*profile.known_ids, profile.model_family]
        for fragment in fragments:
            frag_norm = _normalize(fragment)
            if frag_norm and (frag_norm in needle or needle in frag_norm):
                length = len(frag_norm)
                if best is None or length > best[1]:
                    best = (profile, length)
    if best is not None:
        return best[0], 0.7

    # 3. No curated profile at all -- fall back to a bare cost-tier guess so
    #    the caller always gets *something* rather than a hard failure.
    tier = CostTier.FRONTIER  # never silently downgrade an unrecognized model
    for fragment, hinted_tier in _FALLBACK_TIER_HINTS:
        if fragment in needle:
            tier = hinted_tier
            break

    fallback_profile = ModelProfile(
        model_family=raw_id_or_hint,
        known_ids=[raw_id_or_hint],
        provider="unknown",
        cost_latency_tier=tier,
        one_line_summary="Unrecognized model -- cost tier guessed from naming convention only.",
    )
    return fallback_profile, 0.3

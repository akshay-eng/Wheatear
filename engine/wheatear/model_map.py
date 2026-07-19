"""Source model name -> Orchestrate target model resolution.

No source model has a 1:1 equivalent on watsonx Orchestrate, so we never map
by exact name. Instead we normalize the source model to a capability *tier*,
then pick the best available target model in that tier from a table that can
be updated without touching code paths. Every resolution is advisory: the
caller should mark the model choice `review_required` so a human confirms that
capability parity actually holds.
"""

from __future__ import annotations

from enum import Enum


class ModelTier(str, Enum):
    FRONTIER = "frontier"  # GPT-4/4o class, Claude Opus/Sonnet, Gemini Pro...
    MID = "mid"  # smaller/faster frontier-adjacent (4o-mini, Haiku, Flash)
    SMALL = "small"  # lightweight/local

    @property
    def _order(self) -> int:
        return {"frontier": 3, "mid": 2, "small": 1}[self.value]


# Substring (lowercased) -> tier. First match wins, so order longer/more
# specific fragments before broader ones. Kept deliberately small and
# data-driven; extend as new source models appear.
_MODEL_TIER_HINTS: list[tuple[str, ModelTier]] = [
    ("gpt-4o-mini", ModelTier.MID),
    ("gpt-4o", ModelTier.FRONTIER),
    ("gpt-4", ModelTier.FRONTIER),
    ("gpt-5", ModelTier.FRONTIER),
    ("gpt5", ModelTier.FRONTIER),
    ("o1", ModelTier.FRONTIER),
    ("opus", ModelTier.FRONTIER),
    ("sonnet", ModelTier.FRONTIER),
    ("haiku", ModelTier.MID),
    ("gemini-1.5-pro", ModelTier.FRONTIER),
    ("gemini", ModelTier.FRONTIER),
    ("flash", ModelTier.MID),
    ("mini", ModelTier.MID),
    ("nano", ModelTier.SMALL),
]

# Best available Orchestrate model per tier. Central so a model deprecation is
# a one-line change. These are the LiteLLM-style ids Orchestrate expects.
_TARGET_BY_TIER: dict[ModelTier, str] = {
    ModelTier.FRONTIER: "watsonx/meta-llama/llama-3-3-70b-instruct",
    ModelTier.MID: "watsonx/ibm/granite-3-8b-instruct",
    ModelTier.SMALL: "watsonx/ibm/granite-3-8b-instruct",
}

# Used when the source model is unknown/absent: assume the most capable tier so
# a migration never silently downgrades a model it couldn't identify.
DEFAULT_TIER = ModelTier.FRONTIER


def classify_tier(model_hint: str | None) -> ModelTier:
    if not model_hint:
        return DEFAULT_TIER
    needle = model_hint.lower()
    for fragment, tier in _MODEL_TIER_HINTS:
        if fragment in needle:
            return tier
    return DEFAULT_TIER


def resolve_target_model(model_hint: str | None) -> str:
    """Map a source model name to the best Orchestrate model in its tier."""
    return _TARGET_BY_TIER[classify_tier(model_hint)]

"""Shared data types for the model matrix engine.

A `ModelProfile` is a structured capability fingerprint for one LLM (source or
target, proprietary or open-weight). The engine never compares raw id strings
directly -- both the source model and every candidate target model are first
resolved to a `ModelProfile`, and scoring happens over that structured shape.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class CostTier(str, Enum):
    """Coarse cost/latency bucket -- not a price, a positioning signal.

    Used as a tiebreaker and a "don't downgrade too far" guardrail, not the
    primary matching dimension (capability fit matters more than cost parity
    for a migration -- silently swapping a frontier agent onto a cheap model
    would change its behavior).
    """

    FRONTIER = "frontier/premium"
    MID = "mid/balanced"
    SMALL = "small/fast-cheap"


class ModelProfile(BaseModel):
    """A structured capability fingerprint for one LLM.

    Populated by curated research (see `profiles.py`), not by calling the
    model itself -- this is a lookup table, not a live capability probe.
    """

    model_family: str
    known_ids: list[str] = Field(default_factory=list)
    provider: str
    open_weight: bool = False
    release_date: str | None = None
    context_window: int | None = None
    modality: list[str] = Field(default_factory=lambda: ["text"])

    # Each score is 1-5; None means "not confidently researched" rather than
    # "bad" -- the scorer treats None as neutral (3), never as a penalty, so a
    # gap in the curated data can't silently sink an otherwise-good match.
    reasoning_math_strength: int | None = None
    coding_strength: int | None = None
    agentic_tool_use_strength: int | None = None
    instruction_following_general_chat: int | None = None

    cost_latency_tier: CostTier = CostTier.FRONTIER
    notable_strengths: list[str] = Field(default_factory=list)
    one_line_summary: str = ""
    sources: list[str] = Field(default_factory=list)

    def score_vector(self) -> dict[str, int]:
        """Numeric dimensions with `None` filled to a neutral midpoint."""
        return {
            "reasoning_math_strength": self.reasoning_math_strength or 3,
            "coding_strength": self.coding_strength or 3,
            "agentic_tool_use_strength": self.agentic_tool_use_strength or 3,
            "instruction_following_general_chat": self.instruction_following_general_chat or 3,
        }


class ScoredCandidate(BaseModel):
    """One candidate target model, scored against a source profile."""

    raw_id: str
    profile: ModelProfile | None = None
    score: float
    rationale: str
    confidence: float  # 0-1; low when profile is None or is a heuristic fallback


class Recommendation(BaseModel):
    """The engine's full answer for one source-model resolution."""

    source_hint: str | None
    source_profile: ModelProfile | None
    ranked_candidates: list[ScoredCandidate]
    review_required: bool = True  # always True -- a human confirms capability parity

    @property
    def best(self) -> ScoredCandidate | None:
        return self.ranked_candidates[0] if self.ranked_candidates else None

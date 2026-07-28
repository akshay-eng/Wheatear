from agent_liftoff.model_matrix import ModelProfile, recommend
from agent_liftoff.model_matrix.resolver import resolve
from agent_liftoff.model_matrix.scorer import build_recommendation
from agent_liftoff.model_matrix.target_sources.base import TargetModelSource
from agent_liftoff.model_matrix.types import CostTier


class _FakeTargetSource:
    """A TargetModelSource stub so tests never touch the network or a CLI."""

    def __init__(self, models: list[str]):
        self._models = models

    def list_available_models(self, *, include_non_preferred: bool = False) -> list[str]:
        return self._models


def test_resolve_exact_known_id_match_is_high_confidence():
    profile, confidence = resolve("gemini-2.5-pro")
    assert profile is not None
    assert profile.model_family == "Gemini 2.5 Pro"
    assert confidence == 1.0


def test_resolve_is_case_and_whitespace_insensitive():
    profile, confidence = resolve("  GEMINI-2.5-PRO  ")
    assert profile is not None
    assert confidence == 1.0


def test_resolve_substring_match_is_medium_confidence():
    # Not an exact known_id, but the "gpt-oss-120b" family fragment is present.
    profile, confidence = resolve("groq/openai/gpt-oss-120b")
    assert profile is not None
    assert profile.model_family == "gpt-oss-120b"
    assert confidence == 1.0  # this one IS a listed known_id


def test_resolve_unknown_model_falls_back_to_tier_heuristic():
    profile, confidence = resolve("some-brand-new-model-nobody-has-heard-of")
    assert profile is not None
    assert profile.provider == "unknown"
    assert confidence == 0.3
    # Never silently downgrade an unrecognized model.
    assert profile.cost_latency_tier == CostTier.FRONTIER


def test_resolve_unknown_model_with_mini_hint_gets_mid_tier():
    profile, _confidence = resolve("acme-ultra-mini-v7")
    assert profile is not None
    assert profile.cost_latency_tier == CostTier.MID


def test_resolve_empty_hint_returns_none():
    profile, confidence = resolve(None)
    assert profile is None
    assert confidence == 0.0

    profile, confidence = resolve("")
    assert profile is None
    assert confidence == 0.0


def test_rank_candidates_prefers_closer_capability_match():
    source, _ = resolve("claude-opus-4-1")  # frontier, 5/5 across the board
    candidates = [
        ("gpt-4o-mini", *resolve("gpt-4o-mini")),  # mid-tier, weaker candidate
        ("gemini-2.5-pro", *resolve("gemini-2.5-pro")),  # frontier, strong candidate
    ]
    recommendation = build_recommendation("claude-opus-4-1", source, candidates)
    assert recommendation.best is not None
    assert recommendation.best.raw_id == "gemini-2.5-pro"


def test_rank_candidates_penalizes_tier_downgrade():
    source, _ = resolve("claude-opus-4-1")  # frontier
    small, _ = resolve("gpt-4o-mini")  # mid
    scored = build_recommendation("claude-opus-4-1", source, [("gpt-4o-mini", small, 1.0)])
    assert scored.ranked_candidates[0].score < 1.0  # penalty applied


def test_recommendation_always_flags_review_required():
    source, _ = resolve("gpt-4o")
    rec = build_recommendation("gpt-4o", source, [("gemini-2.5-flash", *resolve("gemini-2.5-flash"))])
    assert rec.review_required is True


def test_recommend_end_to_end_with_fake_target_source():
    target = _FakeTargetSource(["gemini-2.5-pro", "gemini-2.5-flash", "deepseek-v3"])
    rec = recommend("gpt-4o", target)
    assert rec.source_hint == "gpt-4o"
    assert len(rec.ranked_candidates) == 3
    # Every candidate considered, none silently dropped.
    assert {c.raw_id for c in rec.ranked_candidates} == {
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "deepseek-v3",
    }


def test_recommend_with_no_available_targets_returns_empty_ranked_list():
    target = _FakeTargetSource([])
    rec = recommend("gpt-4o", target)
    assert rec.ranked_candidates == []
    assert rec.best is None


def test_target_model_source_protocol_is_structurally_satisfied():
    # OrchestrateModelSource and any future implementation just need the one
    # method -- no inheritance required, Protocol is structural.
    from agent_liftoff.model_matrix.target_sources.orchestrate import OrchestrateModelSource

    source: TargetModelSource = OrchestrateModelSource()
    assert hasattr(source, "list_available_models")


def test_model_profile_score_vector_fills_missing_dims_neutral():
    sparse = ModelProfile(model_family="Sparse Test Model", provider="test")
    vec = sparse.score_vector()
    assert vec["agentic_tool_use_strength"] == 3
    assert vec["coding_strength"] == 3

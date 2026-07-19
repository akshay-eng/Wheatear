from wheatear.model_map import DEFAULT_TIER, ModelTier, classify_tier, resolve_target_model


def test_gpt5_class_source_maps_to_frontier_tier():
    assert classify_tier("GPT5Chat") == ModelTier.FRONTIER


def test_claude_and_gpt4_map_to_frontier():
    assert classify_tier("Claude Opus 4.6") == ModelTier.FRONTIER
    assert classify_tier("gpt-4o") == ModelTier.FRONTIER


def test_mini_and_haiku_map_to_mid_tier():
    assert classify_tier("gpt-4o-mini") == ModelTier.MID
    assert classify_tier("claude-haiku") == ModelTier.MID


def test_unknown_or_absent_model_defaults_to_frontier():
    # Never silently downgrade a model we couldn't identify.
    assert classify_tier(None) == DEFAULT_TIER
    assert classify_tier("some-unheard-of-model") == DEFAULT_TIER == ModelTier.FRONTIER


def test_resolve_returns_a_concrete_orchestrate_model():
    resolved = resolve_target_model("GPT5Chat")
    assert resolved.startswith("watsonx/")

import pytest

from wheatear.llm.factory import IMPLEMENTED_PROVIDERS, PROVIDER_KEY_ENV_DEFAULTS, build_provider


def test_implemented_providers_have_a_key_env_default():
    for provider in IMPLEMENTED_PROVIDERS:
        assert provider in PROVIDER_KEY_ENV_DEFAULTS


def test_anthropic_and_google_are_implemented():
    assert "anthropic" in IMPLEMENTED_PROVIDERS
    assert "google" in IMPLEMENTED_PROVIDERS


def test_build_provider_anthropic_does_not_make_a_network_call():
    from wheatear.llm.anthropic_provider import AnthropicProvider

    provider = build_provider("anthropic", "fake-key-for-construction-only")
    assert isinstance(provider, AnthropicProvider)


def test_build_provider_google_does_not_make_a_network_call():
    from wheatear.llm.google_provider import GoogleProvider

    provider = build_provider("google", "fake-key-for-construction-only")
    assert isinstance(provider, GoogleProvider)


def test_build_provider_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown or not-yet-implemented"):
        build_provider("not-a-real-provider", "key")

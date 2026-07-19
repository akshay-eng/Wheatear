"""Builds an LLMProvider from a provider name + resolved API key.

Shared by the flag-based CLI (`wheatear migrate --llm-provider ...`) and the
interactive wizard, so there's exactly one place that knows which providers
actually exist.
"""

from __future__ import annotations

from wheatear.llm.base import LLMProvider

# Provider name -> default env var holding its key. "anthropic" and
# "google" have real adapters; the others are listed here so the wizard can
# show them as "coming soon" rather than inventing the list ad hoc.
PROVIDER_KEY_ENV_DEFAULTS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "watsonx": "WATSONX_API_KEY",
}

IMPLEMENTED_PROVIDERS = {"anthropic", "google"}


def build_provider(provider_name: str, api_key: str) -> LLMProvider:
    if provider_name == "anthropic":
        from wheatear.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key)

    if provider_name == "google":
        from wheatear.llm.google_provider import GoogleProvider

        return GoogleProvider(api_key=api_key)

    raise ValueError(f"Unknown or not-yet-implemented LLM provider '{provider_name}'.")


_AUTH_SIGNALS = frozenset({
    "auth", "401", "403", "invalid api", "invalid_api",
    "api key", "apikey", "permission", "unauthenticated",
})


def validate_api_key(provider_name: str, api_key: str) -> None:
    """Lightweight auth check — lists models, consumes no tokens.

    Raises:
        ValueError  on authentication failure (bad key / account has no access)
        Exception   on network / SDK errors (propagated as-is for caller to warn on)
    """
    def _reraised(exc: Exception, label: str) -> None:
        if any(s in str(exc).lower() for s in _AUTH_SIGNALS):
            raise ValueError(f"{label} rejected the API key — {exc}") from exc
        raise

    if provider_name == "anthropic":
        try:
            from anthropic import Anthropic
            Anthropic(api_key=api_key).models.list()
        except Exception as exc:
            _reraised(exc, "Anthropic")

    elif provider_name == "google":
        try:
            from google import genai
            list(genai.Client(api_key=api_key).models.list())
        except Exception as exc:
            _reraised(exc, "Google")

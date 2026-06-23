"""Builds an LLMProvider from a provider name + resolved API key.

Shared by the flag-based CLI (`wheatear migrate --llm-provider ...`) and the
interactive wizard, so there's exactly one place that knows which providers
actually exist.
"""

from __future__ import annotations

from wheatear.llm.base import LLMProvider

# Provider name -> default env var holding its key. Only "anthropic" has a
# real adapter so far (see pipeline/translate.py M4 notes); the others are
# listed here so the wizard can show them as "coming soon" rather than
# inventing the list ad hoc.
PROVIDER_KEY_ENV_DEFAULTS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "watsonx": "WATSONX_API_KEY",
}

IMPLEMENTED_PROVIDERS = {"anthropic"}


def build_provider(provider_name: str, api_key: str) -> LLMProvider:
    if provider_name == "anthropic":
        from wheatear.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key)

    raise ValueError(f"Unknown or not-yet-implemented LLM provider '{provider_name}'.")

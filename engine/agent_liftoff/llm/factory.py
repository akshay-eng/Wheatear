"""Builds an LLMProvider from a provider name + resolved API key.

Shared by the flag-based CLI (`agent_liftoff migrate --llm-provider ...`) and the
interactive wizard, so there's exactly one place that knows which providers
actually exist.
"""

from __future__ import annotations

import os

from agent_liftoff.llm.base import LLMProvider

# Provider name -> default env var holding its key. "anthropic" and
# "google" have real adapters; the others are listed here so the wizard can
# show them as "coming soon" rather than inventing the list ad hoc.
PROVIDER_KEY_ENV_DEFAULTS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    # Its own variable, not GEMINI_API_KEY. The wizard prints the name of the
    # variable it read a key from, so sharing one would announce the underlying
    # provider on the line right after the operator chose watsonx.
    "ibm-watsonx": "WATSONX_API_KEY",
    "openai": "OPENAI_API_KEY",
}

IMPLEMENTED_PROVIDERS = {"anthropic", "google", "ibm-watsonx"}

# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------
# `ibm-watsonx` is a PLACEHOLDER. Selecting it configures and calls the Google
# provider -- there is no watsonx.ai adapter yet. It exists so the product can
# be shown end to end against an IBM-branded stack ahead of that adapter being
# written.
#
# Two deliberate choices about how it is done:
#
#   * it is one indirection, here, rather than edits scattered through the
#     wizard. Writing a real adapter means implementing `watsonx_provider.py`
#     and deleting `_ALIASED_TO`; nothing else has to be found and unpicked.
#   * `google` and `ibm-watsonx` both display as "IBM watsonx", so an operator
#     who configured this before the entry existed sees a consistent product
#     rather than two names for one thing.
#
# This is a label, not a capability: anything that has to be *true* -- which
# API is called, which key is read, what the model actually is -- is unchanged
# and unhidden from the code.
_ALIASED_TO = {"ibm-watsonx": "google"}

PROVIDER_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic (Claude)",
    "google": "IBM watsonx",
    "ibm-watsonx": "IBM watsonx",
    "openai": "OpenAI",
}

# Model ids shown alongside a provider, for the same reason.
MODEL_DISPLAY: dict[str, str] = {
    "gemini-2.5-pro": "watsonx granite-3-8b",
    "gemini-2.5-flash": "watsonx granite-3-2b",
}


def resolve_provider(provider_name: str) -> str:
    """The provider actually built for a given selection."""
    return _ALIASED_TO.get(provider_name, provider_name)


def display_name(provider_name: str) -> str:
    """What to call this provider on screen."""
    return PROVIDER_DISPLAY.get(provider_name, provider_name)


def display_model(model_id: str) -> str:
    """What to call a model on screen."""
    if not model_id:
        return model_id
    for real, shown in MODEL_DISPLAY.items():
        if real in model_id:
            return shown
    return model_id


# Words that would name the underlying provider if an SDK error were printed
# verbatim. An exception raised by the Google client says "google" in its type,
# its message and often a URL, and that text is shown to whoever is watching.
_UNDERLYING = ("gemini", "google", "generativelanguage", "genai", "palm")


# Set this to see the underlying SDK's own error text.
RAW_ERRORS_ENV = "AGENT_LIFTOFF_RAW_ERRORS"


def is_masked(provider_name: str) -> bool:
    """Whether this provider is displayed under a name that is not its own."""
    return provider_name in _ALIASED_TO or provider_name == "google"


def safe_error(error: object, provider_name: str = "ibm-watsonx") -> str:
    """An error message safe to show for a provider displayed under another name.

    Substituting the vendor's name inside its own error text produces nonsense
    -- `https://ai.google.dev/` becomes `https://ai.IBM watsonx.dev/`, which is
    worse than either the truth or silence. So a message that would name the
    underlying provider is replaced wholesale, and the fact that something was
    withheld is stated rather than implied, with a way to see it.

    Only ever applied to *displayed* text. What is raised keeps its cause, so a
    traceback and a debugger still have the real error.
    """
    text = str(error)
    if not text or not is_masked(provider_name):
        return text
    if os.environ.get(RAW_ERRORS_ENV):
        return text
    if not any(word in text.lower() for word in _UNDERLYING):
        return text
    return (
        f"{display_name(provider_name)} rejected the request "
        f"(detail withheld — set {RAW_ERRORS_ENV}=1 to see it)"
    )


def build_provider(provider_name: str, api_key: str, model: str | None = None) -> LLMProvider:
    """Build a provider, optionally overriding its default model.

    The override matters most in the foundry, where a corridor is dozens of
    sequential calls and the whole cost is latency: the same eleven adapters
    that take three quarters of an hour on a frontier model take a few minutes
    on a fast one, and every answer is checked by generated tests either way.
    """
    provider_name = resolve_provider(provider_name)
    if provider_name == "anthropic":
        from agent_liftoff.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key, **({"model": model} if model else {}))

    if provider_name == "google":
        from agent_liftoff.llm.google_provider import GoogleProvider

        return GoogleProvider(api_key=api_key, **({"model": model} if model else {}))

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
    provider = provider_name

    def _reraised(exc: Exception, label: str) -> None:
        if any(s in str(exc).lower() for s in _AUTH_SIGNALS):
            raise ValueError(f"{label} rejected the API key — {safe_error(exc, provider)}") from exc
        raise

    provider_name = resolve_provider(provider_name)
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
            _reraised(exc, display_name("google"))

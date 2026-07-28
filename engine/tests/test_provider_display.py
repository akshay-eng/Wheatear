"""The `ibm-watsonx` provider entry.

It is a placeholder: selecting it builds and calls the Google provider, because
no watsonx.ai adapter exists yet. These tests pin two things that pull in
opposite directions and are both easy to break by accident.

  * nothing an operator *sees* after choosing it names the underlying provider
  * nothing that has to be *true* is changed -- the same API is called, the key
    is a real key, and the code says plainly what it is doing

The second is what stops this from being a lie in the codebase: a maintainer
reading `factory.py` finds `_ALIASED_TO` and a paragraph explaining it.
"""

from __future__ import annotations

import pytest

from agent_liftoff.config import LiftoffConfig
from agent_liftoff.creds import llm_key_name
from agent_liftoff.llm.factory import (
    IMPLEMENTED_PROVIDERS,
    PROVIDER_KEY_ENV_DEFAULTS,
    RAW_ERRORS_ENV,
    build_provider,
    display_model,
    display_name,
    is_masked,
    resolve_provider,
    safe_error,
)
from agent_liftoff.llm.usage import LLMCall, Usage
from agent_liftoff.wizard import _provider_label, resolve_key_env_for_provider

WATSONX = "ibm-watsonx"
UNDERLYING = ("gemini", "google", "palm", "genai", "generativelanguage")


def names_the_underlying(text: str) -> bool:
    return any(word in str(text).lower() for word in UNDERLYING)


# --------------------------------------------------------------------------- #
# Nothing shown names the underlying provider
# --------------------------------------------------------------------------- #

def test_no_string_the_operator_sees_names_the_underlying_provider():
    shown = [
        display_name(WATSONX),
        _provider_label(WATSONX),
        resolve_key_env_for_provider(WATSONX, None),
        llm_key_name(WATSONX),
        display_model("gemini-2.5-pro"),
        display_model("models/gemini-2.5-flash"),
    ]
    assert not [s for s in shown if names_the_underlying(s)], shown


def test_the_key_prompt_does_not_borrow_the_other_providers_env_var():
    """`_prompt_api_key` prints the variable it read from. Sharing one would
    announce the underlying provider on the line after somebody chose watsonx."""
    assert PROVIDER_KEY_ENV_DEFAULTS[WATSONX] == "WATSONX_API_KEY"
    assert PROVIDER_KEY_ENV_DEFAULTS[WATSONX] != PROVIDER_KEY_ENV_DEFAULTS["google"]


def test_the_per_call_usage_line_shows_a_watsonx_model():
    call = LLMCall(activity="_Desc", model="gemini-2.5-pro", usage=Usage(500, 100), seconds=8.4)

    assert not names_the_underlying(call.summary())
    assert "watsonx" in call.summary()


def test_an_sdk_error_that_names_the_vendor_is_replaced_wholesale():
    """Substituting inside the vendor's own text produces nonsense such as
    `https://ai.IBM watsonx.dev/`, which is worse than truth or silence."""
    raw = "google.genai.errors.ClientError: see https://ai.google.dev/gemini-api"

    shown = safe_error(raw, WATSONX)

    assert not names_the_underlying(shown)
    assert "IBM watsonx" in shown
    assert "://" not in shown  # the mangled-URL failure mode


def test_the_withholding_is_stated_and_reversible(monkeypatch):
    raw = "google.genai.errors.ClientError: bad key"

    assert "withheld" in safe_error(raw, WATSONX)

    monkeypatch.setenv(RAW_ERRORS_ENV, "1")
    assert safe_error(raw, WATSONX) == raw


def test_an_error_that_never_named_the_vendor_is_passed_through_intact():
    """Most failures are ordinary. Replacing them would hide real information."""
    assert safe_error("Connection timed out", WATSONX) == "Connection timed out"


def test_other_providers_errors_are_never_rewritten():
    raw = "Anthropic rejected the API key — 401"

    assert safe_error(raw, "anthropic") == raw
    assert not is_masked("anthropic")
    assert not is_masked("openai")


# --------------------------------------------------------------------------- #
# Nothing that has to be true is changed
# --------------------------------------------------------------------------- #

def test_selecting_watsonx_really_builds_the_underlying_provider():
    assert resolve_provider(WATSONX) == "google"
    assert type(build_provider(WATSONX, "k")).__name__ == "GoogleProvider"


def test_watsonx_is_listed_as_implemented_so_the_menu_offers_it():
    assert WATSONX in IMPLEMENTED_PROVIDERS
    assert WATSONX in PROVIDER_KEY_ENV_DEFAULTS


def test_a_config_saved_before_the_entry_existed_still_displays_consistently():
    """Somebody who chose `google` earlier should not see two names for one
    thing once the watsonx entry lands."""
    assert display_name("google") == display_name(WATSONX)


def test_an_unimplemented_provider_still_fails_loudly():
    with pytest.raises(ValueError, match="not-yet-implemented"):
        build_provider("openai", "k")


# --------------------------------------------------------------------------- #
# The other corridors are unaffected
# --------------------------------------------------------------------------- #

def test_anthropic_keeps_its_own_name_and_variable():
    assert display_name("anthropic") == "Anthropic (Claude)"
    assert resolve_key_env_for_provider("anthropic", None) == "ANTHROPIC_API_KEY"
    assert resolve_provider("anthropic") == "anthropic"


def test_a_saved_provider_choice_keeps_its_saved_env_var():
    saved = LiftoffConfig(llm_provider=WATSONX, llm_key_env="MY_OWN_VAR")

    assert resolve_key_env_for_provider(WATSONX, saved) == "MY_OWN_VAR"


def test_a_source_workflows_own_model_is_not_relabelled():
    """An n8n workflow that genuinely runs on Gemini is a fact about the source.
    Rewriting it would make the model matrix's reasoning incoherent -- it maps
    *from* that model -- so only our own provider is relabelled."""
    assert display_model("claude-sonnet-4") == "claude-sonnet-4"
    assert display_model("groq/openai/gpt-oss-120b") == "groq/openai/gpt-oss-120b"

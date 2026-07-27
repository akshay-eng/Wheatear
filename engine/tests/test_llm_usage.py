"""Reporting what the model was asked to do, and what it cost.

Two properties matter. Failures must propagate untouched -- swallowing one
here would turn a resolver error into a silent "no match", which is the exact
failure mode the resolver is written to avoid. And token counts must come from
the provider: a guessed count looks authoritative, is wrong by a
tokenizer-shaped factor, and will end up in somebody's cost model.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from wheatear.llm.usage import LLMCall, ObservedProvider, Usage, UsageMeter, read_usage


class ToolMatch(BaseModel):
    verdict: str = "exact"


class Unnamed(BaseModel):
    value: str = "x"


class Fake:
    _model = "gemini-2.5-flash"

    def __init__(self, usage=None, explode=False):
        self.last_usage = usage or Usage(input_tokens=100, output_tokens=20)
        self.explode = explode
        self.prompts: list[str] = []

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        if self.explode:
            raise RuntimeError("provider hiccup")
        return schema()


def test_a_call_is_recorded_with_the_tokens_the_provider_reported():
    meter = UsageMeter()
    provider = ObservedProvider(Fake(), meter=meter)

    provider.generate_structured("hello", ToolMatch)

    assert meter.total_calls == 1
    assert meter.input_tokens == 100
    assert meter.output_tokens == 20
    assert meter.calls[0].activity == "matching a tool"
    assert meter.calls[0].model == "gemini-2.5-flash"


def test_a_failing_call_is_recorded_and_re_raised_untouched():
    """Swallowing it would turn a resolver failure into a silent no-match."""
    meter = UsageMeter()
    provider = ObservedProvider(Fake(explode=True), meter=meter)

    with pytest.raises(RuntimeError, match="provider hiccup"):
        provider.generate_structured("hello", ToolMatch)

    assert meter.total_calls == 1
    assert meter.failures == 1
    assert meter.calls[0].failed is True


def test_the_wrapper_is_transparent_to_the_stage_calling_it():
    """`resolve.py` should only ever know it holds an LLMProvider."""
    inner = Fake()
    provider = ObservedProvider(inner)

    result = provider.generate_structured("a prompt", ToolMatch)

    assert isinstance(result, ToolMatch)
    assert inner.prompts == ["a prompt"]
    # Anything else the real provider exposes is still reachable.
    assert provider._model == "gemini-2.5-flash"


def test_a_provider_that_reports_nothing_says_unknown_not_zero():
    """A confident zero would read as a free call."""
    provider = ObservedProvider(Fake(usage=Usage()))

    provider.generate_structured("hi", ToolMatch)
    call = provider.meter.calls[0]

    assert call.usage.known is False
    assert "tokens not reported" in call.summary()


def test_a_provider_with_no_usage_attribute_at_all_is_tolerated():
    class Bare:
        _model = "m"

        def generate_structured(self, prompt, schema):
            return schema()

    provider = ObservedProvider(Bare())
    provider.generate_structured("hi", ToolMatch)

    assert read_usage(Bare()) == Usage()
    assert provider.meter.calls[0].usage.known is False


def test_an_unmapped_schema_falls_back_to_its_own_name():
    """A new structured call should show up as something rather than nothing."""
    provider = ObservedProvider(Fake())

    provider.generate_structured("hi", Unnamed)

    assert provider.meter.calls[0].activity == "Unnamed"


def test_totals_group_by_what_the_calls_were_for():
    meter = UsageMeter()
    provider = ObservedProvider(Fake(), meter=meter)

    provider.generate_structured("a", ToolMatch)
    provider.generate_structured("b", ToolMatch)
    provider.generate_structured("c", Unnamed)

    grouped = meter.by_activity()
    assert grouped["matching a tool"] == (2, 240)
    assert grouped["Unnamed"] == (1, 120)
    assert "3 model call(s)" in meter.summary()


def test_an_idle_meter_says_so():
    assert UsageMeter().summary() == "no model calls"
    assert UsageMeter().by_activity() == {}


def test_a_call_reports_one_token_number_not_two():
    """The prompt/completion split is real but it is not what somebody
    watching a migration wants to read, and two numbers where one would do
    makes the line harder to scan. The split stays on the record."""
    call = LLMCall("matching a tool", "gemini-2.5-flash", Usage(1240, 61), 1.23)

    assert "1,301 tokens" in call.summary()
    assert " in / " not in call.summary()
    assert "1.2s" in call.summary()
    # Still available to anything that needs the breakdown.
    assert call.usage.input_tokens == 1240
    assert call.usage.output_tokens == 61


def test_the_running_total_is_also_a_single_number():
    meter = UsageMeter()
    meter.record(LLMCall("matching a tool", "m", Usage(1000, 100), 1.0))
    meter.record(LLMCall("matching a tool", "m", Usage(500, 50), 1.0))

    assert meter.total_tokens == 1650
    assert "1,650 tokens" in meter.summary()
    assert " in / " not in meter.summary()

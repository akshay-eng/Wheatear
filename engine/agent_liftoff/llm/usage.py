"""What the model was asked, and what it cost.

Every model call in a migration is a judgement somebody may later want to
question -- "why did it pick that tool?" -- and every one of them is billed.
Neither fact is visible from a progress spinner, so this records both and hands
them to whoever is watching.

The design constraint is that no pipeline stage should have to know it is being
observed. `resolve.py` calls `provider.generate_structured(...)` and that is all
it should ever do; wrapping the provider rather than instrumenting the callers
means the tool lookup, the description writer and anything added later are
covered without being touched.

Token counts come from the providers themselves rather than being estimated. A
guessed count is worse than none: it looks authoritative, it is wrong by a
factor that varies with the tokenizer, and somebody will eventually put it in a
cost model.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Schema name -> what the call is actually for, in words a person reading a
# terminal would recognise. Derived from the schema because that is the one
# thing every structured call has and nothing has to be threaded through.
ACTIVITY = {
    "ToolMatch": "matching a tool",
    "AgentDescription": "writing an agent description",
    "MappingProposal": "inferring a field mapping",
    "RepairProposal": "repairing a generated adapter",
}


@dataclass(frozen=True)
class Usage:
    """Tokens for one call, as the provider reported them."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def known(self) -> bool:
        """False when the provider told us nothing, so a caller can say so
        rather than printing a confident zero."""
        return bool(self.input_tokens or self.output_tokens)


@dataclass(frozen=True)
class LLMCall:
    """One completed model call."""

    activity: str
    model: str
    usage: Usage
    seconds: float
    failed: bool = False

    def summary(self) -> str:
        """One line for a terminal.

        The split between prompt and completion tokens is real but it is not
        what anybody watching a migration wants to know, and two numbers where
        one would do makes the line harder to scan. The total is what shows;
        the split is still on the record for anyone who needs it.
        """
        from agent_liftoff.llm.factory import display_model

        cost = f"{self.usage.total:,} tokens" if self.usage.known else "tokens not reported"
        state = "failed after " if self.failed else ""
        # `self.model` keeps the real id; only the rendering is relabelled, so
        # anything reasoning about the model still sees what actually ran.
        return f"{self.activity} · {display_model(self.model)} · {cost} · {state}{self.seconds:.1f}s"


@dataclass
class UsageMeter:
    """Running totals for a migration."""

    calls: list[LLMCall] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def input_tokens(self) -> int:
        return sum(c.usage.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.usage.output_tokens for c in self.calls)

    @property
    def seconds(self) -> float:
        return sum(c.seconds for c in self.calls)

    @property
    def failures(self) -> int:
        return sum(1 for c in self.calls if c.failed)

    def record(self, call: LLMCall) -> None:
        self.calls.append(call)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        if not self.calls:
            return "no model calls"
        tokens = f"{self.total_tokens:,} tokens" if self.total_tokens else "tokens not reported"
        line = f"{self.total_calls} model call(s) · {tokens} · {self.seconds:.1f}s"
        if self.failures:
            line += f" · {self.failures} failed"
        return line

    def by_activity(self) -> dict[str, tuple[int, int]]:
        """Calls and total tokens, grouped by what they were for."""
        grouped: dict[str, tuple[int, int]] = {}
        for call in self.calls:
            calls, tokens = grouped.get(call.activity, (0, 0))
            grouped[call.activity] = (calls + 1, tokens + call.usage.total)
        return grouped


def read_usage(provider: object) -> Usage:
    """The token counts from the provider's most recent call.

    Providers publish `last_usage` after each call. Absent means the adapter
    does not report usage, which is reported as unknown rather than as zero.
    """
    usage = getattr(provider, "last_usage", None)
    return usage if isinstance(usage, Usage) else Usage()


class ObservedProvider:
    """An `LLMProvider` that reports every call it makes.

    Wraps rather than replaces, so anything holding an `LLMProvider` keeps
    working and no pipeline stage learns it is being watched. A failing call is
    recorded and re-raised untouched -- swallowing it here would turn a
    resolver failure into a silent "no match".
    """

    def __init__(
        self,
        inner: object,
        meter: UsageMeter | None = None,
        on_call: Callable[[LLMCall], None] | None = None,
    ) -> None:
        self._inner = inner
        self.meter = meter or UsageMeter()
        self._on_call = on_call or (lambda _call: None)

    @property
    def model(self) -> str:
        return str(getattr(self._inner, "_model", "") or "unknown model")

    def generate_structured(self, prompt: str, schema: type) -> object:
        activity = ACTIVITY.get(schema.__name__, schema.__name__)
        started = time.monotonic()
        failed = False
        try:
            return self._inner.generate_structured(prompt, schema)
        except Exception:
            failed = True
            raise
        finally:
            call = LLMCall(
                activity=activity,
                model=self.model,
                usage=read_usage(self._inner),
                seconds=time.monotonic() - started,
                failed=failed,
            )
            self.meter.record(call)
            self._on_call(call)

    def __getattr__(self, name: str) -> object:
        # Anything else a caller wants from the real provider.
        return getattr(self._inner, name)

"""The interface every LLM adapter implements.

Translate is the only pipeline stage allowed to call this. Implementations
return a validated instance of `schema`, never freeform text -- that's what
keeps the AI-assisted step boxed in by a schema instead of producing
unconstrained prose.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    def generate_structured(self, prompt: str, schema: type[T]) -> T: ...

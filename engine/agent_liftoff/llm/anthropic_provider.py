"""Anthropic adapter for LLMProvider, via forced tool-use structured output.

Requires the `anthropic` extra: pip install agent_liftoff[anthropic]
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from agent_liftoff.llm.usage import Usage

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicProvider:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "AnthropicProvider requires the 'anthropic' extra: pip install agent_liftoff[anthropic]"
            ) from exc

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        # See google_provider: published for usage reporting, never estimated.
        self.last_usage = Usage()

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        tool_name = schema.__name__
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            tools=[
                {
                    "name": tool_name,
                    "description": f"Return a {tool_name} object.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            )

        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return schema.model_validate(block.input)

        raise RuntimeError(f"Anthropic response did not include a {tool_name} tool call")

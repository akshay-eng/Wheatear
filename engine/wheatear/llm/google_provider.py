"""Google Gemini adapter for LLMProvider.

Uses the Gemini Developer API (a plain API key, via the `google-genai` SDK)
-- not the separate Vertex AI service-account/ADC flow, which has a
different credential model and isn't implemented here. If you only have a
Gemini API key (as opposed to a GCP project + service account), this is the
right path.

Requires the 'google' extra: pip install wheatear[google]
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-2.5-pro"


class GoogleProvider:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError(
                "GoogleProvider requires the 'google' extra: pip install wheatear[google]"
            ) from exc

        self._client = genai.Client(api_key=api_key)
        self._model = model

    def generate_structured(self, prompt: str, schema: type[T]) -> T:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )

        if response.parsed is None:
            raise RuntimeError(f"Gemini response did not include parsed {schema.__name__} output")
        return response.parsed

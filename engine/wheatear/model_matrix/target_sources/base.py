"""Generic interface for "what models can this target platform actually use
right now" -- deliberately not Orchestrate-specific.

Wheatear's product scope already names several future export targets besides
Orchestrate (OpenAI, Vertex AI, Bedrock AgentCore -- see PRODUCT.md), and the
available-model list is a live, tenant/account-specific fact on every one of
them, not a static catalog. One small interface here means the matching
engine in `scorer.py`/`resolver.py` never needs to know which platform it's
resolving against -- add a new `TargetModelSource` implementation per
platform and everything upstream keeps working unchanged, mirroring the same
one-importer-one-exporter seam `connectors/registry.py` already uses for
whole corridors.
"""

from __future__ import annotations

from typing import Protocol


class TargetModelSource(Protocol):
    """Anything that can report which raw model ids are usable right now."""

    def list_available_models(self, *, include_non_preferred: bool = False) -> list[str]:
        """Return raw model-id strings as the target platform names them
        (e.g. "watsonx/meta-llama/llama-3-3-70b-instruct").

        include_non_preferred: include models the platform can technically
        route to but doesn't recommend/prefer (Orchestrate's `-a/--all` flag
        is exactly this distinction) -- default False, matching "don't
        recommend a model the platform itself is hedging on."
        """
        ...

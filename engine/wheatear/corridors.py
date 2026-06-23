"""The migration corridors Wheatear actually supports, plus the full
platform catalog (including not-yet-implemented platforms) the wizard shows
so people can see what's coming rather than guessing.

Shared between cli.py and wizard.py so there's exactly one list to update
when a new platform lands.
"""

from __future__ import annotations

SUPPORTED_CORRIDORS: set[tuple[str, str]] = {("copilot-studio", "orchestrate")}

# (display name, internal key, implemented)
SOURCE_PLATFORMS: list[tuple[str, str, bool]] = [
    ("Microsoft Copilot Studio", "copilot-studio", True),
    ("n8n", "n8n", False),
    ("Google Vertex AI Agent Builder", "vertex-ai", False),
    ("OpenAI Agents SDK", "openai-agents", False),
]

TARGET_PLATFORMS: list[tuple[str, str, bool]] = [
    ("IBM watsonx Orchestrate", "orchestrate", True),
    ("AWS Bedrock AgentCore", "agentcore", False),
    ("OpenAI Agents SDK", "openai-agents", False),
    ("Anthropic Claude SDK", "claude-sdk", False),
]

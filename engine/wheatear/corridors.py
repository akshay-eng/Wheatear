"""The migration corridors Wheatear actually supports, plus the full
platform catalog (including not-yet-implemented platforms) the wizard shows
so people can see what's coming rather than guessing.

Shared between cli.py and wizard.py so there's exactly one list to update
when a new platform lands.
"""

from __future__ import annotations

SUPPORTED_CORRIDORS: set[tuple[str, str]] = {
    ("copilot-studio", "orchestrate"),
    ("orchestrate", "orchestrate"),
    ("orchestrate", "copilot-studio"),
}

# (display name, internal key, implemented)
SOURCE_PLATFORMS: list[tuple[str, str, bool]] = [
    ("Microsoft Copilot Studio", "copilot-studio", True),
    ("IBM watsonx Orchestrate", "orchestrate", True),
    ("Google Vertex AI Agent Builder", "vertex-ai", False),
    ("AWS Bedrock AgentCore", "agentcore", False),
    ("ServiceNow AI Agents", "servicenow", False),
    ("Salesforce Agentforce", "agentforce", False),
    ("n8n", "n8n", False),
    ("OpenAI Agent Builder", "openai-agents", False),
    ("Zapier Agents", "zapier", False),
    ("Dify", "dify", False),
]

# All possible migration targets. Keyed separately from SOURCE_PLATFORMS so
# the same platform doesn't appear as both source and target.
# The wizard excludes the source key from this list at runtime so a user
# never sees their own platform listed as a target.
TARGET_PLATFORMS: list[tuple[str, str, bool]] = [
    ("IBM watsonx Orchestrate", "orchestrate", True),
    ("Microsoft Copilot Studio", "copilot-studio", True),
    ("Google Vertex AI Agent Builder", "vertex-ai", False),
    ("AWS Bedrock AgentCore", "agentcore", False),
    ("ServiceNow AI Agents", "servicenow", False),
    ("Salesforce Agentforce", "agentforce", False),
    ("n8n", "n8n", False),
    ("OpenAI Agent Builder", "openai-agents", False),
    ("Zapier Agents", "zapier", False),
    ("Dify", "dify", False),
    ("Anthropic Claude SDK", "claude-sdk", False),
]

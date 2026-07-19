"""Shared types for both Copilot Studio import paths.

Copilot Studio agents reach the outside world through two different export
mechanisms with two different file formats: a `pac copilot clone` workspace
(.mcs.yml) and a Dataverse solution export (XML + per-component YAML data
files). Both produce the same ImportResult shape so Map and everything
downstream doesn't need to know which one it got.
"""

from __future__ import annotations

# ImportResult / RawKnowledgeRef moved to the platform-neutral connectors.base
# (a hub type belongs at the hub); re-exported here so existing
# `from ...copilot_studio.common import ImportResult` call sites keep working.
from wheatear.connectors.base import ImportResult, RawKnowledgeRef

__all__ = ["SYSTEM_TOPIC_NAMES", "ImportResult", "RawKnowledgeRef"]

# Boilerplate lifecycle topics that ship by default with every new Copilot
# Studio agent (the "default-2.1.0" system template), as opposed to custom
# business logic. Confirmed against a real export, not guessed.
SYSTEM_TOPIC_NAMES = {
    "ConversationStart",
    "EndofConversation",
    "Escalate",
    "Fallback",
    "Goodbye",
    "Greeting",
    "MultipleTopicsMatched",
    "OnError",
    "ResetConversation",
    "Search",
    "Signin",
    "StartOver",
    "ThankYou",
}

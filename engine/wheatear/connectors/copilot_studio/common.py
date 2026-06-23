"""Shared types for both Copilot Studio import paths.

Copilot Studio agents reach the outside world through two different export
mechanisms with two different file formats: a `pac copilot clone` workspace
(.mcs.yml) and a Dataverse solution export (XML + per-component YAML data
files). Both produce the same ImportResult shape so Map and everything
downstream doesn't need to know which one it got.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wheatear.ir.schema import Agent

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


@dataclass
class RawKnowledgeRef:
    """A knowledge source reference as Normalize found it, with whatever
    structured metadata the source format provided. Map is still the one
    place that decides review_required/notes from this -- importers don't
    make that call themselves -- but a SharePoint search source carries much
    richer signal than a bare name, and that's worth preserving.
    """

    name: str
    source_kind: str | None = None
    detail: str | None = None


@dataclass
class ImportResult:
    """Normalize's output: the IR Agent (dialog structure only) plus the raw
    references Map needs to resolve into tools/knowledge/connections.
    """

    agent: Agent
    raw_tool_refs: list[str] = field(default_factory=list)
    raw_knowledge_refs: list[RawKnowledgeRef] = field(default_factory=list)
    raw_connection_refs: list[str] = field(default_factory=list)
    # Process-level notes from Normalize itself (e.g. "skipped an unrecognized
    # component type") -- distinct from Topic.unsupported_notes, which are
    # about agent *content* the parser couldn't model.
    import_notes: list[str] = field(default_factory=list)

"""Canonical intermediate representation (IR) for Wheatear.

Every importer (Normalize stage) produces an `Agent`; every exporter (Export
stage) consumes one. Adding a platform costs one importer and one exporter,
not a translator for every pair already supported.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

IR_SPEC_VERSION = "wheatear/v1"


class DialogNodeKind(str, Enum):
    """A constrained subset of dialog-graph node types Wheatear understands.

    Anything outside this set is recorded in Topic.unsupported_notes instead
    of being silently dropped or guessed at.
    """

    MESSAGE = "message"
    QUESTION = "question"
    CONDITION = "condition"


class DialogNode(BaseModel):
    """One node in a source platform's dialog graph (e.g. a Copilot Studio
    topic node), preserved as structured input for the Translate stage.
    """

    kind: DialogNodeKind
    text: str | None = None
    variable: str | None = None
    children: list["DialogNode"] = Field(default_factory=list)


class Topic(BaseModel):
    """A conversational unit from the source platform (e.g. one Copilot
    Studio topic). Translate collapses these into Agent.instructions; they're
    kept here so that stage has real structure to work from, not prose.
    """

    name: str
    trigger_phrases: list[str] = Field(default_factory=list)
    nodes: list[DialogNode] = Field(default_factory=list)
    unsupported_notes: list[str] = Field(default_factory=list)
    # True for boilerplate lifecycle topics (Greeting, Fallback, Escalate...)
    # that ship by default with every new Copilot Studio agent, as opposed to
    # custom business logic. Translate uses this to avoid drowning a real
    # generative agent's system prompt in template noise.
    is_system_topic: bool = False


class ToolRef(BaseModel):
    """A connector, custom action, or flow reference mapped to a tool on the
    target platform. Populated by the deterministic Map stage.
    """

    ref: str
    source_ref: str | None = None
    confidence: float = 1.0
    review_required: bool = False
    notes: str | None = None


class KnowledgeRef(BaseModel):
    """A knowledge source / knowledge base reference. review_required
    defaults to False (a same-shape knowledge base reference is sometimes a
    clean copy) but Map sets it True for sources that need real re-ingestion
    work, e.g. a SharePoint search connector with no Orchestrate equivalent.
    """

    ref: str
    source_ref: str | None = None
    review_required: bool = False
    notes: str | None = None


class ConnectionRef(BaseModel):
    """An auth/connection reference. Values are never auto-filled by
    Wheatear; review_required defaults to True because a human always has to
    populate real credentials on the target platform.
    """

    ref: str
    auth_type: str
    source_ref: str | None = None
    review_required: bool = True


class Agent(BaseModel):
    """The canonical IR for a single agent, the contract between every
    importer and every exporter.
    """

    spec_version: str = IR_SPEC_VERSION
    name: str
    source_platform: str

    # Populated by Normalize, consumed by Translate.
    topics: list[Topic] = Field(default_factory=list)
    # For generative/GPT-orchestrated source agents, Normalize surfaces the
    # source platform's own system prompt here. Normalize never writes to
    # `instructions` directly -- that stays exclusively Translate's job --
    # but when this is present, Translate can lightly adapt an existing
    # prompt instead of synthesizing one from a dialog graph that may barely
    # represent the agent's real behavior.
    existing_instructions: str | None = None
    model_hint: str | None = None

    # Populated by Translate.
    instructions: str = ""
    translation_confidence: float = 1.0
    translation_notes: list[str] = Field(default_factory=list)

    # Populated by Map (deterministic, no LLM).
    tools: list[ToolRef] = Field(default_factory=list)
    knowledge: list[KnowledgeRef] = Field(default_factory=list)
    connections: list[ConnectionRef] = Field(default_factory=list)

    # Set by any stage that hits something it can't handle; Validate surfaces
    # this to the human reviewer rather than letting it pass silently.
    review_required: bool = False

    @property
    def needs_review(self) -> bool:
        """True if anything in the pipeline flagged this agent for a human
        to look at before import: a tool, connection, or the translation
        itself.
        """
        return (
            self.review_required
            or any(t.review_required for t in self.tools)
            or any(k.review_required for k in self.knowledge)
            or any(c.review_required for c in self.connections)
            or self.translation_confidence < 0.8
        )

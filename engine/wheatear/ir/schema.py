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


class ToolKind(str, Enum):
    """What the source reference actually is on the Copilot side. Determines
    which resolver the Map stage reaches for (see BridgeStrategy).
    """

    CONNECTOR = "connector"  # prebuilt Power Platform connector (ServiceNow, Jira...)
    CUSTOM_CONNECTOR = "custom_connector"  # user-defined, OpenAPI under the hood
    REST = "rest"  # a raw REST/HTTP call
    MCP = "mcp"  # already an MCP server reference
    FLOW = "flow"  # Power Automate / agent flow
    PROMPT = "prompt"  # a Copilot "prompt" tool
    COMPUTER_USE = "computer_use"
    UNKNOWN = "unknown"


class BridgeStrategy(str, Enum):
    """How Map resolved a tool onto Orchestrate. Recorded so the exporter and
    review manifest can explain *how* a tool arrives, not just that it does.
    """

    NATIVE_MCP = "native_mcp"  # re-point an existing MCP endpoint (cleanest)
    OPENAPI = "openapi"  # convert an OpenAPI spec into an Orchestrate tool
    MCP_CATALOG = "mcp_catalog"  # resolved via the curated connector catalog
    MANUAL = "manual"  # no confident mapping; human must implement


class Guideline(BaseModel):
    """Orchestrate's structured behavior primitive (Behavior > Guidelines):
    a `condition` that, when met, triggers an `action`, optionally using a
    tool. The natural landing spot for conditional logic that lives in a
    Copilot topic or buried in a system prompt's guardrails.
    """

    name: str | None = None
    condition: str
    action: str
    tool_ref: str | None = None


class ToolRef(BaseModel):
    """A connector, custom action, or flow reference mapped to a tool on the
    target platform. Populated by the deterministic Map stage.
    """

    ref: str
    source_ref: str | None = None
    confidence: float = 1.0
    review_required: bool = False
    notes: str | None = None
    kind: ToolKind = ToolKind.UNKNOWN
    bridge: BridgeStrategy | None = None
    # For MCP-backed tools: the server endpoint + transport. MCP is the one
    # tool type both platforms consume natively, so carrying the URL makes it a
    # clean migration (re-point the endpoint) rather than a manual rebuild.
    mcp_server_url: str | None = None
    transport: str | None = None
    # Individual tool/operation names the toolkit/server exposes.
    member_tools: list[str] = Field(default_factory=list)


class IngestPlan(str, Enum):
    """What has to happen for a source's content to exist on Orchestrate.
    Orchestrate knowledge is vector-DB-instance-backed (Milvus, Elasticsearch,
    ...) or a <=30MB file upload -- it has no "point at SharePoint" option, so
    most Copilot SaaS-backed sources need real re-ingestion, not a reference copy.
    """

    UPLOAD = "upload"  # re-upload files (enforce the 30MB cap)
    REINDEX_VECTOR = "reindex_vector"  # re-index content into a vector instance
    CUSTOM_SERVICE = "custom_service"  # expose via an Orchestrate Custom service
    UNSUPPORTED = "unsupported"  # no viable target (e.g. live web search)


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
    ingest_plan: IngestPlan | None = None


class ConnectionRef(BaseModel):
    """An auth/connection reference. Values are never auto-filled by
    Wheatear; review_required defaults to True because a human always has to
    populate real credentials on the target platform.
    """

    ref: str
    auth_type: str
    source_ref: str | None = None
    review_required: bool = True


class AgentRef(BaseModel):
    """A reference from one agent to another it can hand work to (a Copilot
    Studio connected agent, an Orchestrate collaborator). `ref` is the target
    agent's name; the actual Agent lives in the enclosing Workflow. Kept as a
    reference rather than a nested Agent so a graph (including cycles) can be
    represented without infinite nesting.
    """

    ref: str
    source_ref: str | None = None
    review_required: bool = False
    notes: str | None = None


class Agent(BaseModel):
    """The canonical IR for a single agent, the contract between every
    importer and every exporter.
    """

    spec_version: str = IR_SPEC_VERSION
    name: str
    source_platform: str
    description: str | None = None

    # First-message shown to the user. On Copilot this is the ConversationStart
    # / Greeting activity; on Orchestrate it's Profile > Welcome message.
    welcome_message: str | None = None
    # Conversation starters (Copilot suggested prompts -> Orchestrate starter prompts).
    starter_prompts: list[str] = Field(default_factory=list)

    # Deployment surfaces the source agent was published to (e.g. "msteams",
    # "Microsoft365Copilot"). Carried so the exporter can flag ones with no
    # Orchestrate equivalent rather than dropping them silently.
    channels: list[str] = Field(default_factory=list)

    # Source content-moderation posture (e.g. "Low"/"High"); Orchestrate has no
    # equivalent slider, so this becomes a guardrail note/Guideline, not a field.
    content_moderation: str | None = None
    # True if the source agent had web browsing / public web search enabled;
    # Orchestrate needs an explicit web-search tool to reproduce this.
    web_search: bool = False

    # Populated by Normalize, consumed by Translate.
    topics: list[Topic] = Field(default_factory=list)
    # For generative/GPT-orchestrated source agents, Normalize surfaces the
    # source platform's own system prompt here. Normalize never writes to
    # `instructions` directly -- that stays exclusively Translate's job --
    # but when this is present, Translate can lightly adapt an existing
    # prompt instead of synthesizing one from a dialog graph that may barely
    # represent the agent's real behavior.
    existing_instructions: str | None = None
    # Raw source model name (e.g. "GPT5Chat"). Never emitted directly -- the
    # exporter resolves it to a target model via model_map, since no source
    # model has a 1:1 Orchestrate equivalent.
    model_hint: str | None = None
    # Normalized target model chosen by model_map; a human should confirm it.
    model_family: str | None = None

    # Planning/execution style on platforms that expose it (Orchestrate:
    # "default" vs "react"). Preserved so a ReAct agent doesn't silently become
    # a default one on round-trip.
    agent_style: str | None = None

    # Populated by Translate.
    instructions: str = ""
    translation_confidence: float = 1.0
    translation_notes: list[str] = Field(default_factory=list)
    # Structured behavior rules. May be seeded by Map (e.g. from
    # content_moderation) and enriched by Translate (decomposing guardrails).
    guidelines: list[Guideline] = Field(default_factory=list)

    # Populated by Map (deterministic, no LLM).
    tools: list[ToolRef] = Field(default_factory=list)
    knowledge: list[KnowledgeRef] = Field(default_factory=list)
    connections: list[ConnectionRef] = Field(default_factory=list)
    # Other agents this one can delegate to (multi-agent workflows). The
    # referenced agents live alongside this one in the enclosing Workflow.
    collaborators: list[AgentRef] = Field(default_factory=list)

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
            or any(c.review_required for c in self.collaborators)
            or self.translation_confidence < 0.8
        )


class Workflow(BaseModel):
    """A bundle of one or more agents migrated together, plus the delegation
    edges between them. A single-agent migration is just a Workflow with one
    agent -- every importer returns a Workflow so the pipeline has one shape
    to handle whether it's one agent or a multi-agent graph.
    """

    spec_version: str = IR_SPEC_VERSION
    source_platform: str
    # The user-facing entry agent (the orchestrator), by name. None for a
    # flat bundle with no designated root.
    root: str | None = None
    agents: list[Agent] = Field(default_factory=list)

    def by_name(self, name: str) -> Agent | None:
        return next((a for a in self.agents if a.name == name), None)

    def migration_order(self) -> list[Agent]:
        """Agents in leaf-first (dependency) order: a collaborator is always
        emitted before the agent that references it, so target-side references
        resolve. Cycles are broken deterministically (by first-seen order) and
        never loop forever.
        """
        by_name = {a.name: a for a in self.agents}
        ordered: list[Agent] = []
        seen: set[str] = set()
        visiting: set[str] = set()

        def visit(agent: Agent) -> None:
            if agent.name in seen:
                return
            if agent.name in visiting:
                # Cycle: stop descending; the agent is emitted when the current
                # DFS unwinds. Deterministic because agents are visited in list
                # order. A collaborator referencing back into a cycle is flagged
                # for review by the importer, not silently resolved here.
                return
            visiting.add(agent.name)
            for collab in agent.collaborators:
                target = by_name.get(collab.ref)
                if target is not None:
                    visit(target)
            visiting.discard(agent.name)
            seen.add(agent.name)
            ordered.append(agent)

        for agent in self.agents:
            visit(agent)
        return ordered

"""IR -> watsonx Orchestrate exporter.

Writes the file layout `orchestrate agents import -f agent.yaml` expects:
one agent.yaml plus a connections/ folder for anything needing credentials.
Tool *implementations* are out of scope (Wheatear doesn't invent function
bodies for a Copilot Studio custom connector) - tools are referenced by name
and anything without a confident mapping lands in review-manifest.yaml
instead of being silently emitted as if it were ready to import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from wheatear.ir.schema import Agent, BridgeStrategy, ToolRef
from wheatear.model_map import resolve_target_model

# Channels the source agent may have been published to that have no equivalent
# Orchestrate deployment surface; the exporter flags these for the human rather
# than pretending they carried over.
_UNMAPPABLE_CHANNELS = {"msteams", "Microsoft365Copilot"}

# Below this, a tool match is not worth writing into an agent that will be
# imported unattended. The same threshold the Resolve stage accepts a verdict
# at, applied a second time here because a match can also arrive from Map.
MIN_TOOL_CONFIDENCE = 0.5

# What a migrated agent runs as when the source said nothing usable.
#
# The console offers three styles -- ReAct Core (Recommended), Default
# (Deprecated) and ReAct (Deprecated) -- and those map to the three values a
# real tenant actually stores: `react_intrinsic`, `default` and `react`. The
# ADK enum also contains `react_core`, which reads like the obvious choice and
# is used by nothing; picking it by its name would land every migrated agent
# on a style the console does not offer.
PREFERRED_STYLE = "react_intrinsic"


@dataclass
class ExportResult:
    agent_path: Path
    connection_paths: list[Path] = field(default_factory=list)
    review_manifest_path: Path | None = None

    @property
    def needs_review(self) -> bool:
        return self.review_manifest_path is not None


def _dump_yaml(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


def _sanitize_name(name: str) -> str:
    """Orchestrate agent names must start with a letter and contain only
    alphanumeric characters and underscores (no spaces). Applied consistently
    to an agent's own name and to every collaborator reference so leaf-first
    deploy still resolves the edges (both sides sanitize identically)."""
    import re

    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_")
    if not cleaned:
        cleaned = "agent"
    if not cleaned[0].isalpha():
        cleaned = "a_" + cleaned
    return cleaned

def agent_style(agent: Agent) -> tuple[str, str | None]:
    """A style Orchestrate will accept, and what was discarded to get one.

    The IR's `agent_style` is whatever the source platform called it, and the
    source's vocabulary is not the target's. Passing it through means the
    entire agent fails to import over one string -- observed: a mapping derived
    `connected` from a Copilot boolean about whether other agents may call this
    one, which is not a conversational style at all.

    The allowed values are read from the ADK's own enum rather than listed
    here, so this cannot drift from what the platform accepts. A value outside
    it is dropped rather than guessed at, and what it falls back to is
    `react_core`: the platform marks `default` and `react` as deprecated and
    recommends `react_core`, so landing a migrated agent on a deprecated style
    is choosing the one option the vendor is steering people off. When the ADK
    is too old to offer it, `default` is the fallback's fallback.
    """
    proposed = (agent.agent_style or "").strip()
    try:
        from ibm_watsonx_orchestrate.agent_builder.agents import AgentStyle  # noqa: PLC0415

        allowed = {style.value for style in AgentStyle}
    except Exception:  # noqa: BLE001 - without the ADK there is nothing to check against
        return proposed or PREFERRED_STYLE, None
    fallback = PREFERRED_STYLE if PREFERRED_STYLE in allowed else "default"
    if not proposed:
        return fallback, None
    if proposed in allowed:
        return proposed, None
    return fallback, proposed


def importable_tools(agent: Agent) -> tuple[list[str], list[ToolRef]]:
    """Split the agent's tools into the ones that can be referenced, and the rest.

    An `agent.yaml` naming a tool the instance does not have fails outright at
    import -- the whole agent, not just the tool. So a tool that resolved to a
    catalog entry nobody has installed, or that resolved to nothing at all, or
    that resolved with confidence too low to act on, is left out and reported.

    Dropping is the right answer rather than a lesser one: an agent that lands
    with three of its four tools is a migration somebody can finish, and an
    agent that refuses to import is not.
    """
    carried: list[str] = []
    dropped: list[ToolRef] = []
    for tool in agent.tools:
        # CATALOG_INSTALL is the explicit "found it, but nobody has installed
        # it" verdict. Every other bridge means the ref is answerable on the
        # instance, so confidence is the only remaining question.
        # Answerable on the instance right now. `None` is a tool pinned to a
        # pre-existing target tool; MCP_CATALOG is one the resolver matched to
        # something already installed. Every other bridge means the tool still
        # has to be provisioned -- a catalog install, an OpenAPI import, an MCP
        # re-point, a manual build -- and naming one of those fails the whole
        # agent's import, so it is carried in the review manifest instead.
        answerable = tool.bridge in (None, BridgeStrategy.MCP_CATALOG)
        if tool.ref and answerable and tool.confidence >= MIN_TOOL_CONFIDENCE:
            # Two source tools routinely land on one target tool -- Copilot's
            # `GetRecord` and `ListRecords` both resolve to a single
            # `get_records`. Naming it twice in the spec claims the agent has
            # two tools when it has one, and is the sort of thing a stricter
            # ADK release rejects outright.
            if tool.ref not in carried:
                carried.append(tool.ref)
        else:
            dropped.append(tool)
    return carried, dropped


def _agent_spec(agent: Agent, llm: str) -> dict:
    carried, _ = importable_tools(agent)
    spec: dict = {
        "spec_version": "v1",
        "kind": "native",
        "name": _sanitize_name(agent.name),
        # description is a required field on the Orchestrate ADK Agent model, so
        # always emit a non-empty value -- fall back to the agent name when the
        # source didn't provide one (e.g. n8n agents have no description field).
        "description": agent.description or agent.name,
        "llm": llm,
        "style": agent_style(agent)[0],
        "instructions": agent.instructions,
        # Only reference entities that will actually exist at import time. The
        # ADK hard-fails an import if a referenced collaborator/tool/knowledge
        # base isn't already registered. Collaborators resolve because the
        # migration deploys leaf-first (both sides sanitize names identically).
        "collaborators": [
            _sanitize_name(c.ref) for c in agent.collaborators if not c.review_required
        ],
        "tools": carried,
    }
    if agent.name != spec["name"]:
        spec["display_name"] = agent.name
    if agent.guidelines:
        # ADK AgentGuideline schema: display_name?/condition/action/tool?.
        spec["guidelines"] = [
            {
                k: v
                for k, v in {
                    "display_name": g.name,
                    "condition": g.condition,
                    "action": g.action,
                    "tool": g.tool_ref,
                }.items()
                if v is not None
            }
            for g in agent.guidelines
        ]
    # Two shapes of knowledge reach this point and they need different specs.
    #
    # File-backed sources (a Copilot componenttype-14 component) are ingested
    # into one merged base per agent -- Orchestrate permits an agent exactly
    # one and rejects the whole agent otherwise, verified live -- so the ref is
    # that base's name, not the individual documents'.
    #
    # Reference-backed sources (n8n pointing at an existing vector store) are
    # already named on the target, so their own refs are what to emit. Ones
    # still flagged for review are left out: a ref to a base that does not
    # exist fails the agent's import outright, the same way a missing tool does.
    if any(k.file_path for k in agent.knowledge):
        spec["knowledge_base"] = [knowledge_base_name(agent)]
    else:
        # A ref with no documents is only safe to name if something positively
        # says the target already has it: nothing outstanding to do, and a
        # source_ref showing it was read off a real platform rather than
        # invented. A Copilot knowledge source pointing at a SharePoint site
        # has neither, and naming it produces an agent that looks migrated,
        # answers nothing, and fails its own import.
        deployable_kb = [
            k.ref
            for k in agent.knowledge
            if not k.review_required and k.ingest_plan is None and k.source_ref
        ]
        if deployable_kb:
            spec["knowledge_base"] = deployable_kb
    if agent.welcome_message:
        # ADK WelcomeContent schema (welcome_content.py).
        spec["welcome_content"] = {
            "welcome_message": agent.welcome_message,
            "is_default_message": False,
        }
    if agent.starter_prompts:
        # ADK StarterPrompts schema: prompts each need a stable id + title.
        spec["starter_prompts"] = {
            "is_default_prompts": False,
            "prompts": [
                {"id": f"prompt_{i}", "title": p, "prompt": p}
                for i, p in enumerate(agent.starter_prompts)
            ],
        }
    if agent.guidelines:
        # ADK AgentGuideline schema: display_name?/condition/action/tool?.
        spec["guidelines"] = [
            {
                k: v
                for k, v in {
                    "display_name": g.name,
                    "condition": g.condition,
                    "action": g.action,
                    "tool": g.tool_ref,
                }.items()
                if v is not None
            }
            for g in agent.guidelines
        ]
    return spec


def knowledge_base_specs(agent: Agent) -> list[dict]:
    """The `knowledge_bases import` specs for one agent -- at most one of them.

    Orchestrate's built-in knowledge base is document-backed: you hand it the
    files and it indexes them. So a source knowledge base only migrates when
    the documents actually shipped inside the export -- which is what
    `KnowledgeRef.file_path` records, and why it exists.

    All of an agent's documents go into a *single* base, because Orchestrate
    permits an agent exactly one and rejects the whole agent otherwise ("Max
    number of knowledge-base exceeded", verified live). That is not a
    workaround: a Copilot agent with three PDFs and an Orchestrate agent with
    one base holding three documents answer questions identically. What is lost
    is the per-file naming, which is why every source's own description is
    carried into the merged one rather than discarded.

    A reference with no `file_path` contributes nothing on purpose. The source
    pointed at something Wheatear never had (a SharePoint site, a
    connector-backed index), and an empty knowledge base named after it would
    put a thing on the target that looks migrated and answers nothing. That
    belongs in the review manifest, which is where it goes.
    """
    carried = [source for source in agent.knowledge if source.file_path]
    if not carried:
        return []

    descriptions = [s.description.strip() for s in carried if s.description]
    return [
        {
            "spec_version": "v1",
            "kind": "knowledge_base",
            "name": knowledge_base_name(agent),
            # Not optional, whatever the ADK's model says: a create without a
            # description is rejected by the service with nothing more useful
            # than "An Unexpected Error Occurred". Verified against a live
            # tenant by bisection.
            "description": " ".join(descriptions)
            or f"Documents migrated with {agent.name}.",
            "documents": [s.file_path for s in carried],
        }
    ]


def knowledge_base_name(agent: Agent) -> str:
    """What the agent's single merged knowledge base is called.

    Named after the agent rather than after any one of its documents: it holds
    all of them, and calling it after the first would misdescribe it the moment
    there are two.
    """
    return f"{agent.name}_knowledge"


def _connection_spec(conn) -> dict:
    return {
        "spec_version": "v1",
        "name": conn.ref,
        "auth_type": conn.auth_type,
        # Wheatear never auto-fills credentials; the human populates this.
        "credentials": "REPLACE_ME",
    }


def _review_manifest(agent: Agent, llm: str) -> dict | None:
    items: list[dict] = []

    # A model the source explicitly chose is never a 1:1 carry-over, so ask a
    # human to confirm the swap and re-check capability parity (see model_map).
    # If the source specified no model, there's nothing to confirm swapping from.
    if agent.model_hint:
        items.append(
            {
                "type": "model",
                "detail": f"Source model '{agent.model_hint}' mapped to '{llm}'.",
                "notes": ["Confirm the target model meets the agent's needs before relying on it."],
            }
        )

    unmappable = [c for c in agent.channels if c in _UNMAPPABLE_CHANNELS]
    if unmappable:
        items.append(
            {
                "type": "channel",
                "detail": f"Source published to {', '.join(unmappable)}, which have no Orchestrate equivalent.",
                "notes": ["Re-publish to an Orchestrate channel; Teams/M365 targets do not carry over."],
            }
        )

    if agent.content_moderation:
        items.append(
            {
                "type": "content_moderation",
                "detail": f"Source content moderation was '{agent.content_moderation}'.",
                "notes": [
                    "Orchestrate has no moderation slider; encode this posture as a guardrail "
                    "guideline or instruction clause if it matters."
                ],
            }
        )

    if agent.web_search:
        items.append(
            {
                "type": "web_search",
                "detail": "Source agent had web browsing / public web search enabled.",
                "notes": ["Add an explicit web-search tool on Orchestrate to reproduce this."],
            }
        )

    if not agent.description:
        items.append(
            {
                "type": "description",
                "detail": (
                    f"'{agent.name}' had no description on the source; Orchestrate "
                    "requires one, so its name was used."
                ),
                "notes": ["Write a real description -- it is what users see when choosing an agent."],
            }
        )

    _, discarded_style = agent_style(agent)
    if discarded_style:
        items.append(
            {
                "type": "style",
                "detail": (
                    f"Source style '{discarded_style}' is not one Orchestrate accepts; "
                    "the agent was imported with 'default'."
                ),
                "notes": [
                    "Pick a style deliberately if the source's conversational behaviour "
                    "mattered -- react, planner and react_core behave quite differently."
                ],
            }
        )

    # Tools left out of agent.yaml. Every one of these is a capability the
    # source agent had and the migrated one does not, so it is the single most
    # important thing in this file.
    carried_refs, dropped = importable_tools(agent)
    carried_refs = set(carried_refs)
    for tool in dropped:
        items.append(
            {
                "type": "tool",
                "detail": (
                    f"Source tool '{tool.source_ref or tool.ref}' was not carried "
                    f"(confidence {tool.confidence:.2f})."
                ),
                "notes": [
                    " ".join((tool.notes or "").split())
                    or "Nothing on the instance or in the catalog matched it closely enough.",
                    "Referencing it anyway would fail the whole agent's import, so it was "
                    "left out; add it by hand and re-import to restore the capability.",
                ],
            }
        )

    if agent.translation_confidence < 0.8:
        items.append(
            {
                "type": "translation",
                "detail": f"Instructions synthesized with confidence {agent.translation_confidence:.2f}",
                "notes": agent.translation_notes,
            }
        )

    for t in agent.tools:
        if t.mcp_server_url:
            # A portable MCP tool: not a blocker, but the endpoint must be
            # registered on the target (orchestrate toolkits import ... mcp).
            item = {
                "type": "mcp_tool",
                "ref": t.ref,
                "detail": f"Register the MCP server '{t.ref}' at {t.mcp_server_url}"
                + (f" (transport: {t.transport})" if t.transport else "")
                + " and confirm it's reachable from Orchestrate.",
            }
            if t.member_tools:
                item["tools"] = t.member_tools
            items.append(item)
        elif t.review_required and t.ref in carried_refs:
            # Only tools that *did* land. A dropped one is already reported in
            # full above, with what to do about it; listing it twice -- once
            # with a detail and once with a bare note -- makes the file read
            # like two tools where there is one.
            items.append(
                {
                    "type": "tool",
                    "ref": t.ref,
                    "source_ref": t.source_ref,
                    "notes": [t.notes or "No confident tool mapping; review before relying on it."],
                }
            )

    for k in agent.knowledge:
        if not k.file_path:
            items.append(
                {
                    "type": "knowledge",
                    "ref": k.ref,
                    "source_ref": k.source_ref,
                    "detail": (
                        f"Knowledge source '{k.ref}' was not carried: its documents did not "
                        "ship inside the export."
                    ),
                    "notes": [
                        k.notes
                        or "The source pointed at an external index (SharePoint, a connector) "
                        "rather than files.",
                        "Orchestrate's built-in knowledge base is document-backed. Re-ingest "
                        "the content there and add the reference by hand.",
                    ],
                }
            )
        elif k.review_required:
            items.append(
                {
                    "type": "knowledge",
                    "ref": k.ref,
                    "source_ref": k.source_ref,
                    "notes": k.notes or "No confident knowledge-base mapping; review before import.",
                }
            )

    for c in agent.connections:
        if c.review_required:
            items.append(
                {
                    "type": "connection",
                    "ref": c.ref,
                    "detail": "Credentials must be populated before this agent will run.",
                }
            )

    if not items:
        return None
    return {"agent": agent.name, "review_items": items}


def target_model_for(agent: Agent, live: bool = False) -> str:
    """The model this agent should run on after the move.

    With `live`, the model matrix ranks the source model's capabilities against
    what the *target tenant actually allows* -- which is the only correct
    answer, because that allow-list is account-specific and no table shipped in
    this repository can know it. A real dev tenant was found permitting exactly
    two models, neither of which the static table would have chosen.

    Off by default because it shells out to the ADK CLI. An export is meant to
    be runnable offline and reproducible, and a function that silently makes a
    network call is neither -- so the live lookup is something a caller asks
    for, not something it discovers it did.

    `model_map`'s tier lookup is the fallback either way: a worse answer than
    the matrix's, and a much better one than a model the tenant will refuse.
    """
    if not live:
        return resolve_target_model(agent.model_hint)

    from wheatear.model_matrix import recommend
    from wheatear.model_matrix.target_sources.orchestrate import OrchestrateModelSource

    try:
        result = recommend(agent.model_hint, OrchestrateModelSource())
    except Exception:  # noqa: BLE001 - no live model list is a fallback, not a failure
        return resolve_target_model(agent.model_hint)
    best = result.ranked_candidates[0] if result.ranked_candidates else None
    return best.raw_id if best else resolve_target_model(agent.model_hint)


def export_agent(agent: Agent, output_dir: Path, llm: str | None = None) -> ExportResult:
    """Write an IR Agent to a watsonx Orchestrate-ready directory layout.

    When `llm` is not given, the target model is resolved from the source model
    hint against what the tenant actually offers (see `target_model_for`); the
    choice is always surfaced in the review manifest for a human to confirm.
    """
    output_dir = Path(output_dir)

    if llm is None:
        llm = target_model_for(agent)
    agent.model_family = llm

    agent_path = output_dir / "agent.yaml"
    _dump_yaml(_agent_spec(agent, llm), agent_path)

    connection_paths = []
    for conn in agent.connections:
        conn_path = output_dir / "connections" / f"{conn.ref}.yaml"
        _dump_yaml(_connection_spec(conn), conn_path)
        connection_paths.append(conn_path)

    review_manifest_path = None
    manifest = _review_manifest(agent, llm)
    if manifest is not None:
        review_manifest_path = output_dir / "review-manifest.yaml"
        _dump_yaml(manifest, review_manifest_path)

    return ExportResult(
        agent_path=agent_path,
        connection_paths=connection_paths,
        review_manifest_path=review_manifest_path,
    )

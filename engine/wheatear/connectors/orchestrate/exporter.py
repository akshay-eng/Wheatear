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

from wheatear.ir.schema import Agent
from wheatear.model_map import resolve_target_model

# Channels the source agent may have been published to that have no equivalent
# Orchestrate deployment surface; the exporter flags these for the human rather
# than pretending they carried over.
_UNMAPPABLE_CHANNELS = {"msteams", "Microsoft365Copilot"}


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


def _agent_spec(agent: Agent, llm: str) -> dict:
    spec: dict = {
        "spec_version": "v1",
        "kind": "native",
        "name": agent.name,
        "llm": llm,
        "style": agent.agent_style or "default",
        "instructions": agent.instructions,
        "collaborators": [c.ref for c in agent.collaborators],
        "tools": [t.ref for t in agent.tools],
    }
    if agent.description:
        spec["description"] = agent.description
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
    if agent.knowledge:
        spec["knowledge_base"] = [k.ref for k in agent.knowledge]
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
        elif t.review_required:
            items.append(
                {
                    "type": "tool",
                    "ref": t.ref,
                    "source_ref": t.source_ref,
                    "notes": t.notes or "No confident tool mapping; implement manually before import.",
                }
            )

    for k in agent.knowledge:
        if k.review_required:
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


def export_agent(agent: Agent, output_dir: Path, llm: str | None = None) -> ExportResult:
    """Write an IR Agent to a watsonx Orchestrate-ready directory layout.

    When `llm` is not given, the target model is resolved from the source
    model hint by capability tier (see model_map); the choice is always
    surfaced in the review manifest for a human to confirm.
    """
    output_dir = Path(output_dir)

    if llm is None:
        llm = resolve_target_model(agent.model_hint)
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

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

DEFAULT_LLM = "watsonx/meta-llama/llama-3-3-70b-instruct"


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
    spec = {
        "spec_version": "v1",
        "kind": "native",
        "name": agent.name,
        "instructions": agent.instructions,
        "llm": llm,
        "style": "default",
        "collaborators": [],
        "tools": [t.ref for t in agent.tools],
    }
    if agent.knowledge:
        spec["knowledge_base"] = [k.ref for k in agent.knowledge]
    return spec


def _connection_spec(conn) -> dict:
    return {
        "spec_version": "v1",
        "name": conn.ref,
        "auth_type": conn.auth_type,
        # Wheatear never auto-fills credentials; the human populates this.
        "credentials": "REPLACE_ME",
    }


def _review_manifest(agent: Agent) -> dict | None:
    items: list[dict] = []

    if agent.translation_confidence < 0.8:
        items.append(
            {
                "type": "translation",
                "detail": f"Instructions synthesized with confidence {agent.translation_confidence:.2f}",
                "notes": agent.translation_notes,
            }
        )

    for t in agent.tools:
        if t.review_required:
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


def export_agent(agent: Agent, output_dir: Path, llm: str = DEFAULT_LLM) -> ExportResult:
    """Write an IR Agent to a watsonx Orchestrate-ready directory layout."""
    output_dir = Path(output_dir)

    agent_path = output_dir / "agent.yaml"
    _dump_yaml(_agent_spec(agent, llm), agent_path)

    connection_paths = []
    for conn in agent.connections:
        conn_path = output_dir / "connections" / f"{conn.ref}.yaml"
        _dump_yaml(_connection_spec(conn), conn_path)
        connection_paths.append(conn_path)

    review_manifest_path = None
    manifest = _review_manifest(agent)
    if manifest is not None:
        review_manifest_path = output_dir / "review-manifest.yaml"
        _dump_yaml(manifest, review_manifest_path)

    return ExportResult(
        agent_path=agent_path,
        connection_paths=connection_paths,
        review_manifest_path=review_manifest_path,
    )

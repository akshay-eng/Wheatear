"""Small, presentation-ready documents bundled with every migration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wheatear.service.models import MigrationRequest

_MAX_DETAIL_AGENTS = 20


def write_evaluation_pack(
    output_root: Path,
    request: MigrationRequest,
    summary: dict,
) -> list[dict[str, str]]:
    """Write concise Markdown guidance without copying credentials or source data."""
    directory = output_root / "evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    documents = {
        "evaluation-prompts.md": _evaluation_prompts(summary),
        "business-summary.md": _business_summary(request, summary),
        "migration-mapping.md": _migration_mapping(request, summary),
    }
    result = []
    for filename, content in documents.items():
        path = directory / filename
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        result.append(
            {
                "name": filename,
                "path": f"evaluation/{filename}",
                "label": _document_label(filename),
            }
        )
    return result


def _evaluation_prompts(summary: dict) -> str:
    agents = list(summary.get("agents") or [])
    names = [_clean(agent.get("name") or "Unnamed agent") for agent in agents]
    roster = ", ".join(names[:_MAX_DETAIL_AGENTS])
    if len(names) > _MAX_DETAIL_AGENTS:
        roster += f", and {len(names) - _MAX_DETAIL_AGENTS} more in the migration results"

    lines = [
        "# Agent Evaluation Prompts",
        "",
        "Use the baseline prompts with every migrated agent. Record pass, partial, or fail and",
        "capture the response when a result needs investigation.",
        "",
        f"**Agent roster:** {roster or 'No agents were produced.'}",
        "",
        "## Baseline prompts for every agent",
        "",
        "1. \"Introduce yourself, state what you can help with, and name one request you should not handle.\"",
        "2. \"Before taking action, repeat my goal, list any missing information, and ask for confirmation.\"",
        "3. \"Explain which source, tool, or collaborator you would use for this request. Do not invent a result.\"",
        "4. \"I am not authorized to approve this request. Explain the safe next step without performing it.\"",
        "",
        "## Capability checks",
        "",
    ]
    for agent in agents[:_MAX_DETAIL_AGENTS]:
        name = _clean(agent.get("name") or "Unnamed agent")
        tools = [_clean(item) for item in (agent.get("tools") or [])]
        collaborators = [_clean(item) for item in (agent.get("collaborators") or [])]
        knowledge = [_clean(item) for item in (agent.get("knowledge") or [])]
        if tools:
            prompt = (
                f"Ask **{name}** to explain when it would use `{tools[0]}`, then run a "
                "read-only example with clearly supplied inputs."
            )
        elif collaborators:
            prompt = (
                f"Ask **{name}** for a request that requires `{collaborators[0]}` and "
                "confirm that the handoff preserves the original goal."
            )
        elif knowledge:
            prompt = (
                f"Ask **{name}** a question grounded in `{knowledge[0]}` and require it "
                "to identify the basis for its answer."
            )
        else:
            prompt = (
                f"Give **{name}** an ambiguous request in its domain and confirm that it "
                "asks a clarifying question before acting."
            )
        lines.append(f"- {prompt}")
    lines.extend(
        [
            "",
            "## Acceptance",
            "",
            "- The agent stays within its stated role.",
            "- Tool calls use supplied inputs and do not fabricate success.",
            "- Authentication prompts appear before a protected action.",
            "- Collaborator handoffs preserve the user's request.",
            "- Missing tools or knowledge are reported clearly.",
        ]
    )
    return "\n".join(lines)


def _business_summary(request: MigrationRequest, summary: dict) -> str:
    source = "n8n" if request.source.platform == "n8n" else "Microsoft Copilot Studio"
    delivery = "deployed to the target" if request.target.deploy else "compiled as a dry run"
    provider = {
        "none": "deterministic translation",
        "anthropic": "Anthropic-assisted translation",
        "google": "Google-assisted translation",
        "watsonx": "IBM watsonx-assisted translation",
    }[request.translation.provider]
    processed = int(summary.get("processed") or 0)
    deployed = int(summary.get("deployed") or 0)
    failed = int(summary.get("failed") or 0)
    blocking = len(
        [
            step
            for step in (summary.get("follow_up") or [])
            if step.get("blocking")
        ]
    )
    connections = len(summary.get("connection_reviews") or [])
    return "\n".join(
        [
            "# Business Migration Summary",
            "",
            "## Objective",
            "",
            f"Move selected AI agents and workflows from {source} to IBM watsonx Orchestrate",
            "while preserving instructions, supported tools, knowledge references, and delegation.",
            "",
            "## Scope and outcome",
            "",
            f"- **Items processed:** {processed}",
            f"- **Items deployed:** {deployed}",
            f"- **Items failed:** {failed}",
            f"- **Delivery:** {delivery}",
            f"- **Translation:** {provider}",
            f"- **Name-conflict policy:** {request.target.on_conflict}",
            f"- **Connections requiring confirmation:** {connections}",
            f"- **Blocking follow-up steps:** {blocking}",
            "",
            "## Controls",
            "",
            "- Source and target credentials were supplied for this browser session only.",
            "- Connection credentials are reviewed separately and are never included in this bundle.",
            "- Generated target definitions remain available for audit and controlled re-import.",
            "- Unsupported capabilities remain visible as follow-up work instead of being silently removed.",
            "",
            "## Recommended acceptance",
            "",
            "1. Confirm every connection in the Agent Liftoff results screen.",
            "2. Run the baseline and capability prompts in `evaluation-prompts.md`.",
            "3. Review blocking items and omitted capabilities before production use.",
            "4. Obtain business-owner approval after the target agents pass the evaluation.",
        ]
    )


def _migration_mapping(request: MigrationRequest, summary: dict) -> str:
    source = "n8n" if request.source.platform == "n8n" else "Copilot Studio"
    agents = list(summary.get("agents") or [])
    lines = [
        "# Source-to-Target Mapping",
        "",
        f"**Corridor:** {source} -> IBM watsonx Orchestrate",
        "",
        "| Source agent or workflow | Target agent | Carried capability | Result |",
        "|---|---|---|---|",
    ]
    for agent in agents[:_MAX_DETAIL_AGENTS]:
        source_name = _cell(agent.get("source_name") or agent.get("name") or "Unknown")
        target_name = _cell(agent.get("name") or "Unknown")
        tools = agent.get("tools") or []
        knowledge = agent.get("knowledge") or []
        collaborators = agent.get("collaborators") or []
        parts = []
        if tools:
            parts.append(f"{len(tools)} tool(s)")
        if knowledge:
            parts.append(f"{len(knowledge)} knowledge source(s)")
        if collaborators:
            parts.append(f"{len(collaborators)} collaborator(s)")
        capability = ", ".join(parts) or "Instructions and behavior"
        result = (
            "Deployed"
            if agent.get("deployed") is True
            else "Failed"
            if agent.get("deployed") is False
            else "Compiled"
        )
        lines.append(f"| {source_name} | {target_name} | {capability} | {result} |")
    if len(agents) > _MAX_DETAIL_AGENTS:
        lines.append(
            f"| Additional selection | {len(agents) - _MAX_DETAIL_AGENTS} more agent(s) | "
            "See generated YAML and migration results | Included |"
        )
    lines.extend(
        [
            "",
            "## Transformation rules",
            "",
            "- Source instructions become Orchestrate agent instructions.",
            "- Supported source operations are resolved to installed Orchestrate tools.",
            "- Knowledge references become target knowledge-base references when material is available.",
            "- Delegation edges become Orchestrate collaborator links.",
            "- Missing or unsupported capabilities are retained in the post-migration checklist.",
            "- Target names follow the selected update, rename, or skip conflict policy.",
            "",
            "## Operator review",
            "",
            f"- Follow-up items: {len(summary.get('follow_up') or [])}",
            f"- Pending catalog tools: {len(summary.get('pending_tools') or [])}",
            f"- Connections to confirm: {len(summary.get('connection_reviews') or [])}",
        ]
    )
    return "\n".join(lines)


def _document_label(filename: str) -> str:
    return {
        "evaluation-prompts.md": "Evaluation prompts",
        "business-summary.md": "Business summary",
        "migration-mapping.md": "Source-to-target mapping",
    }[filename]


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())[:120]


def _cell(value: object) -> str:
    return _clean(value).replace("|", "/")

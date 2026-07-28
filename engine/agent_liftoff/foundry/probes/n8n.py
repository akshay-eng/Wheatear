"""What n8n's records look like, read from workflow JSON.

n8n is structurally unlike the other platforms Agent Liftoff probes, and the
difference decides the whole design of this module. Copilot Studio and
Orchestrate both hand out one record per thing: an agent is a row, a tool is a
row, a connection is a row. n8n hands out one record per *workflow*, and every
migratable entity is a node inside it, identified only by its `type` string:

    @n8n/n8n-nodes-langchain.agent            -> an agent
    @n8n/n8n-nodes-langchain.toolHttpRequest  -> a tool
    @n8n/n8n-nodes-langchain.toolWorkflow     -> a tool *or* a collaborator,
                                                 depending on what it points at
    n8n-nodes-base.readWriteFile              -> part of a knowledge base

So this probe's job is to split, not to fetch. It takes workflows and yields
per-kind record sets that the shape inference can read the way it reads any
other platform's rows.

Two decisions worth stating, because both were tempting to get wrong:

The node's `parameters` are lifted to the top of each observed record rather
than left nested. A tool's real schema is `toolDescription`, `url`, `method`,
`parametersQuery` -- not `parameters.*` -- and burying every meaningful field
one level down under a key that means nothing would make every inferred
mapping start with a hop that carries no information.

Credentials are observed for their *shape* only. An n8n credential block names
a credential and gives its id; the value is encrypted by n8n and redacted by
its API, so there is nothing secret to leak here -- but the id and display name
are tenant identifiers, and `redact` plus the map-vs-record inference in
`shape.py` are what keep them out of the fingerprint.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_liftoff.foundry.probes.base import ProbeContext, ProbeResult, observe
from agent_liftoff.foundry.types import EntityKind, GapReason, ProbeGap, ProbeOrigin

# Node type prefixes, mirrored from `connectors/n8n/graph.py`. Duplicated
# deliberately rather than imported: the foundry describes what a platform's
# records look like and must not depend on the connector that consumes them,
# or a change to one silently rewrites the other's fingerprint.
AGENT_TYPE = "@n8n/n8n-nodes-langchain.agent"
TOOL_PREFIX = "@n8n/n8n-nodes-langchain.tool"
MCP_TOOL = "@n8n/n8n-nodes-langchain.mcpClientTool"
READ_FILE = "n8n-nodes-base.readWriteFile"
EXTRACT_FILE = "n8n-nodes-base.extractFromFile"
LM_PREFIX = "@n8n/n8n-nodes-langchain.lm"


def _looks_like_workflow(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("nodes"), list)
        and isinstance(data.get("connections"), dict)
    )


def load_workflows(path: Path) -> list[dict]:
    """Every n8n workflow JSON under `path`, file or directory.

    Tolerant of a directory that also holds unrelated JSON: a folder of
    workflow exports routinely has a README, a package.json or a credentials
    dump beside it, and refusing the whole probe over one of those would be
    obstructive.
    """
    path = Path(path)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [data] if _looks_like_workflow(data) else []

    found: list[dict] = []
    for candidate in sorted(path.rglob("*.json")):
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if _looks_like_workflow(data):
            found.append(data)
        elif isinstance(data, list):
            found.extend(w for w in data if _looks_like_workflow(w))
    return found


def _flatten(node: dict, workflow: dict) -> dict:
    """One node as a record, with its parameters lifted to the top level.

    `workflow_name` and `workflow_active` are carried because an n8n node has
    no identity of its own -- two workflows can both hold a node called
    "Gemini Chat Model" -- and a mapping that cannot say which workflow a
    record came from cannot reassemble the graph.
    """
    record: dict = {
        "node_name": node.get("name"),
        "node_type": node.get("type"),
        "type_version": node.get("typeVersion"),
        "workflow_name": workflow.get("name"),
        "workflow_active": workflow.get("active"),
    }
    parameters = node.get("parameters")
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            if key not in record:
                record[key] = value
    return record


def _nodes(workflows: list[dict]):
    for workflow in workflows:
        for node in workflow.get("nodes") or []:
            if isinstance(node, dict):
                yield workflow, node


def split_by_kind(workflows: list[dict]) -> dict[EntityKind, list[dict]]:
    """Group every node in every workflow under the IR kind it maps onto."""
    grouped: dict[EntityKind, list[dict]] = {
        EntityKind.AGENT: [],
        EntityKind.TOOL: [],
        EntityKind.KNOWLEDGE: [],
        EntityKind.CONNECTION: [],
    }

    for workflow, node in _nodes(workflows):
        node_type = str(node.get("type") or "")
        record = _flatten(node, workflow)

        if node_type == AGENT_TYPE:
            grouped[EntityKind.AGENT].append(record)
        elif node_type.startswith(TOOL_PREFIX) or node_type == MCP_TOOL:
            grouped[EntityKind.TOOL].append(record)
        elif node_type in (READ_FILE, EXTRACT_FILE):
            grouped[EntityKind.KNOWLEDGE].append(record)

        # A credential block is its own entity: several nodes share one, which
        # is exactly the relationship a connection describes.
        credentials = node.get("credentials")
        if isinstance(credentials, dict):
            for cred_type, detail in credentials.items():
                entry = {
                    "credential_type": cred_type,
                    "used_by_node": node.get("name"),
                    "used_by_type": node_type,
                    "workflow_name": workflow.get("name"),
                }
                if isinstance(detail, dict):
                    entry.update({f"credential_{k}": v for k, v in detail.items()})
                grouped[EntityKind.CONNECTION].append(entry)

    return grouped


class N8nExportScan:
    """The structural probe: n8n workflow JSON, offline and free.

    There is no live counterpart yet. n8n's public API returns the same
    workflow JSON this reads, so a live probe would observe an identical shape
    and only add the ability to enumerate workflows the user did not export --
    worth having, not worth blocking a corridor on.
    """

    name = "n8n-export-scan"

    def probe(self, context: ProbeContext) -> ProbeResult:
        result = ProbeResult()

        if context.export_path is None:
            result.gaps.append(
                ProbeGap(
                    what="n8n workflows",
                    reason=GapReason.NOT_IN_EXPORT,
                    detail="No export path was given, so no n8n workflows could be read.",
                    remedy=(
                        "Export the workflows (n8n UI -> Download, or the public API) "
                        "and probe again with --export <folder>."
                    ),
                )
            )
            return result

        workflows = load_workflows(context.export_path)
        if not workflows:
            result.gaps.append(
                ProbeGap(
                    what="n8n workflows",
                    reason=GapReason.NOT_IN_EXPORT,
                    detail=f"No n8n workflow JSON was found under {context.export_path}.",
                    remedy="Point --export at the folder holding the exported workflow .json files.",
                )
            )
            return result

        result.notes.append(
            f"Read {len(workflows)} workflow(s): "
            + ", ".join(sorted(str(w.get("name") or "unnamed") for w in workflows))
        )

        for kind, records in split_by_kind(workflows).items():
            schema = observe(kind, kind.value, ProbeOrigin.EXPORT, records)
            if schema is not None:
                result.entities.append(schema)
            else:
                result.gaps.append(
                    ProbeGap(
                        what=kind.value,
                        reason=GapReason.NOT_IN_EXPORT,
                        detail=(
                            f"None of the {len(workflows)} workflow(s) contained a "
                            f"{kind.value}, so this build has no mapping for one."
                        ),
                        remedy=(
                            f"Export a workflow that uses a {kind.value} and probe again "
                            "if this corridor needs to carry them."
                        ),
                    )
                )

        # n8n has no equivalent of a Copilot topic: routing is the graph itself,
        # expressed as edges between nodes rather than as a record. Recorded so
        # a missing topic mapping reads as a platform fact, not an oversight.
        result.gaps.append(
            ProbeGap(
                what="topic",
                reason=GapReason.UNSUPPORTED,
                detail=(
                    "n8n expresses routing as connections between nodes rather than as "
                    "records, so there is nothing to map onto a topic. Delegation is "
                    "recovered from toolWorkflow edges at import time instead."
                ),
                remedy=None,
            )
        )
        return result

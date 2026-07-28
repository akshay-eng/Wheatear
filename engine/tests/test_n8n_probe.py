"""The n8n foundry probe: one workflow document -> per-kind record sets.

n8n is the first platform Wheatear probes where the entities are not rows. The
tests here pin the split, because a mis-split does not fail loudly -- it
produces a corpus that describes the wrong thing and an adapter compiled
against it.
"""

from __future__ import annotations

import json

from wheatear.foundry.conformance import declared_versions
from wheatear.foundry.probes.base import ProbeContext
from wheatear.foundry.probes.n8n import N8nExportScan, load_workflows, split_by_kind
from wheatear.foundry.types import EntityKind, GapReason

WORKFLOW = {
    "id": "wf1",
    "name": "ITSM Agent",
    "active": True,
    "connections": {},
    "nodes": [
        {
            "name": "ITSM Agent",
            "type": "@n8n/n8n-nodes-langchain.agent",
            "typeVersion": 2,
            "parameters": {"promptType": "define", "options": {"systemMessage": "You are..."}},
        },
        {
            "name": "Gemini",
            "type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini",
            "typeVersion": 1.1,
            "parameters": {"modelName": "models/gemini-2.5-pro"},
            "credentials": {"googlePalmApi": {"id": "abc", "name": "Gemini cred"}},
        },
        {
            "name": "Get Record",
            "type": "@n8n/n8n-nodes-langchain.toolHttpRequest",
            "typeVersion": 1.1,
            "parameters": {"url": "https://x.example.com/api", "method": "GET"},
            "credentials": {"httpHeaderAuth": {"id": "def", "name": "SN cred"}},
        },
        {
            "name": "Read Files",
            "type": "n8n-nodes-base.readWriteFile",
            "typeVersion": 1,
            "parameters": {"fileSelector": "/kb/*.pdf"},
        },
    ],
}


def write(tmp_path, workflow=WORKFLOW, name="wf.json"):
    (tmp_path / name).write_text(json.dumps(workflow))
    return tmp_path


def test_nodes_split_into_the_kinds_they_map_onto():
    grouped = split_by_kind([WORKFLOW])
    assert [r["node_name"] for r in grouped[EntityKind.AGENT]] == ["ITSM Agent"]
    assert [r["node_name"] for r in grouped[EntityKind.TOOL]] == ["Get Record"]
    assert [r["node_name"] for r in grouped[EntityKind.KNOWLEDGE]] == ["Read Files"]


def test_a_language_model_node_is_not_a_tool():
    """The lm* nodes configure the agent; they are not things it can call."""
    grouped = split_by_kind([WORKFLOW])
    assert "Gemini" not in [r["node_name"] for r in grouped[EntityKind.TOOL]]


def test_every_credential_block_becomes_a_connection_record():
    grouped = split_by_kind([WORKFLOW])
    kinds = {r["credential_type"] for r in grouped[EntityKind.CONNECTION]}
    assert kinds == {"googlePalmApi", "httpHeaderAuth"}


def test_parameters_are_lifted_to_the_top_of_the_record():
    """A tool's schema is `url` and `method`, not `parameters.url`."""
    tool = split_by_kind([WORKFLOW])[EntityKind.TOOL][0]
    assert tool["url"] == "https://x.example.com/api"
    assert tool["method"] == "GET"


def test_workflow_identity_travels_with_every_node():
    """Two workflows can hold nodes with the same name."""
    agent = split_by_kind([WORKFLOW])[EntityKind.AGENT][0]
    assert agent["workflow_name"] == "ITSM Agent"
    assert agent["workflow_active"] is True


def test_probe_reads_a_directory_and_reports_what_it_read(tmp_path):
    result = N8nExportScan().probe(
        ProbeContext(platform="n8n", export_path=write(tmp_path), allow_network=False)
    )
    kinds = {e.kind for e in result.entities}
    assert {EntityKind.AGENT, EntityKind.TOOL, EntityKind.KNOWLEDGE, EntityKind.CONNECTION} <= kinds
    assert any("ITSM Agent" in n for n in result.notes)


def test_topics_are_recorded_as_a_platform_fact_not_an_oversight():
    """n8n has no topic records; that must not read as a failed probe."""
    result = N8nExportScan().probe(ProbeContext(platform="n8n", export_path=None))
    # With no export path the probe reports the missing source, not a topic gap.
    assert result.gaps and result.gaps[0].reason is GapReason.NOT_IN_EXPORT
    assert result.gaps[0].remedy


def test_topic_gap_is_unsupported_when_workflows_were_read(tmp_path):
    result = N8nExportScan().probe(
        ProbeContext(platform="n8n", export_path=write(tmp_path), allow_network=False)
    )
    topic = next(g for g in result.gaps if g.what == "topic")
    assert topic.reason is GapReason.UNSUPPORTED


def test_non_workflow_json_beside_the_export_is_ignored(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "not-a-workflow"}))
    write(tmp_path)
    assert len(load_workflows(tmp_path)) == 1


def test_n8n_declares_a_node_api_version_not_the_product_version():
    """A product patch release must not read as contract drift."""
    versions = declared_versions("n8n")
    assert versions["n8n-node-api"] == "langchain-v2"
    assert "ir" in versions

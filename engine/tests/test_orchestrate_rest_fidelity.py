"""Fidelity of the rich Orchestrate REST export (agent/toolkits/guidelines).

Modeled on a real `export_agent.py` output (an ITSM/ServiceNow agent with two
MCP toolkits, ReAct style, and guidelines). Guards against silently dropping
the high-value data that format carries.
"""

import json

from wheatear.connectors.orchestrate.importer import import_agent
from wheatear.ir.schema import BridgeStrategy, ToolKind
from wheatear.pipeline.map import map_agent

REST_EXPORT = {
    "agent": {
        "name": "Incident_Managementagent_0044s7",
        "display_name": "Incident_Managementagent",
        "description": "ServiceNow Incident Creation Agent.",
        "instructions": "You are an elite SRE and ITSM ServiceNow Automation Agent.",
        "llm": "groq/openai/gpt-oss-120b",
        "style": "react",
        "guidelines": [
            {"display_name": "Bypass CI Check", "condition": "user requests an incident",
             "action": "Do NOT search for a CI.", "tool": "None"},
            {"display_name": "Strict Payload", "condition": "user approves",
             "action": "Payload MUST only contain X.", "tool": "tool-uuid-123"},
        ],
        "collaborators": [],
    },
    "toolkits": [
        {"name": "SNOWMCP", "type": "mcp", "mcp_server_url": "http://10.0.0.1:8000/sse",
         "transport": "sse", "current_tools": [{"id": "tool-uuid-123", "name": "SNOWMCP:create_incident"}]},
        {"name": "itsmtoolsandawx", "type": "mcp", "mcp_server_url": "http://10.0.0.1:8001/sse",
         "transport": "sse", "current_tools": []},
    ],
    "unmapped_tool_ids": ["a", "b"],
    "knowledge_bases": [],
}


def _write(tmp_path):
    f = tmp_path / "agent.json"
    f.write_text(json.dumps(REST_EXPORT))
    return f


def test_rest_import_preserves_model_style_description(tmp_path):
    a = import_agent(_write(tmp_path)).agent
    assert a.model_hint == "groq/openai/gpt-oss-120b"
    assert a.agent_style == "react"
    assert a.description == "ServiceNow Incident Creation Agent."


def test_rest_import_preserves_guidelines_and_resolves_tool_uuid(tmp_path):
    a = import_agent(_write(tmp_path)).agent
    assert len(a.guidelines) == 2
    assert a.guidelines[0].tool_ref is None                 # "None" string normalized
    assert a.guidelines[1].tool_ref == "SNOWMCP:create_incident"  # UUID resolved to name


def test_rest_import_preserves_mcp_server_urls(tmp_path):
    imp = import_agent(_write(tmp_path))
    urls = {r.name: r.mcp_server_url for r in imp.raw_tools}
    assert urls == {"SNOWMCP": "http://10.0.0.1:8000/sse", "itsmtoolsandawx": "http://10.0.0.1:8001/sse"}


def test_mcp_tools_map_as_portable_not_review(tmp_path):
    imp = import_agent(_write(tmp_path))
    agent = map_agent(imp, target_platform="orchestrate")
    assert len(agent.tools) == 2
    for t in agent.tools:
        assert t.kind == ToolKind.MCP
        assert t.bridge == BridgeStrategy.NATIVE_MCP
        assert t.review_required is False
        assert t.mcp_server_url  # carried through for the target to re-point


def test_mcp_endpoints_surface_in_orchestrate_review_manifest(tmp_path):
    import yaml as _yaml
    from wheatear.connectors.orchestrate.exporter import export_agent

    imp = import_agent(_write(tmp_path))
    agent = map_agent(imp, target_platform="orchestrate")
    agent.instructions = agent.existing_instructions
    result = export_agent(agent, tmp_path / "out")
    manifest = _yaml.safe_load(result.review_manifest_path.read_text())
    mcp_items = [i for i in manifest["review_items"] if i["type"] == "mcp_tool"]
    assert len(mcp_items) == 2
    assert any("10.0.0.1:8000" in i["detail"] for i in mcp_items)


def test_individual_tool_names_are_captured_and_surfaced(tmp_path):
    import yaml as _yaml
    from wheatear.connectors.orchestrate.exporter import export_agent

    imp = import_agent(_write(tmp_path))
    # captured from the toolkit's current_tools
    snow = next(r for r in imp.raw_tools if r.name == "SNOWMCP")
    assert snow.tool_names == ["SNOWMCP:create_incident"]

    agent = map_agent(imp, target_platform="orchestrate")
    agent.instructions = agent.existing_instructions
    result = export_agent(agent, tmp_path / "out")
    manifest = _yaml.safe_load(result.review_manifest_path.read_text())
    snow_item = next(i for i in manifest["review_items"] if i.get("ref") == "SNOWMCP")
    assert snow_item["tools"] == ["SNOWMCP:create_incident"]


def test_style_and_guidelines_survive_orchestrate_export(tmp_path):
    import yaml as _yaml
    from wheatear.connectors.orchestrate.exporter import export_agent

    imp = import_agent(_write(tmp_path))
    agent = map_agent(imp, target_platform="orchestrate")
    agent.instructions = agent.existing_instructions
    result = export_agent(agent, tmp_path / "out")
    spec = _yaml.safe_load(result.agent_path.read_text())
    assert spec["style"] == "react"
    assert len(spec["guidelines"]) == 2

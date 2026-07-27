"""Provisioner unit tests with a fake REST client (no network)."""

from wheatear.connectors.orchestrate import provisioner as p
from wheatear.connectors.orchestrate.provisioner import (
    provision_and_deploy,
    resolve_kb_files,
    resolve_tool_ids,
)


class _FakeClient:
    """Minimal stand-in for OrchestrateRestClient recording created agents."""

    def __init__(self, toolkits=None, tools=None):
        self._toolkits = toolkits or []
        self._tools = tools or []
        self.created = []
        self._next = 0

    def list_toolkits(self):
        return self._toolkits

    def tools_for_toolkit(self, tk_id):
        return [t for t in self._tools if t.get("toolkit_id") == tk_id]

    def list_agents(self):
        return []

    def create_agent(self, spec):
        self._next += 1
        aid = f"id-{self._next}"
        self.created.append({**spec, "id": aid})
        return {"id": aid, "is_update": False}

    def get_agent(self, aid):
        rec = next(a for a in self.created if a["id"] == aid)
        return {
            "tools": rec.get("tools", []),
            "knowledge_base": rec.get("knowledge_base", []),
            "collaborators": rec.get("collaborators", []),
        }

    def delete_agent(self, aid):
        pass


def test_resolve_tool_ids_filters_by_include_names():
    tools = [
        {"id": "1", "name": "candidatedata:get_candidate"},
        {"id": "2", "name": "candidatedata:run_sql"},
        {"id": "3", "name": "candidatedata:search_candidates"},
    ]
    assert set(resolve_tool_ids(tools, ["get_candidate", "run_sql"])) == {"1", "2"}
    # empty include -> all
    assert set(resolve_tool_ids(tools, [])) == {"1", "2", "3"}


def test_resolve_kb_files_globs(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "b.pdf").write_text("y")
    found = resolve_kb_files(str(tmp_path / "*.pdf"))
    assert len(found) == 2
    assert resolve_kb_files(None) == []


def test_reuse_existing_mcp_toolkit_by_server_url():
    client = _FakeClient(
        toolkits=[{"id": "tk1", "name": "candidatedata", "mcp": {"server_url": "http://x/sse"}}],
        tools=[{"id": "t1", "name": "candidatedata:get_candidate", "toolkit_id": "tk1"}],
    )
    tk_id, tools = p.find_or_create_mcp_toolkit(client, "candidatedata", "http://x/sse")
    assert tk_id == "tk1"
    assert tools[0]["id"] == "t1"


def test_provision_deploys_leaf_first_with_collaborators():
    from wheatear.connectors.base import ImportResult, RawToolRef
    from wheatear.ir.schema import Agent, AgentRef
    from wheatear.workflow import assemble_workflow

    child = Agent(name="HR Agent", source_platform="n8n", instructions="You are HR.")
    parent = Agent(
        name="Supervisor", source_platform="n8n", instructions="Route requests.",
        collaborators=[AgentRef(ref="HR Agent")],
    )
    wf = assemble_workflow([child, parent], source_platform="n8n", root="Supervisor")
    results = {
        "HR Agent": ImportResult(agent=child),
        "Supervisor": ImportResult(agent=parent),
    }
    client = _FakeClient()
    reports = provision_and_deploy(client, wf, results, "groq/openai/gpt-oss-120b", provider=None)

    by = {r.name: r for r in reports}
    assert by["HR Agent"].ok and by["Supervisor"].ok
    # supervisor deployed after HR and references its created id
    sup_spec = next(a for a in client.created if a["display_name"] == "Supervisor")
    hr_spec = next(a for a in client.created if a["display_name"] == "HR Agent")
    assert sup_spec["collaborators"] == [hr_spec["id"]]
    # names sanitized (no spaces)
    assert hr_spec["name"] == "HR_Agent"


def test_provision_skips_agents_not_selected():
    from wheatear.connectors.base import ImportResult
    from wheatear.ir.schema import Agent
    from wheatear.workflow import assemble_workflow

    a1 = Agent(name="Keep", source_platform="n8n", instructions="x")
    a2 = Agent(name="Drop", source_platform="n8n", instructions="y")
    wf = assemble_workflow([a1, a2], source_platform="n8n")
    client = _FakeClient()
    reports = provision_and_deploy(client, wf, {"Keep": ImportResult(agent=a1)}, "llm", provider=None)
    assert [r.name for r in reports] == ["Keep"]

"""Copilot Studio push (PAC pack/import) + LLM-assisted repair of the output."""

from wheatear.connectors.copilot_studio import deployer
from wheatear.connectors.copilot_studio.exporter import export_agent
from wheatear.ir.schema import Agent, KnowledgeRef
from wheatear.repair import RepairPlan, FileFix, repair_solution


def _solution(tmp_path):
    # A knowledge ref guarantees the exporter writes a review-manifest.yaml.
    agent = Agent(
        name="Helper", source_platform="orchestrate", instructions="Be helpful.",
        knowledge=[KnowledgeRef(ref="Docs", review_required=True)],
    )
    return export_agent(agent, tmp_path / "sol").agent_path


# ---- deployer ----------------------------------------------------------------


def test_staged_copy_excludes_review_manifest(tmp_path):
    sol = _solution(tmp_path)
    assert (sol / "review-manifest.yaml").exists()  # exporter wrote it

    staged = deployer._staged_copy(sol)
    assert (staged / "solution.xml").exists()
    assert (staged / "[Content_Types].xml").exists()
    assert not (staged / "review-manifest.yaml").exists()  # excluded from the package


def test_deploy_reports_failure_when_pac_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(deployer.shutil, "which", lambda _: None)
    result = deployer.deploy_solution(_solution(tmp_path))
    assert result.success is False
    assert "pac" in result.output.lower()


# ---- repair ------------------------------------------------------------------


class FakeProvider:
    def __init__(self, plan):
        self._plan = plan

    def generate_structured(self, prompt, schema):
        return self._plan


def test_repair_applies_valid_fixes(tmp_path):
    sol = _solution(tmp_path)
    plan = RepairPlan(
        explanation="Fixed the solution version.",
        fixes=[FileFix(path="solution.xml", new_content="<fixed/>")],
    )
    result = repair_solution(sol, "some pac error", FakeProvider(plan))
    assert result.changed == ["solution.xml"]
    assert (sol / "solution.xml").read_text() == "<fixed/>"


def test_repair_refuses_path_traversal(tmp_path):
    sol = _solution(tmp_path)
    outside = tmp_path / "evil.txt"
    plan = RepairPlan(fixes=[FileFix(path="../evil.txt", new_content="pwned")])
    result = repair_solution(sol, "err", FakeProvider(plan))
    assert "../evil.txt" in result.skipped
    assert not outside.exists()  # never written outside the solution dir

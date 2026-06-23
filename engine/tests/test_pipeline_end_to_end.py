from pathlib import Path

from click.testing import CliRunner

from wheatear.cli import main
from wheatear.connectors.copilot_studio.importer import import_agent
from wheatear.connectors.orchestrate.exporter import export_agent
from wheatear.pipeline.map import map_agent
from wheatear.pipeline.translate import TranslationOutput, translate_agent
from wheatear.pipeline.validate import validate_agent

FIXTURE_DIR = Path(__file__).parent.parent / "wheatear" / "connectors" / "copilot_studio" / "fixtures" / "sample_agent"


class FakeProvider:
    def generate_structured(self, prompt: str, schema):
        return TranslationOutput(
            instructions=(
                "Greet the user warmly. If they ask about an order, collect the order ID, "
                "look it up, and tell them whether it's delayed or on track. For policy "
                "questions, search the returns policy knowledge base."
            ),
            confidence=0.78,
            notes=["legacy_flow connector has no confirmed Orchestrate equivalent yet."],
        )


def test_full_pipeline_copilot_studio_to_orchestrate(tmp_path):
    """Mirrors what `wheatear migrate` does internally, with a fake LLM
    provider standing in for Translate so this runs with no network access.
    """
    import_result = import_agent(FIXTURE_DIR)
    agent = map_agent(import_result)
    translate_agent(agent, FakeProvider())
    validation = validate_agent(agent)

    assert validation.is_valid

    output_dir = tmp_path / "orchestrate-out"
    result = export_agent(agent, output_dir)

    assert result.agent_path.exists()
    assert result.needs_review  # the unmapped SalesforceOrderLookup connector flags review
    assert result.review_manifest_path.exists()


def test_cli_extract_validates_a_real_clone_dir():
    runner = CliRunner()
    result = runner.invoke(main, ["extract", str(FIXTURE_DIR)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_cli_extract_fails_clearly_on_non_clone_dir(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["extract", str(tmp_path)])
    assert result.exit_code != 0
    assert "pac copilot clone" in result.output


def test_cli_migrate_rejects_unsupported_corridor(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["migrate", "--from", "copilot-studio", "--to", "vertex-ai", str(FIXTURE_DIR), str(tmp_path / "out")],
    )
    assert result.exit_code != 0
    assert "Unsupported corridor" in result.output


def test_cli_migrate_fails_clearly_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["migrate", "--from", "copilot-studio", "--to", "orchestrate", str(FIXTURE_DIR), str(tmp_path / "out")],
    )
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in result.output


def test_cli_help_lists_wizard_and_existing_commands():
    """The wizard command coexists with the flag-based commands -- this
    guards against `invoke_without_command=True` accidentally swallowing
    subcommand dispatch.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "wizard" in result.output
    assert "migrate" in result.output
    assert "extract" in result.output


def test_cli_extract_still_dispatches_normally_with_group_default_enabled():
    """Regression guard: adding invoke_without_command=True to the group
    must not break normal subcommand dispatch.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["extract", str(FIXTURE_DIR)])
    assert result.exit_code == 0
    assert "OK" in result.output

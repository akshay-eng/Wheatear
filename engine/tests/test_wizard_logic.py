from pathlib import Path

from wheatear.config import WheatearConfig
from wheatear.ir.schema import Agent
from wheatear.wizard import (
    _export_for_target,
    _translate_stage,
    config_changed,
    resolve_key_env_for_provider,
    suggest_output_path,
)


def test_suggest_output_path_is_a_sibling_directory():
    export_path = Path("/home/user/exports/my-agent")
    assert suggest_output_path(export_path) == Path("/home/user/exports/my-agent-orchestrate")


def test_resolve_key_env_keeps_saved_value_for_same_provider():
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_CUSTOM_KEY_VAR")
    assert resolve_key_env_for_provider("anthropic", existing) == "MY_CUSTOM_KEY_VAR"


def test_resolve_key_env_falls_back_to_default_when_provider_changes():
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_CUSTOM_KEY_VAR")
    assert resolve_key_env_for_provider("openai", existing) == "OPENAI_API_KEY"


def test_resolve_key_env_falls_back_to_default_when_no_existing_config():
    assert resolve_key_env_for_provider("anthropic", None) == "ANTHROPIC_API_KEY"


def test_config_changed_true_when_no_saved_config():
    assert config_changed(WheatearConfig(), None) is True


def test_config_changed_false_when_identical():
    cfg = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")
    assert config_changed(cfg, WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")) is False


def test_config_changed_true_when_provider_differs():
    cfg = WheatearConfig(llm_provider="openai", llm_key_env="OPENAI_API_KEY")
    old = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")
    assert config_changed(cfg, old) is True


def test_resolve_key_env_deterministic_provider_needs_no_default_key():
    # "none" (deterministic) must not blow up looking for a default key env.
    assert resolve_key_env_for_provider("none", None) == ""
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_KEY")
    assert resolve_key_env_for_provider("none", existing) == "MY_KEY"


def test_translate_stage_deterministic_when_no_provider():
    agent = Agent(name="a", source_platform="orchestrate", existing_instructions="Be helpful.")
    _translate_stage(agent, None)  # provider None -> deterministic carry-over
    assert agent.instructions == "Be helpful."
    assert agent.translation_confidence == 1.0


def test_export_for_target_dispatches_to_the_right_exporter(tmp_path):
    agent = Agent(name="Helper", source_platform="orchestrate", instructions="hi")

    orch = _export_for_target(agent, "orchestrate", tmp_path / "orch")
    assert (orch.agent_path).name == "agent.yaml"

    cp = _export_for_target(agent, "copilot-studio", tmp_path / "cp")
    assert (cp.agent_path / "solution.xml").exists()

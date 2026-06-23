from pathlib import Path

from wheatear.config import WheatearConfig
from wheatear.wizard import config_changed, resolve_key_env_for_provider, suggest_output_path


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

import json

from wheatear.config import WheatearConfig, load_config, save_config


def test_load_config_returns_none_when_no_file_exists(tmp_path):
    assert load_config(tmp_path / "missing.json") is None


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "config.json"
    save_config(WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY"), path)

    loaded = load_config(path)

    assert loaded == WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")


def test_save_config_never_writes_a_secret_value(tmp_path):
    """The config dataclass has no field for the key value itself -- this
    test exists so an accidental future field addition (e.g. 'llm_api_key')
    gets caught immediately rather than silently persisting a secret.
    """
    path = tmp_path / "config.json"
    save_config(WheatearConfig(), path)

    raw = json.loads(path.read_text())
    assert set(raw.keys()) == {"llm_provider", "llm_key_env"}


def test_load_config_handles_corrupt_file_gracefully(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json")

    assert load_config(path) is None


def test_load_config_fills_defaults_for_partial_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_provider": "openai"}))

    loaded = load_config(path)

    assert loaded.llm_provider == "openai"
    assert loaded.llm_key_env == "ANTHROPIC_API_KEY"  # default fallback

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
    """Enumerate every key the config file is allowed to contain.

    If a new field is added to WheatearConfig it must be listed here
    explicitly, forcing a conscious decision that it's safe (not a secret
    value) to persist on disk. Fields ending in '_env' store an env-var
    NAME, not the secret itself.
    """
    path = tmp_path / "config.json"
    save_config(WheatearConfig(), path)

    raw = json.loads(path.read_text())
    assert set(raw.keys()) == {
        "llm_provider",
        "llm_key_env",
        "orchestrate_instance_url",   # a URL, not a secret
        "orchestrate_api_key_env",    # stores env-var name, never the key value
        "source_env_url",             # Dataverse org URL, not a secret
        "source_tenant_id",           # Azure AD GUID, not a secret
        "source_orchestrate_url",     # source WXO instance URL, not a secret
        "source_orchestrate_workspace_id",  # workspace GUID, not a secret
    }


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

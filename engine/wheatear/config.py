"""Local user config: remembers platform choices and non-secret settings.

The key/password values themselves are never written to disk — consistent
with Wheatear's policy of never auto-filling credentials. What IS stored:

  llm_provider           which LLM to use for the Translate stage
  llm_key_env            name of the env var holding the LLM API key
  orchestrate_instance_url   watsonx Orchestrate service instance URL (URL, not secret)
  orchestrate_api_key_env    name of the env var holding the Orchestrate API key
  source_env_url         Copilot Studio / Power Platform environment Dataverse URL
  source_tenant_id       Azure AD tenant ID (a GUID, not a secret)

Fields ending in '_env' store an environment variable NAME so the user only
has to enter the actual secret once per shell session. Anything secret is
kept in the process environment only and is never serialised to disk.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "wheatear" / "config.json"

DEFAULT_PROVIDER = "anthropic"
DEFAULT_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_ORCHESTRATE_API_KEY_ENV = "ORCHESTRATE_API_KEY"


DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


@dataclass
class WheatearConfig:
    llm_provider: str = DEFAULT_PROVIDER
    llm_key_env: str = DEFAULT_KEY_ENV
    # Target Orchestrate (deploy-to instance)
    orchestrate_instance_url: str | None = None
    orchestrate_api_key_env: str = DEFAULT_ORCHESTRATE_API_KEY_ENV
    # Source Orchestrate (migrate-from instance)
    source_orchestrate_url: str | None = None
    source_orchestrate_workspace_id: str = DEFAULT_WORKSPACE_ID
    # Copilot Studio source
    source_env_url: str | None = None    # Power Platform Dataverse URL
    source_tenant_id: str | None = None  # Azure AD tenant ID
    # n8n source (migrate-from instance). URL + env-var *name* only; the API
    # key itself lives in the OS keychain (creds.KEY_N8N_API_KEY), never here.
    n8n_base_url: str | None = None
    n8n_api_key_env: str = "N8N_API_KEY"
    # First-launch onboarding (pre-flight dependency check) already shown
    onboarding_completed: bool = False


def load_config(path: Path = CONFIG_PATH) -> WheatearConfig | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return WheatearConfig(
        llm_provider=data.get("llm_provider", DEFAULT_PROVIDER),
        llm_key_env=data.get("llm_key_env", DEFAULT_KEY_ENV),
        orchestrate_instance_url=data.get("orchestrate_instance_url"),
        orchestrate_api_key_env=data.get("orchestrate_api_key_env", DEFAULT_ORCHESTRATE_API_KEY_ENV),
        source_orchestrate_url=data.get("source_orchestrate_url"),
        source_orchestrate_workspace_id=data.get(
            "source_orchestrate_workspace_id",
            # migrate old configs that stored env_name instead
            DEFAULT_WORKSPACE_ID,
        ),
        source_env_url=data.get("source_env_url"),
        source_tenant_id=data.get("source_tenant_id"),
        n8n_base_url=data.get("n8n_base_url"),
        n8n_api_key_env=data.get("n8n_api_key_env", "N8N_API_KEY"),
        onboarding_completed=data.get("onboarding_completed", False),
    )


def save_config(config: WheatearConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")

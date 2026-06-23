"""Local user config: remembers LLM provider choice, never a secret.

Only the provider name and the *name* of the environment variable holding
the API key are persisted. The key value itself is never written to disk --
consistent with the rest of Wheatear never auto-filling credentials. If the
named env var isn't set when needed, the wizard prompts for it and sets it
in the current process's environment only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "wheatear" / "config.json"

DEFAULT_PROVIDER = "anthropic"
DEFAULT_KEY_ENV = "ANTHROPIC_API_KEY"


@dataclass
class WheatearConfig:
    llm_provider: str = DEFAULT_PROVIDER
    llm_key_env: str = DEFAULT_KEY_ENV


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
    )


def save_config(config: WheatearConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")

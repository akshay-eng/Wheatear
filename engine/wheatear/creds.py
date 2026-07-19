"""Secure credential storage using the OS keychain (macOS Keychain,
Windows Credential Manager, Linux Secret Service via keyring).

Keys are stored under the service name "wheatear".  The module degrades
gracefully — if keyring is unavailable or the backend errors, save/load
are silent no-ops so the wizard still works, just without persistence.
"""

from __future__ import annotations

_SERVICE = "wheatear"

# Canonical keychain key names used across the wizard.
KEY_SRC_ORCHESTRATE = "source_orchestrate_api_key"
KEY_TGT_ORCHESTRATE = "target_orchestrate_api_key"


def llm_key_name(provider: str) -> str:
    """Return the keychain key name for a given LLM provider."""
    return f"llm_api_key_{provider}"


def save_secret(key: str, value: str) -> bool:
    """Persist a secret in the OS keychain. Returns True on success."""
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(_SERVICE, key, value)
        return True
    except Exception:
        return False


def load_secret(key: str) -> str | None:
    """Retrieve a secret from the OS keychain. Returns None if absent."""
    try:
        import keyring  # noqa: PLC0415
        return keyring.get_password(_SERVICE, key)
    except Exception:
        return None


def delete_secret(key: str) -> None:
    """Remove a secret from the OS keychain. Silent if absent."""
    try:
        import keyring  # noqa: PLC0415
        keyring.delete_password(_SERVICE, key)
    except Exception:
        pass

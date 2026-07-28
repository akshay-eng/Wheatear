"""Secure credential storage using the OS keychain (macOS Keychain,
Windows Credential Manager, Linux Secret Service via keyring).

Keys are stored under the service name "agent_liftoff".  The module degrades
gracefully — if keyring is unavailable or the backend errors, save/load
are silent no-ops so the wizard still works, just without persistence.

Reads fall back to the pre-rename service name. A product rename must not
silently invalidate the keys somebody already stored: the failure would not be
"your key is gone", it would be a migration that asks for an Orchestrate key it
already had, and an operator who cannot tell whether the tool broke or their
credential expired. Writes only ever go to the current name, so the old entry
drains away as keys are re-saved rather than being kept in step forever.
"""

from __future__ import annotations

SERVICE = "agent_liftoff"
LEGACY_SERVICE = "wheatear"

# Canonical keychain key names used across the wizard.
KEY_SRC_ORCHESTRATE = "source_orchestrate_api_key"
KEY_TGT_ORCHESTRATE = "target_orchestrate_api_key"
KEY_N8N_API_KEY = "source_n8n_api_key"


def llm_key_name(provider: str) -> str:
    """Return the keychain key name for a given LLM provider."""
    return f"llm_api_key_{provider}"


def save_secret(key: str, value: str) -> bool:
    """Persist a secret in the OS keychain. Returns True on success."""
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(SERVICE, key, value)
        return True
    except Exception:
        return False


def load_secret(key: str) -> str | None:
    """Retrieve a secret from the OS keychain. Returns None if absent.

    Falls back to the pre-rename service so keys stored before the rename keep
    working. Not written back automatically: re-saving somebody's credential
    under a new name as a side effect of reading it is not a read.
    """
    try:
        import keyring  # noqa: PLC0415

        found = keyring.get_password(SERVICE, key)
        if found:
            return found
        return keyring.get_password(LEGACY_SERVICE, key)
    except Exception:
        return None


def delete_secret(key: str) -> None:
    """Remove a secret from the OS keychain. Silent if absent.

    Both services, or "forget my credential" would leave the legacy copy behind
    and the next read would resurrect it.
    """
    for service in (SERVICE, LEGACY_SERVICE):
        try:
            import keyring  # noqa: PLC0415

            keyring.delete_password(service, key)
        except Exception:
            pass

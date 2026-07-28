"""Ephemeral Microsoft sign-in sessions for the web console."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from wheatear.connectors.copilot_studio.api_client import (
    CopilotStudioClient,
    Environment,
)
from wheatear.connectors.copilot_studio.auth import (
    DeviceCodeLogin,
    TokenProvider,
    begin_device_code,
    complete_device_code,
)


class CopilotAuthError(Exception):
    pass


@dataclass
class _Session:
    session_id: str
    login: DeviceCodeLogin
    created_at: float
    expires_at: float
    status: str = "pending"
    account_name: str = ""
    error: str = ""
    provider: TokenProvider | None = None
    environments: dict[str, Environment] = field(default_factory=dict)


class CopilotAuthStore:
    """Keeps Microsoft tokens and Dataverse URLs out of the browser."""

    def __init__(
        self,
        *,
        begin: Callable[[str], DeviceCodeLogin] = begin_device_code,
        complete: Callable[[DeviceCodeLogin], TokenProvider] = complete_device_code,
        client_factory: Callable[[TokenProvider], CopilotStudioClient] = CopilotStudioClient,
        tenant_id: str = "organizations",
        session_ttl: int = 7200,
    ) -> None:
        self._begin = begin
        self._complete = complete
        self._client_factory = client_factory
        self._tenant_id = tenant_id
        self._session_ttl = session_ttl
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    def start(self) -> dict:
        self._prune()
        login = self._begin(self._tenant_id)
        now = time.time()
        session = _Session(
            session_id=secrets.token_urlsafe(32),
            login=login,
            created_at=now,
            expires_at=now + min(login.expires_in, self._session_ttl),
        )
        with self._lock:
            self._sessions[session.session_id] = session
        threading.Thread(
            target=self._finish,
            args=(session.session_id,),
            name=f"copilot-auth-{session.session_id[:8]}",
            daemon=True,
        ).start()
        return self._public(session)

    def public(self, session_id: str) -> dict:
        session = self._require(session_id)
        return self._public(session)

    def context(
        self,
        session_id: str,
        environment_id: str,
    ) -> tuple[CopilotStudioClient, Environment]:
        session = self._require(session_id)
        if session.status != "authenticated" or session.provider is None:
            raise CopilotAuthError("Microsoft sign-in has not completed.")
        environment = session.environments.get(environment_id)
        if environment is None:
            raise CopilotAuthError(
                "The selected Power Platform environment is not available in this sign-in."
            )
        return self._client_factory(session.provider), environment

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _finish(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            provider = self._complete(session.login)
            environments = self._client_factory(provider).list_environments()
            if not environments:
                raise CopilotAuthError(
                    "Sign-in succeeded, but this account has no accessible Dataverse environments."
                )
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None:
                    return
                current.provider = provider
                current.account_name = provider.account_name
                current.environments = {item.id: item for item in environments}
                current.status = "authenticated"
                current.expires_at = time.time() + self._session_ttl
        except Exception as exc:  # noqa: BLE001 - auth errors become a public state
            with self._lock:
                current = self._sessions.get(session_id)
                if current is None:
                    return
                current.status = "failed"
                current.error = " ".join(str(exc).split())[:400]

    def _require(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise CopilotAuthError(
                    "This Microsoft sign-in session is no longer available. Sign in again."
                )
            if session.expires_at <= time.time():
                self._sessions.pop(session_id, None)
                raise CopilotAuthError(
                    "This Microsoft sign-in session expired. Sign in again."
                )
            return session

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if session.expires_at <= now
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)

    @staticmethod
    def _public(session: _Session) -> dict:
        response = {
            "id": session.session_id,
            "status": session.status,
            "account_name": session.account_name,
            "environments": [
                {"id": item.id, "name": item.display_name}
                for item in sorted(
                    session.environments.values(),
                    key=lambda item: item.display_name.casefold(),
                )
            ],
            "expires_in": max(0, int(session.expires_at - time.time())),
        }
        if session.status == "pending":
            response.update(
                {
                    "user_code": session.login.user_code,
                    "verification_uri": session.login.verification_uri,
                    "message": session.login.message,
                }
            )
        if session.error:
            response["error"] = session.error
        return response

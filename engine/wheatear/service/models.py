"""Request models shared by the web API and migration runner."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from wheatear.config import DEFAULT_WORKSPACE_ID

SourcePlatform = Literal["copilot-studio", "n8n"]
SourceMode = Literal["live", "upload"]
ConflictPolicy = Literal["rename", "update", "skip"]
LlmProvider = Literal["none", "anthropic", "google", "watsonx"]
ConnectionAuthKind = Literal[
    "basic_auth",
    "bearer_token",
    "api_key_auth",
    "oauth2_auth_code",
    "oauth2_client_creds",
    "oauth2_password",
]
ConnectionPreference = Literal["member", "team"]


class SourceSettings(BaseModel):
    platform: SourcePlatform
    mode: SourceMode = "live"
    base_url: str = ""
    api_key: str = ""
    environment_url: str = ""
    access_token: str = ""
    auth_session_id: str = ""
    environment_id: str = ""
    upload_id: str = ""
    scan_id: str = ""
    selected_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mode(self) -> "SourceSettings":
        if self.mode == "upload":
            if not self.upload_id:
                raise ValueError("Upload an export before continuing.")
            return self
        if self.platform == "n8n" and (not self.base_url or not self.api_key):
            raise ValueError("The n8n base URL and API key are required.")
        if self.platform == "copilot-studio":
            has_web_session = self.auth_session_id and self.environment_id
            has_legacy_token = self.environment_url and self.access_token
            if not has_web_session and not has_legacy_token:
                raise ValueError(
                    "Sign in with Microsoft and select a Power Platform environment."
                )
        return self

    def secret_values(self) -> list[str]:
        return [
            value
            for value in (self.api_key, self.access_token, self.auth_session_id)
            if value
        ]

    def clear_secrets(self) -> None:
        self.api_key = ""
        self.access_token = ""
        self.auth_session_id = ""


class TargetSettings(BaseModel):
    instance_url: str
    api_key: str
    workspace_id: str = DEFAULT_WORKSPACE_ID
    console_cookie: str = ""
    model: str = "groq/openai/gpt-oss-120b"
    deploy: bool = True
    on_conflict: ConflictPolicy = "update"

    @model_validator(mode="after")
    def required_fields(self) -> "TargetSettings":
        if not self.instance_url.strip():
            raise ValueError("The Orchestrate service instance URL is required.")
        if not self.api_key:
            raise ValueError("The IBM Cloud API key is required.")
        if not self.workspace_id.strip():
            raise ValueError("The workspace ID is required.")
        if not self.model.strip():
            raise ValueError("The target model is required.")
        return self

    def secret_values(self) -> list[str]:
        return [value for value in (self.api_key, self.console_cookie) if value]

    def clear_secrets(self) -> None:
        self.api_key = ""
        self.console_cookie = ""


class TranslationSettings(BaseModel):
    provider: LlmProvider = "none"
    api_key: str = ""

    @model_validator(mode="after")
    def key_for_provider(self) -> "TranslationSettings":
        if self.provider != "none" and not self.api_key:
            raise ValueError(f"An API key is required for {self.provider}.")
        return self

    def secret_values(self) -> list[str]:
        return [self.api_key] if self.api_key else []

    def clear_secrets(self) -> None:
        self.api_key = ""


class DiscoveryRequest(BaseModel):
    source: SourceSettings


class CopilotScanRequest(BaseModel):
    source: SourceSettings
    solution_ids: list[str]

    @model_validator(mode="after")
    def has_solutions(self) -> "CopilotScanRequest":
        if self.source.platform != "copilot-studio" or self.source.mode != "live":
            raise ValueError("Solution scanning requires a live Copilot Studio source.")
        if not self.solution_ids:
            raise ValueError("Select at least one solution to scan.")
        return self


class TargetValidationRequest(BaseModel):
    target: TargetSettings


class MigrationRequest(BaseModel):
    source: SourceSettings
    target: TargetSettings
    translation: TranslationSettings = Field(default_factory=TranslationSettings)

    @model_validator(mode="after")
    def has_selection(self) -> "MigrationRequest":
        if not self.source.selected_ids:
            raise ValueError("Select at least one agent or workflow.")
        return self

    def secret_values(self) -> list[str]:
        return [
            *self.source.secret_values(),
            *self.target.secret_values(),
            *self.translation.secret_values(),
        ]

    def clear_secrets(self) -> None:
        self.source.clear_secrets()
        self.target.clear_secrets()
        self.translation.clear_secrets()


class ConnectionConfigureRequest(BaseModel):
    target: TargetSettings
    app_id: str
    environment: Literal["draft", "live"] = "draft"
    kind: ConnectionAuthKind
    preference: ConnectionPreference = "team"
    server_url: str = ""
    credentials: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_fields(self) -> "ConnectionConfigureRequest":
        self.app_id = self.app_id.strip()
        self.server_url = self.server_url.strip()
        if not self.app_id:
            raise ValueError("The connection application ID is required.")
        return self

    def clear_secrets(self) -> None:
        self.target.clear_secrets()
        for name in list(self.credentials):
            self.credentials[name] = ""
        self.credentials.clear()


class DiscoveredItem(BaseModel):
    id: str
    name: str
    description: str = ""
    active: bool | None = None
    kind: str = ""
    source_id: str = ""
    group_id: str = ""
    group_name: str = ""
    version: str = ""


class DiscoveryResponse(BaseModel):
    items: list[DiscoveredItem]
    message: str
    scan_id: str = ""
    issues: list[str] = Field(default_factory=list)


class TargetValidationResponse(BaseModel):
    message: str
    agent_count: int

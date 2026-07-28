from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from wheatear.connectors.orchestrate.connections import AppConnection
from wheatear.service.app import create_app
from wheatear.service.documents import write_evaluation_pack
from wheatear.service.jobs import JobManager
from wheatear.service.models import ConnectionConfigureRequest, MigrationRequest
from wheatear.service.runner import (
    _connection_reviews,
    _provider,
    configure_target_connection,
)


def _migration_request(provider: str = "none") -> MigrationRequest:
    return MigrationRequest.model_validate(
        {
            "source": {
                "platform": "n8n",
                "mode": "upload",
                "upload_id": "fixture",
                "selected_ids": ["workflow-1"],
            },
            "target": {
                "instance_url": "https://example.invalid/instances/demo",
                "api_key": "ibm-secret",
                "deploy": False,
            },
            "translation": {
                "provider": provider,
                "api_key": "translation-secret" if provider != "none" else "",
            },
        }
    )


def _connection(**overrides) -> AppConnection:
    values = {
        "app_id": "servicenow_prod",
        "connection_id": "connection-guid",
        "environment": "draft",
        "security_scheme": "basic_auth",
        "preference": "team",
        "server_url": "https://contoso.service-now.com",
        "is_configured": True,
        "credentials_entered": True,
    }
    values.update(overrides)
    return AppConnection(**values)


def test_connection_review_always_includes_a_ready_bound_connection(monkeypatch):
    class Client:
        def list_all_tools(self):
            return [
                {
                    "name": "get_records",
                    "binding": {
                        "python": {
                            "connections": {
                                "servicenow_prod": "connection-guid",
                            }
                        }
                    },
                }
            ]

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.connections.list_applications",
        lambda _url, _token: [_connection()],
    )

    reviews = _connection_reviews(
        Client(),
        "https://example.invalid/instances/demo",
        "iam-token",
        [{"name": "Support", "tools": ["get_records"]}],
    )

    assert len(reviews) == 1
    assert reviews[0]["app_id"] == "servicenow_prod"
    assert reviews[0]["ready"] is True
    assert reviews[0]["credentials_entered"] is True
    assert reviews[0]["default_kind"] == "basic_auth"
    assert reviews[0]["tools"] == ["get_records"]
    assert any(option["value"] == "bearer_token" for option in reviews[0]["auth_options"])


def test_unbound_tools_never_create_guessed_credential_prompts(monkeypatch):
    class Client:
        def list_all_tools(self):
            return [
                {
                    "name": "SNOWMCPALL:get_record",
                    "binding": {"mcp": {"connections": {}}},
                }
            ]

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.connections.list_applications",
        lambda _url, _token: [_connection()],
    )

    assert _connection_reviews(
        Client(),
        "https://example.invalid/instances/demo",
        "iam-token",
        [{"name": "Support", "tools": ["SNOWMCPALL:get_record"]}],
    ) == []


def test_connection_configuration_submits_the_secret_once_without_returning_it(
    monkeypatch,
):
    captured = {}

    class RestClient:
        def __init__(self, api_key, instance_url, workspace_id):
            captured["target"] = (api_key, instance_url, workspace_id)
            self._session = type(
                "Session",
                (),
                {"headers": {"Authorization": "Bearer iam-token"}},
            )()

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.rest_client.OrchestrateRestClient",
        RestClient,
    )
    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.adk_session.ensure_session",
        lambda instance_url, api_key, cli: captured.update(
            session=(instance_url, api_key, cli)
        )
        or "migration",
    )
    monkeypatch.setattr(
        "wheatear.pipeline.solution_migration.adk_cli",
        lambda: "orchestrate",
    )

    def provision(request, secrets):
        captured["request"] = request
        captured["secrets"] = dict(secrets)
        return [
            "configured `servicenow_prod` for basic_auth (a shared credential)",
            "stored the credential on `servicenow_prod` (draft)",
        ]

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.provisioning.provision",
        provision,
    )
    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.connections.list_applications",
        lambda _url, _token: [_connection()],
    )
    payload = ConnectionConfigureRequest.model_validate(
        {
            "target": {
                "instance_url": "https://example.invalid/instances/demo",
                "api_key": "ibm-secret",
            },
            "app_id": "servicenow_prod",
            "kind": "basic_auth",
            "preference": "team",
            "server_url": "https://contoso.service-now.com",
            "credentials": {
                "username": "migration-user",
                "password": "connection-secret",
            },
        }
    )

    result = configure_target_connection(payload)

    assert captured["secrets"]["password"] == "connection-secret"
    assert captured["request"].preference == "team"
    assert result["connection"]["ready"] is True
    assert "connection-secret" not in str(result)
    assert "migration-user" not in str(result)


def test_connection_api_clears_submitted_secrets_after_the_request(
    tmp_path, monkeypatch
):
    captured = {}

    def configure(payload):
        captured["payload"] = payload
        return {"message": "Configured.", "actions": [], "connection": {}}

    monkeypatch.setattr("wheatear.service.app.configure_target_connection", configure)
    app = create_app(ui_dir=tmp_path / "no-ui")
    with TestClient(app) as client:
        response = client.post(
            "/api/connections/configure",
            json={
                "target": {
                    "instance_url": "https://example.invalid/instances/demo",
                    "api_key": "ibm-secret",
                },
                "app_id": "servicenow_prod",
                "kind": "bearer_token",
                "preference": "team",
                "credentials": {"token": "connection-secret"},
            },
        )

    assert response.status_code == 200
    assert captured["payload"].credentials == {}
    assert captured["payload"].target.api_key == ""


def test_watsonx_ui_provider_uses_the_google_adapter_but_masks_its_identity(
    monkeypatch,
):
    calls = []

    class Provider:
        def generate_structured(self, *_args, **_kwargs):
            raise RuntimeError("Google Gemini model gemini-2.5-pro rejected the key")

    monkeypatch.setattr(
        "wheatear.llm.factory.build_provider",
        lambda name, key: calls.append((name, key)) or Provider(),
    )
    provider = _provider(_migration_request("watsonx"))

    assert calls == [("google", "translation-secret")]
    with pytest.raises(RuntimeError) as caught:
        provider.generate_structured("prompt", object)
    message = str(caught.value)
    assert "IBM watsonx" in message
    assert "google" not in message.casefold()
    assert "gemini" not in message.casefold()


def test_watsonx_job_stream_and_summary_never_expose_internal_provider(tmp_path):
    def runner(job, _request):
        job.emit("translate", "Google Gemini model gemini-2.5-pro is running.")
        return {
            "provider": "Google Gemini",
            "nested": ["gemini-2.5-pro", {"vendor": "google"}],
        }

    manager = JobManager(runner, root=tmp_path)
    job = manager.submit(_migration_request("watsonx"))
    deadline = time.monotonic() + 2
    while job.status not in {"completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)

    rendered = str(job.events) + str(job.summary)
    assert job.status == "completed"
    assert "IBM watsonx" in rendered
    assert "google" not in rendered.casefold()
    assert "gemini" not in rendered.casefold()
    manager.close()


def test_evaluation_pack_is_concise_complete_and_ibm_branded(tmp_path):
    agents = [
        {
            "source_name": f"Source Agent {index}",
            "name": f"Target_Agent_{index}",
            "deployed": True,
            "tools": ["get_records"] if index == 0 else [],
            "knowledge": ["policy"] if index == 1 else [],
            "collaborators": ["Target_Agent_1"] if index == 2 else [],
        }
        for index in range(25)
    ]
    summary = {
        "processed": len(agents),
        "deployed": len(agents),
        "failed": 0,
        "agents": agents,
        "connection_reviews": [{"app_id": "servicenow_prod"}],
        "follow_up": [],
        "pending_tools": [],
    }

    documents = write_evaluation_pack(
        tmp_path,
        _migration_request("watsonx"),
        summary,
    )

    assert {item["name"] for item in documents} == {
        "evaluation-prompts.md",
        "business-summary.md",
        "migration-mapping.md",
    }
    contents = [
        (tmp_path / item["path"]).read_text(encoding="utf-8")
        for item in documents
    ]
    assert all(len(content.split()) < 900 for content in contents)
    assert all("translation-secret" not in content for content in contents)
    assert all("gemini" not in content.casefold() for content in contents)
    assert "Baseline prompts for every agent" in contents[0]
    assert "Additional selection" in contents[2]


def test_generated_markdown_is_downloadable_from_the_job(tmp_path):
    def runner(job, request):
        documents = write_evaluation_pack(
            job.run_dir / "output",
            request,
            {
                "processed": 1,
                "deployed": 0,
                "failed": 0,
                "agents": [{"name": "Supervisor", "deployed": None}],
            },
        )
        return {"processed": 1, "dry_run": True, "documents": documents}

    jobs = JobManager(runner, root=tmp_path / "runs")
    app = create_app(
        job_manager=jobs,
        ui_dir=tmp_path / "no-ui",
    )
    with TestClient(app) as client:
        started = client.post("/api/jobs", json=_migration_request().model_dump())
        job_id = started.json()["id"]
        for _ in range(200):
            status = client.get(f"/api/jobs/{job_id}").json()
            if status["status"] == "completed":
                break
            time.sleep(0.005)
        response = client.get(
            f"/api/jobs/{job_id}/documents/evaluation-prompts.md"
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Agent Evaluation Prompts" in response.text

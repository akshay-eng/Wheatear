from __future__ import annotations

import time
import zipfile
import shutil
import threading
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from wheatear.service.__main__ import bind_available
from wheatear.service.app import create_app
from wheatear.service.copilot_auth import CopilotAuthStore
from wheatear.service.jobs import Job, JobManager
from wheatear.service.models import (
    DiscoveredItem,
    MigrationRequest,
    SourceSettings,
    TargetSettings,
)
from wheatear.service.redaction import SecretRedactor
from wheatear.service.runner import (
    _run_copilot,
    _run_n8n,
    scan_copilot_solutions,
)
from wheatear.service.uploads import (
    UploadError,
    UploadRecord,
    UploadStore,
    _safe_extract,
)

N8N_FIXTURES = (
    Path(__file__).parent.parent
    / "wheatear"
    / "connectors"
    / "n8n"
    / "fixtures"
)
COPILOT_FIXTURE = (
    Path(__file__).parent.parent
    / "wheatear"
    / "connectors"
    / "copilot_studio"
    / "fixtures"
    / "sample_solution_agent"
)


def migration_payload() -> dict:
    return {
        "source": {
            "platform": "n8n",
            "mode": "upload",
            "upload_id": "fixture-upload",
            "selected_ids": ["h6wAloIJ62rKQMQM"],
        },
        "target": {
            "instance_url": "https://example.invalid/instances/demo",
            "api_key": "ibm-super-secret",
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "deploy": False,
        },
        "translation": {"provider": "none"},
    }


def test_redactor_removes_exact_and_labeled_secrets():
    redact = SecretRedactor(["a-very-secret-value"])
    text = redact(
        "api_key=another-secret Authorization: Bearer abc.def "
        "value=a-very-secret-value"
    )
    assert "a-very-secret-value" not in text
    assert "another-secret" not in text
    assert "abc.def" not in text
    assert text.count("[redacted]") == 3


def test_job_stream_replays_events_and_never_exposes_credentials(tmp_path):
    def runner(job, request):
        job.emit("test", f"received api_key={request.target.api_key}")
        artifact = job.run_dir / "results.zip"
        artifact.write_bytes(b"zip")
        job.artifact_path = artifact
        return {"processed": 1, "deployed": 0, "dry_run": True}

    manager = JobManager(runner, root=tmp_path)
    request = MigrationRequest.model_validate(migration_payload())
    job = manager.submit(request)
    for _ in range(200):
        if job.status in {"completed", "failed"}:
            break
        time.sleep(0.005)

    stream = "".join(job.stream())

    assert job.status == "completed"
    assert "ibm-super-secret" not in stream
    assert "[redacted]" in stream
    assert '"status": "completed"' in stream
    assert request.target.api_key == ""
    manager.close()


def test_api_health_upload_discovery_job_events_and_artifact(tmp_path):
    def runner(job, request):
        job.emit("compile", f"using token {request.target.api_key}", "ok")
        artifact = job.run_dir / "results.zip"
        artifact.write_bytes(b"result")
        job.artifact_path = artifact
        return {"processed": 1, "deployed": 0, "dry_run": True}

    uploads = UploadStore(tmp_path / "uploads")
    jobs = JobManager(runner, root=tmp_path / "runs")
    app = create_app(
        upload_store=uploads,
        job_manager=jobs,
        ui_dir=tmp_path / "no-ui",
    )
    workflow = N8N_FIXTURES / "supervisor.json"

    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        with workflow.open("rb") as handle:
            uploaded = client.post(
                "/api/uploads",
                data={"platform": "n8n"},
                files={"files": ("supervisor.json", handle, "application/json")},
            )
        assert uploaded.status_code == 200
        upload_id = uploaded.json()["upload_id"]

        discovered = client.post(
            "/api/discover",
            json={
                "source": {
                    "platform": "n8n",
                    "mode": "upload",
                    "upload_id": upload_id,
                }
            },
        )
        assert discovered.status_code == 200
        assert discovered.json()["items"][0]["name"] == "Supervisor"

        payload = migration_payload()
        payload["source"]["upload_id"] = upload_id
        started = client.post("/api/jobs", json=payload)
        assert started.status_code == 202
        job_id = started.json()["id"]

        for _ in range(200):
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.005)
        events = client.get(f"/api/jobs/{job_id}/events").text
        assert "ibm-super-secret" not in events
        assert "[redacted]" in events
        artifact = client.get(f"/api/jobs/{job_id}/artifact")
        assert artifact.status_code == 200
        assert artifact.content == b"result"


def test_safe_extract_rejects_parent_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../outside.txt", "no")

    try:
        _safe_extract(archive, tmp_path / "out")
    except UploadError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe ZIP was accepted")
    assert not (tmp_path / "outside.txt").exists()


def test_dynamic_port_falls_forward_when_preferred_is_busy():
    import socket

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    preferred = occupied.getsockname()[1]
    fallback, chosen = bind_available("127.0.0.1", preferred)
    try:
        assert chosen != preferred
        assert chosen > 0
    finally:
        fallback.close()
        occupied.close()


def test_copilot_auth_session_keeps_tokens_and_urls_server_side():
    from wheatear.connectors.copilot_studio.api_client import Environment

    release = threading.Event()

    class Login:
        user_code = "ABCD-EFGH"
        verification_uri = "https://microsoft.com/devicelogin"
        message = "Use the code to sign in."
        expires_in = 900

    class Provider:
        account_name = "maker@contoso.com"

    class Client:
        def __init__(self, provider):
            self.provider = provider

        def list_environments(self):
            return [
                Environment(
                    id="environment-guid",
                    display_name="Contoso Production",
                    instance_url="https://secret.crm.dynamics.com",
                )
            ]

    def begin(tenant_id):
        assert tenant_id == "organizations"
        return Login()

    def complete(_login):
        assert release.wait(timeout=1)
        return Provider()

    store = CopilotAuthStore(
        begin=begin,
        complete=complete,
        client_factory=Client,
    )
    started = store.start()

    assert started["status"] == "pending"
    assert started["user_code"] == "ABCD-EFGH"
    assert "secret.crm" not in str(started)
    release.set()
    for _ in range(100):
        status = store.public(started["id"])
        if status["status"] == "authenticated":
            break
        time.sleep(0.005)

    assert status["account_name"] == "maker@contoso.com"
    assert status["environments"] == [
        {"id": "environment-guid", "name": "Contoso Production"}
    ]
    assert "secret.crm" not in str(status)
    client, environment = store.context(started["id"], "environment-guid")
    assert isinstance(client, Client)
    assert environment.instance_url == "https://secret.crm.dynamics.com"


def test_copilot_auth_api_discovers_solutions_from_selected_environment(tmp_path):
    from wheatear.connectors.copilot_studio.api_client import (
        Environment,
        SolutionInfo,
    )

    class SourceClient:
        def list_solutions(self, environment, unmanaged_only=True):
            assert environment.id == "environment-guid"
            assert unmanaged_only is True
            return [
                SolutionInfo(
                    id="solution-guid",
                    unique_name="customer_service",
                    friendly_name="Customer Service",
                    version="2.0.0.0",
                    managed=False,
                )
            ]

    class AuthStore:
        deleted = []

        def start(self):
            return {
                "id": "opaque-session",
                "status": "pending",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "environments": [],
                "expires_in": 900,
            }

        def public(self, session_id):
            assert session_id == "opaque-session"
            return {
                "id": session_id,
                "status": "authenticated",
                "account_name": "maker@contoso.com",
                "environments": [
                    {"id": "environment-guid", "name": "Contoso Production"}
                ],
                "expires_in": 7200,
            }

        def context(self, session_id, environment_id):
            assert session_id == "opaque-session"
            assert environment_id == "environment-guid"
            return (
                SourceClient(),
                Environment(
                    id=environment_id,
                    display_name="Contoso Production",
                    instance_url="https://secret.crm.dynamics.com",
                ),
            )

        def delete(self, session_id):
            self.deleted.append(session_id)

        def clear(self):
            pass

    auth = AuthStore()
    app = create_app(
        copilot_auth_store=auth,
        ui_dir=tmp_path / "no-ui",
    )
    with TestClient(app) as client:
        started = client.post("/api/copilot/auth/sessions")
        assert started.status_code == 200
        assert started.json()["id"] == "opaque-session"
        status = client.get("/api/copilot/auth/sessions/opaque-session")
        assert status.json()["environments"][0]["name"] == "Contoso Production"

        discovered = client.post(
            "/api/discover",
            json={
                "source": {
                    "platform": "copilot-studio",
                    "mode": "live",
                    "auth_session_id": "opaque-session",
                    "environment_id": "environment-guid",
                }
            },
        )
        assert discovered.status_code == 200
        assert discovered.json()["items"][0]["id"] == "customer_service"
        assert "secret.crm" not in discovered.text

        deleted = client.delete("/api/copilot/auth/sessions/opaque-session")
        assert deleted.status_code == 204
        assert auth.deleted == ["opaque-session"]


def test_n8n_dry_run_compiles_selected_supervisor_and_collaborators(tmp_path):
    uploads = UploadStore(tmp_path / "uploads")
    upload_id = "fixtures"
    uploads._records[upload_id] = UploadRecord(
        upload_id=upload_id,
        platform="n8n",
        root=N8N_FIXTURES,
        source_path=N8N_FIXTURES,
        items=[
            DiscoveredItem(
                id="h6wAloIJ62rKQMQM",
                name="Supervisor",
                kind="workflow",
            )
        ],
        created_at=time.time(),
    )
    request = MigrationRequest.model_validate(
        {
            "source": {
                "platform": "n8n",
                "mode": "upload",
                "upload_id": upload_id,
                "selected_ids": ["h6wAloIJ62rKQMQM"],
            },
            "target": {
                "instance_url": "https://example.invalid/instances/demo",
                "api_key": "not-used-in-dry-run",
                "deploy": False,
            },
            "translation": {"provider": "none"},
        }
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = Job("dry-run", run_dir)

    summary = _run_n8n(
        job,
        request,
        uploads,
        client=object(),
        provider=None,
        output_root=run_dir / "output",
    )

    assert summary["processed"] == 3
    assert summary["dry_run"] is True
    assert len(list((run_dir / "output").rglob("agent.yaml"))) == 3


def test_n8n_service_artifacts_match_the_tui_pipeline(tmp_path):
    from wheatear.config import WheatearConfig
    from wheatear.connectors.n8n import importer
    from wheatear.connectors.orchestrate.catalog import connector_resolver
    from wheatear.pipeline.map import map_agent
    from wheatear.workflow import reachable_ids
    from wheatear.wizard import _run_ai_and_export_stages

    raw = importer._load_json_files(N8N_FIXTURES)
    tui_bundle = importer.import_workflows(raw)

    def collaborators(name):
        agent = tui_bundle.workflow.by_name(name)
        return [item.ref for item in agent.collaborators] if agent else []

    selected = set(reachable_ids({"Supervisor"}, collaborators))
    tui_ordered = [
        agent
        for agent in tui_bundle.workflow.migration_order()
        if agent.name in selected
    ]
    tui_results = {result.agent.name: result for result in tui_bundle.results}
    tui_output = tmp_path / "tui-output"
    for agent in tui_ordered:
        map_agent(
            tui_results[agent.name],
            target_platform="orchestrate",
            connector_resolver=connector_resolver(),
        )
        _run_ai_and_export_stages(
            agent,
            tui_output / agent.name.replace(" ", "_"),
            "orchestrate",
            WheatearConfig(llm_provider="none"),
            provider=None,
            llm="groq/openai/gpt-oss-120b",
        )

    uploads = UploadStore(tmp_path / "uploads")
    uploads._records["fixtures"] = UploadRecord(
        upload_id="fixtures",
        platform="n8n",
        root=N8N_FIXTURES,
        source_path=N8N_FIXTURES,
        items=[
            DiscoveredItem(
                id="h6wAloIJ62rKQMQM",
                name="Supervisor",
                kind="workflow",
            )
        ],
        created_at=time.time(),
    )
    request = MigrationRequest.model_validate(
        {
            "source": {
                "platform": "n8n",
                "mode": "upload",
                "upload_id": "fixtures",
                "selected_ids": ["h6wAloIJ62rKQMQM"],
            },
            "target": {
                "instance_url": "https://example.invalid/instances/demo",
                "api_key": "not-used-in-dry-run",
                "model": "groq/openai/gpt-oss-120b",
                "deploy": False,
            },
            "translation": {"provider": "none"},
        }
    )
    service_output = tmp_path / "service-output"
    service_output.mkdir()
    _run_n8n(
        Job("n8n-parity", tmp_path / "n8n-run"),
        request,
        uploads,
        client=object(),
        provider=None,
        output_root=service_output,
    )

    assert _agent_specs(tui_output) == _agent_specs(service_output)


def test_copilot_dry_run_uses_shipped_foundry_adapters(
    tmp_path, monkeypatch
):
    class ReadOnlyTarget:
        def list_all_tools(self):
            return []

        def list_toolkits(self):
            return []

        def list_agents(self):
            return []

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.adk_session.session_is_live",
        lambda _cli: True,
    )
    monkeypatch.setattr(
        "wheatear.model_matrix.target_sources.orchestrate."
        "OrchestrateModelSource.list_available_models",
        lambda _self, **_kwargs: ["groq/openai/gpt-oss-120b"],
    )

    uploads = UploadStore(tmp_path / "uploads")
    upload_id = "copilot-fixture"
    uploads._records[upload_id] = UploadRecord(
        upload_id=upload_id,
        platform="copilot-studio",
        root=COPILOT_FIXTURE,
        source_path=COPILOT_FIXTURE,
        items=[
            DiscoveredItem(
                id="ai_FakeBot",
                name="IT Help Bot",
                kind="agent",
                source_id="ai_FakeBot",
                group_id="upload",
                group_name="Uploaded solution",
            )
        ],
        created_at=time.time(),
        solutions={"upload": COPILOT_FIXTURE},
    )
    request = MigrationRequest.model_validate(
        {
            "source": {
                "platform": "copilot-studio",
                "mode": "upload",
                "upload_id": upload_id,
                "selected_ids": ["ai_FakeBot"],
            },
            "target": {
                "instance_url": "https://example.invalid/instances/demo",
                "api_key": "not-used-in-dry-run",
                "deploy": False,
            },
            "translation": {"provider": "none"},
        }
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    job = Job("copilot-dry-run", run_dir)

    summary = _run_copilot(
        job,
        request,
        uploads,
        client=ReadOnlyTarget(),
        token="not-used-in-dry-run",
        provider=None,
        output_root=run_dir / "output",
        foundry_root=tmp_path / "foundry",
    )

    assert summary["processed"] == 1
    assert summary["dry_run"] is True
    assert isinstance(summary["follow_up"], list)
    assert isinstance(summary["pending_tools"], list)
    assert summary["agents"][0]["name"] == "IT_Help_Bot"
    assert list((run_dir / "output").rglob("agent.yaml"))
    assert list((tmp_path / "foundry").rglob("artifact.json"))


def test_copilot_service_artifact_matches_the_tui_foundry_path(
    tmp_path, monkeypatch
):
    from wheatear.foundry.store import FoundryStore
    from wheatear.pipeline.solution_migration import adapters_ready
    from wheatear.wizard import (
        OrchestrateCredentials,
        ScannedSolution,
        _run_foundry_migration,
    )

    monkeypatch.setattr(
        "wheatear.connectors.orchestrate.adk_session.session_is_live",
        lambda _cli: True,
    )
    monkeypatch.setattr(
        "wheatear.model_matrix.target_sources.orchestrate."
        "OrchestrateModelSource.list_available_models",
        lambda _self, **_kwargs: ["groq/openai/gpt-oss-120b"],
    )
    monkeypatch.setattr(
        "wheatear.wizard._orchestrate_client",
        lambda _creds: (None, "offline parity run"),
    )
    monkeypatch.setattr("wheatear.wizard._confirm_dry_run", lambda _why: True)

    target_url = "https://example.invalid/instances/demo"
    store = FoundryStore(tmp_path / "foundry")
    ready, detail = adapters_ready(store)
    assert ready, detail
    tui_output = tmp_path / "tui-output"
    tui_report = _run_foundry_migration(
        [
            (
                ScannedSolution(
                    solution_name="fixture",
                    solution_label="Fixture Agents",
                    sol_dir=COPILOT_FIXTURE,
                    bots=[("ai_FakeBot", "IT Help Bot")],
                ),
                "ai_FakeBot",
                "IT Help Bot",
            )
        ],
        store,
        OrchestrateCredentials(
            instance_url=target_url,
            api_key_env="PARITY_API_KEY",
        ),
        provider=None,
        output_base=tui_output,
        on_conflict="update",
    )

    uploads = UploadStore(tmp_path / "uploads")
    uploads._records["fixture"] = UploadRecord(
        upload_id="fixture",
        platform="copilot-studio",
        root=COPILOT_FIXTURE,
        source_path=COPILOT_FIXTURE,
        items=[
            DiscoveredItem(
                id="ai_FakeBot",
                name="IT Help Bot",
                kind="agent",
                source_id="ai_FakeBot",
                group_id="upload",
                group_name="Uploaded solution",
            )
        ],
        created_at=time.time(),
        solutions={"upload": COPILOT_FIXTURE},
    )
    request = MigrationRequest.model_validate(
        {
            "source": {
                "platform": "copilot-studio",
                "mode": "upload",
                "upload_id": "fixture",
                "selected_ids": ["ai_FakeBot"],
            },
            "target": {
                "instance_url": target_url,
                "api_key": "not-used-in-dry-run",
                "deploy": False,
                "on_conflict": "update",
            },
            "translation": {"provider": "none"},
        }
    )
    service_output = tmp_path / "service-output"
    service_output.mkdir()
    service_summary = _run_copilot(
        Job("copilot-parity", tmp_path / "copilot-run"),
        request,
        uploads,
        client=None,
        token="not-used-in-dry-run",
        provider=None,
        output_root=service_output,
        foundry_root=store.root,
    )

    assert tui_report is not None
    assert [item.name for item in tui_report.agents] == [
        item["name"] for item in service_summary["agents"]
    ]
    assert _agent_specs(tui_output) == _agent_specs(service_output)


def test_live_copilot_scan_caches_agents_by_solution(tmp_path, monkeypatch):
    from wheatear.connectors.copilot_studio.api_client import SolutionInfo

    class SourceClient:
        exported = []

        def list_solutions(self, _environment, unmanaged_only=True):
            assert unmanaged_only is True
            return [
                SolutionInfo(
                    id="solution-guid",
                    unique_name="presentation_agents",
                    friendly_name="Presentation Agents",
                    version="1.2.0.0",
                    managed=False,
                )
            ]

        def export_solution(self, _environment, unique_name, destination):
            assert unique_name == "presentation_agents"
            self.exported.append(unique_name)
            shutil.copytree(COPILOT_FIXTURE, destination)
            return destination

    monkeypatch.setattr(
        "wheatear.service.runner._copilot_client",
        lambda _source: (SourceClient(), object()),
    )
    uploads = UploadStore(tmp_path / "uploads")
    source = SourceSettings.model_validate(
        {
            "platform": "copilot-studio",
            "mode": "live",
            "environment_url": "https://example.crm.dynamics.com",
            "access_token": "short-lived",
        }
    )

    result = scan_copilot_solutions(
        source,
        ["presentation_agents"],
        uploads,
    )

    assert result.scan_id
    assert result.items[0].id == "presentation_agents::ai_FakeBot"
    assert result.items[0].group_name == "Presentation Agents"
    record = uploads.get(result.scan_id, "copilot-studio")
    assert record.solutions["presentation_agents"].is_dir()

    source.scan_id = result.scan_id
    cached = scan_copilot_solutions(source, ["presentation_agents"], uploads)
    assert "reused 1 cached solution" in cached.message
    assert SourceClient.exported == ["presentation_agents"]

    api_uploads = UploadStore(tmp_path / "api-uploads")
    app = create_app(upload_store=api_uploads, ui_dir=tmp_path / "no-ui")
    with TestClient(app) as client:
        response = client.post(
            "/api/copilot/scan",
            json={
                "source": source.model_dump(),
                "solution_ids": ["presentation_agents"],
            },
        )

    assert response.status_code == 200
    assert response.json()["scan_id"]
    assert response.json()["items"][0]["group_name"] == "Presentation Agents"


def test_live_copilot_scan_keeps_successes_when_one_solution_fails(
    tmp_path, monkeypatch
):
    from wheatear.connectors.copilot_studio.api_client import SolutionInfo

    class SourceClient:
        def list_solutions(self, _environment, unmanaged_only=True):
            return [
                SolutionInfo("good-id", "good", "Good Agents", "1.0", False),
                SolutionInfo("bad-id", "bad", "Broken Export", "1.0", False),
            ]

        def export_solution(self, _environment, unique_name, destination):
            if unique_name == "bad":
                raise RuntimeError("Dataverse export timed out")
            shutil.copytree(COPILOT_FIXTURE, destination)
            return destination

    monkeypatch.setattr(
        "wheatear.service.runner._copilot_client",
        lambda _source: (SourceClient(), object()),
    )
    source = SourceSettings.model_validate(
        {
            "platform": "copilot-studio",
            "mode": "live",
            "environment_url": "https://example.crm.dynamics.com",
            "access_token": "short-lived",
        }
    )

    result = scan_copilot_solutions(
        source,
        ["good", "bad"],
        UploadStore(tmp_path / "uploads"),
    )

    assert [item.group_id for item in result.items] == ["good"]
    assert result.issues == ["Broken Export: Dataverse export timed out"]


def test_live_copilot_scan_does_not_reuse_a_previous_solution_directory(
    tmp_path, monkeypatch
):
    from wheatear.connectors.copilot_studio.api_client import SolutionInfo

    class SourceClient:
        def list_solutions(self, _environment, unmanaged_only=True):
            return [
                SolutionInfo("bad-id", "bad", "Broken", "1.0", False),
                SolutionInfo("good-id", "good", "Good", "1.0", False),
                SolutionInfo("later-id", "later", "Later", "1.0", False),
            ]

        def export_solution(self, _environment, unique_name, destination):
            if unique_name == "bad":
                raise RuntimeError("export failed")
            shutil.copytree(COPILOT_FIXTURE, destination)
            return destination

    monkeypatch.setattr(
        "wheatear.service.runner._copilot_client",
        lambda _source: (SourceClient(), object()),
    )
    source = SourceSettings.model_validate(
        {
            "platform": "copilot-studio",
            "mode": "live",
            "environment_url": "https://example.crm.dynamics.com",
            "access_token": "short-lived",
        }
    )
    uploads = UploadStore(tmp_path / "uploads")

    first = scan_copilot_solutions(source, ["bad", "good"], uploads)
    source.scan_id = first.scan_id
    second = scan_copilot_solutions(source, ["later"], uploads)

    record = uploads.get(first.scan_id, "copilot-studio")
    assert set(record.solutions) == {"good", "later"}
    assert record.solutions["good"] != record.solutions["later"]
    assert [item.group_id for item in second.items] == ["later"]


def _agent_specs(root: Path) -> dict[str, dict]:
    specs = [yaml.safe_load(path.read_text()) for path in sorted(root.rglob("agent.yaml"))]
    return {str(spec.get("name")): spec for spec in specs}

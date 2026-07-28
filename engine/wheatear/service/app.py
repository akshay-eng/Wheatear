"""FastAPI application serving the migration API and built React console."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from wheatear.service.copilot_auth import CopilotAuthError, CopilotAuthStore
from wheatear.service.jobs import JobManager
from wheatear.service.models import (
    ConnectionConfigureRequest,
    CopilotScanRequest,
    DiscoveryRequest,
    MigrationRequest,
    SourcePlatform,
    TargetValidationRequest,
)
from wheatear.service.runner import (
    build_runner,
    configure_target_connection,
    discover_source,
    scan_copilot_solutions,
    validate_target,
)
from wheatear.service.uploads import UploadError, UploadStore


def _default_ui_dir() -> Path:
    configured = os.environ.get("WHEATEAR_UI_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "UI" / "dist"


def create_app(
    *,
    upload_store: UploadStore | None = None,
    job_manager: JobManager | None = None,
    copilot_auth_store: CopilotAuthStore | None = None,
    ui_dir: Path | None = None,
) -> FastAPI:
    uploads = upload_store or UploadStore()
    jobs = job_manager or JobManager(build_runner(uploads))
    copilot_auth = copilot_auth_store or CopilotAuthStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.close()
        copilot_auth.clear()

    app = FastAPI(
        title="Agent Liftoff Migration Service",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.uploads = uploads
    app.state.jobs = jobs
    app.state.copilot_auth = copilot_auth

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "service": "agent-liftoff", "version": "0.1.0"}

    @app.post("/api/uploads")
    async def upload_source(
        platform: SourcePlatform = Form(...),
        files: list[UploadFile] = File(...),
    ) -> dict:
        try:
            record = await uploads.create(platform, files)
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "upload_id": record.upload_id,
            "items": [item.model_dump() for item in record.items],
            "message": f"Found {len(record.items)} item(s) in the upload.",
        }

    @app.post("/api/copilot/auth/sessions")
    def start_copilot_auth() -> dict:
        try:
            return copilot_auth.start()
        except Exception as exc:  # noqa: BLE001 - MSAL errors become clean API errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/copilot/auth/sessions/{session_id}")
    def copilot_auth_status(session_id: str) -> dict:
        try:
            return copilot_auth.public(session_id)
        except CopilotAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/copilot/auth/sessions/{session_id}", status_code=204)
    def delete_copilot_auth(session_id: str) -> None:
        copilot_auth.delete(session_id)

    @app.post("/api/discover")
    def discover(payload: DiscoveryRequest) -> dict:
        try:
            return discover_source(
                payload.source,
                uploads,
                copilot_auth,
            ).model_dump()
        except UploadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - remote errors become clean API errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            payload.source.clear_secrets()

    @app.post("/api/copilot/scan")
    def scan_copilot(payload: CopilotScanRequest) -> dict:
        try:
            return scan_copilot_solutions(
                payload.source,
                payload.solution_ids,
                uploads,
                copilot_auth,
            ).model_dump()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            payload.source.clear_secrets()

    @app.post("/api/target/validate")
    def target_validate(payload: TargetValidationRequest) -> dict:
        try:
            return validate_target(payload.target).model_dump()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            payload.target.clear_secrets()

    @app.post("/api/connections/configure")
    def connection_configure(payload: ConnectionConfigureRequest) -> dict:
        try:
            return configure_target_connection(payload)
        except Exception as exc:  # noqa: BLE001 - remote errors become clean API errors
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            payload.clear_secrets()

    @app.post("/api/jobs", status_code=202)
    def start_job(payload: MigrationRequest) -> dict:
        job = jobs.submit(payload)
        return job.public()

    def require_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Migration job not found.")
        return job

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        return require_job(job_id).public()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(
        job_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        job = require_job(job_id)
        try:
            after = int(last_event_id) if last_event_id is not None else -1
        except ValueError:
            after = -1
        return StreamingResponse(
            job.stream(after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/jobs/{job_id}/artifact")
    def job_artifact(job_id: str):
        job = require_job(job_id)
        if job.artifact_path is None or not job.artifact_path.exists():
            raise HTTPException(status_code=404, detail="The artifact is not ready.")
        return FileResponse(
            job.artifact_path,
            media_type="application/zip",
            filename=f"agent-liftoff-{job_id[:8]}-results.zip",
        )

    @app.get("/api/jobs/{job_id}/documents/{document_name}")
    def job_document(job_id: str, document_name: str):
        job = require_job(job_id)
        documents = (job.summary or {}).get("documents") or []
        document = next(
            (
                item
                for item in documents
                if Path(str(item.get("path") or "")).name == document_name
            ),
            None,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Migration document not found.")
        path = job.run_dir / "output" / str(document["path"])
        expected_parent = job.run_dir / "output" / "evaluation"
        if not path.is_file() or path.parent != expected_parent:
            raise HTTPException(status_code=404, detail="Migration document not found.")
        return FileResponse(
            path,
            filename=document_name,
            media_type="text/markdown; charset=utf-8",
        )

    static_root = Path(ui_dir) if ui_dir is not None else _default_ui_dir()
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="console")

    return app

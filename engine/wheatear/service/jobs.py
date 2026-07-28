"""In-memory migration jobs and server-sent event delivery."""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from wheatear.service.models import MigrationRequest
from wheatear.service.redaction import SecretRedactor

TerminalStatus = {"completed", "failed"}


def _ibm_brand(value):
    if isinstance(value, str):
        return re.sub(
            r"(?i)\b(?:google|gemini)(?:[./_-][\w.-]+)*\b",
            "IBM watsonx",
            value,
        )
    if isinstance(value, dict):
        return {key: _ibm_brand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_ibm_brand(item) for item in value]
    return value


@dataclass
class Job:
    job_id: str
    run_dir: Path
    status: str = "queued"
    events: list[dict] = field(default_factory=list)
    summary: dict | None = None
    artifact_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    ibm_branded: bool = False
    _redactor: SecretRedactor = field(default_factory=SecretRedactor)
    _condition: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, stage: str, message: str, level: str = "info") -> None:
        with self._condition:
            rendered_message = self._redactor(message)
            if self.ibm_branded:
                rendered_message = _ibm_brand(rendered_message)
            event = {
                "id": len(self.events),
                "timestamp": time.strftime("%H:%M:%S"),
                "stage": stage,
                "level": level,
                "message": rendered_message,
            }
            self.events.append(event)
            self._condition.notify_all()

    def finish(self, status: str, summary: dict | None = None) -> None:
        with self._condition:
            self.status = status
            self.summary = _ibm_brand(summary) if self.ibm_branded else summary
            self.finished_at = time.time()
            self._condition.notify_all()
        self._redactor.clear()

    def stream(self, after: int = -1) -> Iterator[str]:
        cursor = after + 1
        while True:
            heartbeat = False
            with self._condition:
                while cursor >= len(self.events) and self.status not in TerminalStatus:
                    self._condition.wait(timeout=15)
                    if cursor >= len(self.events) and self.status not in TerminalStatus:
                        heartbeat = True
                        break
                pending = self.events[cursor:]
                terminal = self.status in TerminalStatus
            if heartbeat:
                yield ": keep-alive\n\n"
                continue
            for event in pending:
                cursor = event["id"] + 1
                yield (
                    f"id: {event['id']}\n"
                    f"event: log\n"
                    f"data: {json.dumps(event)}\n\n"
                )
            if terminal and cursor >= len(self.events):
                payload = {
                    "status": self.status,
                    "summary": self.summary,
                    "download": bool(self.artifact_path),
                }
                yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                return

    def public(self) -> dict:
        return {
            "id": self.job_id,
            "status": self.status,
            "summary": self.summary,
            "download": bool(self.artifact_path),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


Runner = Callable[[Job, MigrationRequest], dict]


class JobManager:
    """Serialize migrations because the Orchestrate ADK has one active env."""

    def __init__(self, runner: Runner, root: Path | None = None) -> None:
        self.runner = runner
        self.root = Path(root or tempfile.mkdtemp(prefix="wheatear-runs-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wheatear-job")

    def submit(self, request: MigrationRequest) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(
            job_id=job_id,
            run_dir=self.root / job_id,
            ibm_branded=request.translation.provider == "watsonx",
            _redactor=SecretRedactor(request.secret_values()),
        )
        job.run_dir.mkdir(parents=True)
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job, request)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job, request: MigrationRequest) -> None:
        job.status = "running"
        job.emit("prepare", "Migration job started.", "ok")
        try:
            summary = self.runner(job, request)
        except Exception as exc:  # noqa: BLE001 - boundary reports a redacted failure
            job.emit("error", f"{type(exc).__name__}: {exc}", "error")
            job.finish("failed", {"message": "Migration failed. Review the log above."})
        else:
            job.emit("complete", "Migration finished.", "ok")
            job.finish("completed", summary)
        finally:
            request.clear_secrets()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

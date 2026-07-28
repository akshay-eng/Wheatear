"""Ephemeral source uploads with zip-slip and zip-bomb guards."""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from fastapi import UploadFile

from wheatear.connectors.copilot_studio import pac_client
from wheatear.connectors.n8n.importer import _looks_like_n8n_workflow
from wheatear.service.models import DiscoveredItem, SourcePlatform

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
UPLOAD_TTL_SECONDS = 60 * 60


class UploadError(ValueError):
    pass


@dataclass
class UploadRecord:
    upload_id: str
    platform: SourcePlatform
    root: Path
    source_path: Path
    items: list[DiscoveredItem]
    created_at: float
    solutions: dict[str, Path] = field(default_factory=dict)


class UploadStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="wheatear-uploads-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, UploadRecord] = {}
        self._lock = threading.Lock()

    async def create(
        self, platform: SourcePlatform, files: list[UploadFile]
    ) -> UploadRecord:
        if not files:
            raise UploadError("Choose at least one source file.")
        self.cleanup()
        upload_id = uuid.uuid4().hex
        root = self.root / upload_id
        root.mkdir(parents=True)
        try:
            if platform == "copilot-studio":
                record = await self._create_copilot(upload_id, root, files)
            else:
                record = await self._create_n8n(upload_id, root, files)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        with self._lock:
            self._records[upload_id] = record
        return record

    def get(self, upload_id: str, platform: SourcePlatform | None = None) -> UploadRecord:
        self.cleanup()
        with self._lock:
            record = self._records.get(upload_id)
        if record is None or (platform is not None and record.platform != platform):
            raise UploadError("That upload is missing or has expired. Upload it again.")
        return record

    def reserve(self) -> tuple[str, Path]:
        """Reserve an ephemeral directory for a live solution scan."""
        self.cleanup()
        record_id = uuid.uuid4().hex
        root = self.root / record_id
        root.mkdir(parents=True)
        return record_id, root

    def put(self, record: UploadRecord) -> None:
        with self._lock:
            self._records[record.upload_id] = record

    def discard(self, record_id: str, root: Path) -> None:
        with self._lock:
            self._records.pop(record_id, None)
        shutil.rmtree(root, ignore_errors=True)

    def cleanup(self) -> None:
        cutoff = time.time() - UPLOAD_TTL_SECONDS
        stale: list[UploadRecord] = []
        with self._lock:
            for upload_id, record in list(self._records.items()):
                if record.created_at < cutoff:
                    stale.append(self._records.pop(upload_id))
        for record in stale:
            shutil.rmtree(record.root, ignore_errors=True)

    async def _create_copilot(
        self, upload_id: str, root: Path, files: list[UploadFile]
    ) -> UploadRecord:
        if len(files) != 1 or not (files[0].filename or "").lower().endswith(".zip"):
            raise UploadError("Copilot Studio uploads must be one unpacked-solution ZIP.")
        archive = root / "solution.zip"
        await _write_upload(files[0], archive)
        extracted = root / "solution"
        _safe_extract(archive, extracted)
        solution = _find_solution_root(extracted)
        bots = pac_client.list_bots_in_solution(solution)
        if not bots:
            raise UploadError("The solution contains no Copilot Studio agents.")
        items = [
            DiscoveredItem(
                id=schema,
                name=display_name,
                description=f"Schema: {schema}",
                kind="agent",
                source_id=schema,
                group_id="upload",
                group_name="Uploaded solution",
            )
            for schema, display_name in bots
        ]
        return UploadRecord(
            upload_id,
            "copilot-studio",
            root,
            solution,
            items,
            time.time(),
            solutions={"upload": solution},
        )

    async def _create_n8n(
        self, upload_id: str, root: Path, files: list[UploadFile]
    ) -> UploadRecord:
        source = root / "workflows"
        source.mkdir()
        items: list[DiscoveredItem] = []
        used_names: set[str] = set()
        for index, upload in enumerate(files):
            if not (upload.filename or "").lower().endswith(".json"):
                raise UploadError("n8n uploads must be workflow JSON files.")
            raw = await _read_upload(upload)
            try:
                data = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UploadError(f"{upload.filename} is not valid JSON.") from exc
            if not _looks_like_n8n_workflow(data):
                raise UploadError(f"{upload.filename} is not an n8n workflow export.")
            workflow_id = str(data.get("id") or f"upload-{index + 1}")
            data["id"] = workflow_id
            filename = _unique_json_name(upload.filename or workflow_id, used_names)
            (source / filename).write_text(json.dumps(data))
            items.append(
                DiscoveredItem(
                    id=workflow_id,
                    name=str(data.get("name") or filename.removesuffix(".json")),
                    description="Active" if data.get("active") else "Inactive",
                    active=bool(data.get("active", False)),
                    kind="workflow",
                    source_id=workflow_id,
                )
            )
        return UploadRecord(upload_id, "n8n", root, source, items, time.time())


async def _read_upload(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise UploadError("The upload exceeds the 100 MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


async def _write_upload(upload: UploadFile, destination: Path) -> None:
    destination.write_bytes(await _read_upload(upload))


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise UploadError("The ZIP contains too many files.")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise UploadError("The expanded ZIP exceeds the 500 MB limit.")
        for info in infos:
            relative = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or stat.S_ISLNK(mode)
            ):
                raise UploadError("The ZIP contains an unsafe path.")
        zipped.extractall(destination)


def _find_solution_root(extracted: Path) -> Path:
    candidates = [
        path.parent
        for path in extracted.rglob("solution.xml")
        if (path.parent / "bots").is_dir()
    ]
    if not candidates:
        raise UploadError(
            "The ZIP is not an unpacked Copilot Studio solution "
            "(expected solution.xml and bots/)."
        )
    return min(candidates, key=lambda path: len(path.parts))


def _unique_json_name(filename: str, used: set[str]) -> str:
    clean = Path(filename).name
    stem = Path(clean).stem or "workflow"
    candidate = f"{stem}.json"
    index = 2
    while candidate in used:
        candidate = f"{stem}-{index}.json"
        index += 1
    used.add(candidate)
    return candidate

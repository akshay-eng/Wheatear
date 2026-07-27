"""The foundry's memory: probed corpora and compiled adapters on disk.

This is what makes the second migration cheap. A corridor's mapping is
inferred once, compiled, tested, and written here; every later migration of
that corridor looks here first and, on a hit, never calls a model at all.

Two things are stored, under `~/.local/share/wheatear/foundry` by default:

  `corpora/`   what a platform's records looked like when we last probed it,
               keyed by platform and by the fingerprint of the shape itself.
               Re-probing a tenant whose schema hasn't moved rewrites the same
               file, so the directory tracks platform versions, not customers.

  `adapters/`  compiled adapters, keyed by platform, direction, entity kind and
               the schema fingerprint they were compiled against. The
               fingerprint being *in the key* is the whole safety property:
               when a platform changes shape the lookup misses rather than
               silently running code compiled for the old shape.

Lookups return one of three answers, and the distinction matters:

  hit    an adapter compiled for exactly this shape, with passing tests.
         Use it; make no model call.
  stale  an adapter for this corridor exists, but for a different schema
         fingerprint or a different IR version. Do *not* run it silently --
         the schema moved. Worth showing a human, worth using as the starting
         point for a rebuild, not worth trusting.
  miss   nothing here. Build it.

Every artifact is written as authoritative JSON plus readable `.py` sidecars,
because the generated code is meant to be reviewed and diffed by a person, and
nobody reviews code embedded in a JSON string.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from wheatear.foundry.types import (
    AdapterArtifact,
    AdapterKey,
    Direction,
    EntityKind,
    SchemaCorpus,
)

# Overriding this is how tests get an isolated store, and how a CI run keeps a
# shared cache without writing into a home directory it may not own.
HOME_ENV = "WHEATEAR_FOUNDRY_HOME"

ADAPTER_FILE = "artifact.json"
CODE_FILE = "adapter.py"
TESTS_FILE = "test_adapter.py"
SPEC_FILE = "spec.json"

LookupStatus = Literal["hit", "stale", "miss"]


def default_root() -> Path:
    """Where the foundry keeps its memory.

    XDG data dir rather than the config dir under `~/.config/wheatear`, which
    holds settings a user edits. This is generated content: it can be deleted
    at any time and the only cost is recompiling.
    """
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "wheatear" / "foundry"


@dataclass(frozen=True)
class AdapterLookup:
    """The answer to "do we already have this?"."""

    status: LookupStatus
    artifact: AdapterArtifact | None = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "hit" and self.artifact is not None


@dataclass(frozen=True)
class CorpusRecord:
    """A stored corpus, described without loading the whole thing."""

    platform: str
    fingerprint: str
    captured_at: datetime
    entity_kinds: tuple[str, ...]
    path: Path

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(timezone.utc)) - self.captured_at


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written adapter that still parses is the worst possible failure
    here: it would be loaded, executed over ten thousand records, and produce
    plausible garbage. Renaming into place makes that impossible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class FoundryStore:
    """Filesystem-backed store for corpora and compiled adapters."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    @property
    def corpora_dir(self) -> Path:
        return self.root / "corpora"

    @property
    def adapters_dir(self) -> Path:
        return self.root / "adapters"

    def corpus_path(self, platform: str, fingerprint: str) -> Path:
        return self.corpora_dir / _safe(platform) / f"{fingerprint[:16]}.json"

    def adapter_dir(self, key: AdapterKey) -> Path:
        return self.adapters_dir / Path(key.slug())

    def family_dir(self, platform: str, direction: Direction, entity_kind: EntityKind) -> Path:
        return self.adapters_dir / _safe(platform) / direction.value / entity_kind.value

    # ------------------------------------------------------------------
    # Corpora
    # ------------------------------------------------------------------

    def put_corpus(self, corpus: SchemaCorpus) -> Path:
        """Store a probed corpus, keyed by the shape it found.

        Re-probing a platform whose schema has not moved overwrites the same
        file rather than accumulating one per run: the interesting axis is
        schema versions, and keeping a copy per probe would turn the store
        into a log of tenants.
        """
        path = self.corpus_path(corpus.platform, corpus.fingerprint())
        _write_atomic(path, corpus.model_dump_json(indent=2))
        return path

    def get_corpus(self, platform: str, fingerprint: str) -> SchemaCorpus | None:
        return _read_model(self.corpus_path(platform, fingerprint), SchemaCorpus)

    def list_corpora(self, platform: str | None = None) -> list[CorpusRecord]:
        """Every stored corpus, newest capture first."""
        roots = (
            [self.corpora_dir / _safe(platform)]
            if platform
            else sorted(p for p in _children(self.corpora_dir) if p.is_dir())
        )
        records: list[CorpusRecord] = []
        for directory in roots:
            for path in sorted(directory.glob("*.json")):
                corpus = _read_model(path, SchemaCorpus)
                if corpus is None:
                    continue
                records.append(
                    CorpusRecord(
                        platform=corpus.platform,
                        fingerprint=corpus.fingerprint(),
                        captured_at=corpus.captured_at,
                        entity_kinds=tuple(k.value for k in corpus.kinds()),
                        path=path,
                    )
                )
        records.sort(key=lambda r: r.captured_at, reverse=True)
        return records

    def latest_corpus(
        self, platform: str, max_age: timedelta | None = None
    ) -> SchemaCorpus | None:
        """The most recent probe of a platform, optionally only if it's fresh.

        `max_age` is what "check if there is a recent pull" means in practice.
        Leaving it None accepts any age, which is usually right: a schema does
        not go stale on a clock, it goes stale when the vendor ships. The age
        check is for callers who would rather re-probe than find out the hard
        way.
        """
        for record in self.list_corpora(platform):
            if max_age is not None and record.age() > max_age:
                return None
            return _read_model(record.path, SchemaCorpus)
        return None

    # ------------------------------------------------------------------
    # Adapters
    # ------------------------------------------------------------------

    def put(self, artifact: AdapterArtifact) -> Path:
        """Store a compiled adapter and its sidecars."""
        directory = self.adapter_dir(artifact.key)
        _write_atomic(directory / ADAPTER_FILE, artifact.model_dump_json(indent=2))
        # Sidecars: the same content, in the form a person can actually read.
        # Regenerated from the artifact on every write, so they can never drift
        # from it -- the JSON stays authoritative.
        _write_atomic(directory / CODE_FILE, artifact.code)
        _write_atomic(directory / TESTS_FILE, artifact.tests)
        _write_atomic(directory / SPEC_FILE, artifact.spec.model_dump_json(indent=2))
        return directory

    def get(self, key: AdapterKey) -> AdapterArtifact | None:
        return _read_model(self.adapter_dir(key) / ADAPTER_FILE, AdapterArtifact)

    def find(
        self,
        platform: str,
        direction: Direction,
        entity_kind: EntityKind,
        schema_fingerprint: str,
        ir_version: str | None = None,
    ) -> AdapterLookup:
        """Look up an adapter for exactly this schema, and say why if not.

        The stale answer is the one that earns this method's existence. An
        adapter for the right corridor compiled against a different schema is
        not a hit -- running it would apply last quarter's field mapping to
        this quarter's records -- but it is also not nothing, because it is the
        best possible starting point for the rebuild.
        """
        key = AdapterKey(
            platform=platform,
            direction=direction,
            entity_kind=entity_kind,
            schema_fingerprint=schema_fingerprint,
            **({"ir_version": ir_version} if ir_version else {}),
        )
        exact = self.get(key)
        if exact is not None:
            # The path is keyed by the platform's schema, not by the IR
            # version, so an adapter compiled against an older IR lands in this
            # same directory. Its own recorded version is what settles it --
            # the mapping targets fields that may no longer exist.
            if exact.key.ir_version != key.ir_version:
                return AdapterLookup(
                    "stale",
                    exact,
                    f"Compiled against IR {exact.key.ir_version}; this run uses "
                    f"{key.ir_version}.",
                )
            if not exact.verified:
                return AdapterLookup(
                    "stale",
                    exact,
                    "A compiled adapter exists for this schema but its tests did not pass.",
                )
            return AdapterLookup("hit", exact, "Compiled adapter matches this schema exactly.")

        others = self.list_adapters(platform, direction, entity_kind)
        if others:
            newest = max(others, key=lambda a: a.created_at)
            if newest.key.ir_version != key.ir_version:
                why = (
                    f"compiled against IR {newest.key.ir_version}, this run uses "
                    f"{key.ir_version}"
                )
            else:
                why = (
                    f"compiled against schema {newest.key.schema_fingerprint[:12]}, "
                    f"this probe found {schema_fingerprint[:12]}"
                )
            return AdapterLookup("stale", newest, f"The platform's schema moved: {why}.")

        return AdapterLookup("miss", None, "No adapter has been compiled for this corridor yet.")

    def list_adapters(
        self,
        platform: str | None = None,
        direction: Direction | None = None,
        entity_kind: EntityKind | None = None,
    ) -> list[AdapterArtifact]:
        """Every stored adapter matching the filters, newest first."""
        found: list[AdapterArtifact] = []
        for path in sorted(self.adapters_dir.rglob(ADAPTER_FILE)):
            artifact = _read_model(path, AdapterArtifact)
            if artifact is None:
                continue
            key = artifact.key
            if platform and key.platform != platform:
                continue
            if direction and key.direction != direction:
                continue
            if entity_kind and key.entity_kind != entity_kind:
                continue
            found.append(artifact)
        found.sort(key=lambda a: a.created_at, reverse=True)
        return found

    def forget(self, key: AdapterKey) -> bool:
        """Delete one compiled adapter. Returns whether anything was there."""
        directory = self.adapter_dir(key)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True


def _safe(name: str) -> str:
    """Make a platform key safe as a single path segment.

    Platform keys are ours (`copilot-studio`, `orchestrate`) so this is belt
    and braces, but a store path is assembled from them and a key with a slash
    in it would write outside the store.
    """
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return cleaned.strip("._") or "unknown"


def _children(directory: Path) -> list[Path]:
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def _read_model(path: Path, model):
    """Load a stored model, treating corruption as absence.

    A cache entry that will not parse is a cache miss. Raising here would mean
    one bad file blocks every migration until somebody finds and deletes it,
    which is a much worse failure than recompiling.
    """
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

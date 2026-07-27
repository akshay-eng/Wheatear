"""Take a locally-built corridor and make it publishable.

A developer's foundry store and a shipped one are not the same artifact, and
the difference is not a detail. Locally, a corpus keeps the sample records it
was probed from -- the translator needs real records to infer a mapping, and
the engineer needs them to generate tests. Those samples are agent names,
instructions, endpoints and tenant GUIDs.

    samples  : [{'botcomponent': {'@schemaname': 'acme_HRAgent0000a.gpt.default'}}]
    examples : ['da97b371-f728-f011-8c4d-6045bdadad2d']
    enum     : ['da97b371-f728-f011-8c4d-6045bdadad2d']

None of that can be published. What *can* is the thing every user needs and
none of them should have to build: the compiled adapter and the field layout it
was compiled against. Those belong to Microsoft and IBM, not to whoever ran the
probe.

So stripping happens here, on the way out, rather than when the corpus is
stored. Doing it at store time would be simpler and would quietly ruin every
later rebuild, because the translator would have nothing to read.

What is dropped, and why each one:

  samples      whole records, verbatim from a tenant
  entity name  whichever record the probe saw first, e.g. `acme_Candidateagent`
  examples     observed values per field; where the tenant GUIDs were
  enum         "closed set" inferred from one tenant's data. A field with one
               observed value is not an enum, it is a coincidence, and shipping
               it as one would teach the next build that a customer's id is a
               constant of the platform
  tests        generated test bodies, which embed sample records as fixtures
  gaps/notes   free text from a probe; may name a tenant or an endpoint

What survives is field paths, types and required-ness -- the shape -- plus the
adapter's code, spec and verification report.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from wheatear.foundry.store import ADAPTER_FILE, CODE_FILE, SPEC_FILE, FoundryStore
from wheatear.foundry.types import AdapterArtifact, SchemaCorpus


@dataclass
class ShipReport:
    """What was published, and what was withheld."""

    adapters: list[str] = field(default_factory=list)
    corpora: list[str] = field(default_factory=list)
    stripped_samples: int = 0
    stripped_examples: int = 0
    stripped_enums: int = 0
    skipped_unverified: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = f"{len(self.adapters)} adapter(s), {len(self.corpora)} corpus/corpora"
        line += (
            f"; stripped {self.stripped_samples} sample set(s), "
            f"{self.stripped_examples} example set(s), {self.stripped_enums} enum(s)"
        )
        if self.skipped_unverified:
            line += f"; skipped {len(self.skipped_unverified)} unverified"
        return line


# A path segment that is a bare id: `agent_mapping.02a79f6b-ef7e-...`. The probe
# walked a dict keyed by agent id and recorded one "field" per key, which is
# both a leak and wrong about the shape -- those are map entries, not fields.
_ID_SEGMENT = re.compile(
    r"(?<=\.)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _collapse_ids(path: str) -> str:
    """`agent_mapping.<guid>` -> `agent_mapping.*`, which is what it means."""
    return _ID_SEGMENT.sub("*", path)


def strip_corpus(corpus: SchemaCorpus) -> tuple[SchemaCorpus, tuple[int, int, int]]:
    """A corpus with the shape kept and the data removed.

    Returns the stripped copy and how much was taken out, so a caller can
    report it rather than asserting that stripping happened.
    """
    clean = corpus.model_copy(deep=True)
    samples = examples = enums = 0

    for entity in clean.entities:
        if entity.samples:
            samples += 1
            entity.samples = []
        # The entity name is whichever record the probe happened to see first
        # -- `acme_Candidateagent`, a real agent's schema name. It is an
        # example, not the shape, and the kind already says what the entity is.
        entity.name = entity.kind.value
        kept = []
        seen_paths: set[str] = set()
        for f in entity.fields:
            if getattr(f, "examples", None):
                examples += 1
                f.examples = []
            if getattr(f, "enum", None):
                enums += 1
                f.enum = []
            collapsed = _collapse_ids(f.path)
            if collapsed != f.path:
                # One entry per tenant id becomes one entry for the map.
                f.path = collapsed
                if collapsed in seen_paths:
                    continue
            seen_paths.add(collapsed)
            kept.append(f)
        entity.fields = kept

    # Probe prose can name a tenant, an endpoint or an agent. The structured
    # gap reasons are kept; their free text is not.
    clean.notes = []
    for gap in clean.gaps:
        # Emptied, not None: `detail` is a required string, and a stripped
        # corpus that cannot be re-validated is a corpus that cannot be
        # shipped. The structured `reason` is what a reader needs anyway.
        gap.detail = ""
    return clean, (samples, examples, enums)


def strip_artifact(artifact: AdapterArtifact) -> AdapterArtifact:
    """An adapter with its generated tests removed.

    The tests are the only part that embeds records: they were generated from
    the corpus's samples and assert on them by value. The runtime never reads
    them -- `runtime.load` compiles `code` and checks `report.verified` -- so
    dropping them costs nothing at migration time and removes the whole
    customer-data surface.
    """
    clean = artifact.model_copy(deep=True)
    clean.tests = ""
    return clean


def ship(
    store: FoundryStore, destination: Path, platforms: list[str] | None = None
) -> ShipReport:
    """Copy a built corridor into the shippable assets tree.

    Only verified adapters travel. An unverified one is worth reading and
    worth finishing by hand, and is not worth publishing to people who will
    run it unattended against their own tenant.
    """
    destination = Path(destination)
    report = ShipReport()

    names = platforms or sorted(
        {record.platform for record in store.list_corpora()}
    )

    for platform in names:
        corpus = store.latest_corpus(platform)
        if corpus is None:
            continue
        clean, (samples, examples, enums) = strip_corpus(corpus)
        report.stripped_samples += samples
        report.stripped_examples += examples
        report.stripped_enums += enums

        corpus_dir = destination / platform / "corpora"
        corpus_dir.mkdir(parents=True, exist_ok=True)
        path = corpus_dir / f"{clean.fingerprint()[:16]}.json"
        path.write_text(clean.model_dump_json(indent=2))
        report.corpora.append(f"{platform}/{path.name}")

        for artifact_path in sorted((store.adapters_dir / platform).rglob(ADAPTER_FILE)):
            artifact = AdapterArtifact.model_validate_json(artifact_path.read_text())
            family = artifact.key.family()
            if not artifact.verified:
                report.skipped_unverified.append(family)
                continue
            clean_artifact = strip_artifact(artifact)
            out = destination / Path(artifact.key.slug())
            out.mkdir(parents=True, exist_ok=True)
            (out / ADAPTER_FILE).write_text(clean_artifact.model_dump_json(indent=2))
            (out / CODE_FILE).write_text(clean_artifact.code)
            (out / SPEC_FILE).write_text(clean_artifact.spec.model_dump_json(indent=2))
            report.adapters.append(family)

    return report


def install(source: Path, store: FoundryStore) -> int:
    """Load shipped assets into a local store, so a migration can use them.

    The inverse of `ship`, and the reason a user on the same platform versions
    never has to probe or build: the fingerprint is derived from those versions,
    so a shipped adapter is found by exactly the lookup a locally-built one
    would have been.
    """
    source = Path(source)
    if not source.is_dir():
        return 0

    installed = 0
    for platform_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        for corpus_file in sorted((platform_dir / "corpora").glob("*.json")):
            corpus = SchemaCorpus.model_validate_json(corpus_file.read_text())
            store.put_corpus(corpus)
        for artifact_path in sorted(platform_dir.rglob(ADAPTER_FILE)):
            artifact = AdapterArtifact.model_validate_json(artifact_path.read_text())
            store.put(artifact)
            installed += 1
    return installed


def copy_tree(source: Path, destination: Path) -> None:
    """Replace `destination` with `source`, used when refreshing the assets."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)

"""Write everything the foundry learned to a directory you can open.

The store under `AGENT_LIFTOFF_FOUNDRY_HOME` is the machine's copy: JSON keyed by
fingerprint, fine for a cache and awkward to read. This renders the same
content as files a person can actually work with, and adds the one thing the
store does not hold -- the source records actually converted to IR.

    <out>/<platform>/fields.txt        every field, with type and writability
    <out>/<platform>/create-model.txt  only what a CREATE accepts, required first
    <out>/<platform>/corpus.json       the machine-readable corpus
    <out>/<platform>/samples/          redacted sample records per entity kind
    <out>/mappings.md                  every mapping, in a table you can read
    <out>/ir/                          source records converted to Agent Liftoff IR
    <out>/adapters/                    generated code, tests and mapping specs
    <out>/SUMMARY.md                   what was found, what mapped, what did not

Everything written here has already been through `foundry.redact`, so the
credential-shaped values are gone. It is still real tenant content: agent
instructions, tool descriptions, knowledge base names. Treat the directory as
customer data.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from agent_liftoff.foundry import runtime  # noqa: E402
from agent_liftoff.foundry.store import FoundryStore  # noqa: E402
from agent_liftoff.foundry.types import Direction, SchemaCorpus  # noqa: E402

OUT = Path(os.environ.get("FOUNDRY_OUT", "foundry-output"))
PLATFORMS = ("copilot-studio", "orchestrate")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def dump_json(path: Path, payload) -> Path:
    return write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def field_table(corpus: SchemaCorpus) -> str:
    """A flat, greppable inventory: one line per field, per entity kind."""
    lines = [
        f"# {corpus.platform} -- probed field inventory",
        f"# fingerprint {corpus.fingerprint()}",
        f"# captured    {corpus.captured_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
    ]
    for entity in corpus.entities:
        leaves = entity.leaves()
        lines.append(
            f"## {entity.kind.value}  ({entity.name}, {entity.sample_count} record(s), "
            f"{len(leaves)} field(s), source: {entity.origin.value})"
        )
        lines.append(
            f"{'path':<58} {'types':<20} {'create':<10} {'seen':>6}  enum / example"
        )
        lines.append("-" * 130)
        for node in leaves:
            types = "|".join(node.types)[:18]
            seen = f"{node.occurrence:.0%}"
            writability = {True: "yes", False: "REJECTED", None: "-"}[node.writable]
            if node.writable and node.required:
                writability = "REQUIRED"
            tail = ""
            if node.enum:
                tail = "enum: " + ", ".join(node.enum[:6])
            elif node.description:
                tail = " ".join(node.description.split())[:70]
            elif node.examples:
                tail = "e.g. " + node.examples[0][:60].replace("\n", " ")
            lines.append(f"{node.path:<58} {types:<20} {writability:<10} {seen:>6}  {tail}")
        lines.append("")
    if corpus.gaps:
        lines.append("## gaps -- what the probe could not see")
        for gap in corpus.gaps:
            lines.append(f"- [{gap.reason.value}] {gap.what}")
            lines.append(f"    {' '.join(gap.detail.split())}")
            if gap.remedy:
                lines.append(f"    fix: {' '.join(gap.remedy.split())}")
    return "\n".join(lines) + "\n"


def create_model(corpus: SchemaCorpus) -> str:
    """Only the fields a create accepts -- the form you would fill in by hand.

    This is the document to check a mapping against. If a field is here and no
    mapping produces it, the migration will not reproduce it; if a mapping
    produces something that is *not* here, the platform will reject it.
    """
    lines = [
        f"# {corpus.platform} -- what a CREATE accepts",
        "#",
        "# Declared by the platform itself, not inferred from records:",
        "#   copilot-studio  Dataverse EntityDefinitions(...)/Attributes",
        "#   orchestrate     the installed ADK's AgentSpec / ToolSpec",
        "#",
        "# A field absent here is either read-only or undeclared -- see fields.txt.",
        "",
    ]
    for entity in corpus.entities:
        creatable = [f for f in entity.leaves() if f.writable]
        if not creatable:
            lines.append(f"## {entity.kind.value}: no create model was found for this entity.\n")
            continue
        required = sorted((f for f in creatable if f.required), key=lambda f: f.path)
        optional = sorted((f for f in creatable if not f.required), key=lambda f: f.path)
        lines.append(
            f"## {entity.kind.value}  ({len(creatable)} accepted on create, "
            f"{len(required)} required)"
        )
        for label, group in (("REQUIRED", required), ("optional", optional)):
            for node in group:
                described = " ".join((node.description or "").split())[:78]
                lines.append(f"  {label:<9} {node.path:<44} {'|'.join(node.types):<20} {described}")
                if node.enum:
                    lines.append(f"  {'':<9} {'':<44} values: {', '.join(node.enum[:10])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def current_adapters(store: FoundryStore, corpora: dict) -> list:
    """Only adapters compiled against the corpus we just probed.

    The store keeps every generation, keyed by schema fingerprint -- that is
    the point, it is how a rebuild is detected. But a review document listing
    three vintages of the same adapter is worse than useless: the reader cannot
    tell which one would actually run.
    """
    current = []
    for artifact in store.list_adapters():
        corpus = corpora.get(artifact.key.platform)
        if corpus is None:
            continue
        if artifact.key.schema_fingerprint == corpus.entity_fingerprint(artifact.key.entity_kind):
            current.append(artifact)
    current.sort(key=lambda a: a.key.family())
    return current


def mapping_table(store: FoundryStore, adapters: list) -> str:
    """Every adapter's mapping, as a table meant to be read and argued with."""
    lines = [
        "# Field mappings",
        "",
        "One section per adapter. `import` maps a platform record onto Agent Liftoff's IR;",
        "`export` maps the IR onto a platform record. Confidence 0.90 means the",
        "deterministic aligner settled it (leaf names agreed, types compatible, no",
        "competing candidate); anything else came from the model and carries its",
        "reasoning.",
        "",
    ]
    for artifact in adapters:
        spec = artifact.spec
        status = "verified" if artifact.verified else "NOT VERIFIED"
        lines.append(f"## {artifact.key.family()}")
        lines.append("")
        lines.append(
            f"`{status}` — {artifact.report.summary()} — {len(spec.mappings)} mapping(s), "
            f"{len(spec.flags)} flag(s), schema `{artifact.key.schema_fingerprint[:12]}`"
        )
        lines.append("")
        if spec.mappings:
            lines.append("| target | <- source | transform | conf | why |")
            lines.append("|---|---|---|---|---|")
            for m in spec.mappings:
                src = ", ".join(f"`{p}`" for p in m.source_paths) or f"const `{m.constant!r}`"
                why = " ".join((m.rationale or "").split())[:110].replace("|", "/")
                lines.append(
                    f"| `{m.target_path}` | {src} | {m.transform.value} | "
                    f"{m.confidence:.2f} | {why} |"
                )
            lines.append("")
        blocking = spec.blocking_flags()
        if blocking:
            lines.append(f"**{len(blocking)} blocking** — required target with no source:")
            for flag in blocking[:15]:
                lines.append(f"- `{flag.path}`")
            lines.append("")
        lossy = [f for f in spec.flags if f.severity == "warn"]
        if lossy:
            lines.append(f"<details><summary>{len(lossy)} warning flag(s)</summary>")
            lines.append("")
            for flag in lossy[:60]:
                lines.append(f"- `{flag.path}` — {flag.reason.value}: "
                             f"{' '.join(flag.detail.split())[:120]}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        if spec.notes:
            lines.append("<details><summary>build notes</summary>")
            lines.append("")
            for note in spec.notes:
                lines.append(f"- {' '.join(note.split())}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    return "\n".join(lines) + "\n"


def export_corpus(store: FoundryStore, platform: str) -> tuple[SchemaCorpus | None, list[str]]:
    corpus = store.latest_corpus(platform)
    if corpus is None:
        return None, [f"{platform}: nothing probed"]

    base = OUT / platform
    dump_json(base / "corpus.json", corpus.model_dump(mode="json"))
    write(base / "fields.txt", field_table(corpus))
    write(base / "create-model.txt", create_model(corpus))
    for entity in corpus.entities:
        dump_json(base / "samples" / f"{entity.kind.value}.json", entity.samples)

    creatable = sum(1 for e in corpus.entities for f in e.leaves() if f.writable)
    notes = [
        f"{platform}: {len(corpus.entities)} entity kind(s), "
        f"{sum(len(e.leaves()) for e in corpus.entities)} field(s) observed, "
        f"{creatable} accepted on create, fingerprint {corpus.fingerprint()[:16]}"
    ]
    return corpus, notes


def export_ir(store: FoundryStore, corpus: SchemaCorpus) -> list[str]:
    """Run each import adapter over the records it was built from."""
    notes: list[str] = []
    for entity in corpus.entities:
        fingerprint = corpus.entity_fingerprint(entity.kind)
        found = [
            a
            for a in store.list_adapters(corpus.platform, Direction.IMPORT, entity.kind)
            if a.key.schema_fingerprint == fingerprint
        ]
        if not found:
            notes.append(f"{corpus.platform}/{entity.kind.value}: no import adapter")
            continue
        artifact = found[0]
        try:
            adapter = runtime.load(artifact, verified_only=False)
        except ValueError as exc:
            notes.append(f"{corpus.platform}/{entity.kind.value}: adapter unusable -- {exc}")
            continue

        converted, report = runtime.convert_all(adapter, entity.samples)
        validated = []
        invalid = 0
        for index, record in enumerate(converted):
            result = runtime.to_ir(entity.kind, record)
            validated.append(
                {
                    "index": index,
                    "valid_ir": result.ok,
                    "errors": result.errors,
                    "ir": result.model.model_dump(mode="json") if result.ok else record,
                }
            )
            invalid += 0 if result.ok else 1

        stem = f"{corpus.platform}.{entity.kind.value}"
        dump_json(OUT / "ir" / f"{stem}.json", validated)
        dump_json(
            OUT / "ir" / f"{stem}.report.json",
            {
                "adapter": artifact.key.slug(),
                "verified": artifact.verified,
                "tests": artifact.report.summary(),
                "records": report.total,
                "converted": report.converted,
                "failed": report.failed,
                "flagged": report.flagged,
                "flag_counts": report.flag_counts,
                "valid_ir": report.converted - invalid,
                "mappings": [
                    {
                        "target": m.target_path,
                        "sources": m.source_paths,
                        "transform": m.transform.value,
                        "confidence": m.confidence,
                        "rationale": m.rationale,
                    }
                    for m in artifact.spec.mappings
                ],
                "flags": [
                    {"path": f.path, "reason": f.reason.value, "severity": f.severity,
                     "detail": f.detail}
                    for f in artifact.spec.flags
                ],
                "notes": artifact.spec.notes,
            },
        )
        notes.append(
            f"{stem}: {report.converted}/{report.total} converted, "
            f"{report.converted - invalid} valid IR, {len(artifact.spec.mappings)} mapping(s)"
        )
    return notes


def export_adapters(store: FoundryStore, adapters: list) -> list[str]:
    notes = []
    for artifact in adapters:
        base = OUT / "adapters" / Path(artifact.key.family())
        write(base / "adapter.py", artifact.code)
        write(base / "test_adapter.py", artifact.tests)
        dump_json(base / "spec.json", artifact.spec.model_dump(mode="json"))
        dump_json(base / "report.json", artifact.report.model_dump(mode="json"))
        notes.append(
            f"{artifact.key.family()}: {'verified' if artifact.verified else 'NOT VERIFIED'}, "
            f"{artifact.report.summary()}, {len(artifact.spec.mappings)} mapping(s), "
            f"{len(artifact.spec.blocking_flags())} blocking flag(s)"
        )
    return notes


def main() -> None:
    store = FoundryStore(Path(os.environ["AGENT_LIFTOFF_FOUNDRY_HOME"]))
    sections: dict[str, list[str]] = {}

    corpora = {}
    probed: list[str] = []
    for platform in PLATFORMS:
        corpus, notes = export_corpus(store, platform)
        probed += notes
        if corpus is not None:
            corpora[platform] = corpus
    sections["Probed"] = probed

    adapters = current_adapters(store, corpora)
    stale = len(store.list_adapters()) - len(adapters)
    sections["Adapters"] = export_adapters(store, adapters)
    if stale:
        sections["Adapters"].append(
            f"({stale} adapter(s) from earlier schema versions are in the store but excluded "
            "here -- they would not be used.)"
        )
    write(OUT / "mappings.md", mapping_table(store, adapters))

    ir_notes: list[str] = []
    for corpus in corpora.values():
        ir_notes += export_ir(store, corpus)
    sections["Mapped to IR"] = ir_notes

    body = ["# Agent Liftoff foundry -- probe and mapping output", ""]
    body.append("Generated by `scripts/foundry_export.py`. Credentials are redacted;")
    body.append("everything else is real tenant content. Do not commit.")
    body.append("")
    for title, lines in sections.items():
        body.append(f"## {title}")
        body += [f"- {line}" for line in lines] or ["- (nothing)"]
        body.append("")
    write(OUT / "SUMMARY.md", "\n".join(body))

    for title, lines in sections.items():
        print(f"\n{title}")
        for line in lines:
            print(f"  {line}")
    print(f"\nwrote {OUT.resolve()}")


if __name__ == "__main__":
    main()

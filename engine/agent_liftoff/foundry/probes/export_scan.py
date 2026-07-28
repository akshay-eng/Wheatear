"""Pass 1: read a platform's shape out of an export archive.

This is the cheap, offline, complete-about-structure half of the inspector.
An export is the vendor's own serialisation of their model, so it is
authoritative about what fields exist, what nests inside what, and which
values are drawn from a closed set -- and it costs no credentials, no session
and no rate limit to read.

What it cannot tell you is anything the vendor strips on export: connector
endpoints, connection targets, secrets, and in some cases the tool registry
itself. Those are pass 2's job (`orchestrate.py`, `copilot_studio.py`), and
what neither pass reaches is recorded as a `ProbeGap` rather than guessed at.

The scan is format-driven rather than platform-driven. It parses JSON, YAML
and XML, classifies each file by where it sits in the archive, and infers a
shape per entity kind. That means a platform Agent Liftoff has never seen still
produces a usable corpus from its export -- degraded, because the
classification rules won't know its conventions, but not empty.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from agent_liftoff.foundry.probes.base import ProbeContext, ProbeResult, observe
from agent_liftoff.foundry.types import EntityKind, GapReason, ProbeGap, ProbeOrigin

# Extensions worth opening. Everything else in an export -- images, compiled
# assets, binary attachments -- is content, not structure.
PARSEABLE = {".json", ".yaml", ".yml", ".xml"}

# Files with no extension that are known payloads. Copilot Studio writes a
# component's actual definition to a file literally named `data`, with the
# metadata beside it in XML, so an extension-only rule would miss the half of
# the export that matters most.
EXTENSIONLESS = {"data"}

# Files that are deliberately not entities, so they never reach the
# unclassified count. Without this the gap reads "35 unclassified files" on an
# archive whose entities were all found -- a number that is alarming, useless,
# and drowns the handful of files that genuinely need a rule.
#
# Two kinds: packaging the vendor wraps a solution in, and Agent Liftoff's own
# output when a previous migration wrote into the same tree.
IGNORED: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[content_types\]\.xml$"),  # OPC packaging
    re.compile(r"(^|/)_rels/"),  # OPC relationships
    # Dataverse solution packaging: a manifest of what the archive contains and
    # a duplicate flattening of it. Every entity they name is also present as
    # its own file, so classifying them would double-count the whole solution.
    re.compile(r"(^|/)solution\.xml$"),
    re.compile(r"(^|/)customizations\.xml$"),
    re.compile(r"(^|/)review-manifest\.ya?ml$"),  # Agent Liftoff's own review output
    re.compile(r"\.orchestrate\.ya?ml$"),  # Agent Liftoff's own converted agents
    # Hidden dot-directories are tooling/OS/VCS state, never Copilot content: a
    # real export sitting in a working tree routinely carries .git/, .vscode/,
    # .idea/, or a tool's cache (e.g. .impeccable/). Ignore the lot rather than
    # flagging their JSON/state files as unclassified migratable content.
    re.compile(r"(^|/)\.[^/]+/"),
    re.compile(r"(^|/)\.ds_store$"),  # macOS Finder metadata (path is lowercased)
)

# Ordered classification rules, first match wins. Matched against the archive
# -relative POSIX path, lowercased. Ordering matters: the Copilot component
# rules are more specific than the directory rules that follow them.
RULES: tuple[tuple[re.Pattern[str], EntityKind], ...] = (
    # Copilot Studio solution export: the component's kind is a token in its
    # directory name, e.g. `ai_Bot.topic.PasswordReset`.
    (re.compile(r"botcomponents/[^/]*\.topic\."), EntityKind.TOPIC),
    (re.compile(r"botcomponents/[^/]*\.(knowledge|file)\."), EntityKind.KNOWLEDGE),
    (re.compile(r"botcomponents/[^/]*\.gpt\."), EntityKind.AGENT),
    # A connected-agent action is how Copilot Studio serialises "this agent may
    # hand work to that one". It is a component, not a bot, and it is the only
    # place the delegation graph appears -- classified as an agent so the
    # collaborator survives the trip.
    (re.compile(r"botcomponents/[^/]*\.invokeconnectedagent[^/]*\."), EntityKind.AGENT),
    (re.compile(r"botcomponents/[^/]*\.(tool|action|skill)\."), EntityKind.TOOL),
    (re.compile(r"botcomponents/[^/]*\.(trigger|event)\."), EntityKind.TRIGGER),
    (re.compile(r"botcomponents/[^/]*\.(flow|workflow)\."), EntityKind.WORKFLOW),
    # Copilot Studio `pac copilot clone` workspace.
    (re.compile(r"(^|/)topics/[^/]+\.mcs\.ya?ml$"), EntityKind.TOPIC),
    (re.compile(r"(^|/)[^/]+\.mcs\.ya?ml$"), EntityKind.AGENT),
    # Dataverse solution manifests.
    (re.compile(r"connectionreference"), EntityKind.CONNECTION),
    (re.compile(r"(^|/)bots/[^/]+/"), EntityKind.AGENT),
    # watsonx Orchestrate ADK exports.
    (re.compile(r"(^|/)agents?\.ya?ml$"), EntityKind.AGENT),
    (re.compile(r"(^|/)tools?\.ya?ml$"), EntityKind.TOOL),
    # Generic directory conventions, for platforms with no bespoke rule.
    (re.compile(r"(^|/)(agents|assistants|bots)/"), EntityKind.AGENT),
    (re.compile(r"(^|/)(tools|actions|connectors|functions)/"), EntityKind.TOOL),
    (re.compile(r"(^|/)(workflows|flows|pipelines)/"), EntityKind.WORKFLOW),
    (re.compile(r"(^|/)(triggers|events|webhooks)/"), EntityKind.TRIGGER),
    (re.compile(r"(^|/)(knowledge|knowledgebases|datasources)/"), EntityKind.KNOWLEDGE),
    (re.compile(r"(^|/)(connections|credentials)/"), EntityKind.CONNECTION),
    (re.compile(r"(^|/)(topics|dialogs|intents)/"), EntityKind.TOPIC),
)

# Directories whose files describe one entity between them, not one each.
# Explicit rather than inferred: a leaf directory holding several files is
# usually several entities (`topics/`), and only sometimes one entity split
# across files. Guessing wrong merges every topic in an agent into one record.
BUNDLES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"botcomponents/[^/]+/?$"), ("botcomponent.xml", "data")),
    (re.compile(r"bots/[^/]+/?$"), ("bot.xml", "configuration.json")),
)


@dataclass(frozen=True)
class Graft:
    """A record that belongs *inside* another record rather than beside it.

    Bundling joins files in one directory. Grafting joins directories, which is
    a different problem and a worse one when it is missed: a Copilot Studio
    agent is written to the archive twice over. `bots/HRAgent/` holds the
    container -- name, language, authentication -- and
    `botcomponents/HRAgent.gpt.default/` holds the generative half:
    instructions, model, capabilities. Scanned as two records they are two
    agents, each missing everything the other has, and every mapping learned
    from one is simply absent on the other.

    `child_ref` is where in the child to find the parent's identity; `parent_id`
    is where the parent states it. A child whose parent is not in the archive
    stays a record of its own, because dropping it would lose it entirely.
    """

    pattern: re.Pattern[str]
    kind: EntityKind
    child_ref: tuple[str, ...]
    parent_id: tuple[str, ...]
    under: str
    # Whether the slot holds a list. Fixed per graft rather than decided by how
    # many children turned up, so the inferred shape of `collaborators` is the
    # same for an agent with one collaborator and an agent with three.
    many: bool = False


GRAFTS: tuple[Graft, ...] = (
    Graft(
        pattern=re.compile(r"botcomponents/[^/]*\.gpt\."),
        kind=EntityKind.AGENT,
        child_ref=("botcomponent", "parentbotid", "schemaname"),
        parent_id=("bot", "@schemaname"),
        under="gpt",
    ),
    Graft(
        pattern=re.compile(r"botcomponents/[^/]*\.invokeconnectedagent[^/]*\."),
        kind=EntityKind.AGENT,
        child_ref=("botcomponent", "parentbotid", "schemaname"),
        parent_id=("bot", "@schemaname"),
        under="collaborators",
        many=True,
    ),
)

# Guards against an archive that would exhaust the machine reading it.
MAX_FILES = 20_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def is_ignored(relative_path: str) -> bool:
    """Whether a file is deliberately not an entity, as opposed to unrecognised."""
    haystack = relative_path.replace("\\", "/").lower()
    return any(pattern.search(haystack) for pattern in IGNORED)


def classify(relative_path: str) -> EntityKind | None:
    """Which entity kind a file in an export describes, or None if unknown.

    Unknown is a real answer and is counted, not silently dropped: a corridor
    where 40% of the archive went unclassified is a corridor whose rules need
    work, and the only way anyone finds that out is if the scan says so.
    """
    haystack = relative_path.replace("\\", "/").lower()
    for pattern, kind in RULES:
        if pattern.search(haystack):
            return kind
    return None


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def _xml_to_dict(element: ElementTree.Element) -> Any:
    """Convert an XML element into plain JSON-shaped data.

    Attributes are prefixed with `@` so they cannot collide with a child
    element of the same name, and repeated children collapse into a list --
    which is what they mean, and what every downstream consumer expects.
    """
    node: dict[str, Any] = {}
    for name, value in element.attrib.items():
        node[f"@{name}"] = value

    children = list(element)
    if not children:
        text = (element.text or "").strip()
        if not node:
            return text
        if text:
            node["#text"] = text
        return node

    for child in children:
        tag = child.tag.rsplit("}", 1)[-1]  # drop any namespace
        value = _xml_to_dict(child)
        if tag in node:
            existing = node[tag]
            node[tag] = existing if isinstance(existing, list) else [existing]
            node[tag].append(value)
        else:
            node[tag] = value
    return node


def parse_file(path: Path) -> Any:
    """Parse one export file into JSON-shaped data, or None if it can't be.

    Never raises. An export routinely contains one malformed or unexpected
    file, and refusing to read the other four thousand because of it would be
    a strictly worse outcome than scanning what parses and saying how many
    didn't.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None

    suffix = path.suffix.lower()
    try:
        if suffix == ".xml":
            return _xml_to_dict(ElementTree.fromstring(text))
        # YAML's loader reads JSON too, so one call covers .json, .yaml, .yml
        # and the extensionless `data` payloads whose format isn't declared.
        return yaml.safe_load(text)
    except (ElementTree.ParseError, yaml.YAMLError, ValueError, RecursionError):
        return None


def _is_parseable(path: Path) -> bool:
    return path.suffix.lower() in PARSEABLE or path.name.lower() in EXTENSIONLESS


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------


@dataclass
class _Collected:
    records: dict[EntityKind, list[dict]] = field(default_factory=dict)
    names: dict[EntityKind, str] = field(default_factory=dict)
    parsed: int = 0
    unparseable: int = 0
    ignored: int = 0
    grafted: int = 0
    # The paths themselves, not just a count. "35 unclassified files" tells a
    # user nothing they can act on; five example paths tells them immediately
    # whether it is packaging noise or content they are losing.
    unclassified: list[str] = field(default_factory=list)

    def add(self, kind: EntityKind, name: str, record: dict) -> None:
        self.records.setdefault(kind, []).append(record)
        self.names.setdefault(kind, name)


def _bundle_members(directory: Path, root: Path) -> tuple[str, ...] | None:
    relative = directory.relative_to(root).as_posix().lower()
    for pattern, members in BUNDLES:
        if pattern.search(relative):
            return members
    return None


def _dig(record: dict, path: tuple[str, ...]) -> str | None:
    """Follow a fixed key path to a string, or None if it isn't there."""
    node: Any = record
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def _graft_for(relative: str) -> Graft | None:
    haystack = relative.replace("\\", "/").lower()
    for graft in GRAFTS:
        if graft.pattern.search(haystack):
            return graft
    return None


def _apply_grafts(pending: list[tuple[Graft, dict]], collected: _Collected) -> int:
    """Fold each grafted record into its parent, or leave it standing alone.

    Returns how many found a parent. A child whose parent is missing is added
    as its own record rather than dropped: half an agent in the corpus is worth
    more than none, and the shape it contributes is real either way.
    """
    joined = 0
    for graft, child in pending:
        wanted = _dig(child, graft.child_ref)
        parent = None
        if wanted:
            for candidate in collected.records.get(graft.kind, []):
                if _dig(candidate, graft.parent_id) == wanted:
                    parent = candidate
                    break
        if parent is None:
            collected.add(graft.kind, graft.under, child)
            continue
        if graft.many:
            parent.setdefault(graft.under, []).append(child)
        else:
            parent[graft.under] = child
        joined += 1
    return joined


def _scan_tree(root: Path, collected: _Collected) -> None:
    """Walk the archive, bundling where a rule says to and one-file-per-record
    everywhere else.
    """
    bundled_dirs: set[Path] = set()
    # Grafted records are held back until the whole tree is read: a child can
    # appear before its parent in directory order, and resolving as we go would
    # make the result depend on how the archive happens to sort.
    pending: list[tuple[Graft, dict]] = []

    for directory in sorted({p.parent for p in root.rglob("*") if p.is_file()}):
        members = _bundle_members(directory, root)
        if members is None:
            continue
        record: dict[str, Any] = {}
        for member in members:
            path = directory / member
            if not path.is_file():
                continue
            parsed = parse_file(path)
            if parsed is None:
                collected.unparseable += 1
                continue
            record[Path(member).stem] = parsed
        if not record:
            continue
        bundled_dirs.add(directory)
        collected.parsed += len(record)
        relative = directory.relative_to(root).as_posix()
        graft = _graft_for(relative + "/")
        if graft is not None:
            pending.append((graft, record))
            continue
        kind = classify(relative + "/")
        if kind is None:
            collected.unclassified.append(relative + "/")
            continue
        collected.add(kind, directory.name, record)

    seen = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _is_parseable(path):
            continue
        if path.parent in bundled_dirs:
            continue
        relative = path.relative_to(root).as_posix()
        if is_ignored(relative):
            collected.ignored += 1
            continue
        seen += 1
        if seen > MAX_FILES:
            break
        parsed = parse_file(path)
        if parsed is None:
            collected.unparseable += 1
            continue
        collected.parsed += 1
        kind = classify(relative)
        if kind is None:
            collected.unclassified.append(relative)
            continue
        # A YAML document that isn't a mapping (a bare list of ids, a scalar)
        # has no fields to infer, so it is wrapped rather than dropped: the
        # fact that this file holds a list is itself part of the shape.
        record = parsed if isinstance(parsed, dict) else {"value": parsed}
        collected.add(kind, path.name, record)

    collected.grafted = _apply_grafts(pending, collected)


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract a zip, refusing entries that would escape the destination.

    An export is a file a user downloaded from a vendor, which makes it
    untrusted input by definition -- a `../../.ssh/authorized_keys` entry costs
    nothing to write into an archive and everything to extract.
    """
    total = 0
    destination = destination.resolve()
    for info in archive.infolist():
        target = (destination / info.filename).resolve()
        if not target.is_relative_to(destination):
            raise ValueError(f"Refusing to extract '{info.filename}': it escapes the archive root.")
        total += info.file_size
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("Refusing to extract: the archive expands beyond 512MB.")
    archive.extractall(destination)


def scan_export(path: Path) -> ProbeResult:
    """Infer a platform's shape from an export directory or `.zip`."""
    path = Path(path)
    if not path.exists():
        return ProbeResult(
            gaps=[
                ProbeGap(
                    what="export archive",
                    reason=GapReason.NOT_IN_EXPORT,
                    detail=f"{path} does not exist.",
                    remedy="Point the probe at an export directory or .zip.",
                )
            ]
        )

    if path.is_file() and path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="agent_liftoff-scan-") as tmp:
            with zipfile.ZipFile(path) as archive:
                _safe_extract(archive, Path(tmp))
            return _scan_directory(Path(tmp), source=str(path))
    if path.is_file():
        return _scan_directory(path.parent, source=str(path))
    return _scan_directory(path, source=str(path))


def _scan_directory(root: Path, source: str) -> ProbeResult:
    collected = _Collected()
    _scan_tree(root, collected)

    entities = []
    for kind, records in sorted(collected.records.items(), key=lambda item: item[0].value):
        entity = observe(
            kind=kind,
            name=collected.names.get(kind, kind.value),
            origin=ProbeOrigin.EXPORT,
            records=records,
        )
        if entity is not None:
            entities.append(entity)

    notes = [f"Scanned {source}: parsed {collected.parsed} file(s)."]
    gaps: list[ProbeGap] = []
    if collected.unparseable:
        notes.append(f"{collected.unparseable} file(s) could not be parsed and were skipped.")
    if collected.ignored:
        notes.append(
            f"{collected.ignored} packaging or previously-generated file(s) were ignored by "
            "design."
        )
    if collected.unclassified:
        examples = ", ".join(sorted(collected.unclassified)[:5])
        more = len(collected.unclassified) - 5
        gaps.append(
            ProbeGap(
                what=f"{len(collected.unclassified)} unclassified file(s)",
                reason=GapReason.UNSUPPORTED,
                detail=(
                    "These parsed cleanly but matched no entity-kind rule, so their fields "
                    f"are not in the corpus and will not be mapped: {examples}"
                    + (f" (+{more} more)" if more > 0 else "")
                ),
                remedy=(
                    "Add a rule to foundry/probes/export_scan.py:RULES if they hold "
                    "migratable content; add one to IGNORED if they are packaging."
                ),
            )
        )
    if not entities:
        gaps.append(
            ProbeGap(
                what="every entity kind",
                reason=GapReason.NOT_IN_EXPORT,
                detail=f"Nothing in {source} matched a known export layout.",
                remedy="Check this is an unpacked agent export, not an installer or a report.",
            )
        )
    return ProbeResult(entities=entities, gaps=gaps, notes=notes)


class ExportScan:
    """Probe source wrapper for the structural pass."""

    name = "export"

    def probe(self, context: ProbeContext) -> ProbeResult:
        if context.export_path is None:
            return ProbeResult(
                gaps=[
                    ProbeGap(
                        what="export archive",
                        reason=GapReason.NOT_IN_EXPORT,
                        detail="No export path was supplied, so the structural pass was skipped.",
                        remedy="Export the agent from the platform and pass the archive.",
                    )
                ]
            )
        return scan_export(context.export_path)

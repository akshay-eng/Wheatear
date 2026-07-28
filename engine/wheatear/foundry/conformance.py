"""Does the build in hand still describe the platform in front of us?

A build is meant to be made once and used by everybody. That only holds while
the thing it was built against hasn't moved, so something has to say which
version it *was* built against and check that claim at migration time. That is
this module, and it is the price of not rebuilding per tenant.

Two kinds of drift, and they deserve different answers:

  **A declared version moved.** The ADK shipped 2.14, or Dataverse served a
  different API version. The create contract itself may have changed, so the
  adapters were compiled against a schema that is no longer the schema. This
  is worth a loud warning and a rebuild.

  **A tenant carries fields the build never saw.** Entirely expected: a build
  serving every customer cannot have seen every customer's records, and a
  field nobody mapped is a field nobody loses -- it simply isn't carried. This
  is worth reporting and nothing more.

The distinction matters because conflating them produces a tool that either
cries wolf on every migration or stays silent through a breaking change. So
version drift is `stale`, unknown fields are `extra`, and only the first one
suggests anybody do anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wheatear.foundry.shape import infer_fields
from wheatear.foundry.types import EntityKind, SchemaCorpus
from wheatear.ir.schema import IR_SPEC_VERSION

# The Dataverse Web API revision the Copilot Studio probes address. Declared
# here rather than read from a response because it is a choice this codebase
# makes -- the URL is built with it -- not something the platform reports.
DATAVERSE_API_VERSION = "v9.2"

UNKNOWN = "unknown"


def adk_version() -> str:
    """The installed watsonx Orchestrate ADK's version.

    The ADK's pydantic specs *are* Orchestrate's create contract as far as this
    project is concerned, so its version is the version of that contract. Read
    from installed metadata rather than pinned in this file, which would be a
    second source of truth that could disagree with the code actually running.
    """
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("ibm-watsonx-orchestrate")
    except Exception:  # noqa: BLE001 - a missing ADK is a gap, not a crash
        return UNKNOWN


def dataverse_version() -> str:
    return DATAVERSE_API_VERSION


# The n8n node API generation this project's mapping addresses. n8n versions
# its *nodes* independently of the product (an `agent` node is typeVersion 2
# while n8n itself is 2.31.7), and the node schema is what a mapping is built
# against -- so pinning the product version would make every n8n patch release
# look like contract drift while a genuine node schema change looked like none.
N8N_NODE_API_VERSION = "langchain-v2"


def n8n_version() -> str:
    return N8N_NODE_API_VERSION


# Which declared version governs which platform. A platform absent here has no
# declared contract this project knows how to check, and says so rather than
# silently passing.
PLATFORM_VERSIONS: dict[str, Any] = {
    "orchestrate": ("adk", adk_version),
    "copilot-studio": ("dataverse-api", dataverse_version),
    "n8n": ("n8n-node-api", n8n_version),
}


@dataclass
class Drift:
    """One way the build and the platform disagree."""

    kind: str  # "stale" | "extra" | "unknown"
    what: str
    detail: str

    @property
    def blocking(self) -> bool:
        """Whether this should stop somebody trusting the build.

        Only version drift. An unmapped field is a smaller migration, not a
        wrong one, and treating it as blocking would make the report useless
        on the first real tenant that has a custom column.
        """
        return self.kind == "stale"


@dataclass
class ConformanceReport:
    """What the build claims, and what this machine actually found."""

    platform: str
    built_against: dict[str, str] = field(default_factory=dict)
    found: dict[str, str] = field(default_factory=dict)
    drift: list[Drift] = field(default_factory=list)
    records_checked: int = 0
    fields_checked: int = 0

    @property
    def ok(self) -> bool:
        return not any(d.blocking for d in self.drift)

    def summary(self) -> str:
        versions = ", ".join(f"{k} {v}" for k, v in sorted(self.found.items())) or "no declared version"
        if not self.drift:
            return f"{self.platform}: conforms ({versions})."
        stale = sum(1 for d in self.drift if d.blocking)
        extra = len(self.drift) - stale
        parts = [f"{self.platform}: {versions}"]
        if stale:
            parts.append(f"{stale} version change(s) -- rebuild")
        if extra:
            parts.append(f"{extra} field(s) this build does not map")
        return "; ".join(parts) + "."


def declared_versions(platform: str) -> dict[str, str]:
    """The versions a build for `platform` should record, read right now."""
    entry = PLATFORM_VERSIONS.get(platform)
    if entry is None:
        return {}
    label, read = entry
    return {label: read(), "ir": IR_SPEC_VERSION}


def check(
    corpus: SchemaCorpus,
    records: dict[EntityKind, list[Any]] | None = None,
) -> ConformanceReport:
    """Compare a stored build against this machine and this tenant's records.

    `records` is optional and is the tenant half: without it this checks only
    that the declared contracts still match, which is the cheap check and the
    one that matters most.
    """
    report = ConformanceReport(platform=corpus.platform)
    report.found = declared_versions(corpus.platform)
    report.built_against = dict(getattr(corpus, "declared_versions", None) or {})

    if not report.found:
        report.drift.append(
            Drift(
                "unknown",
                f"{corpus.platform} declares no version this project knows how to read",
                "Drift in this platform's create contract will not be detected. Add it to "
                "`PLATFORM_VERSIONS` when its version becomes readable.",
            )
        )
    elif not report.built_against:
        report.drift.append(
            Drift(
                "unknown",
                "this build recorded no versions",
                "It predates the conformance check, so there is nothing to compare against. "
                "Re-probe to stamp it.",
            )
        )
    else:
        for label, found in sorted(report.found.items()):
            was = report.built_against.get(label)
            if was is not None and was != found:
                report.drift.append(
                    Drift(
                        "stale",
                        f"{label} moved from {was} to {found}",
                        "The adapters were compiled against a create contract that is no "
                        "longer the one this machine has. Rebuild before relying on them.",
                    )
                )

    for kind, samples in (records or {}).items():
        entity = corpus.entity(kind)
        if entity is None or not samples:
            continue
        report.records_checked += len(samples)
        known = entity.paths()
        report.fields_checked += len(known)
        # Containers are not carried in their own right, so an unmapped object
        # node is not a loss -- only its leaves would be.
        unseen = sorted(
            node.path for node in infer_fields(samples) if not node.container and node.path not in known
        )
        if unseen:
            shown = ", ".join(unseen[:8])
            more = f" (+{len(unseen) - 8} more)" if len(unseen) > 8 else ""
            report.drift.append(
                Drift(
                    "extra",
                    f"{len(unseen)} field(s) on this tenant's {kind.value} records are not in the build",
                    f"They will not be carried: {shown}{more}.",
                )
            )
    return report

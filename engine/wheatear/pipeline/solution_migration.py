"""Migrate a whole Copilot Studio solution into watsonx Orchestrate.

The four stages, in the order they have to happen:

  1. BUILD (already done, cached)   platform records -> IR, via compiled adapters
  2. COMPOSE                        per-entity IR -> whole agents
  3. LOOKUP                         source tools -> tools that exist on the target
  4. MIGRATE                        IR -> agent.yaml -> the instance

Only stage 3 calls a model, and only to choose between a fixed shortlist the
deterministic ranker produced. Stage 1 is cached compiled code; a second
solution through the same corridor calls no model at all.

This module lives here rather than in `scripts/` because two callers need it --
the wizard and the command line -- and a copy in each is how the two start
disagreeing about what a migration does. Neither of them decides field
mappings, agent YAML shape or target models either: those belong to `foundry/`,
`connectors/orchestrate/exporter.py` and `model_matrix/` respectively.

Nothing here prints. Every stage reports through an injected `Reporter`, so the
terminal wizard can draw a progress bar over the same run the CLI prints
line by line, and a test can assert on the events without capturing stdout.

The other thing this module owns is `ManualStep`: the list of things a person
still has to do on the target before the migration is finished. It is a
first-class output rather than a paragraph in a review file, because "the
ServiceNow tool has to be installed by hand" is the single fact most likely to
decide whether a migrated agent works, and it is worthless if nobody sees it.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from wheatear.connectors.copilot_studio.mcp_scan import McpServer, find_mcp_servers
from wheatear.connectors.orchestrate.catalog_snapshot import load_snapshot, tools_only
from wheatear.connectors.orchestrate.connections import (
    AppConnection,
    list_applications,
    matching,
)
from wheatear.connectors.orchestrate.exporter import (
    export_agent,
    importable_tools,
    knowledge_base_specs,
    target_model_for,
)
from wheatear.connectors.orchestrate.mcp_sync import McpPlan, plan_servers
from wheatear.foundry import conformance, runtime
from wheatear.foundry.probes.export_scan import _Collected, _scan_tree
from wheatear.foundry.store import FoundryStore
from wheatear.foundry.types import Direction, EntityKind
from wheatear.ir.schema import Agent
from wheatear.llm.base import LLMProvider
from wheatear.pipeline.resolve import (
    build_catalog,
    build_marketplace_catalog,
    carry_tool_context,
    resolve_agent_tools,
)
from wheatear.pipeline.translate import describe_agent

# Where a Copilot Studio record says who it belongs to and what it is called.
# Platform knowledge, in one place rather than sprinkled through the composer.
PARENT = ("botcomponent", "parentbotid", "schemaname")
SELF = ("botcomponent", "@schemaname")
DISPLAY = ("botcomponent", "name")
AGENT_SELF = ("bot", "@schemaname")
COLLABORATOR_TARGET = ("data", "action", "botSchemaName")
FILE_NAME = ("botcomponent", "filedata", "#text")

Stage = Literal[
    "scan", "ir", "compose", "lookup", "mcp", "logins", "describe", "export", "import"
]
Level = Literal["info", "ok", "warn", "error"]


@dataclass(frozen=True)
class Event:
    """One thing worth telling the operator, as it happens."""

    stage: Stage
    text: str
    level: Level = "info"


Reporter = Callable[[Event], None]


def silent(event: Event) -> None:
    """The default reporter: a migration that nobody is watching still runs."""


# ----------------------------------------------------------------------
# What a person still has to do
# ----------------------------------------------------------------------

StepKind = Literal["install-tool", "configure-connection", "add-toolkit", "review"]


@dataclass
class ManualStep:
    """Something on the target that Wheatear cannot do and a person must.

    `blocking` is the distinction that matters. A blocking step means a
    capability the source agent had is *absent* from the migrated one until
    somebody acts -- the agent imported, it runs, and it cannot do that job. A
    non-blocking step means the agent works and could work better.

    None of these are failures. A migration that stops because a tool needs
    installing has moved nothing; one that lands what it can and hands over an
    exact list of what is left is a migration somebody can finish.
    """

    kind: StepKind
    title: str
    detail: str
    # Where to do it. A console path rather than a URL: the console's address
    # is not derivable from the instance API URL, and a link that goes nowhere
    # is worse than directions that do.
    where: str
    agents: list[str] = field(default_factory=list)
    command: str | None = None
    blocking: bool = True

    def summary(self) -> str:
        who = f" (needed by {', '.join(self.agents)})" if self.agents else ""
        return f"{self.title}{who}"


# The console route to the catalog. Written out rather than linked because the
# catalog is served by the console, whose hostname is not the instance API
# hostname we hold -- see connectors/orchestrate/catalog_client.py. There is
# also no install API to offer instead: the only endpoint the console's own
# bundle calls for this is a purchase flow for paid artifacts, which free IBM
# tools are not routed through (verified against a live tenant).
CATALOG_ROUTE = "watsonx Orchestrate console -> Manage -> Tools -> Add tool -> Catalog"
CONNECTIONS_ROUTE = "watsonx Orchestrate console -> Manage -> Connections"


def install_steps(agents: Iterable[tuple[str, Agent]]) -> list[ManualStep]:
    """Catalog tools somebody has to install, one step per tool.

    Grouped by the tool rather than by the agent: installing `get_records` once
    serves every agent that wanted it, and three near-identical rows saying so
    reads like three jobs.
    """
    by_ref: dict[str, ManualStep] = {}
    for _, agent in agents:
        carried, _ = importable_tools(agent)
        carried_set = set(carried)
        for tool in agent.tools:
            install_ref = tool.catalog_install_ref
            if not install_ref:
                continue
            # Blocking when the agent has no tool for this job at all. A tool
            # running on an installed stand-in is a real capability today, so
            # installing the better one is an improvement, not a repair.
            landed_on_fallback = tool.ref in carried_set and tool.ref != install_ref
            step = by_ref.get(install_ref)
            if step is None:
                title = tool.catalog_title or install_ref
                needs = (
                    f" Then configure its '{', '.join(tool.catalog_connections)}' connection."
                    if tool.catalog_connections
                    else " Then configure its connection so it can authenticate."
                )
                step = ManualStep(
                    kind="install-tool",
                    title=f"Install '{title}' from the catalog",
                    detail=(
                        f"Search the catalog for '{title}'. It installs as `{install_ref}`."
                        + needs
                        + " The agents were migrated without it; the migration attaches it "
                        "on its next run over the same selection."
                    ),
                    where=CATALOG_ROUTE,
                    blocking=not landed_on_fallback,
                )
                by_ref[install_ref] = step
            step.blocking = step.blocking or not landed_on_fallback
            if agent.name not in step.agents:
                step.agents.append(agent.name)
            if landed_on_fallback:
                note = (
                    f"{agent.name} is using `{tool.ref}` in the meantime, which is "
                    "installed but a weaker match."
                )
                if note not in step.detail:
                    step.detail += f" {note}"
    return list(by_ref.values())


def toolkit_steps(plans: Iterable[McpPlan]) -> list[ManualStep]:
    """MCP servers the target has no toolkit for, or has a conflicting one.

    A `reuse` plan produces nothing on purpose: the target is already pointed
    at that server and the correct action is to leave it entirely alone.
    """
    steps: list[ManualStep] = []
    for plan in plans:
        if plan.action == "reuse":
            continue
        command = plan.command()
        steps.append(
            ManualStep(
                kind="add-toolkit",
                title=(
                    f"Add the MCP server '{plan.server.name}'"
                    if plan.action == "create"
                    else f"Resolve the MCP server clash on '{plan.server.name}'"
                ),
                detail=(
                    plan.reason
                    + (
                        " Credentials do not migrate -- a solution export carries "
                        "connection references, not connections."
                        if plan.needs_credentials
                        else ""
                    )
                ),
                where="watsonx Orchestrate console -> Manage -> Toolkits",
                command=f"orchestrate {' '.join(command)}" if command else None,
                blocking=True,
            )
        )
    return steps


def login_steps(
    agents: Iterable[tuple[str, Agent]], connections: list[AppConnection]
) -> list[ManualStep]:
    """Carried tools whose system the target cannot authenticate to.

    Wheatear never moves credentials -- a solution export does not contain any
    -- so the only useful thing is to read what the target already has and say
    plainly what is missing. A tool with no connection behind it imports
    cleanly and fails on its first call, which is the failure mode worth
    spending a paragraph to prevent.

    Silent when a connection is present and set to member credentials: that is
    the working case, where the agent prompts each user to sign in and calls as
    them, and there is nothing for anybody to do.
    """
    steps: list[ManualStep] = []
    seen: set[str] = set()
    for _, agent in agents:
        carried, _ = importable_tools(agent)
        carried_set = set(carried)
        for tool in agent.tools:
            if tool.ref not in carried_set:
                continue
            candidates = matching(connections, f"{tool.ref} {tool.source_ref or ''}")
            usable = [c for c in candidates if c.usable]
            if any(c.prompts_the_user for c in usable):
                continue  # each user signs in themselves; nothing to configure
            if usable:
                app = usable[0].app_id
                key = f"member:{app}"
                if key in seen:
                    continue
                seen.add(key)
                steps.append(
                    ManualStep(
                        kind="configure-connection",
                        title=f"Consider per-user sign-in for '{app}'",
                        detail=(
                            f"`{tool.ref}` authenticates through '{app}', a shared team "
                            "credential -- every user sees whatever that account can see. "
                            "If the source enforced per-user permissions, switch it to "
                            "member credentials so the agent asks each user to sign in."
                        ),
                        where=CONNECTIONS_ROUTE,
                        agents=[agent.name],
                        command=(
                            f"orchestrate connections configure -a {app} "
                            "--env draft --type member --kind oauth_auth_code_flow"
                        ),
                        blocking=False,
                    )
                )
                continue

            key = f"missing:{tool.ref}"
            if key in seen:
                continue
            seen.add(key)
            near = candidates[0] if candidates else None
            steps.append(
                ManualStep(
                    kind="configure-connection",
                    title=f"Configure a connection for `{tool.ref}`",
                    detail=(
                        f"'{near.app_id}' exists but is not usable ({near.summary()})."
                        if near is not None
                        else "No connection on the target plausibly serves this tool."
                    )
                    + " Until one is configured this tool imports and then fails on its "
                    "first call.",
                    where=CONNECTIONS_ROUTE,
                    agents=[agent.name],
                    blocking=True,
                )
            )
    return steps


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Watching for the installs a person still has to do
# ----------------------------------------------------------------------


@dataclass
class PendingInstall:
    """One catalog tool the target is waiting on, and who wants it.

    The migration does not stop for these. The agents land without the tool --
    an agent that arrives doing four of its five jobs is one somebody can
    finish, and one that refuses to import is not -- and this is what the
    watcher polls for so the tool can be attached the moment it appears.
    """

    install_ref: str
    title: str
    agents: list[str] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)
    # Set when the catalog entry carried an id, which is what an automated
    # install is addressed to. Absent means the only route is a person.
    artifact_id: str | None = None


def pending_installs(agents: Iterable[tuple[str, Agent]]) -> list[PendingInstall]:
    """Catalog tools that are wanted and not installed, one per tool."""
    by_ref: dict[str, PendingInstall] = {}
    for _, agent in agents:
        carried, _ = importable_tools(agent)
        carried_set = set(carried)
        for tool in agent.tools:
            ref = tool.catalog_install_ref
            # A tool that landed on an installed stand-in is not pending: the
            # agent can do the job today. Installing the better one is an
            # improvement, and improvements are not worth blocking an operator
            # at their terminal for.
            if not ref or tool.ref in carried_set:
                continue
            entry = by_ref.setdefault(
                ref,
                PendingInstall(
                    ref,
                    tool.catalog_title or ref,
                    [],
                    list(tool.catalog_connections),
                    tool.catalog_artifact_id,
                ),
            )
            if agent.name not in entry.agents:
                entry.agents.append(agent.name)
    return list(by_ref.values())


def installed_tool_names(client: Any) -> set[str]:
    """Every tool name on the instance right now, or an empty set if unreadable."""
    try:
        return {str(t.get("name")) for t in client.list_all_tools() if t.get("name")}
    except Exception:  # noqa: BLE001 - a failed poll is a retry, not a failure
        return set()


def still_missing(pending: list[PendingInstall], installed: set[str]) -> list[PendingInstall]:
    """The subset of `pending` the instance still does not have.

    Matched through `installed_copy_of` rather than by equality, because
    Orchestrate renames a catalog tool as it lands -- `get_records` becomes
    `get_records_568d4` -- and an exact comparison would wait forever for a
    name that is never going to appear.
    """
    from wheatear.pipeline.resolve import installed_copy_of

    return [
        item
        for item in pending
        if not any(installed_copy_of(name, item.install_ref) for name in installed)
    ]


@dataclass
class AgentOutcome:
    """What became of one source agent."""

    source_key: str
    name: str
    llm: str = ""
    tools: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)
    agent_path: Path | None = None
    review_path: Path | None = None
    # The id the target gave it, read back after import. What makes an outcome
    # point at a real thing on the tenant rather than just name one.
    agent_id: str = ""
    # None until an import is attempted -- a dry run leaves it None rather than
    # False, because "not tried" and "tried and failed" are different answers.
    deployed: bool | None = None
    detail: str = ""


@dataclass
class MigrationReport:
    """Everything a caller needs to render, decide on, or assert against."""

    agents: list[AgentOutcome] = field(default_factory=list)
    manual_steps: list[ManualStep] = field(default_factory=list)
    knowledge_bases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    installed_pool: int = 0
    catalog_pool: int = 0
    # Tools the target is waiting on. Populated even though the agents were
    # migrated anyway, because this is what the watcher polls for.
    pending: list[PendingInstall] = field(default_factory=list)

    @property
    def deployed(self) -> list[AgentOutcome]:
        return [a for a in self.agents if a.deployed]

    @property
    def failed(self) -> list[AgentOutcome]:
        return [a for a in self.agents if a.deployed is False]

    @property
    def blocking_steps(self) -> list[ManualStep]:
        return [s for s in self.manual_steps if s.blocking]

    def summary(self) -> str:
        done = len(self.deployed)
        blocked = len(self.blocking_steps)
        line = f"{done}/{len(self.agents)} agent(s) on the target"
        if blocked:
            line += f", {blocked} step(s) left for you"
        return line


# ----------------------------------------------------------------------
# 0. Names
# ----------------------------------------------------------------------


def dig(record: Any, path: tuple[str, ...]) -> str | None:
    node = record
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


def slug(name: str, suffix: str = "") -> str:
    """An Orchestrate-legal name. Spaces, hyphens and dots are not."""
    cleaned = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name)).strip("_")
    return f"{cleaned}{suffix}"


def free_name(wanted: str, taken: set[str]) -> str:
    """`wanted`, or the first numbered variant of it that nobody is using.

    Overwriting an agent somebody else built is not a migration outcome anyone
    asked for, and the ADK's import updates silently on a name match -- so the
    check has to happen here, before the name reaches a spec.
    """
    if wanted not in taken:
        return wanted
    for n in range(2, 100):
        candidate = f"{wanted}_{n}"
        if candidate not in taken:
            return candidate
    return f"{wanted}_{len(taken)}"


# ----------------------------------------------------------------------
# 1. Records -> IR, through the cached adapters
# ----------------------------------------------------------------------


def scan_solution(solution: Path) -> _Collected:
    """Read the export into per-entity records. Offline; no credentials."""
    collected = _Collected()
    _scan_tree(Path(solution), collected)
    return collected


def ensure_shipped(store: FoundryStore, report: Reporter = silent) -> int:
    """Load the adapters that ship with Wheatear into this machine's store.

    Idempotent and cheap: writing a corpus or an artifact that is already there
    rewrites the same file under the same key. Doing it on every readiness
    check rather than at install time means a `git pull` that brings newer
    adapters takes effect on the next run without anybody remembering a step.

    A failure here is reported, not swallowed. Shipped adapters are a shortcut
    rather than a dependency, so this must not stop a migration -- but the
    first version returned 0 on any exception and a corpus that failed to
    validate looked exactly like an assets tree that was not there. The
    difference matters: one is "nothing shipped yet", the other is "what
    shipped is broken", and only one of them is a bug in this repository.
    """
    try:
        from wheatear.assets import ASSETS
        from wheatear.foundry.shipping import install

        return install(ASSETS, store)
    except Exception as exc:  # noqa: BLE001 - a shortcut, but a loud one when broken
        report(
            Event(
                "ir",
                f"shipped adapters could not be loaded ({type(exc).__name__}: "
                f"{' '.join(str(exc).split())[:160]}). Falling back to whatever this "
                "machine built locally.",
                "warn",
            )
        )
        return 0


def adapters_ready(
    store: FoundryStore, platform: str = "copilot-studio", report: Reporter = silent
) -> tuple[bool, str]:
    """Whether this machine can convert records without building anything.

    Asked before a migration starts rather than discovered partway through: a
    missing corpus means the whole first stage cannot run, and finding that out
    after the user has picked an environment, a solution and four agents is a
    worse way to learn it.
    """
    # Shipped adapters first. They are keyed on the platform versions they were
    # compiled against, so a user on those versions is found by exactly the
    # lookup a locally-built adapter would have been -- no probe, no model call,
    # no Docker. This is the default path and the reason most users never build
    # anything.
    ensure_shipped(store, report)

    corpus = store.latest_corpus(platform)
    if corpus is None:
        return False, (
            f"No stored schema for {platform}. Run `wheatear foundry corridor "
            f"{platform} orchestrate` once to compile its adapters."
        )
    usable = [
        kind
        for kind in corpus.kinds()
        if store.find(
            platform=platform,
            direction=Direction.IMPORT,
            entity_kind=kind,
            schema_fingerprint=corpus.entity_fingerprint(kind),
        ).usable
    ]
    if not usable:
        return False, (
            f"A {platform} schema is stored but no adapter matches it. Run "
            f"`wheatear foundry corridor {platform} orchestrate` to rebuild."
        )
    return True, f"{len(usable)} cached adapter(s) for {platform}"


def records_to_ir(
    store: FoundryStore,
    collected: _Collected,
    report: Reporter = silent,
    platform: str = "copilot-studio",
) -> dict[EntityKind, list[dict]]:
    corpus = store.latest_corpus(platform)
    if corpus is None:
        raise RuntimeError(f"No stored corpus for {platform}. Probe it first.")

    check = conformance.check(corpus, collected.records)
    report(Event("ir", f"conformance: {check.summary()}", "ok" if check.ok else "warn"))
    for drift in check.drift:
        report(
            Event(
                "ir",
                f"[{drift.kind}] {drift.what}: {drift.detail}",
                "error" if drift.blocking else "warn",
            )
        )

    out: dict[EntityKind, list[dict]] = {}
    for kind in sorted(collected.records, key=lambda k: k.value):
        lookup = store.find(
            platform=platform,
            direction=Direction.IMPORT,
            entity_kind=kind,
            schema_fingerprint=corpus.entity_fingerprint(kind),
        )
        if not lookup.usable:
            report(Event("ir", f"{kind.value}: skipped -- {lookup.status}: {lookup.reason}", "warn"))
            continue
        adapter = runtime.load(lookup.artifact)
        converted, run = runtime.convert_all(adapter, collected.records[kind])
        out[kind] = converted
        report(Event("ir", f"{kind.value}: {run.summary()}"))
    return out


# ----------------------------------------------------------------------
# 2. Compose
# ----------------------------------------------------------------------


def compose_agents(
    collected: _Collected,
    ir: dict[EntityKind, list[dict]],
    solution: Path,
    report: Reporter = silent,
) -> list[tuple[str, Agent]]:
    """Link per-entity IR onto the agents that own it.

    The corridor deliberately does not map an agent's `tools` / `knowledge` /
    `topics` (see `inspector.IR_COMPOSED`): they are produced by other kinds'
    adapters, and a record has no field containing them. Joining them is this
    function's whole job, and it joins on the parent key the source uses.
    """
    owned: dict[str, dict[str, list]] = {}
    for kind, slot in (
        (EntityKind.TOOL, "tools"),
        (EntityKind.KNOWLEDGE, "knowledge"),
        (EntityKind.TOPIC, "topics"),
    ):
        for record, payload in zip(collected.records.get(kind, []), ir.get(kind, [])):
            parent = dig(record, PARENT)
            if not parent:
                continue
            entry = dict(payload)
            identity = dig(record, SELF) or ""
            entry["ref"] = entry.get("ref") or dig(record, DISPLAY) or identity
            entry["source_ref"] = identity
            if kind is EntityKind.TOOL:
                # Every source tool starts unresolved. `resolve_agent_tools`
                # only touches tools flagged for review, so a tool that arrived
                # claiming to be fine would silently skip the lookup entirely.
                entry["review_required"] = True
                entry["confidence"] = 0.0
            if kind is EntityKind.KNOWLEDGE:
                entry["file_path"] = document_path(Path(solution), record)
            owned.setdefault(parent, {}).setdefault(slot, []).append(entry)

    agents: list[tuple[str, Agent]] = []
    for record, payload in zip(
        collected.records.get(EntityKind.AGENT, []), ir.get(EntityKind.AGENT, [])
    ):
        key = dig(record, AGENT_SELF)
        if not key:
            continue
        composed = dict(payload)
        composed.update(owned.get(key, {}))
        composed["collaborators"] = [
            {
                "ref": dig(c, COLLABORATOR_TARGET) or dig(c, SELF) or "",
                "source_ref": dig(c, SELF) or "",
                "notes": dig(c, ("botcomponent", "description")),
            }
            for c in (record.get("collaborators") or [])
        ]
        result = runtime.to_ir(EntityKind.AGENT, composed)
        if not result.ok:
            report(Event("compose", f"{key}: invalid IR -- {result.errors}", "error"))
            continue
        agents.append((key, result.model))
    return agents


def document_path(solution: Path, record: dict) -> str | None:
    """Absolute path to a knowledge source's document inside the export.

    Orchestrate's built-in knowledge base indexes documents you hand it, so a
    knowledge source only migrates if its file actually shipped. Copilot writes
    the file name into the component XML and the bytes into a `filedata/`
    directory beside it.
    """
    name = dig(record, FILE_NAME)
    if not name:
        return None
    matches = list(solution.rglob(f"filedata/{name}"))
    return str(matches[0].resolve()) if matches else None


def select_agents(
    agents: list[tuple[str, Agent]], wanted: set[str] | None
) -> tuple[list[tuple[str, Agent]], list[str]]:
    """Narrow to the chosen agents, plus every agent they delegate to.

    Pulling in collaborators is not a liberty: an Orchestrate agent naming a
    collaborator that does not exist fails its own import. Picking a supervisor
    and not its two workers is a request for a broken agent, so the closure is
    taken and the additions are reported rather than made quietly.
    """
    if wanted is None:
        return agents, []

    by_key = {key: agent for key, agent in agents}
    by_name = {agent.name: key for key, agent in agents}
    keep: set[str] = set()
    queue = [k for k in (by_name.get(w, w) for w in wanted) if k in by_key]
    while queue:
        key = queue.pop()
        if key in keep:
            continue
        keep.add(key)
        for collaborator in by_key[key].collaborators:
            target = collaborator.ref if collaborator.ref in by_key else by_name.get(collaborator.ref)
            if target and target not in keep:
                queue.append(target)

    chosen = [(key, agent) for key, agent in agents if key in keep]
    asked = {by_name.get(w, w) for w in wanted}
    pulled = [agent.name for key, agent in chosen if key not in asked]
    return chosen, pulled


# ----------------------------------------------------------------------
# 3. Tool lookup
# ----------------------------------------------------------------------


def resolve_tools(
    agents: list[tuple[str, Agent]],
    installed: list,
    marketplace: list,
    provider: LLMProvider | None,
    report: Reporter = silent,
) -> None:
    if provider is None:
        report(Event("lookup", "no model provider: candidates suggested, nothing chosen", "warn"))
    for key, agent in agents:
        if not agent.tools:
            continue
        resolve_agent_tools(agent, installed, provider, marketplace)
        for tool in agent.tools:
            bridge = tool.bridge.value if tool.bridge else "-"
            report(
                Event(
                    "lookup",
                    f"{agent.name}: {tool.source_ref or '?'} -> {tool.ref} "
                    f"(confidence {tool.confidence:.2f}, {bridge})",
                )
            )


def read_connections(instance_url: str, token: str, report: Reporter = silent) -> list[AppConnection]:
    try:
        return list_applications(instance_url, token)
    except Exception as exc:  # noqa: BLE001 - unknown is a gap to report, not a failure
        report(Event("logins", f"could not read connections ({type(exc).__name__})", "warn"))
        return []


# ----------------------------------------------------------------------
# 4. Migrate
# ----------------------------------------------------------------------


def _model_reasoning(agent: Agent, chosen: str, live: bool) -> list[str]:
    """Why this model, in the words the matrix used.

    The choice is the least inspectable thing a migration makes -- a source
    model name goes in, an unfamiliar target id comes out -- and "trust me" is
    not good enough for the component that decides what every migrated agent
    runs on. So the runner-up and the matrix's own rationale are reported.

    Best effort: a matrix failure already falls back to the static table, and
    failing to *explain* a choice must not undo it.
    """
    source = agent.model_hint or "no model named by the source"
    if not live:
        return [f"{agent.name}: {source} -> {chosen} (static map; tenant list unavailable)"]
    try:
        from wheatear.model_matrix import recommend
        from wheatear.model_matrix.target_sources.orchestrate import OrchestrateModelSource

        result = recommend(agent.model_hint, OrchestrateModelSource())
    except Exception:  # noqa: BLE001 - explaining is optional, choosing is not
        return [f"{agent.name}: {source} -> {chosen}"]

    ranked = result.ranked_candidates or []
    if not ranked:
        return [f"{agent.name}: {source} -> {chosen}"]

    best = ranked[0]
    lines = [
        f"{agent.name}: {source} -> {chosen} "
        f"(score {best.score:.2f}, confidence {best.confidence:.2f})"
    ]
    if best.rationale:
        lines.append(f"    why: {' '.join(str(best.rationale).split())[:220]}")
    if len(ranked) > 1:
        runners = ", ".join(f"{c.raw_id} ({c.score:.2f})" for c in ranked[1:3])
        lines.append(f"    over: {runners}")
    if result.review_required:
        lines.append("    the matrix flagged this for a human to confirm")
    return lines


def adk_cli() -> str:
    """The ADK CLI to import with.

    Resolved through the model matrix's locator rather than assumed to be on
    PATH: the ADK is a declared dependency of this project, so in a virtualenv
    it sits at `<venv>/bin/orchestrate` -- which a process launched as
    `.venv/bin/python` has nowhere on PATH. Assuming `"orchestrate"` there
    fails every import with "command not found" on exactly the setup this
    project documents.
    """
    from wheatear.model_matrix.target_sources.orchestrate import find_cli

    return find_cli() or "orchestrate"


def deploy_spec(path: Path, what: str, orchestrate: str, report: Reporter = silent) -> bool:
    """Import one spec, and report whether it actually landed.

    The exit code alone is not the answer. `knowledge-bases import` exits 0
    after printing "Failed to create knowledge base", so trusting the status
    reports a successful migration of something that does not exist -- the
    worst failure mode available to a tool like this. The output is read too.
    """
    try:
        result = subprocess.run(
            [orchestrate, what, "import", "-f", str(path)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report(Event("import", f"{path.name}: could not run {orchestrate} ({exc})", "error"))
        return False
    lines = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if line.strip() and "WARNING" not in line
    ]
    failed = result.returncode != 0 or any("[ERROR]" in line for line in lines)
    for line in lines[-3:]:
        report(Event("import", f"| {line}", "error" if failed else "info"))
    # The ADK's session token is short-lived and expires mid-migration often
    # enough to be worth naming. Its own message says what to run but is buried
    # in whatever else the import printed, and the failure otherwise reads as a
    # problem with the spec rather than with the login.
    if failed and any("missing or expired" in line for line in lines):
        report(
            Event(
                "import",
                "That is an expired ADK session, not a bad spec. Run "
                "`orchestrate env activate <env> --api-key ...` and migrate again.",
                "warn",
            )
        )
    return not failed


def migrate_solution(
    solution: Path,
    out: Path,
    *,
    store: FoundryStore,
    client: Any | None = None,
    provider: LLMProvider | None = None,
    marketplace: list | None = None,
    instance_url: str | None = None,
    token: str | None = None,
    api_key: str | None = None,
    orchestrate_cli: str | None = None,
    suffix: str = "",
    on_conflict: Literal["rename", "update", "skip"] = "rename",
    dry_run: bool = False,
    only: set[str] | None = None,
    report: Reporter = silent,
) -> MigrationReport:
    """Run the whole corridor and say what happened.

    `client` is an `OrchestrateRestClient` or None. Without one the run still
    works: tools are matched against the shipped catalog snapshot only, no
    connection state is known, and nothing is imported.
    """
    solution, out = Path(solution), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    orchestrate_cli = orchestrate_cli or adk_cli()
    result = MigrationReport(output_dir=out)

    # -- 1. Scan + IR ---------------------------------------------------
    collected = scan_solution(solution)
    for kind, records in sorted(collected.records.items(), key=lambda kv: kv[0].value):
        report(Event("scan", f"{kind.value}: {len(records)} record(s)"))
    if collected.unclassified:
        report(Event("scan", f"unclassified: {collected.unclassified[:5]}", "warn"))
    ir = records_to_ir(store, collected, report)

    # -- 2. Compose -----------------------------------------------------
    agents = compose_agents(collected, ir, solution, report)
    agents, pulled_in = select_agents(agents, only)
    if pulled_in:
        report(
            Event(
                "compose",
                f"pulled in {len(pulled_in)} collaborator(s) the selection delegates to: "
                + ", ".join(pulled_in),
                "warn",
            )
        )
    for _, agent in agents:
        report(
            Event(
                "compose",
                f"{agent.name}: {len(agent.tools)} tool(s), {len(agent.knowledge)} knowledge, "
                f"{len(agent.topics)} topic(s), {len(agent.collaborators)} collaborator(s)",
            )
        )
    if not agents:
        result.notes.append("Nothing to migrate: the export produced no valid agents.")
        return result

    # -- 3. Tool lookup -------------------------------------------------
    installed = build_catalog(client.list_all_tools()) if client is not None else []
    marketplace = (
        marketplace
        if marketplace is not None
        else build_marketplace_catalog(tools_only(load_snapshot()))
    )
    result.installed_pool, result.catalog_pool = len(installed), len(marketplace)
    report(
        Event("lookup", f"pools: {len(installed)} installed on the instance, {len(marketplace)} in the catalog")
    )
    resolve_tools(agents, installed, marketplace, provider, report)

    # -- 3b. MCP servers: point at what exists, never reconfigure it ----
    mcp_servers: list[McpServer] = find_mcp_servers(solution)
    mcp_plans: list[McpPlan] = []
    if not mcp_servers:
        report(
            Event(
                "mcp",
                "none declared by this solution -- its integrations are Power Platform "
                "connectors, so their operations were matched to tools the target has.",
            )
        )
    else:
        toolkits = []
        if client is not None:
            try:
                toolkits = client.list_toolkits()
            except Exception as exc:  # noqa: BLE001 - unknown toolkits, so assume none
                report(Event("mcp", f"could not read target toolkits ({type(exc).__name__})", "warn"))
        mcp_plans = plan_servers(mcp_servers, toolkits)
        for plan in mcp_plans:
            report(Event("mcp", f"[{plan.action}] {plan.server.name}: {plan.reason}"))

    # What the source knew about each tool, bound to the tool that answers for
    # it now. The target's MCP server is left exactly as it is -- what moves is
    # the operating knowledge written around it, which nothing else carries.
    for _, agent in agents:
        carried = carry_tool_context(agent)
        if carried:
            agent.guidelines.extend(carried)
            report(Event("mcp", f"{agent.name}: carried {len(carried)} tool guideline(s) from the source"))

    # -- 3c. Logins -----------------------------------------------------
    connections: list[AppConnection] = []
    if client is not None and instance_url and token:
        connections = read_connections(instance_url, token, report)

    # -- 4. Descriptions ------------------------------------------------
    for _, agent in agents:
        had = bool(agent.description)
        if provider is not None and not had:
            describe_agent(agent, provider)
        origin = "source" if had else ("written from its instructions" if agent.description else "missing")
        report(Event("describe", f"{agent.name} [{origin}]: {(agent.description or '')[:90]}"))

    # -- 5. Names, then YAML --------------------------------------------
    # Names have to be settled before any YAML is written: a collaborator is
    # referenced by the name its target was deployed under, and nothing on
    # Orchestrate has ever heard of a Copilot bot schema name.
    existing: set[str] = set()
    if client is not None and on_conflict != "update":
        try:
            existing = {str(a.get("name")) for a in client.list_agents() if a.get("name")}
        except Exception as exc:  # noqa: BLE001 - unknown names, so assume none are taken
            report(Event("export", f"could not read existing agent names ({type(exc).__name__})", "warn"))

    deployed_names: dict[str, str] = {}
    skipped: set[str] = set()
    for key, agent in agents:
        wanted = slug(agent.name, suffix)
        if wanted in existing:
            if on_conflict == "skip":
                report(Event("export", f"{wanted}: already on the target; skipping", "warn"))
                skipped.add(key)
                continue
            chosen = free_name(wanted, existing | set(deployed_names.values()))
            if chosen != wanted:
                report(Event("export", f"{wanted}: already on the target; importing as {chosen}", "warn"))
            wanted = chosen
        deployed_names[key] = wanted
    agents = [(key, agent) for key, agent in agents if key not in skipped]

    # Everything from here on -- the model list, every import -- goes through
    # the ADK, which holds its own short-lived token separate from the API key.
    # It expires on its own schedule, and when it does a migration writes all
    # its YAML and lands nothing. We hold the key that can refresh it, so
    # asking a person to go and do that by hand is a choice, not a necessity.
    if client is not None and api_key:
        from wheatear.connectors.orchestrate.adk_session import ensure_session, session_is_live

        if not session_is_live(orchestrate_cli):
            try:
                name = ensure_session(instance_url or "", api_key, orchestrate_cli)
                report(Event("export", f"refreshed the ADK session for '{name}'", "ok"))
            except Exception as exc:  # noqa: BLE001 - reported; the import will say more
                report(Event("export", f"could not refresh the ADK session: {exc}", "warn"))

    # The model matrix ranks the source model's capabilities against what this
    # tenant actually allows, which needs a live ADK session. Probed once and
    # reported, because the failure is silent by design: `target_model_for`
    # falls back to a static table rather than refusing, and an operator whose
    # ADK token quietly expired would have no way to tell a tenant-checked
    # choice from a guess.
    live_models = client is not None
    if live_models:
        try:
            from wheatear.model_matrix.target_sources.orchestrate import OrchestrateModelSource

            allowed = OrchestrateModelSource().list_available_models()
            report(Event("export", f"{len(allowed)} model(s) allowed on this tenant"))
        except Exception as exc:  # noqa: BLE001 - a stale session is a warning, not a stop
            live_models = False
            report(
                Event(
                    "export",
                    "could not read this tenant's model list "
                    f"({' '.join(str(exc).split())[:160]}). Falling back to the static "
                    "model map, which cannot know what your account allows -- run "
                    "`orchestrate env activate <name>` and re-run for a checked choice.",
                    "warn",
                )
            )

    knowledge_specs: list[tuple[str, Path]] = []
    plans: list[tuple[Agent, AgentOutcome]] = []
    for key, agent in agents:
        agent.name = deployed_names[key]
        unresolved = [c.ref for c in agent.collaborators if c.ref not in deployed_names]
        agent.collaborators = [c for c in agent.collaborators if c.ref in deployed_names]
        for collaborator in agent.collaborators:
            collaborator.ref = deployed_names[collaborator.ref]
        if unresolved:
            agent.translation_notes.append(
                f"Delegated to {', '.join(sorted(unresolved))}, which was not in this export."
            )

        for source in agent.knowledge:
            source.ref = slug(source.ref, suffix)

        llm = target_model_for(agent, live=live_models)
        for line in _model_reasoning(agent, llm, live_models):
            report(Event("export", line))
        directory = out / agent.name
        written = export_agent(agent, directory, llm)
        carried, dropped = importable_tools(agent)

        outcome = AgentOutcome(
            source_key=key,
            name=agent.name,
            llm=llm,
            tools=list(carried),
            dropped=[t.source_ref or t.ref for t in dropped],
            knowledge=[k.ref for k in agent.knowledge if k.file_path],
            collaborators=[c.ref for c in agent.collaborators],
            agent_path=written.agent_path,
            review_path=written.review_manifest_path,
        )
        report(
            Event(
                "export",
                f"{agent.name}: llm={llm}, tools={carried or '[]'}"
                + (f", dropped {len(dropped)}" if dropped else ""),
            )
        )

        for spec in knowledge_base_specs(agent):
            path = directory / f"knowledge-{spec['name']}.yaml"
            path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
            knowledge_specs.append((spec["name"], path))
        plans.append((agent, outcome))
        result.agents.append(outcome)

    (out / "ir-agents.json").write_text(
        json.dumps([a.model_dump(mode="json") for _, a in agents], indent=2)
    )

    # -- What is left for a person --------------------------------------
    result.manual_steps = (
        install_steps(agents) + toolkit_steps(mcp_plans) + login_steps(agents, connections)
    )
    result.pending = pending_installs(agents)

    if dry_run:
        result.notes.append(f"Dry run: wrote {out}, imported nothing.")
        return result

    # -- 6. Import ------------------------------------------------------
    landed: set[str] = set()
    for name, path in knowledge_specs:
        report(Event("import", f"knowledge base {name}"))
        if deploy_spec(path, "knowledge-bases", orchestrate_cli, report):
            landed.add(name)
            result.knowledge_bases.append(name)

    # An agent naming a knowledge base that failed to create fails its own
    # import. Rewritten here rather than earlier because whether the base
    # exists is only known once the attempt has been made.
    for agent, outcome in plans:
        spec = yaml.safe_load(outcome.agent_path.read_text())
        wanted_bases = spec.get("knowledge_base") or []
        kept = [ref for ref in wanted_bases if ref in landed]
        if kept != wanted_bases:
            missing = sorted(set(wanted_bases) - landed)
            report(Event("import", f"{agent.name}: dropping knowledge base(s) that did not land: {missing}", "warn"))
            outcome.knowledge = kept
            if kept:
                spec["knowledge_base"] = kept
            else:
                spec.pop("knowledge_base", None)
            outcome.agent_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))

    # Collaborators must exist before the agent that names them.
    plans.sort(key=lambda plan: len(plan[0].collaborators))
    for agent, outcome in plans:
        report(Event("import", f"agent {agent.name}"))
        ok = deploy_spec(outcome.agent_path, "agents", orchestrate_cli, report)
        outcome.deployed = ok
        outcome.detail = "Imported" if ok else "Import failed -- see the log above"
        report(Event("import", f"{agent.name}: {'ok' if ok else 'FAILED'}", "ok" if ok else "error"))

    # Read the ids back once, after everything has landed. Cheaper than a
    # lookup per agent, and it is the only way to point a person at what was
    # just created rather than merely telling them its name.
    if client is not None and any(a.deployed for a in result.agents):
        try:
            by_name = {str(a.get("name")): str(a.get("id") or "") for a in client.list_agents()}
        except Exception as exc:  # noqa: BLE001 - no ids is less detail, not a failure
            report(Event("import", f"could not read agent ids ({type(exc).__name__})", "warn"))
        else:
            for outcome in result.agents:
                outcome.agent_id = by_name.get(outcome.name, "")

    return result

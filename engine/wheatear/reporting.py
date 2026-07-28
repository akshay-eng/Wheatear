"""What the migration did, written down for the three people who need to know.

A migration produces one set of facts and three audiences, and collapsing them
into one document serves none of them:

  MIGRATION-REPORT.md    the engineer who has to finish or debug it. Every
                         agent, every tool, every mapping decision, every
                         thing left undone, with paths.
  EXECUTIVE-SUMMARY.md   the person paying for it. What moved, what it costs
                         to finish, and what the risks are -- no identifiers.
  EVALUATION.md          whoever has to prove it works. Concrete prompts to
                         type at each migrated agent, with what a correct
                         answer looks like, derived from the tools that agent
                         actually ended up with.

The evaluation file is the one worth arguing for. A migration that reports
success is a claim; a list of questions whose answers can be checked against
the source system is how the claim gets tested. Its cases are generated from
the tools each agent *actually landed with*, not from the source's intent --
so an agent that lost a tool produces no case for it, and the gap is visible
as a missing test rather than hidden behind a green tick.

Nothing here prints or calls a model. It takes the report objects the corridors
already produce and returns markdown, so both corridors and the tests use the
same code and the CLI can write these without a terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPORT_FILE = "MIGRATION-REPORT.md"
SUMMARY_FILE = "EXECUTIVE-SUMMARY.md"
EVALUATION_FILE = "EVALUATION.md"


@dataclass
class ToolFact:
    """One tool as it ended up on the target."""

    name: str
    origin: str = ""  # "rebuilt from HTTP", "MCP toolkit", "catalog", ...
    detail: str = ""
    parameters: list[str] = field(default_factory=list)


@dataclass
class AgentFact:
    """One migrated agent, flattened out of whichever report produced it."""

    name: str
    deployed: bool | None = None
    agent_id: str = ""
    llm: str = ""
    description: str = ""
    tools: list[ToolFact] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    instructions: str = ""

    @property
    def status(self) -> str:
        if self.deployed is None:
            return "not deployed (dry run)"
        return "deployed" if self.deployed else "failed"


@dataclass
class MigrationFacts:
    """Everything the three documents are rendered from."""

    source_platform: str
    target_platform: str = "IBM watsonx Orchestrate"
    source_label: str = ""
    target_instance: str = ""
    agents: list[AgentFact] = field(default_factory=list)
    manual_steps: list[tuple[str, str, str, bool]] = field(default_factory=list)
    connections: list[tuple[str, str, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    model_calls: int = 0
    model_tokens: int = 0
    generated_at: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @property
    def deployed(self) -> list[AgentFact]:
        return [a for a in self.agents if a.deployed]

    @property
    def failed(self) -> list[AgentFact]:
        return [a for a in self.agents if a.deployed is False]

    @property
    def total_tools(self) -> int:
        return sum(len(a.tools) for a in self.agents)

    @property
    def blocking_steps(self) -> list[tuple[str, str, str, bool]]:
        return [s for s in self.manual_steps if s[3]]


# --------------------------------------------------------------------------- #
# Adapters: corridor report objects -> MigrationFacts
# --------------------------------------------------------------------------- #

def _tool_origin(name: str) -> tuple[str, str]:
    """Classify a tool by the shape of its name, which is all a report keeps."""
    if ":" in name:
        toolkit = name.split(":", 1)[0]
        return "MCP toolkit", f"operation on the `{toolkit}` toolkit"
    return "catalog or rebuilt", ""


def facts_from_solution_report(
    report: Any,
    source_platform: str = "Microsoft Copilot Studio",
    source_label: str = "",
    target_instance: str = "",
    meter: Any | None = None,
) -> MigrationFacts:
    """Flatten `pipeline.solution_migration.MigrationReport`."""
    facts = MigrationFacts(
        source_platform=source_platform,
        source_label=source_label,
        target_instance=target_instance,
        output_dir=getattr(report, "output_dir", None),
    )
    for outcome in getattr(report, "agents", []):
        tools = []
        for name in outcome.tools:
            origin, detail = _tool_origin(name)
            tools.append(ToolFact(name=name, origin=origin, detail=detail))
        facts.agents.append(
            AgentFact(
                name=outcome.name,
                deployed=outcome.deployed,
                agent_id=getattr(outcome, "agent_id", "") or "",
                llm=getattr(outcome, "llm", "") or "",
                tools=tools,
                knowledge=list(getattr(outcome, "knowledge", []) or []),
                collaborators=list(getattr(outcome, "collaborators", []) or []),
                dropped=list(getattr(outcome, "dropped", []) or []),
                warnings=[outcome.detail] if getattr(outcome, "detail", "") else [],
            )
        )
    for step in getattr(report, "manual_steps", []):
        facts.manual_steps.append(
            (step.title, step.detail, step.where, bool(getattr(step, "blocking", True)))
        )
    facts.notes = list(getattr(report, "notes", []) or [])
    _attach_meter(facts, meter)
    return facts


def facts_from_deploy_reports(
    reports: list,
    results_by_name: dict | None = None,
    source_platform: str = "n8n",
    source_label: str = "",
    target_instance: str = "",
    meter: Any | None = None,
    output_dir: Path | None = None,
) -> MigrationFacts:
    """Flatten `connectors.orchestrate.provisioner.AgentDeployReport` list.

    `results_by_name` is optional and adds the source-side detail the deploy
    report does not carry -- an HTTP tool's parameters and endpoint -- which is
    exactly what makes the evaluation file specific rather than generic.
    """
    facts = MigrationFacts(
        source_platform=source_platform,
        source_label=source_label,
        target_instance=target_instance,
        output_dir=output_dir,
    )
    for report in reports:
        specs = {}
        result = (results_by_name or {}).get(report.name)
        for spec in getattr(result, "endpoint_tools", []) or []:
            specs[spec.operation_id()] = spec

        tools = []
        for name in report.tools:
            spec = specs.get(name)
            if spec is not None:
                tools.append(
                    ToolFact(
                        name=name,
                        origin="rebuilt from an HTTP request tool",
                        detail=f"{spec.method} {spec.endpoint}",
                        parameters=[p.name for p in spec.params],
                    )
                )
            else:
                origin, detail = _tool_origin(name)
                tools.append(ToolFact(name=name, origin=origin, detail=detail))

        warnings: list[str] = []
        if getattr(report, "error", ""):
            warnings.append(report.error)
        validation = getattr(report, "validation", "") or ""
        if "|" in validation:
            warnings.extend(w.strip() for w in validation.split("|", 1)[1].split(";") if w.strip())

        agent = getattr(result, "agent", None)
        facts.agents.append(
            AgentFact(
                name=report.name,
                deployed=bool(report.ok),
                agent_id=getattr(report, "agent_id", "") or "",
                description=getattr(report, "description", "") or "",
                tools=tools,
                knowledge=list(getattr(report, "knowledge", []) or []),
                collaborators=list(getattr(report, "collaborators", []) or []),
                warnings=warnings,
                instructions=(
                    getattr(agent, "instructions", "") or getattr(agent, "existing_instructions", "") or ""
                ),
            )
        )
        for warning in warnings:
            facts.manual_steps.append((warning, "", "watsonx Orchestrate console", True))
    _attach_meter(facts, meter)
    return facts


def _attach_meter(facts: MigrationFacts, meter: Any | None) -> None:
    if meter is None:
        return
    facts.model_calls = int(getattr(meter, "total_calls", 0) or 0)
    facts.model_tokens = int(getattr(meter, "total_tokens", 0) or 0)


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #

def _bullet(items: list[str], empty: str = "_none_") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_report(facts: MigrationFacts) -> str:
    """The engineer's document: everything, with identifiers and paths."""
    out: list[str] = []
    a = out.append
    a(f"# Migration report — {facts.source_platform} → {facts.target_platform}")
    a("")
    a(f"_Generated {facts.generated_at}_")
    a("")
    if facts.source_label:
        a(f"- **Source**: {facts.source_label}")
    if facts.target_instance:
        a(f"- **Target instance**: `{facts.target_instance}`")
    a(f"- **Agents migrated**: {len(facts.deployed)} of {len(facts.agents)}")
    a(f"- **Tools attached**: {facts.total_tools}")
    if facts.model_calls:
        a(f"- **Model usage**: {facts.model_calls} call(s), {facts.model_tokens:,} tokens")
    if facts.output_dir:
        a(f"- **Artifacts**: `{facts.output_dir}`")
    a("")

    a("## What moved")
    a("")
    a("| Agent | Status | Tools | Knowledge | Delegates to |")
    a("|---|---|---|---|---|")
    for agent in facts.agents:
        a(
            f"| {agent.name} | {agent.status} | {len(agent.tools)} | "
            f"{len(agent.knowledge)} | {', '.join(agent.collaborators) or '—'} |"
        )
    a("")

    a("## Agent detail")
    a("")
    for agent in facts.agents:
        a(f"### {agent.name}")
        a("")
        if agent.agent_id:
            a(f"- **Target id**: `{agent.agent_id}`")
        if agent.llm:
            a(f"- **Model**: `{agent.llm}`")
        if agent.description:
            a(f"- **Description**: {agent.description}")
        a(f"- **Status**: {agent.status}")
        a("")
        if agent.tools:
            a("**Tools**")
            a("")
            a("| Tool | How it was migrated | Detail | Parameters |")
            a("|---|---|---|---|")
            for tool in agent.tools:
                params = ", ".join(f"`{p}`" for p in tool.parameters) or "—"
                a(f"| `{tool.name}` | {tool.origin} | {tool.detail or '—'} | {params} |")
            a("")
        else:
            a("**Tools**: none attached")
            a("")
        if agent.knowledge:
            a(f"**Knowledge bases**: {', '.join(agent.knowledge)}")
            a("")
        if agent.collaborators:
            a(f"**Delegates to**: {', '.join(agent.collaborators)}")
            a("")
        if agent.dropped:
            a("**Not carried**")
            a("")
            a(_bullet(agent.dropped))
            a("")
        if agent.warnings:
            a("**Warnings**")
            a("")
            a(_bullet(agent.warnings))
            a("")

    a("## Still to do by hand")
    a("")
    if not facts.manual_steps:
        a("Nothing. Every tool, knowledge base and delegation edge the source used was "
          "rebuilt or attached on the target.")
    else:
        for title, detail, where, blocking in facts.manual_steps:
            mark = "**blocking**" if blocking else "optional"
            a(f"- {title} — {mark}")
            if detail:
                a(f"  - {detail}")
            if where:
                a(f"  - Where: {where}")
    a("")

    if facts.notes:
        a("## Notes from the run")
        a("")
        a(_bullet(facts.notes))
        a("")
    return "\n".join(out)


def render_summary(facts: MigrationFacts) -> str:
    """The business document: outcome and risk, no identifiers.

    Deliberately says what is *not* done as plainly as what is. A summary that
    reports only successes is the reason migrations get signed off and then
    discovered to be half-finished in production.
    """
    out: list[str] = []
    a = out.append
    total = len(facts.agents)
    done = len(facts.deployed)
    blocking = len(facts.blocking_steps)

    a(f"# Migration summary — {facts.source_platform} to {facts.target_platform}")
    a("")
    a(f"_{facts.generated_at}_")
    a("")

    a("## Outcome")
    a("")
    if total and done == total and not blocking:
        a(f"All **{total}** agents were migrated and are working on "
          f"{facts.target_platform}. No follow-up work is required.")
    elif done:
        a(f"**{done} of {total}** agents were migrated to {facts.target_platform}.")
        if blocking:
            a("")
            a(f"**{blocking} item(s) still need attention** before the migrated agents can do "
              "everything their originals did. These are listed below.")
    else:
        a(f"No agents were migrated. {total} were attempted.")
    a("")

    a("## What was migrated")
    a("")
    a("| Agent | Migrated | Capabilities carried over |")
    a("|---|---|---|")
    for agent in facts.agents:
        caps = []
        if agent.tools:
            caps.append(f"{len(agent.tools)} tool(s)")
        if agent.knowledge:
            caps.append(f"{len(agent.knowledge)} knowledge base(s)")
        if agent.collaborators:
            caps.append(f"delegates to {len(agent.collaborators)} agent(s)")
        a(f"| {agent.name} | {'Yes' if agent.deployed else 'No'} | {', '.join(caps) or 'none'} |")
    a("")

    a("## What still needs doing")
    a("")
    if not facts.manual_steps:
        a("Nothing.")
    else:
        seen: set[str] = set()
        for title, _detail, where, blocking in facts.manual_steps:
            plain = re.sub(r"`+", "", title)
            if plain in seen:
                continue
            seen.add(plain)
            a(f"- {plain}" + (f" ({where})" if where else ""))
    a("")

    a("## How to verify")
    a("")
    a("`EVALUATION.md` lists specific questions to ask each migrated agent, and what a "
      "correct answer looks like. Working through it is the fastest way to confirm the "
      "migration end to end.")
    a("")
    return "\n".join(out)


# Question templates keyed by what a tool's name suggests it does. Matched on
# the name because that is the one thing every corridor's report carries; the
# parameters, where known, make the question concrete.
_PROBES: list[tuple[str, str, str]] = [
    ("search", "Ask it to look something up by keyword",
     "It should call `{tool}` and quote a result, citing the record or article id."),
    ("get_", "Ask it for one specific record by its identifier",
     "It should call `{tool}` and return that record's real fields — not a summary it invented."),
    ("list", "Ask it to list or filter records matching a condition",
     "It should call `{tool}` and return real identifiers you can check in the source system."),
    ("compare", "Ask it to compare two named items",
     "It should call `{tool}` once with both, not answer from memory."),
    ("schema", "Ask what fields are available",
     "It should call `{tool}` rather than guessing field names."),
    ("sql", "Ask a question needing an ad-hoc filter",
     "It should call `{tool}` with a read-only query."),
]


def _cases_for(agent: AgentFact) -> list[tuple[str, str, str]]:
    """Concrete checks for one agent, from the tools it actually has."""
    cases: list[tuple[str, str, str]] = []
    for tool in agent.tools:
        short = tool.name.split(":")[-1].lower()
        for needle, what, expect in _PROBES:
            if needle in short:
                params = ", ".join(tool.parameters)
                hint = f" (it takes: {params})" if params else ""
                cases.append((tool.name, what + hint, expect.format(tool=tool.name)))
                break
    if agent.collaborators:
        cases.append((
            "delegation",
            "Ask something that belongs to "
            + " or ".join(agent.collaborators)
            + ", and confirm it is answered rather than refused",
            "The supervisor should hand off and return the specialist's answer. A refusal "
            "means the delegation edge exists but the routing instructions do not cover it.",
        ))
    if not agent.tools and not agent.collaborators:
        cases.append((
            "no capability",
            "Ask it anything its original could do",
            "This agent migrated with no tools and no delegates, so it can only answer from "
            "its instructions. If the original could do more, that capability is missing.",
        ))
    return cases


def render_evaluation(facts: MigrationFacts) -> str:
    """The test plan: what to ask, and how to tell whether the answer is real."""
    out: list[str] = []
    a = out.append
    a(f"# Evaluation — migrated {facts.source_platform} agents")
    a("")
    a(f"_{facts.generated_at}_")
    a("")
    a("Ask each agent the questions below in the watsonx Orchestrate chat. The point of "
      "each one is not that the agent answers, but that it answers **from the tool** — "
      "so check the returned identifiers against the source system. An agent that "
      "answers plausibly without calling its tool has failed the check.")
    a("")
    a("> Tip: turn on **Show reasoning** in the chat panel to see which tool was called "
      "and with what arguments.")
    a("")

    for agent in facts.agents:
        a(f"## {agent.name}")
        a("")
        if not agent.deployed:
            a("_Not deployed — nothing to evaluate._")
            a("")
            continue
        if agent.tools:
            a(f"Tools available: {', '.join(f'`{t.name}`' for t in agent.tools)}")
            a("")
        cases = _cases_for(agent)
        if not cases:
            a("_No tool-backed capability to evaluate._")
            a("")
            continue
        for index, (subject, what, expect) in enumerate(cases, start=1):
            a(f"**{index}. {subject}**")
            a("")
            a(f"- Ask: {what}")
            a(f"- Expect: {expect}")
            a("")

    if facts.blocking_steps:
        a("## Known gaps — expect these to fail")
        a("")
        a("These were not finished by the migration, so a test that exercises them should "
          "fail until they are resolved:")
        a("")
        for title, _detail, _where, _blocking in facts.blocking_steps:
            a(f"- {re.sub(r'`+', '', title)}")
        a("")
    return "\n".join(out)


def write_all(facts: MigrationFacts, destination: Path) -> list[Path]:
    """Write all three documents, returning the paths written."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in (
        (REPORT_FILE, render_report(facts)),
        (SUMMARY_FILE, render_summary(facts)),
        (EVALUATION_FILE, render_evaluation(facts)),
    ):
        path = destination / name
        path.write_text(body)
        written.append(path)
    return written

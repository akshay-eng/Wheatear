"""Migrate a Copilot Studio solution export into watsonx Orchestrate.

A thin driver. Everything it does lives in `agent_liftoff.pipeline.solution_migration`,
because the wizard runs the same migration and two copies of a pipeline are how
the two start disagreeing about what a migration does. This file owns the
argument parsing and the printing, and nothing else.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_liftoff.foundry.store import FoundryStore
from agent_liftoff.pipeline.solution_migration import Event, MigrationReport, migrate_solution

STAGE_TITLES = {
    "scan": "1. SCAN + IR   (cached adapters, no model calls)",
    "ir": "1. SCAN + IR   (cached adapters, no model calls)",
    "compose": "2. COMPOSE",
    "lookup": "3. TOOL LOOKUP   (instance first, then the catalog snapshot)",
    "mcp": "3b. MCP SERVERS   (point at what exists; never reconfigure it)",
    "logins": "3c. LOGINS",
    "describe": "4. DESCRIPTIONS",
    "export": "5. MIGRATE",
    "import": "6. IMPORT",
}


def make_printer():
    """Print each stage's heading once, the first time that stage says anything."""
    seen: set[str] = set()

    def emit(event: Event) -> None:
        title = STAGE_TITLES.get(event.stage, event.stage)
        if title not in seen:
            seen.add(title)
            print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
        mark = {"warn": "  ! ", "error": "  x ", "ok": "  + "}.get(event.level, "    ")
        print(f"{mark}{event.text}")

    return emit


def print_manual_steps(result: MigrationReport) -> None:
    if not result.manual_steps:
        return
    print(f"\n{'=' * 74}\nSTILL TO DO ON THE TARGET\n{'=' * 74}")
    for n, step in enumerate(result.manual_steps, 1):
        flag = "REQUIRED" if step.blocking else "optional"
        print(f"\n  {n}. [{flag}] {step.title}")
        if step.agents:
            print(f"     for: {', '.join(step.agents)}")
        print(f"     where: {step.where}")
        print(f"     {step.detail}")
        if step.command:
            print(f"     run: {step.command}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("solution", type=Path)
    parser.add_argument("--store-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--suffix",
        default="",
        help="Appended to every migrated name. Empty by default: an agent should arrive "
        "called what the source called it, not branded by the thing that moved it.",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("rename", "update", "skip"),
        default="rename",
        help="What to do when the target already has an agent of that name. "
        "rename appends _2, _3; update overwrites it; skip leaves it alone.",
    )
    parser.add_argument("--orchestrate", default="orchestrate", help="Path to the ADK CLI.")
    parser.add_argument("--no-llm", action="store_true", help="Rank tools but choose none.")
    parser.add_argument("--dry-run", action="store_true", help="Write everything, import nothing.")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Migrate just this agent (repeatable). Collaborators are pulled in too.",
    )
    args = parser.parse_args()

    client = None
    instance_url, token = os.environ.get("IBMUrl"), None
    try:
        from agent_liftoff.connectors.orchestrate.rest_client import OrchestrateRestClient

        client = OrchestrateRestClient(os.environ["IBMKey"], os.environ["IBMUrl"])
        token = client._session.headers["Authorization"].split()[1]
    except Exception as exc:  # noqa: BLE001 - no instance is a smaller answer, not a failure
        print(f"  instance pool unavailable ({type(exc).__name__}); catalog only")

    provider = None
    if not args.no_llm:
        from agent_liftoff import creds
        from agent_liftoff.config import load_config
        from agent_liftoff.llm.factory import build_provider

        saved = load_config()
        if saved and saved.llm_provider:
            key = creds.load_secret(creds.llm_key_name(saved.llm_provider)) or os.environ.get(
                saved.llm_key_env
            )
            if key:
                provider = build_provider(saved.llm_provider, key, "gemini-2.5-flash")

    result = migrate_solution(
        args.solution,
        args.out,
        store=FoundryStore(args.store_root),
        client=client,
        provider=provider,
        instance_url=instance_url,
        token=token,
        api_key=os.environ.get("IBMKey"),
        orchestrate_cli=args.orchestrate,
        suffix=args.suffix,
        on_conflict=args.on_conflict,
        dry_run=args.dry_run,
        only=set(args.only) if args.only else None,
        report=make_printer(),
    )

    print_manual_steps(result)
    print(f"\n  {result.summary()}")
    for note in result.notes:
        print(f"  {note}")


if __name__ == "__main__":
    sys.exit(main())

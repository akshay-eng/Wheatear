"""Multi-agent workflow assembly and collaborator-graph discovery.

A real migration is rarely one agent: an orchestrator delegates to collaborator
agents, which may delegate further. To migrate the whole thing coherently we
must (1) discover the transitive collaborator closure of what the user picked,
(2) assemble those agents into a Workflow, and (3) emit them leaf-first so a
collaborator always exists on the target before the agent that references it.

The graph logic here is pure and deterministic (no I/O, no LLM) so it's fully
unit-tested; the wizard supplies the platform-specific "fetch this agent's
collaborators" callback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from wheatear.ir.schema import Agent, Workflow


def reachable_ids(seed: Iterable[str], neighbors: Callable[[str], list[str]]) -> list[str]:
    """Breadth-first transitive closure over a collaborator graph.

    `seed` is what the user selected; `neighbors(id)` returns the collaborator
    ids of one agent. Returns every reachable id in first-seen (stable) order,
    including the seeds. Safe on cycles -- each id is visited once.
    """
    order: list[str] = []
    seen: set[str] = set()
    queue: list[str] = []

    for s in seed:
        if s not in seen:
            seen.add(s)
            queue.append(s)
            order.append(s)

    i = 0
    while i < len(queue):
        current = queue[i]
        i += 1
        for nxt in neighbors(current):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
                order.append(nxt)
    return order


def assemble_workflow(
    agents: list[Agent], source_platform: str, root: str | None = None
) -> Workflow:
    """Bundle imported agents into a Workflow and flag any collaborator that
    points outside the bundle. A dangling collaborator reference (its target
    wasn't migrated) is marked review_required with a note rather than silently
    producing a broken reference on the target.
    """
    present = {a.name for a in agents}
    for agent in agents:
        for collab in agent.collaborators:
            if collab.ref not in present:
                collab.review_required = True
                collab.notes = (
                    f"Collaborator '{collab.ref}' was not part of this migration; "
                    "migrate it too or remove the reference on the target."
                )
    return Workflow(source_platform=source_platform, root=root, agents=agents)

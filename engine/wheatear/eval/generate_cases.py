"""Derive smoke-test conversation starters from the source agent's topics.

Deterministic, no LLM call: trigger phrases are already real example
utterances the source agent had to handle, so this stage just carries them
forward so the migrated agent can be checked against the same starting
points it was originally built to handle.
"""

from __future__ import annotations

from pydantic import BaseModel

from wheatear.ir.schema import Agent


class EvalCase(BaseModel):
    topic: str
    utterance: str


def generate_cases(agent: Agent) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for topic in agent.topics:
        for phrase in topic.trigger_phrases:
            cases.append(EvalCase(topic=topic.name, utterance=phrase))
    return cases

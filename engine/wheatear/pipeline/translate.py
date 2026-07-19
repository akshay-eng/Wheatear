"""AI-assisted Translate stage: dialog graph -> agent instructions.

The one pipeline stage that calls an LLM. Everything upstream (Normalize,
Map) is deterministic; everything downstream (Validate) is schema/eval
checking. This is where intent has to be understood, not just copied, so it
runs through a structured-output call rather than freeform generation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from wheatear.ir.schema import Agent, DialogNode
from wheatear.llm.base import LLMProvider


class TranslationOutput(BaseModel):
    instructions: str
    confidence: float = Field(ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


def _render_node(node: DialogNode, indent: int = 0) -> str:
    prefix = "  " * indent
    line = f"{prefix}- [{node.kind.value}] {node.text or ''}".rstrip()
    children = "\n".join(_render_node(child, indent + 1) for child in node.children)
    return f"{line}\n{children}" if children else line


def _render_topic_block(topic) -> str:
    triggers = ", ".join(topic.trigger_phrases) or "(no trigger phrases)"
    nodes = "\n".join(_render_node(n) for n in topic.nodes) or "(no nodes)"
    notes = "\n".join(f"  note: {n}" for n in topic.unsupported_notes)
    block = f"Topic: {topic.name}\nTriggers: {triggers}\n{nodes}"
    if notes:
        block += f"\n{notes}"
    return block


def _build_dialog_tree_prompt(agent: Agent) -> str:
    """The source agent has no generative system prompt of its own -- its
    real behavior IS the dialog graph, so reconstruct instructions from it.
    """
    topics_text = "\n\n".join(_render_topic_block(t) for t in agent.topics)

    return (
        "You are migrating a conversational agent from Microsoft Copilot Studio to IBM "
        "watsonx Orchestrate. Orchestrate agents are not dialog trees: they are a single "
        "`instructions` string an LLM follows at runtime, plus a list of tools it can call.\n\n"
        "Below is the source agent's topic/dialog graph. Write a single `instructions` "
        "string that preserves the agent's actual behavior and intent (what it should do, "
        "when, and how) without describing it as a flowchart. Be concrete about branching "
        "logic described in conditions. Note any place where you had to make an assumption "
        "or where information was lost in `notes`, and set `confidence` honestly (1.0 only "
        "if nothing was lossy or ambiguous).\n\n"
        f"Agent name: {agent.name}\n\n{topics_text}\n"
    )


def _build_generative_prompt(agent: Agent) -> str:
    """The source agent already has its own system prompt (a generative /
    GPT-orchestrated Copilot Studio agent). Reconstruction from scratch
    would throw away a prompt that's already close to what Orchestrate
    wants; this is a lighter adaptation task, not a synthesis task.
    """
    custom_topics = [t for t in agent.topics if not t.is_system_topic]
    custom_topics_text = (
        "\n\n".join(_render_topic_block(t) for t in custom_topics)
        if custom_topics
        else "(none -- every topic on this agent is default lifecycle scaffolding, not custom logic)"
    )
    system_topic_names = ", ".join(t.name for t in agent.topics if t.is_system_topic) or "(none)"

    return (
        "You are migrating a generative AI agent from Microsoft Copilot Studio to IBM "
        "watsonx Orchestrate. The source agent already has its own system prompt below -- "
        "your job is to adapt it for Orchestrate, not to reconstruct one from a dialog graph. "
        "Preserve every behavioral rule, guardrail, escalation condition, tone instruction, "
        "and source restriction exactly; do not soften, summarize away, or drop any of them. "
        "Only adjust platform-specific phrasing (e.g. references to Copilot Studio topics) so "
        "it reads correctly as an Orchestrate agent's instructions.\n\n"
        f"Agent name: {agent.name}\n\n"
        f"Existing system prompt:\n{agent.existing_instructions}\n\n"
        f"Default lifecycle topics present (Greeting/Fallback/etc., already implied by a "
        f"generative agent, not custom logic): {system_topic_names}\n\n"
        f"Custom (non-default) topics, if any -- fold any real behavior here into the "
        f"instructions too:\n{custom_topics_text}\n\n"
        "Set `confidence` honestly: this should usually be high, since you're adapting an "
        "existing well-formed prompt rather than inferring behavior from a dialog tree. Use "
        "`notes` only for things you genuinely had to change or couldn't preserve."
    )


def build_prompt(agent: Agent) -> str:
    if agent.existing_instructions:
        return _build_generative_prompt(agent)
    return _build_dialog_tree_prompt(agent)


def translate_agent(agent: Agent, provider: LLMProvider) -> Agent:
    """Populate instructions/translation_confidence/translation_notes on the
    IR Agent. Returns the same Agent object, mutated.
    """
    prompt = build_prompt(agent)
    result = provider.generate_structured(prompt, TranslationOutput)

    agent.instructions = result.instructions
    agent.translation_confidence = result.confidence
    agent.translation_notes = result.notes
    return agent


def deterministic_instructions(agent: Agent) -> Agent:
    """No-LLM fallback for Translate. The deterministic core must be able to
    migrate without any AI: a generative source agent already has a system
    prompt, which carries over verbatim (lossless). Only a pure dialog-tree
    agent with no prompt truly needs the LLM. Returns the same Agent, mutated.
    """
    if agent.existing_instructions:
        agent.instructions = agent.existing_instructions
        agent.translation_confidence = 1.0
        agent.translation_notes.append("Instructions carried over verbatim (deterministic, no LLM).")
    else:
        rendered = "\n\n".join(
            f"# {t.name}\n" + "\n".join(n.text for n in t.nodes if n.text)
            for t in agent.topics
            if not t.is_system_topic and any(n.text for n in t.nodes)
        )
        agent.instructions = rendered or f"{agent.name} agent."
        agent.translation_confidence = 0.7 if rendered else 0.5
        agent.translation_notes.append(
            "Instructions assembled deterministically from topics without an LLM; "
            "run with an LLM provider for a higher-fidelity translation."
        )
    return agent

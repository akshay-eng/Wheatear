from pathlib import Path

from wheatear.connectors.copilot_studio.importer import import_agent
from wheatear.pipeline.translate import TranslationOutput, build_prompt, translate_agent

FIXTURE_DIR = Path(__file__).parent.parent / "wheatear" / "connectors" / "copilot_studio" / "fixtures" / "sample_agent"


class FakeProvider:
    """Implements LLMProvider without touching a network or a vendor SDK."""

    def __init__(self, response: TranslationOutput):
        self.response = response
        self.last_prompt: str | None = None

    def generate_structured(self, prompt: str, schema):
        self.last_prompt = prompt
        assert schema is TranslationOutput
        return self.response


def _sample_agent():
    return import_agent(FIXTURE_DIR).agent


def test_build_prompt_includes_topic_names_and_triggers():
    agent = _sample_agent()
    prompt = build_prompt(agent)
    assert "greeting" in prompt
    assert "hi" in prompt
    assert "order_status" in prompt
    assert "where is my order" in prompt


def test_build_prompt_includes_dialog_node_content():
    agent = _sample_agent()
    prompt = build_prompt(agent)
    assert "how can I help you today" in prompt
    assert "What's your order ID" in prompt


def test_translate_agent_writes_instructions_and_confidence():
    agent = _sample_agent()
    fake = FakeProvider(
        TranslationOutput(
            instructions="Greet the user, then look up their order status if asked.",
            confidence=0.85,
            notes=["Collapsed the delayed/on-track branch into a conditional instruction."],
        )
    )

    result = translate_agent(agent, fake)

    assert result is agent  # mutates and returns the same object
    assert agent.instructions == "Greet the user, then look up their order status if asked."
    assert agent.translation_confidence == 0.85
    assert agent.translation_notes == ["Collapsed the delayed/on-track branch into a conditional instruction."]
    assert fake.last_prompt is not None and "order_status" in fake.last_prompt


def test_translate_agent_flags_low_confidence_for_downstream_review():
    agent = _sample_agent()
    fake = FakeProvider(TranslationOutput(instructions="Best guess instructions.", confidence=0.4, notes=["Lots of ambiguity."]))

    translate_agent(agent, fake)

    assert agent.needs_review is True


SOLUTION_FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "wheatear"
    / "connectors"
    / "copilot_studio"
    / "fixtures"
    / "sample_solution_agent"
)


def _generative_agent():
    return import_agent(SOLUTION_FIXTURE_DIR).agent


def test_build_prompt_adapts_existing_instructions_for_generative_agents():
    agent = _generative_agent()
    prompt = build_prompt(agent)

    assert "adapt it for Orchestrate" in prompt
    assert "IT Help Bot" in prompt
    assert "Escalate using the Escalate topic" in prompt or "escalate" in prompt.lower()


def test_build_prompt_excludes_system_topics_from_dialog_tree_dump():
    """System topics shouldn't get the full dialog-graph treatment when an
    existing system prompt is present -- they're listed by name only.
    """
    agent = _generative_agent()
    prompt = build_prompt(agent)

    # Greeting is a system topic: name appears in the summary list, but its
    # internal trigger phrases/nodes should not be dumped in full.
    assert "Greeting" in prompt
    assert "Good morning" not in prompt  # a Greeting trigger phrase, not surfaced


def test_build_prompt_still_surfaces_custom_topics_on_generative_agents():
    agent = _generative_agent()
    prompt = build_prompt(agent)

    assert "PasswordReset" in prompt
    assert "username" in prompt.lower()


def test_build_prompt_uses_dialog_tree_path_when_no_existing_instructions():
    """Sanity check that the branch logic actually depends on
    existing_instructions, not just on which fixture happens to be loaded.
    """
    agent = _sample_agent()
    assert agent.existing_instructions is None
    prompt = build_prompt(agent)
    assert "reconstruct" not in prompt.lower()
    assert "dialog graph" in prompt

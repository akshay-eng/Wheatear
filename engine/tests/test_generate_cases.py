from pathlib import Path

from wheatear.connectors.copilot_studio.importer import import_agent
from wheatear.eval.generate_cases import generate_cases

FIXTURE_DIR = Path(__file__).parent.parent / "wheatear" / "connectors" / "copilot_studio" / "fixtures" / "sample_agent"


def test_generate_cases_uses_trigger_phrases_from_every_topic():
    agent = import_agent(FIXTURE_DIR).agent

    cases = generate_cases(agent)

    greeting_utterances = {c.utterance for c in cases if c.topic == "greeting"}
    order_utterances = {c.utterance for c in cases if c.topic == "order_status"}
    assert greeting_utterances == {"hi", "hello", "hey there"}
    assert order_utterances == {"where is my order", "order status"}


def test_generate_cases_produces_nothing_for_topic_with_no_triggers():
    from wheatear.ir.schema import Agent, Topic

    agent = Agent(name="x", source_platform="copilot-studio", topics=[Topic(name="empty", trigger_phrases=[])])

    cases = generate_cases(agent)

    assert cases == []

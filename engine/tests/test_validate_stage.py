from wheatear.ir.schema import Agent, ConnectionRef, ToolRef
from wheatear.pipeline.validate import validate_agent


def make_agent(**overrides) -> Agent:
    defaults = dict(name="support_router", source_platform="copilot-studio", instructions="Route tickets.")
    defaults.update(overrides)
    return Agent(**defaults)


def test_valid_agent_has_no_errors():
    agent = make_agent()
    result = validate_agent(agent)
    assert result.is_valid
    assert result.errors == []


def test_empty_name_is_an_error():
    agent = make_agent(name="  ")
    result = validate_agent(agent)
    assert not result.is_valid
    assert any(i.field == "name" for i in result.errors)


def test_missing_instructions_is_an_error():
    agent = make_agent(instructions="")
    result = validate_agent(agent)
    assert not result.is_valid
    assert any(i.field == "instructions" for i in result.errors)


def test_review_required_tool_is_a_warning_not_an_error():
    agent = make_agent(tools=[ToolRef(ref="legacy_flow", review_required=True)])
    result = validate_agent(agent)
    assert result.is_valid  # warnings don't block producing output
    assert any(i.field == "tools" and i.severity == "warning" for i in result.warnings)


def test_review_required_connection_is_a_warning():
    agent = make_agent(connections=[ConnectionRef(ref="sf_conn", auth_type="oauth2")])
    result = validate_agent(agent)
    assert result.is_valid
    assert any(i.field == "connections" for i in result.warnings)


def test_low_translation_confidence_is_a_warning():
    agent = make_agent(translation_confidence=0.5)
    result = validate_agent(agent)
    assert result.is_valid
    assert any("confidence" in i.message.lower() for i in result.warnings)

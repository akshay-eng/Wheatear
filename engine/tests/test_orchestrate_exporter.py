import yaml

from agent_liftoff.connectors.orchestrate.exporter import export_agent
from agent_liftoff.ir.schema import Agent, ConnectionRef, KnowledgeRef, ToolRef


def make_agent(**overrides) -> Agent:
    defaults = dict(
        name="support_router",
        source_platform="copilot-studio",
        instructions="Route tickets.",
        # Orchestrate requires one, so an agent without it is itself a
        # review item -- which would fire in every test that is about
        # something else.
        description="Routes support tickets to the right team.",
    )
    defaults.update(overrides)
    return Agent(**defaults)


def test_export_writes_agent_yaml_in_orchestrate_shape(tmp_path):
    agent = make_agent(tools=[ToolRef(ref="lookup_ticket", confidence=0.99)])

    result = export_agent(agent, tmp_path)

    assert result.agent_path.exists()
    spec = yaml.safe_load(result.agent_path.read_text())
    assert spec["spec_version"] == "v1"
    assert spec["kind"] == "native"
    assert spec["name"] == "support_router"
    assert spec["instructions"] == "Route tickets."
    assert spec["tools"] == ["lookup_ticket"]
    assert spec["collaborators"] == []


def test_export_never_autofills_connection_credentials(tmp_path):
    agent = make_agent(connections=[ConnectionRef(ref="salesforce_conn", auth_type="oauth2")])

    result = export_agent(agent, tmp_path)

    assert len(result.connection_paths) == 1
    spec = yaml.safe_load(result.connection_paths[0].read_text())
    assert spec["credentials"] == "REPLACE_ME"
    assert spec["auth_type"] == "oauth2"


def test_export_flags_low_confidence_and_review_required_items(tmp_path):
    agent = make_agent(
        translation_confidence=0.6,
        translation_notes=["Collapsed 3 branching conditions into one instruction."],
        tools=[ToolRef(ref="legacy_flow", confidence=0.3, review_required=True, notes="No 1:1 Orchestrate equivalent.")],
        connections=[ConnectionRef(ref="salesforce_conn", auth_type="oauth2")],
    )

    result = export_agent(agent, tmp_path)

    assert result.needs_review
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    types = {item["type"] for item in manifest["review_items"]}
    assert types == {"translation", "tool", "connection"}


def test_export_flags_review_required_knowledge_sources(tmp_path):
    """A SharePoint-backed (or any connector-backed) knowledge source needs
    re-ingestion, not a reference copy -- this must surface in the review
    manifest the same way tools/connections do.
    """
    agent = make_agent(
        knowledge=[
            KnowledgeRef(
                ref="HumanResources",
                review_required=True,
                notes="SharePointSearchSource source needs re-indexing into an Orchestrate knowledge base.",
            )
        ]
    )

    result = export_agent(agent, tmp_path)

    assert result.needs_review
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    knowledge_items = [item for item in manifest["review_items"] if item["type"] == "knowledge"]
    assert len(knowledge_items) == 1
    assert knowledge_items[0]["ref"] == "HumanResources"


def test_export_omits_review_manifest_when_nothing_needs_review(tmp_path):
    agent = make_agent(tools=[ToolRef(ref="lookup_ticket", confidence=1.0)])

    result = export_agent(agent, tmp_path)

    assert not result.needs_review
    assert result.review_manifest_path is None


def test_export_emits_welcome_content_in_adk_shape(tmp_path):
    agent = make_agent(welcome_message="Hello, I'm Helper Bee.")

    result = export_agent(agent, tmp_path)
    spec = yaml.safe_load(result.agent_path.read_text())

    assert spec["welcome_content"]["welcome_message"] == "Hello, I'm Helper Bee."
    assert spec["welcome_content"]["is_default_message"] is False


def test_export_resolves_model_by_tier_and_flags_it(tmp_path):
    agent = make_agent(model_hint="GPT5Chat")

    result = export_agent(agent, tmp_path)
    spec = yaml.safe_load(result.agent_path.read_text())

    # Model tiered to a concrete Orchestrate model, and the swap is flagged.
    assert spec["llm"].startswith("watsonx/")
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    model_items = [i for i in manifest["review_items"] if i["type"] == "model"]
    assert len(model_items) == 1
    assert "GPT5Chat" in model_items[0]["detail"]


def test_export_flags_unmappable_channels_moderation_and_web_search(tmp_path):
    agent = make_agent(
        channels=["msteams", "Microsoft365Copilot"],
        content_moderation="Low",
        web_search=True,
    )

    result = export_agent(agent, tmp_path)
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    types = {item["type"] for item in manifest["review_items"]}

    assert {"channel", "content_moderation", "web_search"} <= types


def test_export_does_not_flag_model_when_source_specified_none(tmp_path):
    agent = make_agent()  # no model_hint

    result = export_agent(agent, tmp_path)

    # Nothing to confirm swapping from, so no manifest at all.
    assert result.review_manifest_path is None


def test_all_of_an_agents_documents_go_into_one_knowledge_base(tmp_path):
    """Orchestrate permits an agent exactly one knowledge base and rejects the
    whole agent otherwise ("Max number of knowledge-base exceeded"). One base
    holding three documents answers questions identically to three holding one.
    """
    from agent_liftoff.connectors.orchestrate.exporter import knowledge_base_specs

    agent = make_agent(
        knowledge=[
            KnowledgeRef(ref="handbook", file_path="/tmp/a.pdf", description="HR policy."),
            KnowledgeRef(ref="overview", file_path="/tmp/b.pdf", description="Company overview."),
        ]
    )
    specs = knowledge_base_specs(agent)
    assert len(specs) == 1
    assert specs[0]["documents"] == ["/tmp/a.pdf", "/tmp/b.pdf"]
    # Every source's own words survive the merge; the per-file naming does not.
    assert "HR policy." in specs[0]["description"]
    assert "Company overview." in specs[0]["description"]

    result = export_agent(agent, tmp_path)
    spec = yaml.safe_load(result.agent_path.read_text())
    assert spec["knowledge_base"] == [specs[0]["name"]]


def test_a_knowledge_base_is_never_created_without_a_description(tmp_path):
    """The service rejects it with nothing more useful than "An Unexpected
    Error Occurred" -- verified live, by bisection.
    """
    from agent_liftoff.connectors.orchestrate.exporter import knowledge_base_specs

    agent = make_agent(knowledge=[KnowledgeRef(ref="handbook", file_path="/tmp/a.pdf")])
    assert knowledge_base_specs(agent)[0]["description"]


def test_a_knowledge_source_with_no_documents_creates_nothing(tmp_path):
    """A base named after a SharePoint site nobody ingested looks migrated and
    answers nothing.
    """
    from agent_liftoff.connectors.orchestrate.exporter import knowledge_base_specs

    agent = make_agent(knowledge=[KnowledgeRef(ref="sharepoint_site")])
    assert knowledge_base_specs(agent) == []
    result = export_agent(agent, tmp_path)
    assert "knowledge_base" not in yaml.safe_load(result.agent_path.read_text())
    assert result.needs_review


def test_a_style_the_target_does_not_know_is_dropped_not_passed_through(tmp_path):
    """One bad string in `style` fails the entire agent's import. Observed: a
    mapping derived `connected` from a Copilot boolean about whether other
    agents may call this one.
    """
    result = export_agent(make_agent(agent_style="connected"), tmp_path)
    spec = yaml.safe_load(result.agent_path.read_text())
    assert spec["style"] == "react_intrinsic"
    manifest = yaml.safe_load(result.review_manifest_path.read_text())
    assert any(item["type"] == "style" for item in manifest["review_items"])


def test_a_style_the_target_does_know_is_kept(tmp_path):
    result = export_agent(make_agent(agent_style="planner"), tmp_path)
    assert yaml.safe_load(result.agent_path.read_text())["style"] == "planner"


# ----------------------------------------------------------------------
# Carrying the source's operating knowledge onto an existing tool
# ----------------------------------------------------------------------


def _snow_tool(**overrides):
    from agent_liftoff.ir.schema import BridgeStrategy, ToolParameter

    defaults = dict(
        ref="SNOWMCPALL:get_record",
        source_ref="ServiceNow-GetRecord",
        confidence=1.0,
        bridge=BridgeStrategy.MCP_CATALOG,
        description=(
            "Gets a single ServiceNow record by its sys_id. Record Type is the "
            "TABLE NAME: lowercase, singular. Display labels like 'Incidents' "
            "are invalid and return HTTP 400."
        ),
        inputs=[ToolParameter(name="sysid", description="The 32-character hex sys_id.")],
    )
    defaults.update(overrides)
    return ToolRef(**defaults)


def test_what_the_source_knew_about_a_tool_becomes_a_guideline():
    """The target's `get_record` works; what it lacks is the guidance the
    source platform had written around it. An agent that arrives without that
    makes exactly the mistakes the text was written to prevent.
    """
    from agent_liftoff.pipeline.resolve import carry_tool_context

    guidelines = carry_tool_context(make_agent(tools=[_snow_tool()]))
    assert len(guidelines) == 1
    assert guidelines[0].tool_ref == "SNOWMCPALL:get_record"
    assert "HTTP 400" in guidelines[0].action
    assert "sysid" in guidelines[0].action


def test_a_tool_that_is_not_on_the_target_gets_no_guideline():
    """There is no reference to bind guidance to, and a guideline naming a
    tool the agent cannot call is noise on every turn.
    """
    from agent_liftoff.ir.schema import BridgeStrategy
    from agent_liftoff.pipeline.resolve import carry_tool_context

    tool = _snow_tool(bridge=BridgeStrategy.CATALOG_INSTALL)
    assert carry_tool_context(make_agent(tools=[tool])) == []


def test_a_tool_the_source_said_nothing_useful_about_gets_no_guideline():
    from agent_liftoff.pipeline.resolve import carry_tool_context

    tool = _snow_tool(description="Get record.", inputs=[])
    assert carry_tool_context(make_agent(tools=[tool])) == []


def test_carried_guidelines_reach_the_exported_agent(tmp_path):
    from agent_liftoff.pipeline.resolve import carry_tool_context

    agent = make_agent(tools=[_snow_tool()])
    agent.guidelines.extend(carry_tool_context(agent))
    spec = yaml.safe_load(export_agent(agent, tmp_path).agent_path.read_text())
    assert spec["guidelines"][0]["tool"] == "SNOWMCPALL:get_record"
    assert "HTTP 400" in spec["guidelines"][0]["action"]


def test_nothing_that_reaches_the_target_names_the_tool_that_moved_it(tmp_path):
    """A background accelerator does not brand what it migrates."""
    agent = make_agent(
        description="",
        tools=[_snow_tool()],
        knowledge=[KnowledgeRef(ref="handbook", file_path="/tmp/a.pdf")],
    )
    agent.guidelines.extend(__import__(
        "agent_liftoff.pipeline.resolve", fromlist=["carry_tool_context"]
    ).carry_tool_context(agent))
    result = export_agent(agent, tmp_path)
    from agent_liftoff.connectors.orchestrate.exporter import knowledge_base_specs

    landed = [result.agent_path.read_text(), yaml.safe_dump(knowledge_base_specs(agent))]
    for text in landed:
        assert "agent_liftoff" not in text.lower(), text


def test_a_generated_description_is_marked_for_review():
    """It is a starting point, not a fact about the agent."""
    from agent_liftoff.pipeline.translate import AgentDescription, describe_agent

    class Provider:
        def generate_structured(self, prompt, schema):
            assert "Route tickets." in prompt, "the prompt must show what the agent does"
            return AgentDescription(description="Routes support tickets to the right team.")

    agent = make_agent(description=None)
    describe_agent(agent, Provider())
    assert agent.description == "Routes support tickets to the right team."
    assert agent.review_required
    assert any("starting point" in n for n in agent.translation_notes)


def test_an_agent_that_already_has_a_description_keeps_it():
    from agent_liftoff.pipeline.translate import describe_agent

    class Provider:
        def generate_structured(self, prompt, schema):
            raise AssertionError("must not be called")

    agent = make_agent(description="The source wrote this.")
    assert describe_agent(agent, Provider()).description == "The source wrote this."


def test_a_provider_that_fails_leaves_the_agent_importable():
    """No description is a gap to report, not a failed migration."""
    from agent_liftoff.pipeline.translate import describe_agent

    class Provider:
        def generate_structured(self, prompt, schema):
            raise RuntimeError("no")

    agent = describe_agent(make_agent(description=None), Provider())
    assert agent.description is None
    assert any("Could not generate" in n for n in agent.translation_notes)


def test_two_source_tools_landing_on_one_target_tool_are_named_once():
    """Copilot's `GetRecord` and `ListRecords` both resolve to `get_records`.
    Naming it twice claims the agent has two tools when it has one.
    """
    from agent_liftoff.connectors.orchestrate.exporter import importable_tools
    from agent_liftoff.ir.schema import Agent, BridgeStrategy, ToolRef

    agent = Agent(
        name="ITSM",
        source_platform="copilot-studio",
        tools=[
            ToolRef(ref="get_records", source_ref="GetRecord", confidence=0.9,
                    bridge=BridgeStrategy.MCP_CATALOG),
            ToolRef(ref="get_records", source_ref="ListRecords", confidence=1.0,
                    bridge=BridgeStrategy.MCP_CATALOG),
        ],
    )

    carried, dropped = importable_tools(agent)

    assert carried == ["get_records"]
    assert dropped == []


def test_a_migrated_agent_lands_on_the_recommended_style_not_a_deprecated_one():
    """The platform marks `default` and `react` deprecated and recommends
    `react_core`. A Copilot agent says nothing about either, so the fallback is
    the whole decision -- and defaulting to a style the vendor is steering
    people off is choosing the wrong one for every migration.
    """
    from agent_liftoff.connectors.orchestrate.exporter import agent_style
    from agent_liftoff.ir.schema import Agent

    def styled(value):
        return Agent(name="a", source_platform="copilot-studio", agent_style=value)

    # `react_intrinsic` is what the console labels "ReAct Core". The enum also
    # has a `react_core`, which reads like the obvious answer and is used by
    # nothing -- a live tenant stores only default, react and react_intrinsic,
    # matching the three styles the console offers.
    assert agent_style(styled(None))[0] == "react_intrinsic"
    assert agent_style(styled(""))[0] == "react_intrinsic"
    # A style the source genuinely set and the target knows is still honoured.
    assert agent_style(styled("planner")) == ("planner", None)
    # And an unknown one is reported as discarded rather than silently swapped.
    assert agent_style(styled("connected")) == ("react_intrinsic", "connected")

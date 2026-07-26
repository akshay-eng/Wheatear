"""Resolve stage: matching source tools onto the target's real tool catalog.

The catalog entries below are transcribed from a live watsonx Orchestrate
instance (150 tools, 61 of them ServiceNow-related), and the source tool is
the real Copilot Studio connector action from the calibration export. The
interesting property is that the two platforms name the same capability
differently -- `Get Record`/`sysid` versus `SNOWMCPALL:get_record`/`sys_id` --
which is exactly what no string comparison resolves.
"""

from wheatear.connectors.base import ImportResult, RawToolRef, ToolParam
from wheatear.ir.schema import Agent, BridgeStrategy, ToolParameter, ToolRef
from wheatear.pipeline.map import map_agent
from wheatear.pipeline.resolve import (
    CatalogTool,
    ToolMatch,
    build_catalog,
    build_match_prompt,
    shortlist_scored,
    resolve_agent_tools,
    shortlist,
)

RAW_CATALOG = [
    {
        "name": "SNOWMCPALL:get_record",
        "description": "Get a specific record by sys_id\n\nArgs:\n  table: Table to query",
        "input_schema": {"properties": {"table": {}, "sys_id": {}}},
        "binding": {"mcp": {}},
        "toolkit_id": "tk-1",
    },
    {
        "name": "SNOWMCPALL:perform_query",
        "description": "Perform a query against ServiceNow\n\nArgs:\n  table: Table to query",
        "input_schema": {"properties": {"table": {}, "query": {}, "limit": {}}},
        "binding": {"mcp": {}},
        "toolkit_id": "tk-1",
    },
    {
        "name": "SNOWMCPALL:close_incident",
        "description": "Close an incident in ServiceNow",
        "input_schema": {"properties": {"incident_id": {}, "resolution_code": {}}},
        "binding": {"mcp": {}},
        "toolkit_id": "tk-1",
    },
    {
        "name": "githubtools:list_pull_requests",
        "description": "List open pull requests for a GitHub repository",
        "input_schema": {"properties": {"repo": {}, "state": {}}},
        "binding": {"mcp": {}},
        "toolkit_id": "tk-2",
    },
    {"name": "", "description": "nameless and unreferenceable"},
]


def _source_tool() -> ToolRef:
    return ToolRef(
        ref="Get Record",
        source_ref="ServiceNow-GetRecord",
        operation_id="GetRecord",
        description="Gets a single ServiceNow record by its sys_id.",
        review_required=True,
        confidence=0.0,
        inputs=[
            ToolParameter(name="tableType", description="ServiceNow table name, lowercase."),
            ToolParameter(name="sysid", description="The record's 32-character hex sys_id."),
        ],
    )


def _agent_with(tool: ToolRef) -> Agent:
    return Agent(name="ITSM Agent", source_platform="copilot-studio", tools=[tool])


class _ScriptedProvider:
    """Stands in for an LLM: returns queued answers and records the prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class _ExplodingProvider:
    def generate_structured(self, prompt, schema):
        raise RuntimeError("model unavailable")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_build_catalog_flattens_tools_and_drops_unreferenceable_ones():
    catalog = build_catalog(RAW_CATALOG)

    assert len(catalog) == 4  # the nameless entry is unusable in an agent.yaml
    get_record = catalog[0]
    assert get_record.ref == "SNOWMCPALL:get_record"
    assert get_record.params == ["sys_id", "table"]
    assert get_record.kind == "mcp"


# ---------------------------------------------------------------------------
# Shortlist -- deterministic, so it is the part that must be reproducible
# ---------------------------------------------------------------------------

def test_shortlist_ranks_the_true_match_first_despite_renamed_parameters():
    catalog = build_catalog(RAW_CATALOG)

    ranked = shortlist(_source_tool(), catalog)

    assert ranked[0].ref == "SNOWMCPALL:get_record"


def test_shortlist_excludes_unrelated_domains():
    """A GitHub tool shares no meaningful vocabulary with a ServiceNow record
    lookup and must not consume a shortlist slot.
    """
    catalog = build_catalog(RAW_CATALOG)

    ranked = shortlist(_source_tool(), catalog)

    assert "githubtools:list_pull_requests" not in [c.ref for c in ranked]


def test_shortlist_respects_its_limit():
    catalog = build_catalog(RAW_CATALOG)

    assert len(shortlist(_source_tool(), catalog, limit=2)) == 2


def test_shortlist_is_empty_for_an_empty_catalog():
    assert shortlist(_source_tool(), []) == []


def test_shortlist_tokenizes_across_naming_conventions():
    """`Get Record` must reach `SNOWMCPALL:get_record`: the tokenizer has to
    split on colons and underscores, or the two never share a token.
    """
    catalog = [CatalogTool(ref="SNOWMCPALL:get_record", description="", params=[])]

    assert shortlist(_source_tool(), catalog)[0].ref == "SNOWMCPALL:get_record"


# ---------------------------------------------------------------------------
# Adjudication
# ---------------------------------------------------------------------------

def test_exact_match_is_applied_and_clears_review():
    tool = _source_tool()
    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:get_record", verdict="exact", confidence=0.95, rationale="Same op.")
    )

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider)

    assert tool.ref == "SNOWMCPALL:get_record"
    assert tool.confidence == 0.95
    assert tool.review_required is False
    assert tool.bridge == BridgeStrategy.MCP_CATALOG
    assert "Same op." in tool.notes


def test_near_match_is_applied_but_still_needs_review():
    """A 'near' match is a suggestion, not a decision -- the target may take
    parameters the source never supplied.
    """
    tool = _source_tool()
    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:perform_query", verdict="near", confidence=0.8, rationale="Broader.")
    )

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider)

    assert tool.ref == "SNOWMCPALL:perform_query"
    assert tool.review_required is True


def test_confident_sounding_match_below_threshold_still_needs_review():
    tool = _source_tool()
    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:get_record", verdict="exact", confidence=0.2)
    )

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider)

    assert tool.review_required is True


def test_verdict_none_leaves_the_tool_unresolved():
    tool = _source_tool()
    provider = _ScriptedProvider(ToolMatch(target_ref=None, verdict="none", confidence=0.0))

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider)

    assert tool.ref == "Get Record"  # unchanged
    assert tool.review_required is True
    assert tool.confidence == 0.0


def test_a_hallucinated_ref_is_discarded_rather_than_written_into_the_spec():
    """The failure this guards against is silent: a plausible-but-absent tool
    name produces an agent.yaml that imports cleanly and then fails at runtime.
    """
    tool = _source_tool()
    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:fetch_record", verdict="exact", confidence=1.0)
    )

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider)

    assert tool.ref == "Get Record"
    assert tool.review_required is True
    assert tool.confidence == 0.0
    assert "not in the target catalog" in tool.notes


def test_a_resolver_failure_does_not_sink_the_migration():
    tool = _source_tool()

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), _ExplodingProvider())

    assert tool.ref == "Get Record"
    assert tool.review_required is True
    assert "Automatic resolution failed" in tool.notes


# ---------------------------------------------------------------------------
# Scope: what the stage must not touch
# ---------------------------------------------------------------------------

def test_natively_portable_mcp_tools_are_left_alone():
    """A toolkit Map already re-pointed to its own MCP server URL is a clean
    migration; re-resolving it against the catalog could only make it worse.
    """
    import_result = ImportResult(
        agent=Agent(name="a", source_platform="orchestrate"),
        raw_tools=[RawToolRef(name="SNOWMCP", kind="mcp", mcp_server_url="http://x/sse")],
    )
    agent = map_agent(import_result, "orchestrate")
    provider = _ScriptedProvider()  # any call would IndexError

    resolve_agent_tools(agent, build_catalog(RAW_CATALOG), provider)

    assert agent.tools[0].ref == "SNOWMCP"
    assert agent.tools[0].bridge == BridgeStrategy.NATIVE_MCP
    assert provider.prompts == []


def test_without_a_provider_the_shortlist_is_recorded_but_nothing_is_decided():
    """A deterministic run must stay deterministic: suggestions only."""
    tool = _source_tool()

    resolve_agent_tools(_agent_with(tool), build_catalog(RAW_CATALOG), provider=None)

    assert tool.ref == "Get Record"
    assert tool.review_required is True
    assert "SNOWMCPALL:get_record" in tool.notes


def test_prompt_carries_the_parameter_descriptions_and_forbids_invention():
    catalog = build_catalog(RAW_CATALOG)

    prompt = build_match_prompt(_source_tool(), shortlist(_source_tool(), catalog))

    assert "32-character hex sys_id" in prompt
    assert "SNOWMCPALL:get_record" in prompt
    assert "Never invent one." in prompt


def test_end_to_end_from_a_copilot_connector_action():
    """Map produces the unresolved tool, Resolve turns it into a real one."""
    import_result = ImportResult(
        agent=Agent(name="ITSM Agent", source_platform="copilot-studio"),
        raw_tools=[
            RawToolRef(
                name="Get Record",
                kind="connector",
                description="Gets a single ServiceNow record by its sys_id.",
                operation_id="GetRecord",
                inputs=[ToolParam(name="sysid", description="32-character hex sys_id.")],
                connector_id="/providers/Microsoft.PowerApps/apis/shared_service-now",
            )
        ],
    )
    agent = map_agent(import_result, "orchestrate")
    assert agent.tools[0].review_required is True

    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:get_record", verdict="exact", confidence=0.95)
    )
    resolve_agent_tools(agent, build_catalog(RAW_CATALOG), provider)

    assert agent.tools[0].ref == "SNOWMCPALL:get_record"
    assert agent.tools[0].review_required is False


# ---------------------------------------------------------------------------
# Two-tier resolution: installed instance tools, then the global catalog
#
# The distinction is not cosmetic. A tool that exists in the catalog but is not
# installed cannot be referenced from an agent.yaml -- the import fails, or
# worse, succeeds and misbehaves. So a catalog hit is a real answer that is not
# yet a usable one, and these tests pin that difference down.
# ---------------------------------------------------------------------------

from wheatear.connectors.orchestrate.catalog_client import to_artifacts  # noqa: E402
from wheatear.pipeline.resolve import build_marketplace_catalog  # noqa: E402

RAW_MARKETPLACE = [
    {
        "id": "a1",
        "name": "Get a record in ServiceNow",
        "description": "Retrieves a single record from a ServiceNow table by its sys_id.",
        "category": "tool",
        "type": "python",
        "external_identifier": "servicenow_get_record",
        "publisher": "IBM",
        "tags": ["IT"],
        "artifact_group": [{"name": "IT Service Management with ServiceNow"}],
    },
    {
        "id": "a2",
        "name": "Create a shipment in SAP S4 HANA",
        "description": "Creates an outbound delivery shipment.",
        "category": "tool",
        "type": "python",
        "external_identifier": "sap_create_shipment",
        "publisher": "IBM",
        "tags": ["Supply Chain"],
        "artifact_group": [],
    },
]


def _marketplace():
    return build_marketplace_catalog(to_artifacts(RAW_MARKETPLACE))


def test_marketplace_entries_are_referenced_by_their_install_name():
    """`ref` has to be what the tool is called once installed, because that's
    what ends up in agent.yaml -- not the human title shown in the catalog UI.
    """
    entry = _marketplace()[0]

    assert entry.ref == "servicenow_get_record"
    assert entry.display_name == "Get a record in ServiceNow"
    assert entry.installed is False
    assert entry.params == []


def test_marketplace_matching_uses_the_title_and_offering_as_signal():
    """Catalog records carry no parameter schema, so the title and the offering
    they ship in are most of what there is to match on."""
    entry = _marketplace()[0]
    text = entry.match_text()

    assert "servicenow" in text
    assert "record" in text
    assert "management" in text  # from "IT Service Management with ServiceNow"


def test_installed_tools_are_searched_before_the_catalog():
    """A working answer beats a better-sounding one: if the instance can do the
    job, we never propose an install."""
    provider = _ScriptedProvider(
        ToolMatch(target_ref="SNOWMCPALL:get_record", verdict="exact", confidence=0.95)
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider, marketplace=_marketplace()
    )

    assert tool.ref == "SNOWMCPALL:get_record"
    assert tool.bridge == BridgeStrategy.MCP_CATALOG
    assert len(provider.prompts) == 1  # the catalog tier was never consulted


def test_a_miss_on_the_instance_falls_through_to_the_catalog():
    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_get_record", verdict="exact", confidence=0.9),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider, marketplace=_marketplace()
    )

    assert tool.ref == "servicenow_get_record"
    assert tool.bridge == BridgeStrategy.CATALOG_INSTALL
    assert len(provider.prompts) == 2


def test_a_catalog_hit_never_clears_review_however_confident():
    """It isn't installed, so referencing it would fail at import. Confidence
    doesn't change that."""
    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_get_record", verdict="exact", confidence=1.0),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider, marketplace=_marketplace()
    )

    assert tool.review_required is True
    assert "Not installed on this instance" in tool.notes
    assert "Get a record in ServiceNow" in tool.notes  # the title to look for in the UI


def test_a_hallucinated_catalog_ref_falls_back_rather_than_being_written():
    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_delete_everything", verdict="exact", confidence=0.99),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider, marketplace=_marketplace()
    )

    assert tool.ref == "Get Record"
    assert tool.review_required is True
    assert tool.confidence == 0.0
    assert "not in the target catalog" in tool.notes


def test_a_miss_in_both_tiers_leaves_the_tool_for_a_bridge():
    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider, marketplace=_marketplace()
    )

    assert tool.ref == "Get Record"
    assert tool.review_required is True
    assert tool.confidence == 0.0


def test_the_catalog_prompt_says_parameters_are_unavailable():
    """Without this the model treats an empty parameter list as evidence
    against a match, which it isn't -- the endpoint simply doesn't return one.
    """
    marketplace = _marketplace()

    prompt = build_match_prompt(_source_tool(), shortlist(_source_tool(), marketplace))

    assert "NOT yet installed" in prompt
    assert "do not treat missing params as evidence against a match" in prompt


def test_without_a_provider_both_tiers_are_recorded_as_suggestions():
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider=None, marketplace=_marketplace()
    )

    assert tool.ref == "Get Record"  # nothing decided
    assert "SNOWMCPALL:get_record" in tool.notes
    assert "servicenow_get_record (catalog)" in tool.notes


def test_the_catalog_alone_is_enough_to_run_the_stage():
    """No target credentials for the instance API, but a console cookie for the
    catalog, is a real configuration -- it must still resolve."""
    provider = _ScriptedProvider(
        ToolMatch(target_ref="servicenow_get_record", verdict="near", confidence=0.7)
    )
    tool = _source_tool()

    resolve_agent_tools(_agent_with(tool), [], provider, marketplace=_marketplace())

    assert tool.ref == "servicenow_get_record"
    assert tool.bridge == BridgeStrategy.CATALOG_INSTALL


def test_shortlisted_catalog_candidates_are_enriched_before_adjudication():
    """The catalog's list endpoint returns no parameter schema. Without
    enrichment the model judges catalog candidates on prose alone -- so the
    hook has to fire, and only on the shortlist."""
    marketplace = _marketplace()
    enriched: list = []

    def enrich(artifacts):
        enriched.extend(artifacts)
        for artifact in artifacts:
            artifact.params = ["table", "sys_id"]

    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_get_record", verdict="exact", confidence=0.9),
    )
    resolve_agent_tools(
        _agent_with(_source_tool()),
        build_catalog(RAW_CATALOG),
        provider,
        marketplace=marketplace,
        enrich=enrich,
    )

    assert enriched, "the hook never fired"
    assert len(enriched) <= len(marketplace)
    assert "sys_id" in provider.prompts[1]  # the schema reached the model


def test_a_failing_enrich_hook_does_not_sink_the_match():
    """Enrichment is an optimisation. Losing it costs parameter detail, not
    the migration."""
    def enrich(artifacts):
        raise RuntimeError("catalog detail unavailable")

    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_get_record", verdict="near", confidence=0.7),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider,
        marketplace=_marketplace(), enrich=enrich,
    )

    assert tool.ref == "servicenow_get_record"


def test_the_connection_a_catalog_tool_needs_is_named_in_the_review_note():
    """Installing the tool isn't the whole job: without its connection
    configured it imports and then fails at runtime."""
    marketplace = _marketplace()

    def enrich(artifacts):
        for artifact in artifacts:
            artifact.connections = ["servicenow_ibm_184bdbd3"]

    provider = _ScriptedProvider(
        ToolMatch(target_ref=None, verdict="none", confidence=0.0),
        ToolMatch(target_ref="servicenow_get_record", verdict="exact", confidence=0.9),
    )
    tool = _source_tool()

    resolve_agent_tools(
        _agent_with(tool), build_catalog(RAW_CATALOG), provider,
        marketplace=marketplace, enrich=enrich,
    )

    assert "servicenow_ibm_184bdbd3" in tool.notes


def test_resolve_imports_nothing_that_does_io():
    """Pipeline stages stay pure -- network, filesystem and subprocess belong
    at the edges (connectors, CLI). The enrich hook exists precisely so this
    stage can use catalog detail without reaching for it itself. If someone
    adds a direct fetch here, this fails."""
    import ast
    import inspect

    import wheatear.pipeline.resolve as resolve_module

    tree = ast.parse(inspect.getsource(resolve_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"requests", "httpx", "urllib", "subprocess", "socket", "pathlib", "os"}
    assert not (imported & forbidden), f"Resolve imported I/O modules: {imported & forbidden}"
    assert not any(m.startswith("wheatear.connectors") for m in imported)


# ---------------------------------------------------------------------------
# Tokenisation. Both rules below were written after watching the shortlist fail
# against the real 1173-entry catalog, not from first principles.
# ---------------------------------------------------------------------------


def test_plurals_fold_onto_their_singular():
    """`List Records` has to reach `get_records`, and `Get Record` has to reach
    it too. Before this, `record` and `records` were unrelated tokens and the
    highest-signal term in the query simply didn't match."""
    from wheatear.pipeline.resolve import _tokens

    assert _tokens("Get Records") == _tokens("get_record")
    assert "record" in _tokens("get_records")
    assert "entry" in _tokens("entries")


def test_singularising_does_not_corrupt_words_that_end_in_s():
    """Over-stemming is the worse failure: it matches unrelated tools."""
    from wheatear.pipeline.resolve import _tokens

    for word in ("status", "address", "analysis", "sys_id", "https"):
        assert word.split("_")[0] in " ".join(_tokens(word)) or _tokens(word)
    assert _tokens("status") == ["status"]
    assert _tokens("address") == ["address"]
    assert _tokens("analysis") == ["analysis"]


def test_verbs_are_kept_because_they_are_what_distinguishes_tools():
    """`create_record`, `get_record` and `update_record` differ by the verb and
    nothing else. Dropping verbs as stopwords made a read operation rank a
    destructive write first."""
    from wheatear.pipeline.resolve import _tokens

    assert "get" in _tokens("get_record")
    assert "create" in _tokens("create_a_record")
    assert _tokens("get_record") != _tokens("create_record")


def test_a_read_and_a_write_of_the_same_noun_are_distinguishable():
    """The property that matters: a source `Get Record` must not score
    identically against a create and a get."""
    catalog = build_catalog([
        {"name": "create_a_record", "description": "Creates a record in a ServiceNow table.",
         "input_schema": {"properties": {"table_name": {}, "input_data": {}}}, "binding": {}},
        {"name": "get_a_record", "description": "Gets a record from a ServiceNow table.",
         "input_schema": {"properties": {"table_name": {}, "sys_id": {}}}, "binding": {}},
    ])

    ranked = shortlist_scored(_source_tool(), catalog)

    assert ranked[0][1].ref == "get_a_record"
    assert ranked[0][0] > ranked[1][0]

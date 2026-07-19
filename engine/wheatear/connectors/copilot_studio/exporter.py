"""IR -> Microsoft Copilot Studio (Dataverse solution export) exporter.

The reverse of solution_importer.py: writes the unpacked solution layout that
`solution_importer.detect_format` recognizes and that Copilot Studio can import
as a managed/unmanaged solution --

    solution.xml
    bots/<schema>/bot.xml
    bots/<schema>/configuration.json
    botcomponents/<schema>.gpt.default/{botcomponent.xml,data}
    botcomponents/<schema>.topic.ConversationStart/{botcomponent.xml,data}
    botcomponents/<schema>.knowledge.<kb>/{botcomponent.xml,data}

Everything here is deterministic: no LLM call. Copilot Studio's generative
agent centers on one instructions string (the GPT component), which is exactly
what the IR carries, so the high-value content round-trips cleanly. Things with
no faithful Copilot representation (a generic Orchestrate MCP tool, a
vector-DB knowledge base, a collaborator wiring) are written as best-effort
stubs AND recorded in review-manifest.yaml rather than emitted as if ready.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from wheatear.errors import ExportError
from wheatear.ir.schema import Agent

# Copilot Studio botcomponent type codes (mirror solution_importer's constants).
COMPONENT_TYPE_TOPIC = 9
COMPONENT_TYPE_GPT = 15
COMPONENT_TYPE_KNOWLEDGE = 16

# Publisher customization prefix for generated schema names (ai_* is what real
# exports use; kept identical so a re-import classifies components the same way).
SCHEMA_PREFIX = "wx"


@dataclass
class ExportResult:
    # The solution root directory (what you'd zip and import). Named agent_path
    # for a uniform interface with the Orchestrate exporter's ExportResult.
    agent_path: Path
    written_paths: list[Path] = field(default_factory=list)
    review_manifest_path: Path | None = None

    @property
    def needs_review(self) -> bool:
        return self.review_manifest_path is not None


def _sanitize(name: str) -> str:
    """A safe schema identifier fragment from an agent display name."""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", name)
    return cleaned or "Agent"


def _write(path: Path, content: str, written: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    written.append(path)


def _dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _solution_xml(unique_name: str) -> str:
    return (
        '<ImportExportXml version="9.2" SolutionPackageVersion="9.2" languagecode="1033" '
        'generatedBy="Wheatear" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <SolutionManifest>\n"
        f"    <UniqueName>{escape(unique_name)}</UniqueName>\n"
        "    <LocalizedNames>\n"
        f'      <LocalizedName description="{escape(unique_name)}" languagecode="1033" />\n'
        "    </LocalizedNames>\n"
        "    <Descriptions />\n"
        "    <Version>1.0.0.0</Version>\n"
        "    <Managed>0</Managed>\n"
        "    <Publisher>\n"
        f"      <UniqueName>{SCHEMA_PREFIX}wheatear</UniqueName>\n"
        f"      <CustomizationPrefix>{SCHEMA_PREFIX}</CustomizationPrefix>\n"
        "    </Publisher>\n"
        "    <RootComponents />\n"
        "    <MissingDependencies />\n"
        "  </SolutionManifest>\n"
        "</ImportExportXml>\n"
    )


def _bot_xml(schema: str, display_name: str) -> str:
    return (
        f'<bot schemaname="{escape(schema)}">\n'
        "  <authenticationmode>2</authenticationmode>\n"
        "  <authenticationtrigger>1</authenticationtrigger>\n"
        "  <iscustomizable>1</iscustomizable>\n"
        "  <language>1033</language>\n"
        f"  <name>{escape(display_name)}</name>\n"
        "  <runtimeprovider>0</runtimeprovider>\n"
        "  <template>default-2.1.0</template>\n"
        "</bot>\n"
    )


def _configuration_json(schema: str, agent: Agent) -> str:
    config = {
        "categories": [],
        "channels": [{"channelId": c} for c in agent.channels],
        "settings": {"GenerativeActionsEnabled": True},
        "publishOnImport": True,
        "$kind": "BotConfiguration",
        "isAgentConnectable": bool(agent.collaborators) or True,
        "gPTSettings": {"$kind": "GPTSettings", "defaultSchemaName": f"{schema}.gpt.default"},
        "aISettings": {
            "$kind": "AISettings",
            "useModelKnowledge": bool(agent.knowledge),
            "contentModeration": agent.content_moderation or "High",
        },
        "recognizer": {"$kind": "GenerativeAIRecognizer"},
    }
    return json.dumps(config)


def _botcomponent_xml(schema: str, component_type: int, name: str) -> str:
    return (
        f'<botcomponent schemaname="{escape(schema)}">\n'
        f"  <componenttype>{component_type}</componenttype>\n"
        "  <iscustomizable>1</iscustomizable>\n"
        f"  <name>{escape(name)}</name>\n"
        "  <parentbotid>\n"
        f"    <schemaname>{escape(schema.rsplit('.', 2)[0])}</schemaname>\n"
        "  </parentbotid>\n"
        "  <statecode>0</statecode>\n"
        "  <statuscode>1</statuscode>\n"
        "</botcomponent>\n"
    )


def _gpt_instructions(agent: Agent) -> str:
    """The instructions the GPT component carries. Copilot has no structured
    Guidelines slot, so any IR guidelines are folded into the instructions
    text deterministically (faithful: Copilot's instructions field is the
    catch-all for behavior).
    """
    text = agent.instructions or agent.existing_instructions or ""
    if agent.guidelines:
        lines = ["", "## Guidelines"]
        for g in agent.guidelines:
            header = f"- When {g.condition}: {g.action}"
            if g.tool_ref:
                header += f" (using {g.tool_ref})"
            lines.append(header)
        text = f"{text}\n" + "\n".join(lines)
    return text.strip()


def _gpt_data(agent: Agent) -> str:
    data: dict = {
        "kind": "GptComponentMetadata",
        "instructions": _gpt_instructions(agent),
        "gptCapabilities": {"webBrowsing": bool(agent.web_search)},
    }
    model = agent.model_hint or agent.model_family
    if model:
        data["aISettings"] = {"model": {"modelNameHint": model}}
    return _dump_yaml(data)


def _conversation_start_data(agent: Agent) -> str:
    data = {
        "kind": "AdaptiveDialog",
        "beginDialog": {
            "kind": "OnConversationStart",
            "id": "main",
            "actions": [
                {
                    "kind": "SendActivity",
                    "id": "sendMessage_welcome",
                    "activity": {"text": [agent.welcome_message]},
                }
            ],
        },
    }
    return _dump_yaml(data)


def _content_types_xml(data_partnames: list[str]) -> str:
    """[Content_Types].xml: declares default xml/json content types plus an
    Override for each component `data` file. PAC's `solution pack` requires it.
    """
    overrides = "".join(
        f'<Override PartName="{escape(p)}" ContentType="application/octet-stream" />'
        for p in data_partnames
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/octet-stream" />'
        '<Default Extension="json" ContentType="application/octet-stream" />'
        f"{overrides}</Types>"
    )


def _customizations_xml() -> str:
    """A minimal customizations.xml (no entities/roles/etc.); required by the
    Dataverse solution package format alongside solution.xml.
    """
    return (
        '<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <Entities></Entities>\n"
        "  <Roles></Roles>\n"
        "  <Workflows></Workflows>\n"
        "  <FieldSecurityProfiles></FieldSecurityProfiles>\n"
        "  <Templates />\n"
        "  <EntityMaps />\n"
        "  <EntityRelationships />\n"
        "  <OrganizationSettings />\n"
        "  <optionsets />\n"
        "  <CustomControls />\n"
        "  <EntityDataProviders />\n"
        "  <Languages>\n    <Language>1033</Language>\n  </Languages>\n"
        "</ImportExportXml>\n"
    )


def _knowledge_data(knowledge_ref) -> str:
    data = {
        "kind": "KnowledgeSourceConfiguration",
        "source": {"kind": "CustomKnowledgeSource", "displayName": knowledge_ref.ref},
    }
    return _dump_yaml(data)


def _review_manifest(agent: Agent) -> dict | None:
    items: list[dict] = []

    for t in agent.tools:
        if t.mcp_server_url:
            item = {
                "type": "mcp_tool",
                "ref": t.ref,
                "detail": (
                    f"In Copilot Studio, add tool → Model Context Protocol and register '{t.ref}' at "
                    f"{t.mcp_server_url}"
                    + (f" (transport: {t.transport})" if t.transport else "")
                    + "; the same MCP server carries over, no rebuild needed."
                ),
            }
            if t.member_tools:
                item["tools"] = t.member_tools
            items.append(item)
        else:
            items.append(
                {
                    "type": "tool",
                    "ref": t.ref,
                    "detail": (
                        f"Tool '{t.ref}' has no automatic Copilot Studio equivalent; recreate it as a "
                        "connector, custom connector, or MCP tool in Copilot Studio."
                    ),
                }
            )

    for k in agent.knowledge:
        items.append(
            {
                "type": "knowledge",
                "ref": k.ref,
                "detail": (
                    f"Knowledge base '{k.ref}' was emitted as a placeholder source; reconnect it to a "
                    "real Copilot Studio knowledge source (SharePoint, Dataverse, a file upload, etc.)."
                ),
            }
        )

    for c in agent.collaborators:
        items.append(
            {
                "type": "collaborator",
                "ref": c.ref,
                "detail": (
                    f"Connected agent '{c.ref}' must exist in the same environment and be wired up as a "
                    "connected agent in Copilot Studio; the reference is not auto-created."
                ),
            }
        )

    if agent.model_hint or agent.model_family:
        items.append(
            {
                "type": "model",
                "detail": (
                    f"Model set to '{agent.model_hint or agent.model_family}'; confirm Copilot Studio "
                    "offers an equivalent model and select it."
                ),
            }
        )

    if not items:
        return None
    return {"agent": agent.name, "review_items": items}


def export_agent(agent: Agent, output_dir: Path) -> ExportResult:
    """Write an IR Agent as a Copilot Studio solution export directory.

    Wraps any filesystem/serialization failure in ExportError so a partial or
    unwritable target surfaces as a clear Wheatear error, not a raw traceback.
    """
    try:
        output_dir = Path(output_dir)
        base = _sanitize(agent.name)
        schema = f"{SCHEMA_PREFIX}_{base}"
        written: list[Path] = []
        # Part-names (leading-slash, solution-root-relative) of every component
        # `data` file, needed for [Content_Types].xml.
        data_partnames: list[str] = []

        _write(output_dir / "solution.xml", _solution_xml(base), written)
        _write(output_dir / "bots" / schema / "bot.xml", _bot_xml(schema, agent.name), written)
        _write(
            output_dir / "bots" / schema / "configuration.json",
            _configuration_json(schema, agent),
            written,
        )

        components = output_dir / "botcomponents"
        gpt_schema = f"{schema}.gpt.default"
        _write(components / gpt_schema / "botcomponent.xml", _botcomponent_xml(gpt_schema, COMPONENT_TYPE_GPT, agent.name), written)
        _write(components / gpt_schema / "data", _gpt_data(agent), written)
        data_partnames.append(f"/botcomponents/{gpt_schema}/data")

        if agent.welcome_message:
            cs_schema = f"{schema}.topic.ConversationStart"
            _write(components / cs_schema / "botcomponent.xml", _botcomponent_xml(cs_schema, COMPONENT_TYPE_TOPIC, "Conversation Start"), written)
            _write(components / cs_schema / "data", _conversation_start_data(agent), written)
            data_partnames.append(f"/botcomponents/{cs_schema}/data")

        for kb in agent.knowledge:
            kb_schema = f"{schema}.knowledge.{_sanitize(kb.ref)}"
            _write(components / kb_schema / "botcomponent.xml", _botcomponent_xml(kb_schema, COMPONENT_TYPE_KNOWLEDGE, kb.ref), written)
            _write(components / kb_schema / "data", _knowledge_data(kb), written)
            data_partnames.append(f"/botcomponents/{kb_schema}/data")

        # Solution-package scaffolding PAC's `solution pack` requires.
        _write(output_dir / "customizations.xml", _customizations_xml(), written)
        _write(output_dir / "[Content_Types].xml", _content_types_xml(data_partnames), written)

        review_manifest_path = None
        manifest = _review_manifest(agent)
        if manifest is not None:
            review_manifest_path = output_dir / "review-manifest.yaml"
            _write(review_manifest_path, _dump_yaml(manifest), written)

        return ExportResult(
            agent_path=output_dir,
            written_paths=written,
            review_manifest_path=review_manifest_path,
        )
    except OSError as exc:
        raise ExportError(f"Failed to write Copilot Studio export to {output_dir}: {exc}") from exc

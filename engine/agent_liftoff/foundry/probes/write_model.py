"""Pass 3: what each platform will accept when you *create* something.

Passes 1 and 2 read records that already exist. That answers "what does an
agent look like", and it is the wrong question for the half of a migration
that writes. A `GET` response is a read model: it carries `id`, `created_on`,
`tenant_id` and `modified_by`, none of which a create call accepts, and it
omits anything the platform takes as input but never echoes back.

An export adapter mapping onto a read model is therefore wrong twice over --
it proposes fields the target will reject, and it never learns about fields the
target requires. On the calibration tenant that showed up as 572 "no
counterpart" flags on one adapter, almost all of them for response metadata.

Both supported platforms declare their write model. Neither needs guessing:

  **Copilot Studio** -- Dataverse publishes attribute metadata per table.
  `EntityDefinitions(LogicalName='bot')/Attributes` returns every column with
  `IsValidForCreate`, `IsValidForUpdate`, `RequiredLevel`, a display name and a
  human description. For `bot` that is 69 attributes, 28 of them creatable.

  **watsonx Orchestrate** -- no OpenAPI document is served (verified: 404 on
  the instance, 500 at the host root). But the ADK, which is already a declared
  dependency, ships the request models as pydantic classes: `AgentSpec`,
  `ToolSpec`. Those are the create contract, and `shape.schema_from_model`
  reads them with the same code that reads Agent Liftoff's own IR.

Everything here is read-only. Discovering a create schema by *doing* a create
is not an option this module will take -- writing to a live tenant to find out
what it accepts belongs behind an explicit deploy step, never a probe.
"""

from __future__ import annotations

from typing import Any

import requests

from agent_liftoff.foundry.probes.base import ProbeContext, ProbeResult
from agent_liftoff.foundry.shape import schema_from_model
from agent_liftoff.foundry.types import (
    EntityKind,
    EntitySchema,
    FieldNode,
    GapReason,
    ProbeGap,
    ProbeOrigin,
)

API_VERSION = "v9.2"

# Dataverse tables whose write model matters, and the entity kind they carry.
# One table can back several entity kinds: a `botcomponent` row is a topic, a
# tool or a knowledge source depending on its `componenttype`, and all three
# are created through the same columns. Listing them separately would leave two
# of the three with no declared write model at all.
DATAVERSE_TABLES: tuple[tuple[str, tuple[EntityKind, ...]], ...] = (
    ("bot", (EntityKind.AGENT,)),
    ("botcomponent", (EntityKind.TOPIC, EntityKind.TOOL, EntityKind.KNOWLEDGE)),
    ("connectionreference", (EntityKind.CONNECTION,)),
)

# Attribute properties to project. `RequiredLevel` and the two labels are the
# ones a reviewer actually reads; the rest is what the mapping needs.
ATTRIBUTE_FIELDS = (
    "LogicalName,AttributeType,IsValidForCreate,IsValidForUpdate,IsValidForRead,"
    "RequiredLevel,Description,DisplayName"
)

# Dataverse attribute types -> JSON types, so a declared field and an observed
# one describe themselves the same way and can be merged.
DATAVERSE_TYPES: dict[str, str] = {
    "String": "string",
    "Memo": "string",
    "Uniqueidentifier": "string",
    "Lookup": "string",
    "Customer": "string",
    "Owner": "string",
    "EntityName": "string",
    "Picklist": "string",
    "State": "integer",
    "Status": "integer",
    "Integer": "integer",
    "BigInt": "integer",
    "Decimal": "number",
    "Double": "number",
    "Money": "number",
    "Boolean": "boolean",
    "DateTime": "string",
    "Virtual": "object",
    "ManagedProperty": "object",
    "Image": "string",
    "File": "string",
}

# Required levels that mean the platform will reject a create without the field.
REQUIRED_LEVELS = {"ApplicationRequired", "SystemRequired"}


def _label(value: Any) -> str | None:
    """Pull the localized label out of a Dataverse label object."""
    if not isinstance(value, dict):
        return None
    localized = value.get("UserLocalizedLabel") or {}
    label = localized.get("Label")
    return label.strip() or None if isinstance(label, str) else None


def _attribute_field(attribute: dict) -> FieldNode | None:
    name = attribute.get("LogicalName")
    if not name:
        return None
    level = (attribute.get("RequiredLevel") or {}).get("Value")
    display = _label(attribute.get("DisplayName"))
    description = _label(attribute.get("Description"))
    if display and description and display.lower() not in description.lower():
        description = f"{display}: {description}"
    return FieldNode(
        path=name,
        types=[DATAVERSE_TYPES.get(attribute.get("AttributeType") or "", "string")],
        required=bool(attribute.get("IsValidForCreate")) and level in REQUIRED_LEVELS,
        # Declared, not observed: this is the platform's own statement, so it
        # is not diluted by how often a field happened to appear in a sample.
        occurrence=1.0,
        description=description or display,
        writable=bool(attribute.get("IsValidForCreate")),
    )


class DataverseWriteModel:
    """Copilot Studio's create model, from Dataverse attribute metadata."""

    name = "copilot-studio-write-model"

    def probe(self, context: ProbeContext) -> ProbeResult:
        result = ProbeResult()
        token = context.extra.get("dataverse_token")
        if not context.allow_network or not context.instance_url or not token:
            result.gaps.append(
                ProbeGap(
                    what="the Copilot Studio create model",
                    reason=GapReason.NO_CREDENTIALS,
                    detail=(
                        "Without Dataverse metadata the corpus describes what records look "
                        "like when read, not what a create accepts -- so an export adapter "
                        "may propose fields the platform rejects."
                    ),
                    remedy="Supply the Dataverse URL and a bearer token for it.",
                )
            )
            return result

        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
            }
        )
        base = context.instance_url.rstrip("/")

        for table, kinds in DATAVERSE_TABLES:
            url = f"{base}/api/data/{API_VERSION}/EntityDefinitions(LogicalName='{table}')/Attributes"
            try:
                response = session.get(url, params={"$select": ATTRIBUTE_FIELDS}, timeout=(10, 60))
            except requests.RequestException as exc:
                result.gaps.append(
                    ProbeGap(
                        what=f"create model for `{table}`",
                        reason=GapReason.API_REFUSED,
                        detail=f"Could not reach {url}: {exc}",
                        remedy="Check the environment URL and network access.",
                    )
                )
                continue

            if response.status_code >= 400:
                result.gaps.append(
                    ProbeGap(
                        what=f"create model for `{table}`",
                        reason=GapReason.API_REFUSED,
                        detail=f"{response.status_code}: {' '.join(response.text.split())[:180]}",
                        remedy="Confirm the token can read entity metadata for this environment.",
                    )
                )
                continue

            try:
                attributes = response.json().get("value") or []
            except ValueError:
                continue

            fields = [f for f in (_attribute_field(a) for a in attributes) if f is not None]
            if not fields:
                continue
            creatable = sum(1 for f in fields if f.writable)
            for kind in kinds:
                result.entities.append(
                    EntitySchema(
                        kind=kind,
                        name=f"{table} (create model)",
                        origin=ProbeOrigin.SCHEMA,
                        sample_count=0,
                        fields=sorted(
                            (f.model_copy(deep=True) for f in fields), key=lambda f: f.path
                        ),
                        notes=[
                            f"Dataverse declares {len(fields)} attribute(s) on `{table}`, "
                            f"{creatable} accepted on create."
                        ],
                    )
                )
        return result


# ----------------------------------------------------------------------
# watsonx Orchestrate
# ----------------------------------------------------------------------

# Which ADK request model is the create contract for each entity kind. Imported
# lazily and by name, because the ADK is an optional dependency and its module
# layout is not ours to depend on at import time.
ADK_SPECS: tuple[tuple[EntityKind, str, str], ...] = (
    (EntityKind.AGENT, "ibm_watsonx_orchestrate.agent_builder.agents", "AgentSpec"),
    (EntityKind.TOOL, "ibm_watsonx_orchestrate.agent_builder.tools", "ToolSpec"),
)


class OrchestrateWriteModel:
    """Orchestrate's create model, from the ADK's own request models.

    No network call: the ADK is a Python package, and its `AgentSpec` /
    `ToolSpec` are the schemas its CLI validates against before POSTing. Using
    them means the write model tracks the ADK version actually installed rather
    than a copy that would rot.
    """

    name = "orchestrate-write-model"

    def probe(self, context: ProbeContext) -> ProbeResult:
        import importlib

        result = ProbeResult()
        for kind, module_name, class_name in ADK_SPECS:
            try:
                module = importlib.import_module(module_name)
                spec = getattr(module, class_name)
            except (ImportError, AttributeError) as exc:
                result.gaps.append(
                    ProbeGap(
                        what=f"the Orchestrate create model for `{kind.value}`",
                        reason=GapReason.UNSUPPORTED,
                        detail=(
                            f"Could not read {module_name}.{class_name} "
                            f"({type(exc).__name__}). The corpus has the read model only, so "
                            "an export adapter may propose fields a create would reject."
                        ),
                        remedy=(
                            "Install the ADK: pip install ibm-watsonx-orchestrate "
                            "(the `watsonx` extra)."
                        ),
                    )
                )
                continue

            fields = schema_from_model(spec, writable=True)
            required = sum(1 for f in fields if f.required and not f.container)
            result.entities.append(
                EntitySchema(
                    kind=kind,
                    name=f"{class_name} (create model)",
                    origin=ProbeOrigin.SCHEMA,
                    sample_count=0,
                    fields=fields,
                    notes=[
                        f"Read from the installed ADK: {module_name}.{class_name}, "
                        f"{len(fields)} field(s), {required} required on create."
                    ],
                )
            )
        return result

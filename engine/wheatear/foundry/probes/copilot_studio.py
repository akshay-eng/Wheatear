"""Pass 2 for Copilot Studio: read the Dataverse tables the export flattens.

A Copilot Studio agent is rows in Dataverse. The solution export is a
projection of those rows onto files, and the projection loses things -- the
`data` payload survives, but the row's own columns (state, publish status,
ownership, the connection reference a component is bound to) mostly do not, and
a custom connector's endpoint never does.

The Web API returns the rows themselves:

    GET {environment}/api/data/v9.2/bots
    GET {environment}/api/data/v9.2/botcomponents
    GET {environment}/api/data/v9.2/connectionreferences

which is the same data the export came from, one layer earlier. Read-only, and
skipped entirely without a token -- in which case the corpus is whatever the
structural pass found, and a gap says so.

Auth is an Azure AD bearer token for the environment's Dataverse URL. Wheatear
already knows how to get one (`connectors/copilot_studio/auth.py`), so a caller
that has a `TokenProvider` passes it; a caller that would rather paste a token
from the browser or `az account get-access-token` supplies it directly. Neither
is stored by this module.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import requests
import yaml

from wheatear.connectors.copilot_studio.solution_importer import (
    COMPONENT_TYPE_DIALOG,
    COMPONENT_TYPE_FILE_KNOWLEDGE,
    COMPONENT_TYPE_GPT,
    COMPONENT_TYPE_KNOWLEDGE,
)
from wheatear.foundry.probes.base import ProbeContext, ProbeResult, observe
from wheatear.foundry.types import EntityKind, GapReason, ProbeGap, ProbeOrigin

API_VERSION = "v9.2"

# Rows per table. Shape inference converges on tens of records; pulling a whole
# tenant would be slower, no more accurate, and a lot more customer data on
# disk than the question needs.
PAGE_SIZE = 200

# Tables worth reading, and what they describe. `botcomponents` is absent
# because its rows span four entity kinds, discriminated per row below.
TABLES: tuple[tuple[str, EntityKind], ...] = (
    ("bots", EntityKind.AGENT),
    ("connectionreferences", EntityKind.CONNECTION),
    ("workflows", EntityKind.WORKFLOW),
)

# The `kind` a componenttype-9 payload declares when it is a connector action
# rather than a conversational topic. Both are "dialogs" to Dataverse; only the
# payload tells them apart.
TOOL_KINDS = {"taskdialog"}


def _component_kind(row: dict) -> EntityKind | None:
    """Which entity kind one `botcomponents` row is.

    componenttype 9 is the awkward one: Copilot stores topics and connector
    actions in the same table under the same type, and only the `kind` inside
    the payload distinguishes them. Mapping them to one kind would produce a
    topic adapter that had to cope with tool records.
    """
    component_type = row.get("componenttype")
    if component_type in (COMPONENT_TYPE_FILE_KNOWLEDGE, COMPONENT_TYPE_KNOWLEDGE):
        return EntityKind.KNOWLEDGE
    if component_type == COMPONENT_TYPE_GPT:
        return EntityKind.AGENT
    if component_type == COMPONENT_TYPE_DIALOG:
        payload = row.get("data")
        declared = ""
        if isinstance(payload, dict):
            declared = str(payload.get("kind") or "")
        return EntityKind.TOOL if declared.lower() in TOOL_KINDS else EntityKind.TOPIC
    return None


def _parse_payload(row: dict) -> dict:
    """Parse the `data` column, which arrives as a YAML string.

    Left as a nested object rather than merged into the row: the row's own
    columns and the payload's fields are two different namespaces, and
    flattening them would collide on `name` and `description`, which both have.
    """
    payload = row.get("data")
    if not isinstance(payload, str) or not payload.strip():
        return row
    try:
        parsed = yaml.safe_load(payload)
    except yaml.YAMLError:
        return row
    return {**row, "data": parsed} if isinstance(parsed, dict) else row


class DataverseProbe:
    """Live hydration for Copilot Studio, over the Dataverse Web API."""

    name = "copilot-studio-live"

    def __init__(self, tokens: Any = None, page_size: int = PAGE_SIZE) -> None:
        # A `connectors.copilot_studio.auth.TokenProvider`, or anything with a
        # `token_for(resource_url)` method. Injected rather than constructed so
        # this module never triggers an interactive device-code flow on its own.
        self.tokens = tokens
        self.page_size = page_size

    # ------------------------------------------------------------------

    def _token(self, context: ProbeContext) -> str | None:
        direct = context.extra.get("dataverse_token")
        if direct:
            return direct
        if self.tokens is not None and context.instance_url:
            try:
                return self.tokens.token_for(context.instance_url)
            except Exception:  # noqa: BLE001 - an auth failure is a gap, not a crash
                return None
        return None

    def probe(self, context: ProbeContext) -> ProbeResult:
        result = ProbeResult()
        if not context.allow_network:
            result.gaps.append(
                ProbeGap(
                    what="live Dataverse environment",
                    reason=GapReason.NO_CREDENTIALS,
                    detail="Network access was disabled for this probe.",
                    remedy="Re-run without --offline to read the Dataverse tables.",
                )
            )
            return result

        token = self._token(context)
        if not context.instance_url or not token:
            result.gaps.append(
                ProbeGap(
                    what="Dataverse rows behind the export",
                    reason=GapReason.NO_CREDENTIALS,
                    detail=(
                        "No environment URL and bearer token were supplied. The corpus has "
                        "the export's projection of these rows, without the columns the "
                        "export drops -- notably custom connector endpoints."
                    ),
                    remedy=(
                        "Supply the environment's Dataverse URL and an access token for it "
                        "(the wizard can authenticate, or paste one from the browser)."
                    ),
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

        for table, kind in TABLES:
            rows = self._read(session, base, table, result)
            entity = observe(kind=kind, name=table, origin=ProbeOrigin.API, records=rows)
            if entity is not None:
                result.entities.append(entity)

        self._probe_components(session, base, result)
        return result

    # ------------------------------------------------------------------

    def _probe_components(self, session, base: str, result: ProbeResult) -> None:
        rows = [_parse_payload(row) for row in self._read(session, base, "botcomponents", result)]
        buckets: dict[EntityKind, list[dict]] = {}
        # Counted by componenttype rather than in total: "12 unrecognised" is
        # alarming and useless, while "type 2 (x5), type 6 (x2)" is a lookup
        # away from knowing whether they matter. On the calibration tenant they
        # are all `msdyn_*` system components -- variables and language
        # -understanding artifacts belonging to Microsoft's default templates,
        # not customer content -- but that is a fact about one tenant, so it is
        # reported rather than assumed.
        unrecognized: Counter[Any] = Counter()
        for row in rows:
            kind = _component_kind(row)
            if kind is None:
                unrecognized[row.get("componenttype")] += 1
                continue
            buckets.setdefault(kind, []).append(row)

        for kind, bucket in sorted(buckets.items(), key=lambda item: item[0].value):
            entity = observe(
                kind=kind, name="botcomponent", origin=ProbeOrigin.API, records=bucket
            )
            if entity is not None:
                result.entities.append(entity)

        if unrecognized:
            breakdown = ", ".join(
                f"type {ct} (x{n})" for ct, n in sorted(unrecognized.items(), key=lambda kv: -kv[1])
            )
            result.gaps.append(
                ProbeGap(
                    what=f"{sum(unrecognized.values())} bot component(s) of an unrecognised type",
                    reason=GapReason.UNSUPPORTED,
                    detail=(
                        "Their `componenttype` maps to no entity kind Wheatear models, so "
                        f"their fields are not in the corpus: {breakdown}."
                    ),
                    remedy=(
                        "If they carry migratable content, extend "
                        "foundry/probes/copilot_studio.py:_component_kind."
                    ),
                )
            )

    def _read(self, session, base: str, table: str, result: ProbeResult) -> list[dict]:
        url = f"{base}/api/data/{API_VERSION}/{table}"
        try:
            response = session.get(url, params={"$top": self.page_size}, timeout=(10, 60))
        except requests.RequestException as exc:
            result.gaps.append(
                ProbeGap(
                    what=f"Dataverse table `{table}`",
                    reason=GapReason.API_REFUSED,
                    detail=f"Could not reach {url}: {exc}",
                    remedy="Check the environment URL is the Dataverse org URL.",
                )
            )
            return []

        if response.status_code >= 400:
            detail = " ".join(response.text.split())[:200]
            reason = (
                GapReason.NO_CREDENTIALS
                if response.status_code in (401, 403)
                else GapReason.API_REFUSED
            )
            result.gaps.append(
                ProbeGap(
                    what=f"Dataverse table `{table}`",
                    reason=reason,
                    detail=f"{response.status_code}: {detail}",
                    remedy=(
                        "Confirm the token is for this environment and the account can read "
                        f"the `{table}` table."
                    ),
                )
            )
            return []

        try:
            payload = response.json()
        except ValueError:
            result.gaps.append(
                ProbeGap(
                    what=f"Dataverse table `{table}`",
                    reason=GapReason.API_REFUSED,
                    detail="The environment returned a non-JSON body.",
                    remedy="Check the URL points at Dataverse, not the maker portal.",
                )
            )
            return []

        rows = payload.get("value") if isinstance(payload, dict) else payload
        return [row for row in (rows or []) if isinstance(row, dict)]

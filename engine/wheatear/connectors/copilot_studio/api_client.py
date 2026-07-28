"""Power Platform + Dataverse client for Copilot Studio auto-discovery.

Provides three operations used by the auto-migration wizard:

  1. list_environments()  -- returns all Power Platform environments the
                             authenticated user/service-principal can see.

  2. list_bots(env)       -- queries the Dataverse Web API for bots in the
                             selected environment.

  3. export_bot(env, bot, dest) -- exports a single bot as a solution ZIP
                             and extracts it to a local directory that
                             solution_importer.import_agent() can read.

The client never stores credentials; it holds a TokenProvider that issues
short-lived access tokens on demand. All HTTP calls use the 'requests'
library (part of the copilot-studio optional extra).
"""

from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wheatear.connectors.copilot_studio import pac_client
from wheatear.connectors.copilot_studio.auth import TokenProvider

# Power Platform management API -- used for environment listing and solution
# export. Tokens must be scoped to this URL.
_MANAGEMENT_URL = "https://service.powerapps.com"
_BAP_API = "https://api.bap.microsoft.com"
_BAP_API_VERSION = "2023-06-01"

# Dataverse Web API version used for all org-scoped queries.
_DATAVERSE_API_VERSION = "v9.2"

# Dataverse solution component type for Copilot Studio bots.
_BOT_COMPONENT_TYPE = 491


class ApiError(Exception):
    pass


@dataclass
class Environment:
    id: str
    display_name: str
    instance_url: str  # Dataverse org URL, e.g. https://org.crm.dynamics.com


@dataclass
class BotInfo:
    id: str
    name: str
    schema_name: str
    description: str


@dataclass
class SolutionInfo:
    id: str
    unique_name: str
    friendly_name: str
    version: str
    managed: bool


class CopilotStudioClient:
    """HTTP client wrapping the Power Platform management API and the
    Dataverse Web API for a specific environment."""

    def __init__(self, tokens: TokenProvider) -> None:
        try:
            import requests
        except ImportError as exc:
            raise ImportError(
                "Auto-discovery mode requires the 'copilot-studio' extra: "
                "pip install wheatear[copilot-studio]"
            ) from exc
        self._tokens = tokens
        self._requests = requests

    # ------------------------------------------------------------------
    # Environment listing
    # ------------------------------------------------------------------

    def list_environments(self) -> list[Environment]:
        """Return all Power Platform environments visible to the auth identity."""
        token = self._tokens.token_for(_MANAGEMENT_URL)
        resp = self._requests.get(
            f"{_BAP_API}/providers/Microsoft.BusinessAppPlatform/environments",
            headers={"Authorization": f"Bearer {token}"},
            params={"api-version": _BAP_API_VERSION, "$expand": "properties/linkedEnvironmentMetadata"},
            timeout=30,
        )
        self._raise_for_status(resp, "list environments")
        envs = []
        for item in resp.json().get("value", []):
            env_id = item.get("name", "")
            display = item.get("properties", {}).get("displayName", env_id)
            meta = item.get("properties", {}).get("linkedEnvironmentMetadata", {})
            instance_url = meta.get("instanceUrl", "").rstrip("/")
            if instance_url:
                envs.append(Environment(id=env_id, display_name=display, instance_url=instance_url))
        return envs

    # ------------------------------------------------------------------
    # Solution listing
    # ------------------------------------------------------------------

    def list_solutions(
        self, env: Environment, unmanaged_only: bool = True
    ) -> list[SolutionInfo]:
        """Return exportable Dataverse solutions in the environment."""
        token = self._tokens.token_for(env.instance_url)
        params = {
            "$select": "solutionid,uniquename,friendlyname,version,ismanaged",
            "$orderby": "friendlyname asc",
        }
        if unmanaged_only:
            params["$filter"] = "ismanaged eq false"
        resp = self._requests.get(
            f"{env.instance_url}/api/data/{_DATAVERSE_API_VERSION}/solutions",
            headers={
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
            },
            params=params,
            timeout=30,
        )
        self._raise_for_status(resp, "list solutions")
        return [
            SolutionInfo(
                id=str(item.get("solutionid", "")),
                unique_name=str(item.get("uniquename", "")),
                friendly_name=str(
                    item.get("friendlyname") or item.get("uniquename") or ""
                ),
                version=str(item.get("version", "")),
                managed=bool(item.get("ismanaged", False)),
            )
            for item in resp.json().get("value", [])
            if item.get("solutionid") and item.get("uniquename")
        ]

    # ------------------------------------------------------------------
    # Bot listing
    # ------------------------------------------------------------------

    def list_bots(self, env: Environment) -> list[BotInfo]:
        """Return all Copilot Studio bots in the given environment."""
        token = self._tokens.token_for(env.instance_url)
        resp = self._requests.get(
            f"{env.instance_url}/api/data/{_DATAVERSE_API_VERSION}/bots",
            headers={
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
            },
            # `description` is not present on the bot table in every
            # Dataverse tenant. Discovery only needs these stable identifiers.
            params={"$select": "botid,name,schemaname"},
            timeout=30,
        )
        self._raise_for_status(resp, "list bots")
        bots = []
        for item in resp.json().get("value", []):
            bots.append(
                BotInfo(
                    id=item.get("botid", ""),
                    name=item.get("name", ""),
                    schema_name=item.get("schemaname", ""),
                    description="",
                )
            )
        return bots

    # ------------------------------------------------------------------
    # Bot export
    # ------------------------------------------------------------------

    def export_bot(self, env: Environment, bot: BotInfo, dest: Path) -> Path:
        """Export a single bot as a Dataverse solution and extract it to
        dest, returning the path that solution_importer.import_agent() expects.

        Steps:
          1. Find which solution contains the bot.
          2. Export that solution via the Dataverse ExportSolution action.
          3. Decode the base64 ZIP response, extract to dest.
        """
        token = self._tokens.token_for(env.instance_url)
        solution_name = self._find_solution_for_bot(env, bot, token)
        return self.export_solution(env, solution_name, dest, token=token)

    def export_solution(
        self,
        env: Environment,
        unique_name: str,
        dest: Path,
        *,
        token: str | None = None,
    ) -> Path:
        """Export and PAC-unpack one solution, matching the TUI procedure."""
        access_token = token or self._tokens.token_for(env.instance_url)
        zip_bytes = self._export_solution(env, unique_name, access_token)
        return self._unpack_solution(zip_bytes, dest)

    def _find_solution_for_bot(self, env: Environment, bot: BotInfo, token: str) -> str:
        """Return the uniquename of the Dataverse solution that contains bot."""
        resp = self._requests.get(
            f"{env.instance_url}/api/data/{_DATAVERSE_API_VERSION}/solutioncomponents",
            headers={
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
            },
            params={
                "$filter": f"objectid eq {bot.id} and componenttype eq {_BOT_COMPONENT_TYPE}",
                "$select": "_solutionid_value",
                "$top": "1",
            },
            timeout=30,
        )
        self._raise_for_status(resp, f"find solution for bot '{bot.name}'")
        items = resp.json().get("value", [])
        if not items:
            # Many bots live in the Default Solution (unmanaged customizations).
            # Fall back rather than hard-failing.
            return "Default"
        solution_id = items[0].get("_solutionid_value")
        return self._get_solution_uniquename(env, solution_id, token)

    def _get_solution_uniquename(self, env: Environment, solution_id: str, token: str) -> str:
        resp = self._requests.get(
            f"{env.instance_url}/api/data/{_DATAVERSE_API_VERSION}/solutions({solution_id})",
            headers={
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
            },
            params={"$select": "uniquename,friendlyname"},
            timeout=30,
        )
        self._raise_for_status(resp, f"fetch solution metadata for {solution_id}")
        return resp.json().get("uniquename", "Default")

    def _export_solution(self, env: Environment, solution_name: str, token: str) -> bytes:
        """Call the Dataverse ExportSolution action and return raw ZIP bytes."""
        resp = self._requests.post(
            f"{env.instance_url}/api/data/{_DATAVERSE_API_VERSION}/ExportSolution",
            headers={
                "Authorization": f"Bearer {token}",
                "OData-MaxVersion": "4.0",
                "OData-Version": "4.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=json.dumps({"SolutionName": solution_name, "Managed": False}),
            timeout=120,  # large solutions can take a while
        )
        self._raise_for_status(resp, f"export solution '{solution_name}'")
        b64 = resp.json().get("ExportSolutionFile", "")
        if not b64:
            raise ApiError(f"ExportSolution returned no file for solution '{solution_name}'.")
        return base64.b64decode(b64)

    def _unpack_solution(self, zip_bytes: bytes, dest: Path) -> Path:
        """Run PAC's offline unpack step over a packed Dataverse export."""
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(zip_bytes)
            tmp_path = Path(tmp.name)
        try:
            pac_client.unpack_solution(tmp_path, dest)
        finally:
            tmp_path.unlink(missing_ok=True)
        return dest

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raise_for_status(self, resp, context: str) -> None:
        if resp.status_code < 400:
            return
        try:
            detail = resp.json().get("error", {}).get("message", resp.text[:300])
        except Exception:
            detail = resp.text[:300]
        raise ApiError(
            f"Power Platform API error while trying to {context} "
            f"(HTTP {resp.status_code}): {detail}"
        )

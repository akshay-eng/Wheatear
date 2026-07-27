"""Pass 2 for watsonx Orchestrate: what the export doesn't carry.

An Orchestrate ADK export is one `agent.yaml` -- the agent's own fields and a
list of tool ids. It says nothing about what those tools *are*, which is
exactly the half a field mapping needs. The live instance does, over two
different services with two different authentication stories:

  the instance API   `/v1/orchestrate/{agents,tools,toolkits}`, IAM-authenticated
                     with an API key. Returns what is installed here.
  the console catalog the global library of installable tools. Rejects IAM
                     tokens outright; needs a browser session cookie. See
                     `connectors/orchestrate/catalog_client.py` for why.

Both are read-only here. Neither is required: without credentials this probe
records a gap naming the credential it wanted and returns, and the corpus is
built from the structural pass alone.
"""

from __future__ import annotations

from typing import Any

from wheatear.errors import RemoteAPIError, WheatearError
from wheatear.foundry.probes.base import ProbeContext, ProbeResult, observe
from wheatear.foundry.types import EntityKind, GapReason, ProbeGap, ProbeOrigin

# How many agents to fetch in full. The list endpoint returns a summary; the
# detail endpoint returns the fields a mapping actually has to produce. One
# request each, so this is a sample, not a sweep -- shape inference converges
# long before a tenant's agent count does.
MAX_DETAIL = 10

# Knowledge bases are only discoverable through the agents that reference
# them, and an agent references few. Bounded for the same reason.
MAX_KNOWLEDGE = 10


def _ids_in(record: dict, needle: str) -> list[str]:
    """Ids referenced by a record under any key containing `needle`.

    Key-shaped rather than key-exact because the field has been spelled
    `knowledge_base_ids` and `knowledge_bases` across versions of this API,
    and a probe that hardcodes one spelling silently stops finding them when
    the other ships.
    """
    found: list[str] = []
    for key, value in record.items():
        if needle not in key.lower():
            continue
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(
                item if isinstance(item, str) else item.get("id", "")
                for item in value
                if isinstance(item, (str, dict))
            )
    return [i for i in found if i]


class OrchestrateProbe:
    """Live hydration for watsonx Orchestrate."""

    name = "orchestrate-live"

    def __init__(self, max_detail: int = MAX_DETAIL) -> None:
        self.max_detail = max_detail

    def probe(self, context: ProbeContext) -> ProbeResult:
        result = ProbeResult()
        if not context.allow_network:
            result.gaps.append(
                ProbeGap(
                    what="live Orchestrate instance",
                    reason=GapReason.NO_CREDENTIALS,
                    detail="Network access was disabled for this probe.",
                    remedy="Re-run without --offline to hydrate from the live instance.",
                )
            )
            return result

        if context.instance_url and context.api_key:
            self._probe_instance(context, result)
        else:
            result.gaps.append(
                ProbeGap(
                    what="installed tools and agents",
                    reason=GapReason.NO_CREDENTIALS,
                    detail=(
                        "No instance URL and API key were supplied, so the corpus has only "
                        "what the export archive carried."
                    ),
                    remedy=(
                        "Set the instance URL and API key (the wizard stores them) and probe "
                        "again to pick up tool schemas."
                    ),
                )
            )

        if context.instance_url:
            self._probe_catalog(context, result)
        return result

    # ------------------------------------------------------------------

    def _probe_instance(self, context: ProbeContext, result: ProbeResult) -> None:
        from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient

        try:
            client = OrchestrateRestClient(context.api_key or "", context.instance_url or "")
        except WheatearError as exc:
            result.gaps.append(
                ProbeGap(
                    what="the Orchestrate instance API",
                    reason=GapReason.API_REFUSED,
                    detail=str(exc),
                    remedy="Check the instance URL and API key, then probe again.",
                )
            )
            return

        agents = self._collect(result, "agents", client.list_agents)
        details: list[dict] = []
        for summary in agents[: self.max_detail]:
            agent_id = summary.get("id") or summary.get("agent_id")
            if not agent_id:
                continue
            try:
                details.append(client.get_agent(str(agent_id)))
            except RemoteAPIError:
                # One unreadable agent costs its detail, not the probe. A
                # tenant routinely has an agent somebody else owns.
                continue

        # Detail records first: they are supersets of the summaries, so the
        # inferred `required` set is judged against the fuller shape.
        agent_records = details + [a for a in agents if isinstance(a, dict)]
        self._add(result, EntityKind.AGENT, "agent", agent_records)
        if details:
            result.notes.append(f"Fetched {len(details)} agent(s) in full from the instance API.")

        tools = self._collect(result, "tools", client.list_all_tools)
        toolkits = self._collect(result, "toolkits", client.list_toolkits)
        self._add(result, EntityKind.TOOL, "tool", tools + toolkits)

        self._probe_knowledge(client, agent_records, result)

    def _probe_knowledge(self, client, agents: list[dict], result: ProbeResult) -> None:
        wanted: list[str] = []
        for agent in agents:
            for kb_id in _ids_in(agent, "knowledge"):
                if kb_id not in wanted:
                    wanted.append(kb_id)
        if not wanted:
            return
        bases = []
        for kb_id in wanted[:MAX_KNOWLEDGE]:
            record = client.get_knowledge_base(kb_id)
            # get_knowledge_base returns an {id, error} stub rather than raising,
            # and a stub would teach the corpus that a knowledge base has an
            # `error` field. Drop those.
            if isinstance(record, dict) and "error" not in record:
                bases.append(record)
        self._add(result, EntityKind.KNOWLEDGE, "knowledge_base", bases)

    def _probe_catalog(self, context: ProbeContext, result: ProbeResult) -> None:
        from wheatear.connectors.orchestrate.catalog_client import OrchestrateCatalogClient

        auth: dict[str, Any] = {}
        if context.session_cookie:
            auth["session_cookie"] = context.session_cookie
        elif context.api_key:
            auth["api_key"] = context.api_key
        else:
            return

        try:
            client = OrchestrateCatalogClient(context.instance_url or "", **auth)
            artifacts = client.list_installable()
        except WheatearError as exc:
            result.gaps.append(
                ProbeGap(
                    what="the global tool catalog",
                    reason=GapReason.REQUIRES_SESSION,
                    detail=str(exc),
                    remedy=(
                        "Open the Orchestrate catalog in a browser, copy the `Cookie:` header "
                        "from any /mfe_catalog request, and supply it as the session cookie."
                    ),
                )
            )
            return

        self._add(result, EntityKind.TOOL, "catalog_artifact", artifacts, ProbeOrigin.SESSION)
        result.notes.append(f"Read {len(artifacts)} installable artifact(s) from the catalog.")

    # ------------------------------------------------------------------

    def _collect(self, result: ProbeResult, what: str, call) -> list[dict]:
        try:
            records = call()
        except RemoteAPIError as exc:
            result.gaps.append(
                ProbeGap(
                    what=f"installed {what}",
                    reason=GapReason.API_REFUSED,
                    detail=str(exc),
                    remedy="Confirm the API key can read this instance and workspace.",
                )
            )
            return []
        return [r for r in records if isinstance(r, dict)]

    def _add(
        self,
        result: ProbeResult,
        kind: EntityKind,
        name: str,
        records: list[dict],
        origin: ProbeOrigin = ProbeOrigin.API,
    ) -> None:
        entity = observe(kind=kind, name=name, origin=origin, records=records)
        if entity is not None:
            result.entities.append(entity)

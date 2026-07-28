"""Adapters from web requests to Agent Liftoff's production migration paths."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from wheatear.service.jobs import Job
from wheatear.service.models import (
    ConnectionConfigureRequest,
    DiscoveredItem,
    DiscoveryResponse,
    MigrationRequest,
    SourceSettings,
    TargetSettings,
    TargetValidationResponse,
)
from wheatear.service.uploads import UploadError, UploadRecord, UploadStore

if TYPE_CHECKING:
    from wheatear.service.copilot_auth import CopilotAuthStore


class _StaticTokenProvider:
    """TokenProvider-compatible wrapper for a user-supplied Dataverse token."""

    def __init__(self, token: str) -> None:
        self.token = token

    def token_for(self, _resource_url: str) -> str:
        return self.token


class _BrandedProvider:
    """Keep an implementation adapter behind the provider selected in the UI."""

    def __init__(self, inner, brand: str) -> None:
        self._inner = inner
        self._brand = brand

    def generate_structured(self, *args, **kwargs):
        try:
            return self._inner.generate_structured(*args, **kwargs)
        except Exception as exc:
            detail = re.sub(
                r"(?i)\b(?:google|gemini)(?:[./_-][\w.-]+)*\b",
                self._brand,
                " ".join(str(exc).split()),
            )
            raise RuntimeError(f"{self._brand} translation request failed: {detail}") from None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def discover_source(
    source: SourceSettings,
    uploads: UploadStore,
    copilot_auth: CopilotAuthStore | None = None,
) -> DiscoveryResponse:
    if source.mode == "upload":
        record = uploads.get(source.upload_id, source.platform)
        return DiscoveryResponse(
            items=record.items,
            message=f"Found {len(record.items)} item(s) in the uploaded export.",
        )

    if source.platform == "n8n":
        from wheatear.connectors.n8n.n8n_client import list_workflows

        workflows = list_workflows(source.base_url, source.api_key)
        items = [
            DiscoveredItem(
                id=workflow.workflow_id,
                name=workflow.name,
                description="Active" if workflow.active else "Inactive",
                active=workflow.active,
                kind="workflow",
            )
            for workflow in workflows
        ]
        return DiscoveryResponse(
            items=items,
            message=f"Connected to n8n. Found {len(items)} workflow(s).",
        )

    client, environment = _copilot_context(source, copilot_auth)
    solutions = client.list_solutions(environment, unmanaged_only=True)
    items = [
        DiscoveredItem(
            id=solution.unique_name,
            name=solution.friendly_name,
            description=solution.unique_name,
            kind="solution",
            source_id=solution.id,
            version=solution.version,
        )
        for solution in solutions
    ]
    return DiscoveryResponse(
        items=items,
        message=f"Connected to Dataverse. Found {len(items)} unmanaged solution(s).",
    )


def scan_copilot_solutions(
    source: SourceSettings,
    solution_ids: list[str],
    uploads: UploadStore,
    copilot_auth: CopilotAuthStore | None = None,
) -> DiscoveryResponse:
    """Export, PAC-unpack, and cache selected solutions just like the TUI."""
    from wheatear.connectors.copilot_studio import pac_client

    client, environment = _copilot_context(source, copilot_auth)
    available = {
        solution.unique_name: solution
        for solution in client.list_solutions(environment, unmanaged_only=True)
    }
    missing = [solution_id for solution_id in solution_ids if solution_id not in available]
    if missing:
        raise RuntimeError(
            "These solutions are no longer available: " + ", ".join(missing)
        )

    record = None
    if source.scan_id:
        try:
            record = uploads.get(source.scan_id, "copilot-studio")
        except UploadError:
            record = None
    if record is None:
        scan_id, root = uploads.reserve()
        record = UploadRecord(
            upload_id=scan_id,
            platform="copilot-studio",
            root=root,
            source_path=root,
            items=[],
            created_at=time.time(),
        )

    cached = set(record.solutions)
    issues: list[str] = []
    for solution_id in solution_ids:
        if solution_id in record.solutions:
            continue
        solution = available[solution_id]
        destination = record.root / f"solution-{_safe_name(solution.unique_name)}"
        try:
            unpacked = client.export_solution(
                environment,
                solution.unique_name,
                destination,
            )
            bots = pac_client.list_bots_in_solution(unpacked)
        except Exception as exc:  # noqa: BLE001 - one solution must not sink the batch
            shutil.rmtree(destination, ignore_errors=True)
            issues.append(
                f"{solution.friendly_name}: {' '.join(str(exc).split())[:180]}"
            )
            continue
        record.solutions[solution.unique_name] = unpacked
        record.items.extend(
            DiscoveredItem(
                id=f"{solution.unique_name}::{schema}",
                name=display_name,
                description=f"Schema: {schema}",
                kind="agent",
                source_id=schema,
                group_id=solution.unique_name,
                group_name=solution.friendly_name,
                version=solution.version,
            )
            for schema, display_name in bots
        )

    record.created_at = time.time()
    uploads.put(record)
    selected = set(solution_ids)
    items = [item for item in record.items if item.group_id in selected]
    groups_with_agents = len({item.group_id for item in items})
    reused = len(selected & cached)
    scanned = len(solution_ids) - reused
    return DiscoveryResponse(
        items=items,
        scan_id=record.upload_id,
        message=(
            f"Scanned {scanned} solution(s)"
            + (f" and reused {reused} cached solution(s)" if reused else "")
            + f". Found {len(items)} agent(s) across {groups_with_agents} solution(s)."
        ),
        issues=issues,
    )


def validate_target(target: TargetSettings) -> TargetValidationResponse:
    from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient

    client = OrchestrateRestClient(
        target.api_key, target.instance_url, target.workspace_id
    )
    agents = client.list_agents()
    if target.console_cookie:
        # Construction validates that the complete cookie carries the CSRF
        # material needed by the console catalog. The catalog itself is read
        # during execution, where its progress belongs in the live log.
        from wheatear.connectors.orchestrate.catalog_client import OrchestrateCatalogClient

        OrchestrateCatalogClient(
            target.instance_url, session_cookie=target.console_cookie
        )
    return TargetValidationResponse(
        message=f"Connected to watsonx Orchestrate. {len(agents)} agent(s) currently on target.",
        agent_count=len(agents),
    )


def configure_target_connection(payload: ConnectionConfigureRequest) -> dict:
    """Configure one reviewed connection and submit its secret exactly once."""
    from wheatear.connectors.orchestrate import provisioning
    from wheatear.connectors.orchestrate.adk_session import ensure_session
    from wheatear.connectors.orchestrate.connections import list_applications
    from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient
    from wheatear.pipeline.solution_migration import adk_cli

    fields = provisioning.CREDENTIAL_FIELDS[payload.kind]
    expected = {name for name, _label, _secret in fields}
    supplied = {name for name, value in payload.credentials.items() if value}
    if payload.preference == "team":
        missing = sorted(expected - supplied)
        unexpected = sorted(supplied - expected)
        if missing:
            raise ValueError(
                "Enter " + ", ".join(_field_label(fields, name) for name in missing) + "."
            )
        if unexpected:
            raise ValueError(
                "Unexpected credential field(s): " + ", ".join(unexpected) + "."
            )
    else:
        payload.credentials.clear()

    if payload.server_url and not provisioning.looks_like_a_url(payload.server_url):
        raise ValueError(
            "The server URL must look like https://your-system.example.com."
        )

    client = OrchestrateRestClient(
        payload.target.api_key,
        payload.target.instance_url,
        payload.target.workspace_id,
    )
    token = client._session.headers["Authorization"].split()[1]
    ensure_session(
        payload.target.instance_url,
        payload.target.api_key,
        adk_cli(),
    )
    request = provisioning.CredentialRequest(
        app_id=payload.app_id,
        kind=payload.kind,
        environment=payload.environment,
        preference=payload.preference,
        server_url=payload.server_url or None,
    )
    actions = provisioning.provision(
        request,
        payload.credentials if payload.preference == "team" else None,
    )
    current = next(
        (
            item
            for item in list_applications(payload.target.instance_url, token)
            if item.app_id == payload.app_id
            and item.environment == payload.environment
        ),
        None,
    )
    return {
        "message": f"Configured {payload.app_id}.",
        "actions": actions,
        "connection": _connection_state(current),
    }


def build_runner(
    uploads: UploadStore, foundry_root: Path | None = None
) -> Callable[[Job, MigrationRequest], dict]:
    store_root = Path(foundry_root) if foundry_root else uploads.root.parent / "foundry"

    def run(job: Job, request: MigrationRequest) -> dict:
        return run_migration(job, request, uploads, store_root)

    return run


def run_migration(
    job: Job,
    request: MigrationRequest,
    uploads: UploadStore,
    foundry_root: Path,
) -> dict:
    from wheatear.connectors.orchestrate.rest_client import OrchestrateRestClient

    job.emit("target", "Authenticating with IBM Cloud IAM.")
    client = OrchestrateRestClient(
        request.target.api_key,
        request.target.instance_url,
        request.target.workspace_id,
    )
    token = client._session.headers["Authorization"].split()[1]
    job.emit("target", "Target credentials accepted.", "ok")
    provider = _provider(request)

    output_root = job.run_dir / "output"
    output_root.mkdir(parents=True)
    if request.source.platform == "n8n":
        summary = _run_n8n(job, request, uploads, client, provider, output_root)
    else:
        summary = _run_copilot(
            job,
            request,
            uploads,
            client,
            token,
            provider,
            output_root,
            foundry_root,
        )

    if request.target.deploy:
        try:
            summary["connection_reviews"] = _connection_reviews(
                client,
                request.target.instance_url,
                token,
                summary.get("agents") or [],
            )
            count = len(summary["connection_reviews"])
            job.emit(
                "connections",
                (
                    f"Found {count} migrated tool connection(s) for confirmation."
                    if count
                    else "No migrated tool declares a target connection."
                ),
                "ok",
            )
        except Exception as exc:  # noqa: BLE001 - migration output remains usable
            summary["connection_reviews"] = []
            summary["connection_review_error"] = (
                "Could not inspect target connections: "
                + " ".join(str(exc).split())[:180]
            )
            job.emit("connections", summary["connection_review_error"], "warn")
    else:
        summary["connection_reviews"] = []

    from wheatear.service.documents import write_evaluation_pack

    summary["documents"] = write_evaluation_pack(output_root, request, summary)
    job.emit("documents", "Wrote the evaluation and migration review pack.", "ok")
    archive_base = job.run_dir / "agent-liftoff-results"
    archive = shutil.make_archive(str(archive_base), "zip", output_root)
    job.artifact_path = Path(archive)
    summary["artifact"] = job.artifact_path.name
    return summary


def _provider(request: MigrationRequest):
    if request.translation.provider == "none":
        return None
    from wheatear.llm.factory import build_provider

    provider_name = (
        "google" if request.translation.provider == "watsonx" else request.translation.provider
    )
    provider = build_provider(provider_name, request.translation.api_key)
    if request.translation.provider == "watsonx":
        return _BrandedProvider(provider, "IBM watsonx")
    return provider


def _copilot_client(source: SourceSettings):
    from wheatear.connectors.copilot_studio.api_client import (
        CopilotStudioClient,
        Environment,
    )

    environment = Environment(
        id="direct",
        display_name=source.environment_url,
        instance_url=source.environment_url.rstrip("/"),
    )
    return CopilotStudioClient(_StaticTokenProvider(source.access_token)), environment


def _copilot_context(
    source: SourceSettings,
    copilot_auth: CopilotAuthStore | None,
):
    if source.auth_session_id:
        if copilot_auth is None:
            raise RuntimeError("The Microsoft sign-in session is unavailable.")
        return copilot_auth.context(
            source.auth_session_id,
            source.environment_id,
        )
    return _copilot_client(source)


def _run_copilot(
    job: Job,
    request: MigrationRequest,
    uploads: UploadStore,
    client,
    token: str,
    provider,
    output_root: Path,
    foundry_root: Path,
) -> dict:
    from wheatear.foundry.store import FoundryStore
    from wheatear.pipeline.resolve import build_marketplace_catalog
    from wheatear.pipeline.solution_migration import (
        MigrationReport,
        adapters_ready,
        migrate_solution,
    )

    store = FoundryStore(foundry_root)
    ready, detail = adapters_ready(store)
    if not ready:
        raise RuntimeError(detail)
    job.emit("foundry", detail, "ok")

    marketplace = None
    if request.target.console_cookie:
        try:
            from wheatear.connectors.orchestrate.catalog_client import (
                OrchestrateCatalogClient,
                to_artifacts,
            )

            job.emit("catalog", "Reading the live Orchestrate catalog.")
            catalog = OrchestrateCatalogClient(
                request.target.instance_url,
                session_cookie=request.target.console_cookie,
            )
            marketplace = build_marketplace_catalog(
                to_artifacts(catalog.list_installable())
            )
            job.emit(
                "catalog",
                f"Loaded {len(marketplace)} live catalog entries.",
                "ok",
            )
        except Exception as exc:  # noqa: BLE001 - shipped snapshot is a valid fallback
            job.emit(
                "catalog",
                f"Live catalog unavailable ({exc}); using the shipped snapshot.",
                "warn",
            )

    record_id = (
        request.source.upload_id
        if request.source.mode == "upload"
        else request.source.scan_id
    )
    if not record_id:
        raise RuntimeError("Scan the selected Copilot Studio solutions before migrating.")
    record = uploads.get(record_id, "copilot-studio")
    selected = set(request.source.selected_ids)
    selected_items = [item for item in record.items if item.id in selected]
    if not selected_items:
        raise RuntimeError("None of the selected agents are present in the cached scan.")

    solutions: list[tuple[Path, set[str], str]] = []
    for group_id in dict.fromkeys(item.group_id for item in selected_items):
        group_items = [item for item in selected_items if item.group_id == group_id]
        solution_path = record.solutions.get(group_id)
        if solution_path is None:
            raise RuntimeError(f"The cached solution {group_id} has expired.")
        wanted = {item.source_id or item.id for item in group_items}
        label = ", ".join(item.name for item in group_items)
        solutions.append((solution_path, wanted, label))

    combined = MigrationReport(output_dir=output_root)
    for index, (solution, wanted, label) in enumerate(solutions, 1):
        job.emit("source", f"Migrating {label}.", "ok")

        def report(event) -> None:
            job.emit(event.stage, event.text, event.level)

        result = migrate_solution(
            solution,
            output_root / f"solution-{index}",
            store=store,
            client=client,
            provider=provider,
            marketplace=marketplace,
            instance_url=request.target.instance_url,
            token=token,
            api_key=request.target.api_key,
            on_conflict=request.target.on_conflict,
            dry_run=not request.target.deploy,
            only=wanted,
            report=report,
        )
        combined.agents.extend(result.agents)
        combined.manual_steps.extend(result.manual_steps)
        combined.knowledge_bases.extend(result.knowledge_bases)
        combined.notes.extend(result.notes)
        combined.pending.extend(result.pending)

    source_names = {
        item.source_id or item.id: item.name
        for item in selected_items
    }
    return {
        "source": "copilot-studio",
        "deployed": len(combined.deployed),
        "processed": len(combined.agents),
        "failed": len(combined.failed),
        "manual_steps": len(combined.manual_steps),
        "follow_up": [
            {
                "kind": step.kind,
                "title": step.title,
                "detail": step.detail,
                "where": step.where,
                "agents": step.agents,
                "command": step.command,
                "blocking": step.blocking,
            }
            for step in combined.manual_steps
        ],
        "pending_tools": [
            {
                "install_ref": item.install_ref,
                "title": item.title,
                "agents": item.agents,
                "connections": item.connections,
                "can_auto_install": bool(item.artifact_id),
            }
            for item in combined.pending
        ],
        "knowledge_bases": combined.knowledge_bases,
        "notes": combined.notes,
        "dry_run": not request.target.deploy,
        "agents": [
            {
                "name": outcome.name,
                "source_name": source_names.get(outcome.source_key, outcome.source_key),
                "id": outcome.agent_id,
                "deployed": outcome.deployed,
                "detail": outcome.detail,
                "tools": outcome.tools,
                "dropped": outcome.dropped,
                "knowledge": outcome.knowledge,
                "collaborators": outcome.collaborators,
            }
            for outcome in combined.agents
        ],
    }


def _run_n8n(
    job: Job,
    request: MigrationRequest,
    uploads: UploadStore,
    client,
    provider,
    output_root: Path,
) -> dict:
    from wheatear.connectors.n8n import importer, n8n_client
    from wheatear.connectors.orchestrate.catalog import connector_resolver
    from wheatear.connectors.orchestrate.exporter import export_agent
    from wheatear.connectors.orchestrate.provisioner import provision_and_deploy
    from wheatear.pipeline.map import map_agent
    from wheatear.pipeline.translate import deterministic_instructions, translate_agent
    from wheatear.pipeline.validate import validate_agent
    from wheatear.workflow import reachable_ids

    if request.source.mode == "upload":
        record = uploads.get(request.source.upload_id, "n8n")
        raw = importer._load_json_files(record.source_path)
    else:
        workflows = n8n_client.list_workflows(
            request.source.base_url, request.source.api_key
        )
        job.emit("source", f"Discovered {len(workflows)} n8n workflow(s).", "ok")
        raw = n8n_client.fetch_all_workflows(
            request.source.base_url,
            request.source.api_key,
            [workflow.workflow_id for workflow in workflows],
        )

    job.emit("extract", "Parsing the n8n workflow graph.")
    bundle = importer.import_workflows(raw)
    source_names = {
        str(workflow.get("id")): str(workflow.get("name"))
        for workflow in raw
    }
    selected_names = {
        source_names[item]
        for item in request.source.selected_ids
        if item in source_names
    }

    def neighbors(name: str) -> list[str]:
        agent = bundle.workflow.by_name(name)
        return [ref.ref for ref in agent.collaborators] if agent else []

    present = {agent.name for agent in bundle.workflow.agents}
    selected = set(reachable_ids(selected_names & present, neighbors))
    ordered = [
        agent
        for agent in bundle.workflow.migration_order()
        if agent.name in selected
    ]
    if not ordered:
        raise RuntimeError("The selected workflows contain no n8n agent nodes.")
    job.emit(
        "compose",
        f"Selected {len(ordered)} agent(s), including collaborator dependencies.",
        "ok",
    )

    results_by_name = {result.agent.name: result for result in bundle.results}
    resolver = connector_resolver()
    for agent in ordered:
        result = results_by_name[agent.name]
        job.emit("map", f"{agent.name}: resolving tools and connections.")
        map_agent(
            result,
            target_platform="orchestrate",
            connector_resolver=resolver,
        )
        if provider is None:
            deterministic_instructions(agent)
        else:
            job.emit("translate", f"{agent.name}: translating instructions with the LLM.")
            translate_agent(agent, provider)
        validation = validate_agent(agent)
        for issue in validation.issues:
            job.emit(
                "validate",
                f"{agent.name}: {issue.field}: {issue.message}",
                "error" if issue.severity == "error" else "warn",
            )
        if not validation.is_valid:
            raise RuntimeError(f"Validation failed for {agent.name}.")
        result_dir = output_root / _safe_name(agent.name)
        exported = export_agent(agent, result_dir, llm=request.target.model)
        job.emit("export", f"{agent.name}: wrote {exported.agent_path}.", "ok")

    selected_results = {
        agent.name: results_by_name[agent.name] for agent in ordered
    }
    reports = []
    if request.target.deploy:
        try:
            from wheatear.connectors.orchestrate.adk_session import ensure_session
            from wheatear.pipeline.solution_migration import adk_cli

            env_name = ensure_session(
                request.target.instance_url,
                request.target.api_key,
                adk_cli(),
            )
            job.emit("target", f"Activated Orchestrate environment {env_name}.", "ok")
        except Exception as exc:  # noqa: BLE001 - REST deploy can still proceed
            job.emit(
                "target",
                f"ADK session activation failed ({exc}); REST deployment will continue.",
                "warn",
            )

        reports = provision_and_deploy(
            client,
            bundle.workflow,
            selected_results,
            request.target.model,
            provider=provider,
            on_progress=lambda message: job.emit("deploy", message),
            on_conflict=request.target.on_conflict,
        )
        for report in reports:
            job.emit(
                "deploy",
                f"{report.name}: {'deployed' if report.ok else report.error or 'failed'}.",
                "ok" if report.ok else "error",
            )

    failed = [report for report in reports if not report.ok]
    return {
        "source": "n8n",
        "deployed": len([report for report in reports if report.ok]),
        "processed": len(ordered),
        "failed": len(failed),
        "manual_steps": 0,
        "follow_up": [],
        "pending_tools": [],
        "dry_run": not request.target.deploy,
        "agents": [
            {
                "name": report.name,
                "source_name": report.name,
                "deployed": report.ok,
                "detail": report.error or report.validation,
                "tools": report.tools,
                "dropped": [],
            }
            for report in reports
        ]
        or [
            {
                "name": agent.name,
                "source_name": agent.name,
                "deployed": None,
                "detail": "Artifacts written",
                "tools": [tool.ref for tool in agent.tools],
                "dropped": [],
            }
            for agent in ordered
        ],
    }


def _safe_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return cleaned.strip("_") or "agent"


def _field_label(fields, field_name: str) -> str:
    return next(
        (label for name, label, _secret in fields if name == field_name),
        field_name.replace("_", " "),
    )


def _auth_options() -> list[dict]:
    from wheatear.connectors.orchestrate.provisioning import CREDENTIAL_FIELDS

    labels = {
        "basic_auth": "Username and password",
        "bearer_token": "Bearer token",
        "api_key_auth": "API key",
        "oauth2_auth_code": "OAuth authorization code",
        "oauth2_client_creds": "OAuth client credentials",
        "oauth2_password": "OAuth password",
    }
    return [
        {
            "value": kind,
            "label": labels[kind],
            "fields": [
                {"name": name, "label": label, "secret": secret}
                for name, label, secret in CREDENTIAL_FIELDS[kind]
            ],
        }
        for kind in labels
    ]


def _default_auth_kind(connection) -> str:
    scheme = getattr(connection, "security_scheme", "") or ""
    if scheme == "oauth2":
        return "oauth2_auth_code"
    if scheme in {"basic_auth", "bearer_token", "api_key_auth"}:
        return scheme
    return "basic_auth"


def _connection_state(connection) -> dict:
    if connection is None:
        return {
            "ready": False,
            "configured": False,
            "credentials_entered": False,
            "preference": "",
            "security_scheme": "",
            "server_url": "",
            "summary": "Not configured on this target.",
        }
    return {
        "ready": connection.ready,
        "configured": connection.is_configured,
        "credentials_entered": connection.credentials_entered,
        "preference": connection.preference or "",
        "security_scheme": connection.security_scheme or "",
        "server_url": connection.server_url or "",
        "summary": connection.summary(),
    }


def _connection_reviews(
    client,
    instance_url: str,
    token: str,
    agents: list[dict],
) -> list[dict]:
    """Describe every connection actually bound to a carried target tool."""
    from wheatear.connectors.orchestrate.connections import (
        bound_connection,
        list_applications,
    )

    carried = {
        str(tool)
        for agent in agents
        for tool in (agent.get("tools") or [])
        if tool
    }
    if not carried:
        return []
    records = {
        str(tool.get("name")): tool
        for tool in client.list_all_tools()
        if tool.get("name")
    }
    state = {}
    for connection in list_applications(instance_url, token):
        if connection.environment == "draft" or connection.app_id not in state:
            state[connection.app_id] = connection

    wanted: dict[str, list[str]] = {}
    for tool_name in sorted(carried):
        record = records.get(tool_name)
        if record is None:
            continue
        for app_id in bound_connection(record).app_ids:
            wanted.setdefault(app_id, []).append(tool_name)

    options = _auth_options()
    reviews = []
    for app_id, tools in sorted(wanted.items()):
        current = state.get(app_id)
        reviews.append(
            {
                "app_id": app_id,
                "environment": "draft",
                "tools": tools,
                "default_kind": _default_auth_kind(current),
                "auth_options": options,
                **_connection_state(current),
            }
        )
    return reviews

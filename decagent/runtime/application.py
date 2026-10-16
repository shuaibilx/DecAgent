"""Assemble explicit control-plane callbacks; no repository code runs in this process."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from urllib.parse import quote

import httpx

from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_client import (
    MCPAuthenticationError,
    MCPConnectionManager,
    MCPProtocolError,
    MCPToolError,
    MCPTransportError,
)
from decagent.executor.protocol import TaskControlRequest
from decagent.gateway import GatewayCallbacks, create_app
from decagent.gateway.app import ApiError, GatewayRedactor
from decagent.gateway.auth import OwnerAuth
from decagent.integrations.delivery import DeliveryService
from decagent.integrations.github import API_VERSION, GitHub, GitHubError, branch_name, require_sha
from decagent.memory import MemoryManager
from decagent.storage.database import Database
from decagent.storage.evidence import EvidenceStore
from decagent.tools.repair.broker_backend import HttpBrokerClient

from .config import RuntimeConfig
from .context import current_context_for


def create_workbench(
    settings,
    *,
    database=None,
    runtime_config=None,
    github_transport=None,
    broker_client=None,
    live_diagnostic=None,
    publication_reconciler=None,
):
    database = database or Database(settings.database_url)
    database.check_version(settings.graph_version)
    database.check_version("business-2", task_kind="business")
    config = runtime_config or RuntimeConfig.from_env()
    database.redactor = GatewayRedactor(
        known_secrets=tuple(
            filter(
                None,
                (
                    settings.database_url,
                    settings.github_token,
                    settings.main_api_key,
                    settings.jev_api_key,
                    config.broker_token,
                ),
            )
        ),
        max_text_chars=1_048_576,
    )
    http = None
    broker = broker_client
    owns_broker = False
    employee_origin = OwnerAuth(database, settings.public_url)
    employee_auth = EmployeeAuth(
        database, employee_origin.origin, employee_origin.secure, settings.data_dir / "demo-auth"
    )
    mcp_manager = None
    employee_clients = {}
    employee_client_lock = Lock()

    def employee_mcp_client(identity, *, refresh=False):
        nonlocal mcp_manager
        if identity.source != "employee_session":
            raise ApiError(401, "employee_auth_required", "Employee browser session required")
        with employee_client_lock:
            if refresh:
                previous = employee_clients.pop(identity.token_hash, None)
                if previous:
                    previous.close()
            client = employee_clients.get(identity.token_hash)
            if client is None:
                if mcp_manager is None:
                    mcp_manager = MCPConnectionManager(config.tickets_mcp_url, employee_auth)
                issued = employee_auth.issue_mcp(identity)
                token = Path(issued["credential_path"]).read_text(encoding="utf-8").strip()
                principal = employee_auth.authenticate_mcp_token(token)
                if principal is None or principal.employee_id != identity.employee_id:
                    raise ApiError(401, "employee_auth_required", "Employee credential unavailable")
                client = mcp_manager.client(principal, token)
                employee_clients[identity.token_hash] = client
            return client

    def employee_read(identity, command, params):
        try:
            client = employee_mcp_client(identity)
            try:
                return client.read(command, params)
            except MCPAuthenticationError:
                return employee_mcp_client(identity, refresh=True).read(command, params)
        except MCPToolError as exc:
            status = {"not_found": 404, "forbidden": 403, "invalid_input": 422}.get(exc.code, 409)
            raise ApiError(status, exc.code, str(exc)) from exc
        except (MCPTransportError, MCPProtocolError) as exc:
            raise ApiError(503, "service_unavailable", "Business MCP service unavailable") from exc
        except (MCPAuthenticationError, ValueError) as exc:
            raise ApiError(401, "employee_auth_required", "Employee session unavailable") from exc

    def github_http():
        nonlocal http
        if http is None:
            http = httpx.Client(
                timeout=15, follow_redirects=False, trust_env=False, transport=github_transport
            )
        return http

    def repair_broker():
        nonlocal broker, owns_broker
        if broker is None and config.broker_token:
            broker = HttpBrokerClient(config.broker_url, config.broker_token)
            owns_broker = True
        if broker is None:
            raise ApiError(503, "integration_unavailable", "Repair executor is not connected")
        return broker

    def github(project):
        if not settings.github_token:
            raise ApiError(503, "github_unavailable", "GitHub credential is not configured")
        return GitHub(
            project["repository"],
            settings.github_token,
            client=github_http(),
            repository_id=project["trust"].get("repository_id"),
        )

    def authenticated_login():
        if not settings.github_token:
            raise ApiError(503, "github_unavailable", "GitHub credential is not configured")
        response = github_http().get(
            "https://api.github.com/user",
            headers={
                "Authorization": "Bearer " + settings.github_token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        if response.status_code != 200:
            raise GitHubError("GitHub owner authentication could not be verified")
        login = response.json().get("login")
        if not isinstance(login, str) or not login or len(login) > 100:
            raise GitHubError("GitHub authenticated owner identity is unavailable")
        return login

    def diagnose(project):
        if not settings.github_token:
            return {"status": "unavailable", "reason": "GitHub credential is not configured"}
        try:
            account = authenticated_login()
            scoped = github(project)
            diagnostic = asdict(scoped.diagnose())
            repository = scoped.get("")
            owner = repository.get("owner", {}).get("login")
            if (
                not isinstance(owner, str)
                or owner.casefold() != project["repository"].split("/", 1)[0].casefold()
            ):
                raise GitHubError("Repository owner differs from registered scope")
            owned = account.casefold() == owner.casefold()
            return {
                **diagnostic,
                "status": "verified",
                "code_origin": "owner" if owned else "external",
                "authenticated_owner_login": account,
                "repository_owner_login": owner,
                "ownership_verified": True,
                "source": "github_authenticated_diagnostic",
            }
        except (GitHubError, httpx.HTTPError, ValueError):
            return {"status": "unavailable", "reason": "GitHub read-only identity check failed"}

    def resolve(project, ref):
        branch_name(ref)
        data = github(project).get("commits/" + quote(ref, safe=""))
        return require_sha(data.get("sha"))

    def observed_publication(task_id, _operation_id):
        task = database.get_task(task_id)
        project = database.get_project(task["project_id"])
        try:
            delivery = DeliveryService(
                database,
                EvidenceStore(settings.data_dir / "evidence", database.redactor.redact_text),
                github(project),
                author_name=config.author_name,
                author_email=config.author_email,
            ).reconcile_observed(
                task_id,
                expected_generation=task["lease_generation"],
                expected_lease_owner=task["lease_owner"],
            )
            return {
                "reconciled": delivery["status"] in {"draft", "closed_unmerged"},
                "status": delivery["status"],
                "pr_url": delivery["pr_url"],
            }
        except (GitHubError, ValueError):
            return {"reconciled": False, "reason": "Publication outcome is not confirmed"}

    reconcile_publication = publication_reconciler or observed_publication

    def control(task_id, operation_id, *, reconcile=False):
        task = database.get_task(task_id)
        request = TaskControlRequest(
            task_id=task_id, owner=task["lease_owner"], lease_generation=task["lease_generation"]
        )
        path = "/v1/tasks/reconcile" if reconcile else "/v1/tasks/stop"
        receipt = repair_broker()(path, request.model_dump())
        if (
            not isinstance(receipt, dict)
            or type(receipt.get("lease_generation")) is not int
            or receipt["lease_generation"] != task["lease_generation"]
        ):
            raise ApiError(
                409, "control_unconfirmed", "Broker control receipt does not match the lease"
            )
        if reconcile:
            with database.connect() as conn:
                delivery = conn.execute(
                    "SELECT * FROM app.deliveries WHERE task_id=%s "
                    "AND status IN ('unknown','publishing')",
                    (task_id,),
                ).fetchone()
            if delivery:
                publication = reconcile_publication(task_id, operation_id)
                if not isinstance(publication, dict) or publication.get("reconciled") is not True:
                    return {**receipt, "reconciled": False, "publication": publication}
                return {**receipt, "publication": publication}
        return receipt

    def manager(_project_id, scoped_database):
        # Memory review uses RuleStrategy and never invokes a paid model.
        return MemoryManager(
            scoped_database,
            EvidenceStore(settings.data_dir / "evidence", database.redactor.redact_text),
        )

    def local_live(target, operation_id):
        if live_diagnostic is not None:
            return live_diagnostic(target, operation_id)
        if target in {"main", "jev"}:
            from .model_diagnostics import ModelDiagnostics

            result = ModelDiagnostics(database, settings, config)(target, operation_id)
            if result["status"] == "unavailable":
                raise ApiError(503, "diagnostic_unavailable", result["reason"])
            return result
        if target == "github":
            return {
                "status": "verified",
                "authenticated_owner_login": authenticated_login(),
                "external_requests": 1,
                "paid_requests": 0,
            }
        if target == "executor" and config.broker_token:
            response = repair_broker().client.get("/health")
            response.raise_for_status()
            return {"status": "verified", "receipt": response.json(), "paid_requests": 0}
        raise ApiError(
            503, "diagnostic_unavailable", "Explicit diagnostic is not connected for this target"
        )

    callbacks = GatewayCallbacks(
        current_policy_context=lambda task_id: current_context_for(database, settings, task_id),
        memory_manager_factory=manager,
        cancel_task=(lambda task_id, operation_id: control(task_id, operation_id))
        if broker or config.broker_token
        else None,
        reconcile_task=(
            lambda task_id, operation_id: control(task_id, operation_id, reconcile=True)
        )
        if broker or config.broker_token
        else None,
        live_diagnostic=local_live,
        resolve_base=resolve,
        project_diagnostic=diagnose,
        business_employee_read=employee_read,
    )
    app = create_app(database, settings, callbacks=callbacks)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if http is not None:
                http.close()
            if owns_broker:
                broker.close()
            if mcp_manager is not None:
                await mcp_manager.aclose()

    app.router.lifespan_context = lifespan
    return app

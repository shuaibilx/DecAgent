"""Authenticated control plane. It queues durable requests and never executes repository code."""

import asyncio
import hashlib
import json
import re
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb
from starlette.exceptions import HTTPException

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.employee_auth import COOKIE_NAME as EMPLOYEE_COOKIE_NAME
from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_client import MCPToolError
from decagent.business.operations import OperationService
from decagent.policy import (
    Action,
    ApprovalKind,
    ApprovalReply,
    CodeTrustRequest,
    ReplyDecision,
    TrustKind,
    action_digest,
    issue_code_trust,
)
from decagent.policy.approvals import confirmation_digest
from decagent.policy.canonical import primitive
from decagent.policy.redaction import Redactor
from decagent.storage.approval_service import DurableApprovals
from decagent.storage.safe_record import exact_safe_record

from .auth import COOKIE_NAME, OwnerAuth
from .business import BusinessControl
from .models import (
    BusinessOperationReplyInput,
    BusinessTaskInput,
    DecisionThresholdInput,
    DiagnosticInput,
    EmployeeLoginInput,
    MemoryReviewInput,
    MemoryRevokeInput,
    ProjectInput,
    ReplyInput,
    ResponseInput,
    ResumeInput,
    SemanticReplyInput,
    SessionInput,
    TaskInput,
)

TERMINAL = {"succeeded", "failed", "cancelled"}


class GatewayRedactor(Redactor):
    def redact_text(self, value):
        return super().redact_text(re.sub(r"\bdca_[A-Za-z0-9_-]{43,}\b", "[REDACTED]", value))


@dataclass(frozen=True)
class GatewayCallbacks:
    """Explicit trusted integrations; None means unavailable, never successful."""

    current_policy_context: Callable | None = None  # (task_id) -> PolicyContext
    memory_manager_factory: Callable | None = None  # (project_id, scoped_database) -> manager
    cancel_task: Callable | None = None  # (task_id, operation_id) -> confirmed broker receipt
    reconcile_task: Callable | None = None  # (task_id, operation_id) -> reconciliation receipt
    live_diagnostic: Callable | None = None  # (target, operation_id) -> bounded diagnostic result
    resolve_base: Callable | None = None  # (project, base_ref) -> exact SHA
    project_diagnostic: Callable | None = None  # (project) -> persisted permission diagnostic
    business_read: Callable | None = None  # (spec, command, params) -> validated read data
    business_employee_read: Callable | None = None  # (employee, command, params) -> MCP data


class ApiError(Exception):
    def __init__(self, status, code, message, *, task_id=None, details=None):
        self.status, self.code, self.message = status, code, message
        self.task_id, self.details = task_id, details or {}


class ScopedDatabase:
    """Join durable service transitions to the API idempotency transaction."""

    def __init__(self, database, connection):
        self.database, self.connection = database, connection
        self.redactor = database.redactor

    def connect(self, **kwargs):
        if kwargs:
            raise ValueError("Operative API transitions require the current transaction")
        return nullcontext(self.connection)

    def __getattr__(self, name):
        return getattr(self.database, name)


def create_app(database, settings, *, callbacks=None, session_seconds=43200, runtime_config=None):
    from decagent.runtime.config import RuntimeConfig

    from .business_memory import install_business_memory_routes
    from .business_trace import install_business_trace_routes

    callbacks = callbacks or GatewayCallbacks()
    runtime_config = runtime_config or RuntimeConfig.from_env()
    business = BusinessControl(
        database,
        read=callbacks.business_read,
        employee_read=callbacks.business_employee_read,
        conditional_workflows_enabled=runtime_config.conditional_workflows_enabled,
    )
    operations = OperationService(database)
    auth = OwnerAuth(database, settings.public_url, session_seconds=session_seconds)
    employee_auth = EmployeeAuth(
        database, auth.origin, auth.secure, settings.data_dir / "demo-auth"
    )
    redactor = GatewayRedactor(
        known_secrets=tuple(
            filter(
                None,
                (
                    *database.redactor.known_secrets,
                    settings.main_api_key,
                    settings.jev_api_key,
                    settings.github_token,
                    settings.database_url,
                ),
            )
        )
    )
    app = FastAPI(title="DecAgent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database, app.state.callbacks, app.state.auth = database, callbacks, auth
    app.state.employee_auth = employee_auth

    def safe(value):
        return redactor.sanitize(jsonable_encoder(value), surface="ui")

    def operative(value):
        bounded = GatewayRedactor(
            known_secrets=redactor.known_secrets,
            max_text_chars=1_048_576,
            max_items=1000,
            max_depth=32,
        )
        if bounded.sanitize(value, surface="evidence") != value:
            raise ValueError("Operative input contains suspected credentials or exceeds limits")
        return exact_safe_record(value, redactor)

    def error(exc):
        return JSONResponse(
            safe(
                {
                    "code": exc.code,
                    "message": exc.message,
                    "task_id": exc.task_id,
                    "details": exc.details,
                }
            ),
            status_code=exc.status,
        )

    @app.middleware("http")
    async def owner_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/v1"):
            if auth.secure and request.url.scheme != "https":
                return error(ApiError(403, "https_required", "This instance requires HTTPS"))
            login = request.url.path == "/api/v1/auth/session" and request.method == "POST"
            if not login:
                identity = await asyncio.to_thread(auth.authenticate, request)
                if identity is None:
                    return error(ApiError(401, "unauthenticated", "Owner authentication required"))
                request.state.owner = identity
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    valid = await asyncio.to_thread(auth.valid_mutation, request, identity)
                    if not valid:
                        return error(
                            ApiError(403, "csrf_origin", "CSRF or Origin validation failed")
                        )
            elif not auth.valid_origin(request):
                return error(ApiError(403, "origin", "Exact configured Origin required"))
        elif request.url.path.startswith("/api/v2/business"):
            if employee_auth.secure and request.url.scheme != "https":
                return error(ApiError(403, "https_required", "This instance requires HTTPS"))
            login = request.url.path == "/api/v2/business/auth/session" and request.method == "POST"
            if login:
                if "authorization" in request.headers:
                    return error(ApiError(401, "unauthenticated", "Employee login required"))
                if not employee_auth.valid_origin(request):
                    return error(ApiError(403, "origin", "Exact configured Origin required"))
            else:
                employee = await asyncio.to_thread(employee_auth.authenticate_browser, request)
                if employee is None:
                    return error(ApiError(401, "unauthenticated", "Employee login required"))
                request.state.employee = employee
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    valid = await asyncio.to_thread(employee_auth.valid_mutation, request, employee)
                    if not valid:
                        return error(
                            ApiError(403, "csrf_origin", "CSRF or Origin validation failed")
                        )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if auth.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's raw errors include request inputs, which may be credentials.
        fields = [{"field": list(item["loc"]), "type": item["type"]} for item in exc.errors()]
        return error(
            ApiError(422, "invalid_input", "Request validation failed", details={"fields": fields})
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(ApiError(exc.status_code, "http_error", "Request could not be handled"))

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return error(ApiError(422, "invalid_input", "Input failed validation"))

    @app.exception_handler(AccessError)
    async def business_access_error(request, exc):
        status = {
            "unauthorized": 401,
            "not_found": 404,
            "forbidden": 403,
            "invalid_input": 422,
            "invalid_reference": 422,
            "service_unavailable": 503,
        }.get(exc.code, 409)
        return error(ApiError(status, exc.code, str(exc)))

    @app.exception_handler(MCPToolError)
    async def business_tool_error(request, exc):
        status = {"unauthorized": 401, "not_found": 404, "forbidden": 403}.get(exc.code, 409)
        return error(ApiError(status, exc.code, str(exc)))

    @app.exception_handler(KeyError)
    async def missing_error(request, exc):
        return error(ApiError(404, "not_found", "Requested record was not found"))

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return error(ApiError(503, "database_unavailable", "Database operation unavailable"))

    @app.exception_handler(Exception)
    async def unavailable_error(request, exc):
        return error(
            ApiError(503, "service_unavailable", "Operation unavailable; inspect local audit")
        )

    def required(callback, name):
        if callback is None:
            raise ApiError(503, "integration_unavailable", f"{name} is not connected")
        return callback

    def identity(request):
        return request.state.owner

    def task_row(conn, task_id, *, lock=False):
        row = conn.execute(
            "SELECT * FROM app.tasks WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (UUID(str(task_id)),),
        ).fetchone()
        if not row:
            raise KeyError("Task not found")
        provenance = row["authorization_snapshot"].get("provenance", {})
        if provenance.get("owner_id") != "owner" or provenance.get("authenticated") is not True:
            raise ApiError(403, "owner_scope", "Task has no authenticated owner authorization")
        return row

    def context_for(task_id, request):
        context = required(callbacks.current_policy_context, "Current policy context")(str(task_id))
        if (
            context.task_id != str(task_id)
            or context.authorization.provenance.owner_id != identity(request).owner_id
            or not context.authorization.active
            or not context.authorization.provenance.trusted_owner
        ):
            raise ApiError(
                409, "authorization_changed", "Current authorization requires owner review"
            )
        return context

    def append_event(conn, task, kind, payload):
        row = conn.execute(
            "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s AND workspace_id=%s "
            "RETURNING event_seq",
            (task["id"], task["workspace_id"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,workspace_id,seq,kind,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (
                task["id"],
                task["project_id"],
                task["workspace_id"],
                row["event_seq"],
                kind,
                Jsonb(safe(payload)),
            ),
        )

    def mutate(request, body, operation, *, status=200):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", key):
            raise ApiError(422, "idempotency_required", "A bounded Idempotency-Key is required")
        scope = f"{identity(request).owner_id}:{request.method}:{request.url.path}"
        payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        lock = int.from_bytes(
            hashlib.sha256((scope + ":" + key).encode()).digest()[:8], "big", signed=True
        )
        with database.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            row = conn.execute(
                "SELECT * FROM app.idempotency_keys WHERE scope=%s AND key=%s", (scope, key)
            ).fetchone()
            if row:
                if row["input_hash"] != fingerprint:
                    raise ApiError(
                        409, "idempotency_conflict", "Key was used for different content"
                    )
                if row["response"] is None:
                    raise ApiError(409, "result_unknown", "Prior operation requires reconciliation")
                return JSONResponse(row["response"]["body"], status_code=row["response"]["status"])
            conn.execute(
                "INSERT INTO app.idempotency_keys(scope,key,input_hash) VALUES(%s,%s,%s)",
                (scope, key, fingerprint),
            )
            operation_id = hashlib.sha256((scope + ":" + key).encode()).hexdigest()
            result = safe(operation(conn, operation_id))
            saved = {"status": status, "body": result}
            conn.execute(
                "UPDATE app.idempotency_keys SET response=%s WHERE scope=%s AND key=%s",
                (Jsonb(saved), scope, key),
            )
        return JSONResponse(result, status_code=status)

    def mutate_employee(request, body, operation, *, status=200):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", key):
            raise ApiError(422, "idempotency_required", "A bounded Idempotency-Key is required")
        employee = request.state.employee
        scope = f"employee:{employee.employee_id}:{request.method}:{request.url.path}"
        payload = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        lock = int.from_bytes(
            hashlib.sha256((scope + ":" + key).encode()).digest()[:8], "big", signed=True
        )
        with database.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
            previous = conn.execute(
                "SELECT * FROM app.idempotency_keys WHERE scope=%s AND key=%s", (scope, key)
            ).fetchone()
            if previous:
                if previous["input_hash"] != fingerprint:
                    raise ApiError(409, "idempotency_conflict", "Key has different content")
                if previous["response"] is None:
                    raise ApiError(409, "result_unknown", "Prior result requires reconciliation")
                saved = previous["response"]
                return JSONResponse(saved["body"], status_code=saved["status"])
            conn.execute(
                "INSERT INTO app.idempotency_keys(scope,key,input_hash) VALUES(%s,%s,%s)",
                (scope, key, fingerprint),
            )
            operation_id = hashlib.sha256((scope + ":" + key).encode()).hexdigest()
            result = safe(operation(conn, operation_id))
            saved = {"status": status, "body": result}
            conn.execute(
                "UPDATE app.idempotency_keys SET response=%s WHERE scope=%s AND key=%s",
                (Jsonb(saved), scope, key),
            )
        return JSONResponse(result, status_code=status)

    def task_view(conn, row):
        approvals = conn.execute(
            "SELECT a.*,x.snapshot AS action FROM app.approvals a "
            "LEFT JOIN app.actions x ON a.action_id=x.id WHERE a.task_id=%s ORDER BY a.created_at",
            (row["id"],),
        ).fetchall()
        decisions = conn.execute(
            "SELECT * FROM app.decision_records WHERE task_id=%s ORDER BY created_at DESC LIMIT 50",
            (row["id"],),
        ).fetchall()
        interventions = conn.execute(
            "SELECT kind,payload,seq FROM app.events WHERE task_id=%s "
            "AND (kind LIKE 'intervention.%%' OR kind='progress.observed') "
            "ORDER BY seq DESC LIMIT 50",
            (row["id"],),
        ).fetchall()[::-1]
        delivery = conn.execute(
            "SELECT * FROM app.deliveries WHERE task_id=%s", (row["id"],)
        ).fetchone()
        waits = conn.execute(
            "SELECT kind,payload,seq FROM app.events WHERE task_id=%s "
            "AND (kind LIKE '%%wait%%' OR kind LIKE '%%interrupt%%' "
            "OR kind LIKE '%%pause%%') ORDER BY seq DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        actions = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s ORDER BY created_at,id", (row["id"],)
        ).fetchall()
        result = conn.execute(
            "SELECT payload FROM app.events WHERE task_id=%s AND kind='task.finished' "
            "ORDER BY seq DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        return {
            **row,
            "approvals": approvals,
            "decisions": decisions,
            "interventions": interventions,
            "delivery": delivery,
            "waiting": waits,
            "actions": actions,
            "business_result": result["payload"] if row["kind"] == "business" and result else None,
            "requested_issue": row["authorization_snapshot"].get(
                "instruction", row["authorization_snapshot"].get("issue", "")
            ),
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/v1/auth/session")
    def login(body: SessionInput):
        issued = auth.session(body.token)
        if issued is None:
            raise ApiError(401, "unauthenticated", "Owner authentication failed")
        session, csrf, expires = issued
        response = JSONResponse({"csrf_token": csrf, "expires_at": expires.isoformat()})
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=auth.session_seconds,
            httponly=True,
            secure=auth.secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.delete("/api/v1/auth/session")
    def logout(request: Request):
        auth.revoke(identity(request))
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=auth.secure, httponly=True, samesite="strict"
        )
        return response

    @app.post("/api/v2/business/auth/session")
    def employee_login(body: EmployeeLoginInput):
        issued = employee_auth.login(body.login, body.password)
        if issued is None:
            raise ApiError(401, "unauthenticated", "Employee authentication failed")
        session, csrf, expires = issued
        response = JSONResponse({"csrf_token": csrf, "expires_at": expires.isoformat()})
        response.set_cookie(
            EMPLOYEE_COOKIE_NAME,
            session,
            max_age=employee_auth.session_seconds,
            httponly=True,
            secure=employee_auth.secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/v2/business/auth/session")
    def employee_session(request: Request):
        return request.state.employee.public()

    @app.delete("/api/v2/business/auth/session")
    def employee_logout(request: Request):
        employee_auth.revoke_browser(request.state.employee)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(
            EMPLOYEE_COOKIE_NAME,
            path="/",
            secure=employee_auth.secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/api/v2/business/auth/mcp-credentials", status_code=201)
    def issue_employee_mcp_credential(request: Request):
        try:
            return employee_auth.issue_mcp(request.state.employee)
        except ValueError as exc:
            raise ApiError(401, "unauthenticated", "Employee session unavailable") from exc

    @app.delete("/api/v2/business/auth/mcp-credentials/{credential_ref}")
    def revoke_employee_mcp_credential(credential_ref: UUID, request: Request):
        if not employee_auth.revoke_mcp(request.state.employee, credential_ref):
            raise ApiError(404, "not_found", "Credential not found")
        return {"revoked": True}

    def employee_task_row(conn, employee, task_id, *, lock=False):
        row = conn.execute(
            "SELECT * FROM app.tasks WHERE id=%s AND kind='business' AND employee_id=%s "
            "AND workspace_id=%s" + (" FOR UPDATE" if lock else ""),
            (task_id, employee.employee_id, employee.workspace_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "Business task unavailable")
        return row

    def employee_task_view(conn, row):
        record = task_view(conn, row)
        return safe(
            {
                key: record[key]
                for key in (
                    "id",
                    "kind",
                    "state",
                    "phase",
                    "created_at",
                    "event_seq",
                    "budget",
                    "requested_issue",
                    "business_result",
                    "waiting",
                    "approvals",
                    "decisions",
                    "actions",
                )
            }
        )

    def browser_operation(employee, operation_id, *, known=None):
        view = operations.get(employee, operation_id)
        with database.connect() as conn:
            row = (
                known
                or conn.execute(
                    "SELECT supersedes,resource_snapshot FROM business.operations WHERE id=%s",
                    (operation_id,),
                ).fetchone()
            )
        return safe(
            {
                **view,
                "supersedes": row["supersedes"] if row else None,
                "resource_snapshot": row["resource_snapshot"] if row else None,
            }
        )

    def employee_business_read(request, command, params):
        read = required(callbacks.business_employee_read, "Employee business MCP read")
        return safe(read(request.state.employee, command, params))

    @app.get("/api/v2/business/plugins")
    def employee_plugins(request: Request):
        with database.connect() as conn:
            commands = access.allowed_commands(
                conn,
                request.state.employee.employee_id,
                workspace_id=request.state.employee.workspace_id,
            )
        return safe(
            [
                {
                    **plugin,
                    "commands": [
                        command for command in plugin["commands"] if command["name"] in commands
                    ],
                }
                for plugin in business.plugins()
                if plugin["enabled"]
            ]
        )

    @app.get("/api/v2/business/directory")
    def employee_directory(request: Request):
        return employee_business_read(request, "directory", {})

    @app.get("/api/v2/business/tickets")
    def employee_tickets(
        request: Request, status: str | None = None, person_id: UUID | None = None, limit: int = 100
    ):
        if status is not None and status not in {
            "open",
            "assigned",
            "in_progress",
            "resolved",
            "closed",
        }:
            raise ApiError(422, "invalid_input", "Unknown ticket status")
        if not 1 <= limit <= 100:
            raise ApiError(422, "invalid_input", "Ticket limit must be between 1 and 100")
        params = {"limit": limit}
        if status is not None:
            params["status"] = status
        if person_id is not None:
            params["person_id"] = str(person_id)
        return employee_business_read(request, "list", params)

    @app.get("/api/v2/business/tickets/{ticket_id}")
    def employee_ticket(ticket_id: UUID, request: Request):
        return employee_business_read(request, "get", {"ticket_id": str(ticket_id)})

    @app.get("/api/v2/business/tasks")
    def employee_tasks(request: Request):
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT id,kind,state,phase,created_at,event_seq,budget,"
                "authorization_snapshot FROM app.tasks WHERE kind='business' AND employee_id=%s "
                "AND workspace_id=%s "
                "ORDER BY created_at DESC,id LIMIT 100",
                (request.state.employee.employee_id, request.state.employee.workspace_id),
            ).fetchall()
        return safe(
            [
                {
                    key: row[key]
                    for key in ("id", "kind", "state", "phase", "created_at", "event_seq", "budget")
                }
                | {"requested_issue": row["authorization_snapshot"].get("instruction", "")}
                for row in rows
            ]
        )

    @app.get("/api/v2/business/tasks/{task_id}")
    def employee_task(task_id: UUID, request: Request):
        with database.connect() as conn:
            row = employee_task_row(conn, request.state.employee, task_id)
            return employee_task_view(conn, row)

    @app.post("/api/v2/business/tasks", status_code=201)
    def submit_employee_business(body: BusinessTaskInput, request: Request):
        employee = request.state.employee
        created_refs = []

        def operation(conn, operation_id):
            if not body.instruction.strip():
                raise ApiError(422, "invalid_input", "Business instruction cannot be blank")
            if body.budget.max_cost is not None:
                cost = Decimal(body.budget.max_cost)
                if not cost.is_finite() or cost <= 0:
                    raise ApiError(422, "invalid_input", "Budget cost must be positive")
            issued = employee_auth.issue_mcp(employee)
            created_refs.append(UUID(issued["credential_ref"]))
            task = business.submit_employee(conn, body, employee, created_refs[-1])
            append_event(
                conn,
                task,
                "task.submitted",
                {
                    "task_id": str(task["id"]),
                    "kind": "business",
                    "employee_id": str(employee.employee_id),
                },
            )
            task["event_seq"] = 1
            return employee_task_view(conn, task)

        try:
            return mutate_employee(request, body, operation, status=201)
        except Exception:
            for credential_ref in created_refs:
                employee_auth.revoke_mcp(employee, credential_ref)
            raise

    @app.post("/api/v2/business/tasks/{task_id}/approvals/{approval_id}/reply")
    def employee_task_reply(task_id: UUID, approval_id: UUID, body: ReplyInput, request: Request):
        employee = request.state.employee

        def operation(conn, operation_id):
            task = employee_task_row(conn, employee, task_id, lock=True)
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s AND task_id=%s FOR UPDATE",
                (approval_id, task_id),
            ).fetchone()
            if approval is None:
                raise ApiError(404, "not_found", "Task approval unavailable")
            result = business.reply_employee(conn, task, approval, body, employee, operation_id)
            append_event(
                conn,
                task,
                "approval.replied",
                {"approval_id": str(approval_id), "decision": body.decision},
            )
            return result

        return mutate_employee(request, body, operation)

    @app.post("/api/v2/business/tasks/{task_id}/decisions/{approval_id}/reply")
    def employee_semantic_reply(
        task_id: UUID, approval_id: UUID, body: SemanticReplyInput, request: Request
    ):
        employee = request.state.employee

        def operation(conn, operation_id):
            task = employee_task_row(conn, employee, task_id, lock=True)
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s AND task_id=%s FOR UPDATE",
                (approval_id, task_id),
            ).fetchone()
            if approval is None:
                raise ApiError(404, "not_found", "Semantic decision unavailable")
            return business.reply_semantic(conn, task, approval, body, employee, operation_id)

        return mutate_employee(request, body, operation)

    @app.post("/api/v2/business/tasks/{task_id}/decision-thresholds")
    def employee_decision_threshold(task_id: UUID, body: DecisionThresholdInput, request: Request):
        from decagent.business.policy_store import DecisionPolicyStore

        employee = request.state.employee

        def operation(conn, _operation_id):
            employee_task_row(conn, employee, task_id, lock=True)
            policies = DecisionPolicyStore(ScopedDatabase(database, conn))
            updated = policies.set_task_threshold(
                employee,
                task_id,
                body.policy_id,
                body.policy_version,
                body.threshold,
            )
            effective = policies.resolve(employee, task_id, body.policy_id, body.policy_version)
            if effective.policy is None:
                raise ApiError(409, "policy_unavailable", "Published decision policy unavailable")
            return {
                "task_id": str(task_id),
                "policy_id": body.policy_id,
                "policy_version": body.policy_version,
                "threshold": updated["threshold"],
                "revision": updated["revision"],
                "effective_revision": effective.effective_revision,
            }

        return mutate_employee(request, body, operation)

    @app.post("/api/v2/business/tasks/{task_id}/resume")
    def resume_employee_business(task_id: UUID, body: ResumeInput, request: Request):
        employee = request.state.employee
        created_refs = []

        def operation(conn, operation_id):
            task = employee_task_row(conn, employee, task_id, lock=True)
            if task["state"] in TERMINAL or task["state"] in {"queued", "running"}:
                raise ApiError(409, "resume_conflict", "Task cannot be resumed now")
            if task["state"] == "paused_budget" and body.budget is None:
                raise ApiError(422, "budget_required", "A revised explicit budget is required")
            pending = conn.execute(
                "SELECT id FROM app.approvals WHERE task_id=%s AND status='pending' LIMIT 1",
                (task_id,),
            ).fetchone()
            if pending:
                raise ApiError(409, "approval_required", "Reply to current question first")
            prior_budget = task["budget"]
            budget = body.budget.model_dump() if body.budget else task["budget"]
            if budget.get("max_cost") is not None:
                cost = Decimal(budget["max_cost"])
                if not cost.is_finite() or cost <= 0:
                    raise ApiError(422, "invalid_input", "Budget cost must be positive")
            issued = employee_auth.issue_mcp(employee)
            created_refs.append(UUID(issued["credential_ref"]))
            task = business.reauthenticate(conn, task, employee, created_refs[-1])
            task = conn.execute(
                "UPDATE app.tasks SET state='queued',budget=%s,lease_owner=NULL,"
                "lease_until=NULL,lease_generation=lease_generation+1 "
                "WHERE id=%s RETURNING *",
                (Jsonb(budget), task_id),
            ).fetchone()
            append_event(
                conn,
                task,
                "task.resume_requested",
                {"employee_id": str(employee.employee_id), "reconcile_original_operation": True},
            )
            if body.budget is not None and prior_budget != budget:
                append_event(
                    conn,
                    task,
                    "task.budget_changed",
                    {
                        "employee_id": str(employee.employee_id),
                        "source": "employee_session",
                        "before": prior_budget,
                        "after": budget,
                    },
                )
            return employee_task_view(conn, task)

        try:
            return mutate_employee(request, body, operation)
        except Exception:
            for credential_ref in created_refs:
                employee_auth.revoke_mcp(employee, credential_ref)
            raise

    @app.post("/api/v2/business/tasks/{task_id}/cancel")
    def cancel_employee_business(task_id: UUID, request: Request):
        employee = request.state.employee

        def operation(conn, operation_id):
            task = employee_task_row(conn, employee, task_id, lock=True)
            if task["state"] in TERMINAL:
                raise ApiError(409, "terminal_task", "Terminal task cannot be cancelled")
            task = conn.execute(
                "UPDATE app.tasks SET state='cancelled',lease_owner=NULL,lease_until=NULL,"
                "lease_generation=lease_generation+1 WHERE id=%s RETURNING *",
                (task_id,),
            ).fetchone()
            append_event(conn, task, "task.cancelled", {"employee_id": str(employee.employee_id)})
            return employee_task_view(conn, task)

        return mutate_employee(request, {}, operation)

    @app.get("/api/v2/business/tasks/{task_id}/events")
    async def employee_task_events(task_id: UUID, request: Request, follow: bool = True):
        last = request.headers.get("Last-Event-ID", request.query_params.get("after", "0"))
        if not re.fullmatch(r"[0-9]{1,19}", last) or int(last) > 2**63 - 1:
            raise ApiError(422, "invalid_event_cursor", "Event cursor must be nonnegative")
        with database.connect() as conn:
            employee_task_row(conn, request.state.employee, task_id)

        def batch(after, employee):
            with database.connect() as conn:
                employee_task_row(conn, employee, task_id)
                return conn.execute(
                    "SELECT seq,kind,payload FROM app.events WHERE task_id=%s AND seq>%s "
                    "ORDER BY seq LIMIT 500",
                    (task_id, after),
                ).fetchall()

        async def stream():
            after = int(last)
            while not await request.is_disconnected():
                current = await asyncio.to_thread(employee_auth.authenticate_browser, request)
                if current is None or current.employee_id != request.state.employee.employee_id:
                    return
                try:
                    rows = await asyncio.to_thread(batch, after, current)
                except ApiError:
                    return
                for row in rows:
                    after = row["seq"]
                    payload = json.dumps(safe(row), ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {after}\nevent: task_event\ndata: {payload}\n\n"
                if rows:
                    continue
                if not follow:
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/v2/business/operations")
    def employee_operations(request: Request):
        employee = request.state.employee
        with database.connect() as conn:
            actor = access.principal(conn, employee.employee_id, workspace_id=employee.workspace_id)
            if actor["role"] == "supervisor":
                rows = conn.execute(
                    "SELECT id,supersedes,resource_snapshot FROM business.operations "
                    "WHERE workspace_id=%s "
                    "ORDER BY created_at DESC,id LIMIT 100",
                    (employee.workspace_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id,supersedes,resource_snapshot FROM business.operations "
                    "WHERE employee_id=%s AND workspace_id=%s "
                    "ORDER BY created_at DESC,id LIMIT 100",
                    (employee.employee_id, employee.workspace_id),
                ).fetchall()
        return [browser_operation(employee, row["id"], known=row) for row in rows]

    @app.get("/api/v2/business/operations/{operation_id}")
    def employee_operation(operation_id: str, request: Request):
        return browser_operation(request.state.employee, operation_id)

    @app.post("/api/v2/business/operations/{operation_id}/reply")
    def employee_operation_reply(
        operation_id: str, body: BusinessOperationReplyInput, request: Request
    ):
        employee = request.state.employee
        current = operations.get(employee, operation_id)
        if current["source"] == "decagent":
            raise ApiError(409, "task_approval_required", "Reply to the linked Agent task approval")
        view = operations.reply(
            employee,
            operation_id,
            body.expected_hash,
            body.decision,
            edited_params=body.edited_params,
        )
        return browser_operation(employee, view["operation_id"])

    @app.get("/api/v1/projects")
    def projects():
        with database.connect() as conn:
            rows = conn.execute("SELECT * FROM app.projects ORDER BY created_at,id").fetchall()
        return safe(rows)

    @app.post("/api/v1/projects")
    def register_project(body: ProjectInput, request: Request):
        def operation(conn, operation_id):
            operative(body.model_dump())
            project = conn.execute(
                "INSERT INTO app.projects(id,repository,base_branch) VALUES(%s,%s,%s) "
                "ON CONFLICT(repository) DO UPDATE SET repository=EXCLUDED.repository RETURNING *",
                (uuid4(), body.repository, body.base_branch),
            ).fetchone()
            diagnostic = (
                callbacks.project_diagnostic(project)
                if callbacks.project_diagnostic
                else {
                    "status": "unverified",
                    "reason": "GitHub permission diagnostic is not connected",
                }
            )
            if (
                diagnostic.get("ownership_verified") is True
                and diagnostic.get("source") == "github_authenticated_diagnostic"
                and diagnostic.get("code_origin") in {"owner", "external"}
            ):
                trust = operative(
                    {
                        key: diagnostic[key]
                        for key in (
                            "code_origin",
                            "authenticated_owner_login",
                            "repository_owner_login",
                            "repository_id",
                            "ownership_verified",
                            "source",
                        )
                    }
                )
                project = conn.execute(
                    "UPDATE app.projects SET trust=%s WHERE id=%s RETURNING *",
                    (Jsonb(trust), project["id"]),
                ).fetchone()
            return {**project, "permissions": diagnostic}

        return mutate(request, body, operation, status=201)

    @app.get("/api/v1/tasks")
    def tasks():
        with database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM app.tasks WHERE "
                "authorization_snapshot->'provenance'->>'owner_id'='owner' "
                "ORDER BY created_at DESC,id LIMIT 100"
            ).fetchall()
        return safe(
            [
                {
                    **row,
                    "requested_issue": row["authorization_snapshot"].get(
                        "instruction", row["authorization_snapshot"].get("issue", "")
                    ),
                }
                for row in rows
            ]
        )

    @app.get("/api/v1/plugins")
    def plugins():
        return safe(business.plugins())

    @app.post("/api/v1/business/tasks")
    def submit_business(body: BusinessTaskInput, request: Request):
        raise ApiError(410, "legacy_business_disabled", "Employee business workspace is required")

    @app.post("/api/v1/tasks")
    def submit_task(body: TaskInput, request: Request):
        def operation(conn, operation_id):
            project = conn.execute(
                "SELECT * FROM app.projects WHERE id=%s", (UUID(body.project_id),)
            ).fetchone()
            if not project:
                raise KeyError("Project not found")
            if body.trust_scope and body.trust_scope.kind == "own_branch":
                trust = project["trust"]
                if (
                    trust.get("code_origin") != "owner"
                    or trust.get("ownership_verified") is not True
                    or trust.get("source") != "github_authenticated_diagnostic"
                    or body.trust_scope.branch != project["base_branch"]
                    or body.base_ref != project["base_branch"]
                ):
                    raise ApiError(
                        422,
                        "invalid_branch_trust",
                        "Owned branch trust requires the verified owner's base branch",
                    )
            base_sha = body.base_ref
            if not re.fullmatch(r"[a-f0-9]{40}", base_sha):
                base_sha = required(callbacks.resolve_base, "Exact base resolution")(
                    project, body.base_ref
                )
            if type(base_sha) is not str or not re.fullmatch(r"[a-f0-9]{40}", base_sha):
                raise ApiError(422, "invalid_base", "An exact 40-character commit SHA is required")
            budget = body.budget.model_dump()
            if budget["max_cost"] is not None:
                cost = Decimal(budget["max_cost"])
                if not cost.is_finite() or cost <= 0:
                    raise ValueError("Invalid budget cost")
            task_id = (
                uuid4()
            )  # Generate before authorization, never patch a mismatched saved scope.
            authorization = {
                "id": str(uuid4()),
                "version": "1",
                "task_id": str(task_id),
                "project_id": str(project["id"]),
                "repository": project["repository"],
                "provenance": primitive(identity(request).provenance),
                "publish_draft": body.publish_draft_pr,
                "active": True,
                "publish_draft_pr": body.publish_draft_pr,
                "issue": body.issue,
                "base_ref": body.base_ref,
                "config_version": project["config_version"],
                "trust_scope": body.trust_scope.model_dump() if body.trust_scope else None,
            }
            authorization = operative(authorization)
            task = conn.execute(
                "INSERT INTO app.tasks(id,project_id,base_sha,authorization_snapshot,budget,"
                "graph_version) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    task_id,
                    project["id"],
                    base_sha,
                    Jsonb(authorization),
                    Jsonb(budget),
                    settings.graph_version,
                ),
            ).fetchone()
            append_event(conn, task, "task.submitted", {"task_id": str(task_id), "state": "queued"})
            task["event_seq"] = 1
            return task

        return mutate(request, body, operation, status=201)

    @app.get("/api/v1/tasks/{task_id}")
    def task_status(task_id: UUID):
        with database.connect() as conn:
            return safe(task_view(conn, task_row(conn, task_id)))

    @app.get("/api/v1/tasks/{task_id}/events")
    async def task_events(task_id: UUID, request: Request, follow: bool = True):
        last = request.headers.get("Last-Event-ID", request.query_params.get("after", "0"))
        if not re.fullmatch(r"[0-9]{1,19}", last) or int(last) > 2**63 - 1:
            raise ApiError(
                422, "invalid_event_cursor", "Event cursor must be a nonnegative sequence"
            )
        with database.connect() as conn:
            task_row(conn, task_id)

        def batch(after):
            with database.connect() as conn:
                return conn.execute(
                    "SELECT * FROM app.events WHERE task_id=%s AND seq>%s ORDER BY seq LIMIT 500",
                    (task_id, after),
                ).fetchall()

        async def stream():
            after = int(last)
            while not await request.is_disconnected():
                if await asyncio.to_thread(auth.authenticate, request) is None:
                    return
                rows = await asyncio.to_thread(batch, after)
                for row in rows:
                    after = row["seq"]
                    payload = json.dumps(safe(row), ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {after}\nevent: task_event\ndata: {payload}\n\n"
                if rows:
                    continue
                if not follow:
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/v1/tasks/{task_id}/diff")
    def task_diff(task_id: UUID):
        with database.connect() as conn:
            task_row(conn, task_id)
            record = conn.execute(
                "SELECT seq,payload FROM app.events WHERE task_id=%s "
                "AND kind IN ('diff','repair.diff','workspace.diff') "
                "ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return safe(record or {"status": "unavailable", "reason": "No persisted diff yet"})

    @app.get("/api/v1/tasks/{task_id}/verification")
    def task_verification(task_id: UUID):
        with database.connect() as conn:
            task_row(conn, task_id)
            rows = conn.execute(
                "SELECT * FROM app.verification_runs WHERE task_id=%s ORDER BY created_at,id",
                (task_id,),
            ).fetchall()
        return safe({"runs": rows, "status": "recorded" if rows else "unverified"})

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel(task_id: UUID, request: Request):
        def operation(conn, operation_id):
            # Broker control takes its own task row lock. Never hold that lock over RPC.
            kind = conn.execute("SELECT kind FROM app.tasks WHERE id=%s", (task_id,)).fetchone()
            if kind and kind["kind"] == "business":
                raise ApiError(
                    410, "legacy_business_disabled", "Employee business workspace is required"
                )
            task = task_row(conn, task_id)
            if task["state"] in TERMINAL:
                raise ApiError(409, "terminal_task", "Terminal task cannot be cancelled")
            receipt = None
            if task["state"] in {"running", "recovering"} or task["lease_until"] is not None:
                receipt = required(callbacks.cancel_task, "Executor cancellation")(
                    str(task_id), operation_id
                )
                if not isinstance(receipt, dict) or receipt.get("stopped") is not True:
                    raise ApiError(409, "stop_unconfirmed", "Executor stop is not confirmed")
            current = task_row(conn, task_id, lock=True)
            if (
                current["lease_generation"] != task["lease_generation"]
                or current["lease_owner"] != task["lease_owner"]
                or current["state"] not in {task["state"], "recovering"}
            ):
                raise ApiError(
                    409, "task_changed", "Task lease or state changed during cancellation"
                )
            row = conn.execute(
                "UPDATE app.tasks SET state='cancelled',lease_until=NULL,"
                "lease_owner=NULL,lease_generation=lease_generation+1 WHERE id=%s "
                "RETURNING *",
                (task_id,),
            ).fetchone()
            append_event(conn, row, "task.cancelled", {"receipt": receipt, "owner_id": "owner"})
            return row

        return mutate(request, {}, operation)

    @app.post("/api/v1/tasks/{task_id}/resume")
    def resume(task_id: UUID, body: ResumeInput, request: Request):
        def operation(conn, operation_id):
            kind = conn.execute("SELECT kind FROM app.tasks WHERE id=%s", (task_id,)).fetchone()
            if kind and kind["kind"] == "business":
                raise ApiError(
                    410, "legacy_business_disabled", "Employee business workspace is required"
                )
            task = task_row(conn, task_id)
            if task["state"] in TERMINAL or task["state"] in {"queued", "running"}:
                raise ApiError(409, "resume_conflict", "Current task state cannot be resumed")
            pending = conn.execute(
                "SELECT id FROM app.approvals WHERE task_id=%s AND status='pending' LIMIT 1",
                (task_id,),
            ).fetchone()
            if pending:
                raise ApiError(
                    409, "approval_required", "Reply to current approval before resuming"
                )
            context = context_for(task_id, request)
            # A queue transition cannot turn an old approval into execution authority.
            approved = conn.execute(
                "SELECT a.digest,x.snapshot FROM app.approvals a JOIN app.actions x "
                "ON a.action_id=x.id WHERE a.task_id=%s AND a.kind='action' "
                "AND a.reply='approve' AND x.status='planned'",
                (task_id,),
            ).fetchall()
            for record in approved:
                snapshot = record["snapshot"]
                if (
                    action_digest(Action(snapshot["tool"], snapshot["params"]), context)
                    != record["digest"]
                ):
                    raise ApiError(409, "stale_approval", "Approval versions changed before resume")
            if task["state"] == "paused_budget" and body.budget is None:
                raise ApiError(422, "budget_required", "A revised explicit budget is required")
            receipt = None
            if task["state"] == "recovering":
                receipt = required(callbacks.reconcile_task, "Executor reconciliation")(
                    str(task_id), operation_id
                )
                if not isinstance(receipt, dict) or receipt.get("reconciled") is not True:
                    raise ApiError(
                        409, "reconciliation_required", "External result remains unknown"
                    )
            current = task_row(conn, task_id, lock=True)
            if (
                current["lease_generation"] != task["lease_generation"]
                or current["lease_owner"] != task["lease_owner"]
                or current["state"] != task["state"]
            ):
                raise ApiError(
                    409, "task_changed", "Task lease or state changed during reconciliation"
                )
            budget = body.budget.model_dump() if body.budget else task["budget"]
            if budget.get("max_cost") is not None:
                cost = Decimal(budget["max_cost"])
                if not cost.is_finite() or cost <= 0:
                    raise ValueError("Invalid budget cost")
            row = conn.execute(
                "UPDATE app.tasks SET state='queued',budget=%s,lease_owner=NULL,"
                "lease_until=NULL,lease_generation=lease_generation+1 WHERE id=%s "
                "RETURNING *",
                (Jsonb(budget), task_id),
            ).fetchone()
            append_event(
                conn,
                row,
                "task.resume_requested",
                {"owner_id": "owner", "budget": budget, "reconciliation": receipt},
            )
            return row

        return mutate(request, body, operation)

    def reply_operation(conn, operation_id, approval_id, body, request):
        row = conn.execute(
            "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (UUID(str(approval_id)),)
        ).fetchone()
        if not row:
            raise KeyError("Approval not found")
        task = task_row(conn, row["task_id"], lock=True)
        if task["kind"] == "business":
            raise ApiError(
                410, "legacy_business_disabled", "Employee business workspace is required"
            )
        context = context_for(row["task_id"], request)
        action, trust_request = None, None
        if row["kind"] == "action":
            record = conn.execute(
                "SELECT snapshot FROM app.actions WHERE id=%s AND task_id=%s",
                (row["action_id"], row["task_id"]),
            ).fetchone()
            if not record:
                raise ApiError(409, "approval_invalid", "Action approval lacks operative snapshot")
            action = Action(record["snapshot"]["tool"], record["snapshot"]["params"])
            current_digest = action_digest(action, context)
        elif row["kind"] == "code_trust":
            saved = (row["reply_payload"] or {}).get("code_trust_request")
            if not saved:
                raise ApiError(
                    409, "trust_request_required", "Code trust requires a pure scoped request"
                )
            trust_request = CodeTrustRequest(
                saved["repository"], TrustKind(saved["kind"]), saved["sha"], saved.get("branch")
            )
            current_digest = trust_request.digest(context)
        else:
            subject = (row["reply_payload"] or {}).get("subject")
            if not subject:
                raise ApiError(
                    409, "confirmation_subject_required", "Confirmation lacks its subject"
                )
            current_digest = confirmation_digest(ApprovalKind(row["kind"]), subject, context)
        if current_digest != row["digest"] or body.expected_digest != current_digest:
            raise ApiError(
                409, "stale_approval", "Approval versions changed; review current proposal"
            )
        reply = ApprovalReply(
            operation_id,
            body.expected_digest,
            ReplyDecision(body.decision),
            identity(request).provenance,
            body.edited_params,
            body.confirmation,
        )
        operative(primitive(reply))
        service = DurableApprovals(ScopedDatabase(database, conn))
        try:
            result = service.reply(row["id"], reply, action=action, context=context)
            if trust_request and body.decision == "approve":
                trust = issue_code_trust(
                    trust_request, context, result.approval, trust_id=str(uuid4()), version="1"
                )
                service.claim(
                    row["id"], current_digest, "code-trust:" + operation_id, connection=conn
                )
                snapshot = {**task["authorization_snapshot"], "code_trust": primitive(trust)}
                conn.execute(
                    "UPDATE app.tasks SET authorization_snapshot=%s WHERE id=%s",
                    (Jsonb(operative(snapshot)), task["id"]),
                )
        except ValueError as exc:
            raise ApiError(
                409, "approval_conflict", "Approval requires a fresh valid owner reply"
            ) from exc
        append_event(
            conn,
            task,
            "approval.replied",
            {"approval_id": str(row["id"]), "decision": body.decision},
        )
        return primitive(result)

    @app.post("/api/v1/approvals/{approval_id}/reply")
    def reply_approval(approval_id: UUID, body: ReplyInput, request: Request):
        return mutate(
            request,
            body,
            lambda conn, operation_id: reply_operation(
                conn, operation_id, approval_id, body, request
            ),
        )

    @app.post("/api/v1/tasks/{task_id}/responses")
    def clarification(task_id: UUID, body: ResponseInput, request: Request):
        def operation(conn, operation_id):
            task_row(conn, task_id)
            row = conn.execute(
                "SELECT task_id,kind FROM app.approvals WHERE id=%s", (UUID(body.question_id),)
            ).fetchone()
            if not row or row["task_id"] != task_id or row["kind"] != "clarification":
                raise ApiError(409, "question_scope", "Question is not current for this task")
            reply = ReplyInput(
                expected_digest=body.expected_digest, decision="confirm", confirmation=body.content
            )
            return reply_operation(conn, operation_id, body.question_id, reply, request)

        return mutate(request, body, operation)

    @app.get("/api/v1/projects/{project_id}/memories")
    def memories(project_id: UUID):
        with database.connect() as conn:
            if not conn.execute(
                "SELECT id FROM app.projects WHERE id=%s", (project_id,)
            ).fetchone():
                raise KeyError("Project not found")
            rows = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE project_id=%s ORDER BY created_at,id",
                (project_id,),
            ).fetchall()
            for row in rows:
                row["revision"] = row["applicability"].get("_revision", 1)
                row["versions"] = conn.execute(
                    "SELECT * FROM app.memory_versions WHERE candidate_id=%s ORDER BY version",
                    (row["id"],),
                ).fetchall()
        return safe(rows)

    def memory_operation(conn, candidate_id, request, body, revoke=False):
        row = conn.execute(
            "SELECT * FROM app.memory_candidates WHERE id=%s", (candidate_id,)
        ).fetchone()
        if not row:
            raise KeyError("Memory not found")
        if not revoke and body.operation == "activate":
            evidence = conn.execute(
                "SELECT sha256 FROM app.evidence WHERE sha256=%s", (row["evidence_hash"],)
            ).fetchone()
            if not evidence:
                raise ApiError(
                    409,
                    "memory_evidence_missing",
                    "Memory evidence must be restored before activation",
                )
        context = context_for(row["source_task_id"], request)
        manager = required(callbacks.memory_manager_factory, "Memory lifecycle manager")(
            str(row["project_id"]), ScopedDatabase(database, conn)
        )
        try:
            if revoke:
                result = manager.revoke(
                    str(candidate_id), expected_revision=body.expected_revision, context=context
                )
            else:
                confirmation_id = body.confirmation_id
                if body.confirmation is not None:
                    operative({"confirmation": body.confirmation})
                    subject = manager.review_subject(str(candidate_id), body.operation)
                    expected = confirmation_digest(ApprovalKind.MEMORY_CONFLICT, subject, context)
                    approval = conn.execute(
                        "INSERT INTO app.approvals(id,task_id,digest,kind,reply_payload) "
                        "VALUES(%s,%s,%s,'memory_conflict',%s) ON CONFLICT(task_id,digest,kind) "
                        "DO UPDATE SET digest=EXCLUDED.digest RETURNING *",
                        (uuid4(), row["source_task_id"], expected, Jsonb({"subject": subject})),
                    ).fetchone()
                    confirmation_id = str(approval["id"])
                    DurableApprovals(ScopedDatabase(database, conn)).reply(
                        approval["id"],
                        ApprovalReply(
                            str(uuid4()),
                            expected,
                            ReplyDecision.CONFIRM,
                            identity(request).provenance,
                            confirmation=body.confirmation,
                        ),
                        context=context,
                    )
                if not confirmation_id:
                    raise ValueError("Memory review requires explicit owner confirmation")
                result = manager.review(
                    str(candidate_id),
                    body.operation,
                    confirmation_id=confirmation_id,
                    context=context,
                    expected_revision=body.expected_revision,
                )
        except ValueError as exc:
            raise ApiError(
                409, "memory_conflict", "Memory sources or review revision changed"
            ) from exc
        return primitive(result)

    @app.post("/api/v1/memories/{candidate_id}/review")
    def review_memory(candidate_id: UUID, body: MemoryReviewInput, request: Request):
        return mutate(
            request, body, lambda conn, _: memory_operation(conn, candidate_id, request, body)
        )

    @app.post("/api/v1/memories/{candidate_id}/revoke")
    def revoke_memory(candidate_id: UUID, body: MemoryRevokeInput, request: Request):
        return mutate(
            request, body, lambda conn, _: memory_operation(conn, candidate_id, request, body, True)
        )

    @app.get("/api/v1/config")
    def configuration():
        return safe(
            {
                "main_model": settings.main_model,
                "main_base_url": settings.main_base_url,
                "jev_model": settings.jev_model,
                "jev_provider": getattr(settings, "jev_provider", "typesafe"),
                "graph_version": settings.graph_version,
                "public_url": auth.origin,
                "credentials": {
                    "main": bool(settings.main_api_key),
                    "jev": bool(settings.jev_api_key),
                    "github": bool(settings.github_token),
                },
                "database_configured": bool(database.url),
                "rotation": "Change environment or Compose secrets and restart control services",
                "diagnostics": {
                    "local_external_requests": 0,
                    "live_requires_explicit_selection": True,
                },
            }
        )

    @app.post("/api/v1/diagnostics")
    def diagnostics(body: DiagnosticInput, request: Request):
        def operation(conn, operation_id):
            if body.mode == "live":
                if not body.confirm_external_request or body.target is None:
                    raise ApiError(
                        422, "explicit_live_required", "Select and confirm one live diagnostic"
                    )
                result = required(callbacks.live_diagnostic, "Live diagnostics")(
                    body.target, operation_id
                )
                return {"mode": "live", "target": body.target, "result": result}
            conn.execute("SELECT 1")
            return {
                "mode": "local",
                "external_requests": 0,
                "database": "reachable",
                "credentials_configured": {
                    "main": bool(settings.main_api_key),
                    "jev": bool(settings.jev_api_key),
                    "github": bool(settings.github_token),
                },
                "executor": "not_verified",
                "models": "not_contacted",
                "github_permissions": "not_verified",
            }

        return mutate(request, body, operation)

    install_business_memory_routes(app, database, mutate_employee, safe, ScopedDatabase)
    install_business_trace_routes(app, database, redactor)

    assets = Path(__file__).parent / "static"

    @app.get("/")
    def workbench_page():
        return FileResponse(assets / "business.html")

    @app.get("/repair")
    def repair_page():
        return FileResponse(assets / "index.html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "style.css", "business.js", "business.css"}:
            raise KeyError("Asset not found")
        return FileResponse(assets / name)

    return app

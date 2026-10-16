"""Lease-fenced MCP action journal with immutable service operation recovery."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.contracts import validate_schema
from decagent.business.grants import GrantAuthority
from decagent.business.mcp_client import (
    MCPAuthenticationError,
    MCPProtocolError,
    MCPToolError,
    MCPTransportError,
)
from decagent.business.mcp_contract import operation_schema
from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import digest
from decagent.storage.budget import BudgetExceeded
from decagent.storage.safe_record import exact_safe_record
from decagent.storage.usage import UsageLedger

KNOWN_FAILURES = {
    "invalid_input",
    "invalid_reference",
    "invalid_transition",
    "revision_conflict",
    "operation_conflict",
    "not_found",
    "forbidden",
    "contract_changed",
    "approval_expired",
}


class BusinessReadError(ValueError):
    def __init__(self, code):
        super().__init__("Business read failed: " + code)
        self.code = code


class BusinessExecutor:
    def __init__(self, database, spec, task, owner, generation, *, mcp_client):
        if spec.connection is None or task.get("employee_id") is None:
            raise ValueError("Employee-bound MCP business task required")
        self.database, self.spec, self.task = database, spec, task
        self.owner, self.generation = owner, generation
        self.client = mcp_client
        self.grants = GrantAuthority(database)

    def guard(self, conn=None):
        row = self.database.assert_lease(self.task["id"], self.owner, self.generation, conn)
        if row["employee_id"] != self.task["employee_id"]:
            raise ValueError("Business task employee changed")
        if conn is None:
            PluginRegistry(self.database).get(row["business_scope_id"], self.spec.digest)
        else:
            scope = conn.execute(
                "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE",
                (row["business_scope_id"],),
            ).fetchone()
            if not scope or not scope["enabled"] or scope["manifest_digest"] != self.spec.digest:
                raise ValueError("Business plugin changed or was disabled")
        return row

    def read(self, command, params):
        task = self.guard()
        if task["graph_version"] == "business-3":
            UsageLedger(self.database, self.task["id"]).assert_action_available()
        return self._read_data(command, params)

    def _read_data(self, command, params):
        self.guard()
        if self.spec.commands[command].effect != "read":
            raise ValueError("Read helper cannot execute a business write")
        try:
            data = self.client.read(command, params)
        except MCPToolError as exc:
            raise BusinessReadError(exc.code) from exc
        validate_schema(self.spec.commands[command].result_schema, data)
        exact_safe_record(data, self.database.redactor)
        return data

    def plan(self, key, assessment):
        self.guard()
        snapshot = {
            **assessment["snapshot"],
            "assessment": {field: assessment[field] for field in ("outcome", "reason", "digest")},
            "operation_id": digest(
                {"task_id": str(self.task["id"]), "key": key, "action_digest": assessment["digest"]}
            ),
        }
        return self.database.plan_action(self.task["id"], key, assessment["digest"], snapshot)

    def _action(self, conn, action_id):
        row = conn.execute(
            "SELECT * FROM app.actions WHERE id=%s AND task_id=%s FOR UPDATE",
            (action_id, self.task["id"]),
        ).fetchone()
        if not row:
            raise ValueError("Business action does not belong to this task")
        return row

    def _persist(self, action_id, result):
        with self.database.connect() as conn:
            self.guard(conn)
            action = self._action(conn, action_id)
            if action["status"] == "succeeded":
                return action["receipt"]
            if result["status"] == "unknown" and result.get("operation_id") is None:
                result = {**result, "operation_id": action["server_operation_id"]}
            saved = exact_safe_record(result, self.database.redactor)
            conn.execute(
                "UPDATE app.actions SET status=%s,receipt=%s WHERE id=%s",
                (result["status"], Jsonb(saved), action_id),
            )
            conn.execute(
                "UPDATE app.action_attempts SET status=%s,receipt=%s "
                "WHERE action_id=%s AND generation=%s",
                (result["status"], Jsonb(saved), action_id, self.generation),
            )
        return result

    def _grant(self, action_id):
        try:
            return self.grants.issue(self.task["id"], action_id, self.owner, self.generation)
        except AccessError as exc:
            if exc.code == "budget_exceeded":
                raise BudgetExceeded("Agent budget exhausted") from exc
            if exc.code == "employee_credential_unavailable":
                raise MCPAuthenticationError("Original employee credential unavailable") from exc
            raise

    def _bound_operation(self, action, operation):
        snapshot = action["snapshot"]
        expected = {
            "employee_id": str(self.task["employee_id"]),
            "request_id": snapshot["operation_id"],
            "command": snapshot["command"],
            "params": snapshot["params"],
            "contract_digest": self.spec.digest,
            "source": "decagent",
        }
        if not isinstance(operation, dict) or any(
            operation.get(key) != value for key, value in expected.items()
        ):
            raise MCPProtocolError("MCP operation does not match the planned action")
        with self.database.connect() as conn:
            self.guard(conn)
            saved = self._action(conn, action["id"])
            if saved["server_operation_id"] not in {None, operation["operation_id"]}:
                raise MCPProtocolError("Action is already bound to another service operation")
            conn.execute(
                "UPDATE app.actions SET server_operation_id=%s WHERE id=%s",
                (operation["operation_id"], action["id"]),
            )
        return operation

    def _result(self, action, operation):
        operation = self._bound_operation(action, operation)
        state = operation["state"]
        if state == "awaiting_approval":
            return {
                "status": "awaiting_approval",
                "operation_id": operation["operation_id"],
                "operation": operation,
            }
        if state == "succeeded":
            data = operation["result"]
            try:
                validate_schema(
                    self.spec.commands[action["snapshot"]["command"]].result_schema, data
                )
            except ValueError as exc:
                raise MCPProtocolError("MCP business result violates the frozen schema") from exc
            receipt = data["receipt"]
            if any(
                receipt.get(key) != value
                for key, value in {
                    "operation_id": operation["operation_id"],
                    "command": operation["command"],
                    "employee_id": operation["employee_id"],
                    "input_hash": operation["input_hash"],
                    "contract_digest": operation["contract_digest"],
                }.items()
            ):
                raise MCPProtocolError("MCP receipt does not match its immutable operation")
            ticket = data["ticket"]
            if ticket["id"] != receipt["resource_id"] or ticket["revision"] != receipt["revision"]:
                raise MCPProtocolError("MCP resource does not match its receipt")
            return self._persist(
                action["id"],
                {
                    "status": "succeeded",
                    "data": data,
                    "verification": {
                        "operation_id": operation["operation_id"],
                        "input_bound": True,
                        "server_verified": True,
                        "recorded_revision": receipt["revision"],
                    },
                },
            )
        if state in {"failed", "expired", "rejected", "cancelled"}:
            return self._persist(
                action["id"],
                {
                    "status": "denied" if state in {"rejected", "cancelled"} else "failed",
                    "reason": operation.get("error_code") or state,
                    "operation_id": operation["operation_id"],
                },
            )
        return self._persist(
            action["id"],
            {
                "status": "unknown",
                "operation_id": operation["operation_id"],
                "reason": operation.get("error_code") or "Operation outcome requires verification",
            },
        )

    def _dispatch_error(self, action, exc):
        if isinstance(exc, MCPAuthenticationError):
            raise exc
        if isinstance(exc, MCPToolError) and exc.code in {
            "unauthorized",
            "employee_credential_unavailable",
        }:
            raise MCPAuthenticationError("Original employee credential unavailable") from exc
        code = exc.code if isinstance(exc, MCPToolError) else "outcome_unknown"
        return self._persist(
            action["id"],
            {
                "status": "failed" if code in KNOWN_FAILURES else "unknown",
                "reason": code,
                "operation_id": action.get("server_operation_id"),
            },
        )

    def _prepare(self, action):
        snapshot = action["snapshot"]
        operation = self.client.prepare(
            snapshot["operation_id"],
            snapshot["command"],
            snapshot["params"],
            grant=self._grant(action["id"]),
        )
        return self._bound_operation(action, operation)

    def execute(self, action_id, expected_digest, approval_id=None, *, before_dispatch=None):
        with self.database.connect() as conn:
            self.guard(conn)
            action = self._action(conn, action_id)
            if action["digest"] != expected_digest:
                raise ValueError("Business action changed before execution")
            if action["status"] == "succeeded":
                return action["receipt"]
            if action["status"] in {"failed", "denied"} or (
                action["snapshot"]["assessment"]["outcome"] not in {"allow", "confirm"}
            ):
                return {"status": "denied", "reason": "Action lacks execution authority"}
            reconciling = action["status"] in {"running", "unknown"}
            if not reconciling:
                UsageLedger(self.database, self.task["id"]).assert_available(connection=conn)
                conn.execute("UPDATE app.actions SET status='running' WHERE id=%s", (action_id,))
                conn.execute(
                    "INSERT INTO app.action_attempts(id,action_id,generation,execution_id,status) "
                    "VALUES(%s,%s,%s,%s,'running') ON CONFLICT(execution_id) DO NOTHING",
                    (
                        uuid4(),
                        action_id,
                        self.generation,
                        action["snapshot"]["operation_id"] + ":" + str(self.generation),
                    ),
                )
        if reconciling:
            return self.reconcile(action_id, before_dispatch=before_dispatch)
        snapshot = action["snapshot"]
        if self.spec.commands[snapshot["command"]].effect == "read":
            data = self._read_data(snapshot["command"], snapshot["params"])
            return self._persist(
                action_id,
                {
                    "status": "succeeded",
                    "data": data,
                    "verification": {"schema_valid": True},
                },
            )
        try:
            if before_dispatch:
                before_dispatch()
            operation = self._prepare(action)
            if operation["state"] == "ready":
                if before_dispatch:
                    before_dispatch()
                self.client.commit(operation["operation_id"], grant=self._grant(action_id))
                operation = self.client.query_operation(operation["operation_id"])
            return self._result(action, operation)
        except (MCPToolError, MCPTransportError, MCPAuthenticationError, MCPProtocolError) as exc:
            return self._dispatch_error(action, exc)

    def reconcile(self, action_id, *, before_dispatch=None):
        with self.database.connect() as conn:
            self.guard(conn)
            action = self._action(conn, action_id)
            if action["status"] == "succeeded":
                return action["receipt"]
            if action["status"] not in {"running", "unknown"}:
                return {
                    "status": action["status"],
                    "reason": "No dispatched operation to reconcile",
                }
        try:
            if action["server_operation_id"] is None:
                if before_dispatch:
                    before_dispatch()
                operation = self._prepare(action)
            else:
                operation = self.client.query_operation(action["server_operation_id"])
            if operation["state"] == "ready" or (
                operation["state"] == "unknown"
                and operation.get("error_code") == "recovery_grant_required"
            ):
                if before_dispatch:
                    before_dispatch()
                self.client.commit(operation["operation_id"], grant=self._grant(action_id))
                operation = self.client.query_operation(operation["operation_id"])
            return self._result(action, operation)
        except (MCPToolError, MCPTransportError, MCPAuthenticationError, MCPProtocolError) as exc:
            return self._dispatch_error(action, exc)

    def verify_original(self, action_id, expected_digest, *, expected_inputs=None):
        """Inspect a bound operation without preparing, issuing a grant or committing.

        This path remains usable when a plan dependency is withdrawn. Its result
        is historical operation evidence, never permission to run the remainder.
        """
        with self.database.connect() as conn:
            self.guard(conn)
            action = self._action(conn, action_id)
            if action["digest"] != expected_digest or (
                expected_inputs is not None and action["snapshot"]["params"] != expected_inputs
            ):
                raise MCPProtocolError("Original action inputs changed")
            operation_id = action["server_operation_id"]
            if operation_id is None:
                return {"status": "unknown", "reason": "No original service operation is bound"}
            hashes = {
                row["input_hash"]
                for row in conn.execute(
                    "SELECT input_hash FROM business.execution_grants WHERE task_id=%s "
                    "AND action_id=%s AND action_digest=%s AND request_id=%s AND employee_id=%s",
                    (
                        self.task["id"],
                        action_id,
                        expected_digest,
                        action["snapshot"]["operation_id"],
                        self.task["employee_id"],
                    ),
                ).fetchall()
            }
        try:
            operation = self.client.query_operation(operation_id)
            try:
                validate_schema(
                    operation_schema(self.spec, action["snapshot"]["command"]), operation
                )
            except ValueError as exc:
                raise MCPProtocolError(
                    "Original service operation violates its frozen contract"
                ) from exc
            if (
                not isinstance(operation, dict)
                or operation.get("operation_id") != operation_id
                or operation.get("input_hash") not in hashes
            ):
                raise MCPProtocolError("Original service operation input digest changed")
            return self._result(action, operation)
        except (MCPToolError, MCPTransportError, MCPAuthenticationError, MCPProtocolError) as exc:
            if isinstance(exc, MCPAuthenticationError):
                raise
            if isinstance(exc, MCPToolError) and exc.code in {
                "unauthorized",
                "employee_credential_unavailable",
            }:
                raise MCPAuthenticationError("Original employee credential unavailable") from exc
            # Failed observation must not replace an earlier valid receipt, or
            # mistake a cached success for verification of this mismatched reply.
            return {
                "status": "unknown",
                "operation_id": operation_id,
                "reason": exc.code if isinstance(exc, MCPToolError) else "verification_unavailable",
            }

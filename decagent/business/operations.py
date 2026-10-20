"""Immutable employee operations; all callers share one preparation and approval core."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.contracts import PluginSpec, canonical, validate_schema
from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.grants import GrantAuthority
from decagent.business.mcp_contract import operation_schema
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketError, TicketStore, _json


class OperationService:
    def __init__(self, database):
        self.database = database
        self.spec = PluginSpec.from_mapping(builtin_manifest())
        self.output_schema = operation_schema(self.spec)
        self.grants = GrantAuthority(database)

    def _identity(self, conn, identity, *, lock=False, browser_only=False):
        if not isinstance(identity, EmployeeIdentity):
            raise AccessError("unauthorized", "Employee authentication required")
        if browser_only and identity.source != "employee_session":
            raise AccessError("forbidden", "Original employee browser session required")
        if identity.source == "mcp_token":
            query = (
                "SELECT employee_id,workspace_id FROM business.mcp_credentials "
                "WHERE token_hash=%s AND workspace_id=%s "
                "AND employee_id=%s AND account_revision=%s AND id=%s "
                "AND revoked_at IS NULL AND expires_at>now()"
            )
            args = (
                identity.token_hash,
                identity.workspace_id,
                identity.employee_id,
                identity.account_revision,
                identity.credential_ref,
            )
        elif identity.source == "employee_session":
            query = (
                "SELECT employee_id,workspace_id FROM business.employee_sessions "
                "WHERE token_hash=%s AND workspace_id=%s "
                "AND employee_id=%s AND account_revision=%s "
                "AND revoked_at IS NULL AND expires_at>now()"
            )
            args = (
                identity.token_hash,
                identity.workspace_id,
                identity.employee_id,
                identity.account_revision,
            )
        else:
            raise AccessError("unauthorized", "Employee authentication required")
        if not conn.execute(query + (" FOR SHARE" if lock else ""), args).fetchone():
            raise AccessError("unauthorized", "Employee credential unavailable")
        actor = access.principal(
            conn, identity.employee_id, workspace_id=identity.workspace_id, lock=lock
        )
        if actor["credential_revision"] != identity.account_revision:
            raise AccessError("unauthorized", "Employee credential unavailable")
        return actor

    def _scope(self, conn):
        scope = conn.execute(
            "SELECT * FROM app.business_scopes WHERE plugin_id=%s FOR SHARE", (self.spec.id,)
        ).fetchone()
        if not scope or not scope["enabled"] or scope["manifest_digest"] != self.spec.digest:
            raise AccessError("contract_changed", "Registered business service unavailable")
        return scope

    def _operation_scope(self, conn, row):
        scope = self._scope(conn)
        if row["scope_id"] != scope["id"] or row["scope_revision"] != scope["revision"]:
            raise AccessError(
                "contract_changed", "Business registration changed; prepare a new operation"
            )
        return scope

    def _preflight(self, conn, actor_id, command, params, *, workspace_id, lock=False):
        effective = access.authorize_write(
            conn, actor_id, command, params, workspace_id=workspace_id, lock=lock
        )
        resource = None
        if command == "create":
            if not params["title"].strip():
                raise AccessError("invalid_input", "Ticket title must not be blank")
            if not conn.execute(
                "SELECT id FROM business.categories WHERE id=%s AND workspace_id=%s",
                (params["category_id"], workspace_id),
            ).fetchone():
                raise AccessError("invalid_reference", "Category unavailable")
        else:
            ticket = access.get_ticket(
                conn, actor_id, params["ticket_id"], workspace_id=workspace_id
            )
            if ticket["revision"] != params["expected_revision"]:
                raise AccessError("revision_conflict", "Ticket changed; prepare a new operation")
            transition = self.spec.manifest["workflow"].get("transitions", {}).get(command)
            if transition and ticket["status"] not in transition["from"]:
                raise AccessError("invalid_transition", "Command is invalid in current status")
            resource = _json(
                {key: ticket[key] for key in ("id", "revision", "status", "assignee_id")}
            )
        for key in ("body", "resolution"):
            if key in params and not params[key].strip():
                raise AccessError("invalid_input", "Required business text must not be blank")
        return _json(effective), resource

    def _view(self, conn, row):
        approval = conn.execute(
            "SELECT * FROM business.operation_approvals WHERE operation_id=%s", (row["id"],)
        ).fetchone()
        view = _json(
            {
                "operation_id": row["id"],
                "employee_id": row["employee_id"],
                "request_id": row["request_id"],
                "command": row["command"],
                "params": row["params"],
                "input_hash": row["input_hash"],
                "contract_digest": row["contract_digest"],
                "expected_revision": row["expected_revision"],
                "source": row["source"],
                "state": row["state"],
                "approval_url": "/#operation=" + row["id"]
                if row["state"] == "awaiting_approval"
                else None,
                "approval_expires_at": approval["expires_at"] if approval else None,
                "approved_at": approval["approved_at"] if approval else None,
                "result": row["result"],
                "error_code": row["error_code"],
            }
        )
        validate_schema(self.output_schema, view)
        return view

    def _operation(self, conn, actor, operation_id, *, lock=False, original=False):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise AccessError("invalid_input", "Invalid operation handle")
        row = conn.execute(
            "SELECT * FROM business.operations WHERE id=%s AND workspace_id=%s"
            + (" FOR UPDATE" if lock else ""),
            (operation_id, actor["workspace_id"]),
        ).fetchone()
        if not row or (
            row["employee_id"] != actor["id"] and (original or actor["role"] != "supervisor")
        ):
            raise AccessError("not_found", "Operation unavailable")
        return row

    def prepare(self, identity, request_id, command, params, *, grant=None):
        with self.database.connect() as conn:
            return self._prepare_in_transaction(
                conn, identity, request_id, command, params, grant=grant
            )

    def _prepare_in_transaction(
        self, conn, identity, request_id, command, params, *, grant=None, supersedes=None
    ):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise AccessError("invalid_input", "Request key must contain 1 to 200 characters")
        try:
            serialized = canonical(params)
            if len(serialized.encode("utf-8")) > 65536:
                raise ValueError("Input too large")
            self.spec.validate(command, params)
        except (ValueError, KeyError, TypeError) as exc:
            raise AccessError("invalid_input", "Input does not match business schema") from exc
        actor = self._identity(conn, identity)
        scope = self._scope(conn)
        # The same employee/request key serializes even before an operation row exists.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,46124))",
            (str(actor["id"]) + ":" + request_id,),
        )
        effective = _json(
            {**params, "requester_id": actor["person_id"]} if command == "create" else params
        )
        input_hash = hashlib.sha256(
            canonical(
                {
                    "employee_id": str(actor["id"]),
                    "command": command,
                    "params": effective,
                    "contract_digest": self.spec.digest,
                }
            ).encode("utf-8")
        ).hexdigest()
        program = (
            self.grants.validate(
                conn, grant, actor["id"], request_id=request_id, input_hash=input_hash
            )
            if grant is not None
            else None
        )
        prior = conn.execute(
            "SELECT * FROM business.operations WHERE employee_id=%s AND workspace_id=%s "
            "AND request_id=%s",
            (actor["id"], actor["workspace_id"], request_id),
        ).fetchone()
        if prior:
            if prior["input_hash"] != input_hash:
                raise AccessError("operation_conflict", "Request key already has different input")
            if prior["source"] == "decagent":
                self.grants.validate(conn, grant, actor["id"], operation=prior)
            elif program is not None:
                raise AccessError("invalid_grant", "External operation cannot attach an Agent task")
            return self._view(conn, prior)
        effective, resource = self._preflight(
            conn, actor["id"], command, params, workspace_id=actor["workspace_id"]
        )
        confirmation = self.spec.commands[command].confirmation
        needs_approval = (
            (program is not None and program["requires_confirmation"])
            or confirmation == "confirm"
            or (confirmation == "initial_assignment" and resource and resource["assignee_id"])
        )
        state = "awaiting_approval" if needs_approval else "ready"
        row = conn.execute(
            "INSERT INTO business.operations(id,employee_id,workspace_id,request_id,scope_id,"
            "command,"
            "params,effective_params,input_hash,contract_digest,expected_revision,"
            "resource_snapshot,source,state,confirmation_required,supersedes,agent_task_id,"
            "agent_action_id,agent_generation,scope_revision) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                "op_" + uuid4().hex,
                actor["id"],
                actor["workspace_id"],
                request_id,
                scope["id"],
                command,
                Jsonb(params),
                Jsonb(effective),
                input_hash,
                self.spec.digest,
                params.get("expected_revision"),
                Jsonb(resource),
                "decagent" if program else "external_mcp",
                state,
                bool(needs_approval),
                supersedes,
                program["task_id"] if program else None,
                program["action_id"] if program else None,
                program["generation"] if program else None,
                scope["revision"],
            ),
        ).fetchone()
        if program:
            conn.execute(
                "UPDATE app.actions SET server_operation_id=%s WHERE id=%s",
                (row["id"], program["action_id"]),
            )
        conn.execute(
            "INSERT INTO business.employee_audit(id,actor_employee_id,workspace_id,source,event,"
            "outcome,operation_id) VALUES(%s,%s,%s,%s,'operation_prepared','allowed',%s)",
            (
                uuid4(),
                actor["id"],
                actor["workspace_id"],
                "mcp" if identity.source == "mcp_token" else "web",
                row["id"],
            ),
        )
        return self._view(conn, row)

    def get(self, identity, operation_id):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise AccessError("invalid_input", "Invalid operation handle")
        with self.database.connect(autocommit=True) as conn:
            with conn.transaction():
                actor = self._identity(conn, identity)
                row = self._operation(conn, actor, operation_id)
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,46125)) AS acquired",
                (operation_id,),
            ).fetchone()["acquired"]
            if not acquired:
                with conn.transaction():
                    actor = self._identity(conn, identity)
                    row = self._operation(conn, actor, operation_id)
                    if row["state"] in {"ready", "running", "unknown", "succeeded"}:
                        row = {
                            **row,
                            "state": "unknown",
                            "result": None,
                            "error_code": "commit_in_flight",
                        }
                    return self._view(conn, row)
            try:
                with conn.transaction():
                    actor = self._identity(conn, identity)
                    row = self._operation(conn, actor, operation_id, lock=True)
                    row = self._reconcile(conn, row)
                    row = self._expire(conn, row)
                    if row["state"] in {"ready", "awaiting_approval"}:
                        try:
                            self._operation_scope(conn, row)
                        except AccessError as exc:
                            return self._failure(conn, row, exc.code)
                    return self._view(conn, row)
            finally:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,46125))", (operation_id,)
                )

    def _receipt_evidence(self, conn, row):
        """Only a bound receipt plus matching durable history proves a write committed."""
        receipt = conn.execute(
            "SELECT * FROM business.operation_receipts WHERE operation_id=%s AND workspace_id=%s",
            (row["id"], row["workspace_id"]),
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM business.ticket_history WHERE operation_id=%s AND workspace_id=%s",
            (row["id"], row["workspace_id"]),
        ).fetchall()
        if receipt is None:
            return ("partial_history" if history else "absent"), None
        if len(history) != 1:
            return "receipt_mismatch", None
        event = history[0]
        ticket = conn.execute(
            "SELECT id,revision FROM business.tickets WHERE id=%s AND workspace_id=%s",
            (receipt["resource_id"], row["workspace_id"]),
        ).fetchone()
        saved_result = receipt["result"]
        saved_ticket = saved_result.get("ticket") if isinstance(saved_result, dict) else None
        if any(
            (
                receipt["employee_id"] != row["employee_id"],
                receipt["command"] != row["command"],
                receipt["input_hash"] != row["input_hash"],
                receipt["contract_digest"] != row["contract_digest"],
                event["employee_id"] != row["employee_id"],
                event["source"] != row["source"],
                event["command"] != row["command"],
                event["details"] != row["effective_params"],
                event["ticket_id"] != receipt["resource_id"],
                event["revision"] != receipt["revision"],
                not ticket or ticket["revision"] < receipt["revision"],
                not isinstance(saved_ticket, dict)
                or saved_ticket.get("id") != str(receipt["resource_id"]),
                not isinstance(saved_ticket, dict)
                or saved_ticket.get("revision") != receipt["revision"],
            )
        ):
            return "receipt_mismatch", None
        data = {**saved_result, "receipt": TicketStore._receipt(receipt)}
        try:
            validate_schema(self.spec.commands[row["command"]].result_schema, data)
        except (KeyError, TypeError, ValueError):
            return "receipt_mismatch", None
        if row["state"] == "succeeded" and row["result"] != data:
            return "receipt_mismatch", None
        return "verified", data

    def _unknown(self, conn, row, code):
        return conn.execute(
            "UPDATE business.operations SET state='unknown',result=NULL,error_code=%s,"
            "updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
            (code, row["id"], row["workspace_id"]),
        ).fetchone()

    def _reconcile(self, conn, row, *, grant=None):
        if row["state"] not in {"ready", "running", "unknown", "succeeded"}:
            return row
        evidence, result = self._receipt_evidence(conn, row)
        if evidence == "verified":
            if row["state"] != "succeeded":
                row = conn.execute(
                    "UPDATE business.operations SET state='succeeded',result=%s,error_code=NULL,"
                    "updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                    (Jsonb(result), row["id"], row["workspace_id"]),
                ).fetchone()
            return row
        if evidence != "absent":
            return self._unknown(conn, row, evidence)
        if row["state"] == "succeeded":
            return self._unknown(conn, row, "receipt_missing")
        if row["state"] == "ready":
            return row
        if row["error_code"] in {"receipt_mismatch", "partial_history", "receipt_missing"}:
            return row
        if row["agent_task_id"] is not None:
            try:
                self.grants.validate(conn, grant, row["employee_id"], operation=row)
            except AccessError:
                return self._unknown(conn, row, "recovery_grant_required")
        try:
            self._operation_scope(conn, row)
            self._preflight(
                conn,
                row["employee_id"],
                row["command"],
                row["params"],
                workspace_id=row["workspace_id"],
                lock=True,
            )
        except AccessError as exc:
            return self._failure(conn, row, exc.code)
        if row["confirmation_required"]:
            approval = conn.execute(
                "SELECT * FROM business.operation_approvals WHERE operation_id=%s "
                "AND workspace_id=%s FOR UPDATE",
                (row["id"], row["workspace_id"]),
            ).fetchone()
            if not approval or approval["consumed_at"] is not None:
                return self._unknown(conn, row, "approval_evidence_missing")
            if any(
                (
                    approval["employee_id"] != row["employee_id"],
                    approval["decision"] != "approve",
                    approval["input_hash"] != row["input_hash"],
                    approval["contract_digest"] != row["contract_digest"],
                    approval["expected_revision"] != row["expected_revision"],
                    approval["policy_version"] != "business-approval-v2",
                )
            ):
                return self._unknown(conn, row, "approval_evidence_missing")
            if approval["expires_at"] <= datetime.now(UTC):
                return self._failure(conn, row, "approval_expired")
        return conn.execute(
            "UPDATE business.operations SET state='ready',attempt_id=NULL,error_code=NULL,"
            "updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
            (row["id"], row["workspace_id"]),
        ).fetchone()

    def _expire(self, conn, row):
        if row["confirmation_required"] and row["state"] == "ready":
            approval = conn.execute(
                "SELECT expires_at FROM business.operation_approvals WHERE operation_id=%s "
                "AND workspace_id=%s",
                (row["id"], row["workspace_id"]),
            ).fetchone()
            if approval and approval["expires_at"] <= datetime.now(UTC):
                row = conn.execute(
                    "UPDATE business.operations SET state='expired',error_code='approval_expired',"
                    "updated_at=now() WHERE id=%s AND workspace_id=%s RETURNING *",
                    (row["id"], row["workspace_id"]),
                ).fetchone()
        return row

    def reply(self, identity, operation_id, expected_hash, decision, *, edited_params=None):
        with self.database.connect() as conn:
            return self.reply_in_transaction(
                conn, identity, operation_id, expected_hash, decision, edited_params=edited_params
            )

    def reply_in_transaction(
        self, conn, identity, operation_id, expected_hash, decision, *, edited_params=None
    ):
        if decision not in {"approve", "reject", "edit"}:
            raise AccessError("invalid_input", "Unknown approval decision")
        if (decision == "edit") != (edited_params is not None):
            raise AccessError("invalid_input", "Edited input belongs to an edit decision")
        actor = self._identity(conn, identity, lock=True, browser_only=True)
        row = self._operation(conn, actor, operation_id, lock=True, original=True)
        if expected_hash != row["input_hash"]:
            raise AccessError("operation_conflict", "Approval input binding changed")
        row = self._expire(conn, row)
        prior = conn.execute(
            "SELECT * FROM business.operation_approvals WHERE operation_id=%s AND workspace_id=%s",
            (row["id"], row["workspace_id"]),
        ).fetchone()
        if prior:
            if prior["decision"] != decision:
                raise AccessError("operation_conflict", "Operation already has a different reply")
            if decision == "edit":
                replacement = conn.execute(
                    "SELECT * FROM business.operations WHERE supersedes=%s AND workspace_id=%s",
                    (row["id"], row["workspace_id"]),
                ).fetchone()
                if not replacement or replacement["params"] != edited_params:
                    raise AccessError("operation_conflict", "Edited operation input changed")
                return self._view(conn, replacement)
            return self._view(conn, row)
        if row["state"] not in {"ready", "awaiting_approval"}:
            raise AccessError("operation_conflict", "Operation cannot receive a new reply")
        if decision != "reject":
            try:
                self._operation_scope(conn, row)
                self._preflight(
                    conn,
                    actor["id"],
                    row["command"],
                    row["params"],
                    workspace_id=actor["workspace_id"],
                    lock=True,
                )
            except AccessError as exc:
                row = conn.execute(
                    "UPDATE business.operations SET state='failed',error_code=%s,"
                    "updated_at=now() "
                    "WHERE id=%s AND workspace_id=%s RETURNING *",
                    (exc.code, row["id"], row["workspace_id"]),
                ).fetchone()
                return self._view(conn, row)
        replacement = None
        if decision == "edit":
            replacement = self._prepare_in_transaction(
                conn,
                identity,
                "edit_" + uuid4().hex,
                row["command"],
                edited_params,
                supersedes=row["id"],
            )
        conn.execute(
            "INSERT INTO business.operation_approvals(operation_id,employee_id,workspace_id,"
            "input_hash,"
            "contract_digest,expected_revision,policy_version,decision,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'business-approval-v2',%s,now()+interval '15 minutes')",
            (
                row["id"],
                actor["id"],
                actor["workspace_id"],
                row["input_hash"],
                row["contract_digest"],
                row["expected_revision"],
                decision,
            ),
        )
        row = conn.execute(
            "UPDATE business.operations SET state=%s,updated_at=now() "
            "WHERE id=%s AND workspace_id=%s RETURNING *",
            ("ready" if decision == "approve" else "rejected", row["id"], row["workspace_id"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO business.employee_audit(id,actor_employee_id,workspace_id,source,event,"
            "outcome,operation_id) VALUES(%s,%s,%s,'web',%s,'allowed',%s)",
            (uuid4(), actor["id"], actor["workspace_id"], "operation_" + decision, row["id"]),
        )
        return replacement or self._view(conn, row)

    def _failure(self, conn, row, code):
        row = conn.execute(
            "UPDATE business.operations SET state=%s,error_code=%s,updated_at=now() "
            "WHERE id=%s AND workspace_id=%s RETURNING *",
            (
                "expired" if code == "approval_expired" else "failed",
                code,
                row["id"],
                row["workspace_id"],
            ),
        ).fetchone()
        conn.execute(
            "INSERT INTO business.employee_audit(id,actor_employee_id,workspace_id,source,event,"
            "outcome,operation_id) VALUES(%s,%s,%s,'system','operation_commit','failed',%s)",
            (uuid4(), row["employee_id"], row["workspace_id"], row["id"]),
        )
        return self._view(conn, row)

    def commit(self, identity, operation_id, *, grant=None):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise AccessError("invalid_input", "Invalid operation handle")
        with self.database.connect(autocommit=True) as conn:
            # A session lock spans the durable running marker and the domain transaction.
            # Every duplicate commit and recovery path must share this lock.
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s,46125))", (operation_id,))
            try:
                with conn.transaction():
                    actor = self._identity(conn, identity, lock=True)
                    row = self._operation(conn, actor, operation_id, lock=True, original=True)
                    row = self._reconcile(conn, row, grant=grant)
                    row = self._expire(conn, row)
                    if row["state"] != "ready":
                        return self._view(conn, row)
                    try:
                        self._operation_scope(conn, row)
                        self._preflight(
                            conn,
                            actor["id"],
                            row["command"],
                            row["params"],
                            workspace_id=actor["workspace_id"],
                            lock=True,
                        )
                    except AccessError as exc:
                        return self._failure(conn, row, exc.code)
                    if row["agent_task_id"] is not None:
                        self.grants.validate(conn, grant, actor["id"], operation=row)
                    elif grant is not None:
                        raise AccessError(
                            "invalid_grant", "External operation cannot attach an Agent task"
                        )
                    row = conn.execute(
                        "UPDATE business.operations SET state='running',attempt_id=%s,"
                        "started_at=now(),updated_at=now() WHERE id=%s RETURNING *",
                        (uuid4(), operation_id),
                    ).fetchone()
                try:
                    with conn.transaction():
                        actor = self._identity(conn, identity, lock=True)
                        row = self._operation(conn, actor, operation_id, lock=True, original=True)
                        self._operation_scope(conn, row)
                        self._preflight(
                            conn,
                            actor["id"],
                            row["command"],
                            row["params"],
                            workspace_id=actor["workspace_id"],
                            lock=True,
                        )
                        result = TicketStore(self.database.url).execute_in_transaction(
                            conn, actor["id"], operation_id, grant=grant
                        )
                        row = conn.execute(
                            "UPDATE business.operations SET state='succeeded',result=%s,"
                            "updated_at=now() WHERE id=%s RETURNING *",
                            (Jsonb(result), operation_id),
                        ).fetchone()
                        conn.execute(
                            "INSERT INTO business.employee_audit(id,actor_employee_id,workspace_id,"
                            "source,event,outcome,operation_id) VALUES(%s,%s,%s,'system',"
                            "'operation_commit','allowed',%s)",
                            (uuid4(), actor["id"], actor["workspace_id"], operation_id),
                        )
                        completed = self._view(conn, row)
                    return completed
                except Exception as exc:
                    if conn.closed or conn.broken:
                        raise AccessError(
                            "service_unavailable", "Commit outcome requires verification"
                        ) from exc
                    # The transaction context has confirmed rollback before this state change.
                    code = (
                        exc.code
                        if isinstance(exc, (AccessError, TicketError))
                        else "domain_failure"
                    )
                    if isinstance(exc, psycopg.IntegrityError):
                        code = "invalid_reference"
                    with conn.transaction():
                        row = conn.execute(
                            "SELECT * FROM business.operations WHERE id=%s AND workspace_id=%s "
                            "FOR UPDATE",
                            (operation_id, identity.workspace_id),
                        ).fetchone()
                        return self._failure(conn, row, code)
            except AccessError as exc:
                if exc.code == "unauthorized" and not conn.closed and not conn.broken:
                    self._credential_failure(conn, identity, operation_id)
                raise
            finally:
                if not conn.closed and not conn.broken:
                    conn.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,46125))", (operation_id,)
                    )

    def _credential_failure(self, conn, identity, operation_id):
        if not isinstance(identity, EmployeeIdentity):
            return
        table = {"mcp_token": "mcp_credentials", "employee_session": "employee_sessions"}.get(
            identity.source
        )
        if table is None:
            return
        with conn.transaction():
            original = conn.execute(
                "SELECT employee_id FROM business." + table + " WHERE token_hash=%s "
                "AND workspace_id=%s",
                (identity.token_hash, identity.workspace_id),
            ).fetchone()
            if not original or original["employee_id"] != identity.employee_id:
                return
            row = conn.execute(
                "SELECT * FROM business.operations WHERE id=%s AND employee_id=%s "
                "AND workspace_id=%s FOR UPDATE",
                (operation_id, identity.employee_id, identity.workspace_id),
            ).fetchone()
            if not row:
                return
            if row["state"] in {"ready", "awaiting_approval"}:
                conn.execute(
                    "UPDATE business.operations SET error_code='employee_credential_unavailable',"
                    "updated_at=now() WHERE id=%s AND workspace_id=%s",
                    (operation_id, identity.workspace_id),
                )
            conn.execute(
                "INSERT INTO business.employee_audit(id,actor_employee_id,workspace_id,"
                "source,event,"
                "outcome,operation_id) VALUES(%s,%s,%s,%s,'credential_rejected','denied',%s)",
                (
                    uuid4(),
                    identity.employee_id,
                    identity.workspace_id,
                    "mcp" if identity.source == "mcp_token" else "web",
                    operation_id,
                ),
            )

"""Private program authorization for Agent dispatch; never a model-facing capability."""

import hashlib
import secrets
from uuid import uuid4

from decagent.business.access import AccessError
from decagent.business.contracts import PluginSpec, canonical
from decagent.business.tickets import builtin_manifest
from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger


class GrantAuthority:
    def __init__(self, database):
        self.database = database
        self.spec = PluginSpec.from_mapping(builtin_manifest())

    def _current(self, conn, task_id, action_id, owner, generation):
        try:
            task = self.database.assert_lease(task_id, owner, generation, conn)
        except ValueError as exc:
            raise AccessError("invalid_grant", "Agent lease or task unavailable") from exc
        if task["kind"] != "business" or task["employee_id"] is None:
            raise AccessError("invalid_grant", "Employee-bound business task required")
        scope = conn.execute(
            "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (task["business_scope_id"],)
        ).fetchone()
        if not scope or not scope["enabled"] or scope["manifest_digest"] != self.spec.digest:
            raise AccessError("contract_changed", "Agent business contract unavailable")
        action = conn.execute(
            "SELECT * FROM app.actions WHERE id=%s AND task_id=%s FOR UPDATE", (action_id, task_id)
        ).fetchone()
        if not action or action["status"] not in {"planned", "running", "unknown"}:
            raise AccessError("invalid_grant", "Agent action unavailable")
        snapshot = action["snapshot"]
        assessment = snapshot.get("assessment", {})
        if any(
            (
                snapshot.get("task_id") != str(task_id),
                snapshot.get("scope_id") != str(scope["id"]),
                snapshot.get("plugin_digest") != self.spec.digest,
                assessment.get("digest") != action["digest"],
                assessment.get("outcome") not in {"allow", "confirm"},
            )
        ):
            raise AccessError("invalid_grant", "Agent action authorization changed")
        credential = conn.execute(
            "SELECT a.person_id FROM business.mcp_credentials c "
            "JOIN business.employee_accounts a ON a.id=c.employee_id "
            "JOIN business.people p ON p.id=a.person_id WHERE c.id=%s AND c.employee_id=%s "
            "AND c.revoked_at IS NULL AND c.expires_at>now() AND a.active AND p.active "
            "AND c.account_revision=a.credential_revision FOR SHARE OF c",
            (task["credential_ref"], task["employee_id"]),
        ).fetchone()
        if not credential:
            raise AccessError(
                "employee_credential_unavailable", "Task employee credential unavailable"
            )
        command, params = snapshot.get("command"), snapshot.get("params")
        try:
            self.spec.validate(command, params)
            if self.spec.commands[command].effect != "write":
                raise ValueError("Grant requires a write action")
        except (ValueError, KeyError, TypeError) as exc:
            raise AccessError("invalid_grant", "Agent action input unavailable") from exc
        request_id = snapshot.get("operation_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise AccessError("invalid_grant", "Agent request binding unavailable")
        effective = (
            {**params, "requester_id": str(credential["person_id"])}
            if command == "create"
            else params
        )
        input_hash = hashlib.sha256(
            canonical(
                {
                    "employee_id": str(task["employee_id"]),
                    "command": command,
                    "params": effective,
                    "contract_digest": self.spec.digest,
                }
            ).encode("utf-8")
        ).hexdigest()
        try:
            UsageLedger(self.database, task_id).assert_action_available(
                connection=conn,
                current_action=action_id if task["graph_version"] == "business-3" else None,
            )
        except BudgetExceeded as exc:
            raise AccessError("budget_exceeded", "Agent budget exhausted") from exc
        task = {**task, "scope_revision": scope["revision"]}
        return task, action, request_id, input_hash, assessment["outcome"] == "confirm"

    def issue(self, task_id, action_id, owner, generation, *, ttl_seconds=30):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
            raise ValueError("Invalid Agent grant lifetime")
        token = "dcg_" + secrets.token_urlsafe(48)
        with self.database.connect() as conn:
            task, action, request_id, input_hash, requires = self._current(
                conn, task_id, action_id, owner, generation
            )
            conn.execute(
                "INSERT INTO business.execution_grants(id,token_hash,employee_id,task_id,action_id,"
                "request_id,input_hash,action_digest,scope_id,contract_digest,lease_owner,generation,"
                "expires_at,requires_confirmation,scope_revision) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "LEAST(now()+%s*interval '1 second',%s),%s,%s)",
                (
                    uuid4(),
                    hashlib.sha256(token.encode()).hexdigest(),
                    task["employee_id"],
                    task_id,
                    action_id,
                    request_id,
                    input_hash,
                    action["digest"],
                    task["business_scope_id"],
                    self.spec.digest,
                    owner,
                    generation,
                    ttl_seconds,
                    task["lease_until"],
                    requires,
                    task["scope_revision"],
                ),
            )
        return token

    def validate(
        self, conn, token, employee_id, *, request_id=None, input_hash=None, operation=None
    ):
        if type(token) is not str or not token.startswith("dcg_") or len(token) > 512:
            raise AccessError("invalid_grant", "Agent execution grant required")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = conn.execute(
            "SELECT * FROM business.execution_grants WHERE token_hash=%s "
            "AND revoked_at IS NULL AND expires_at>clock_timestamp()",
            (token_hash,),
        ).fetchone()
        if not row or row["employee_id"] != employee_id:
            raise AccessError("invalid_grant", "Agent execution grant unavailable")
        task, action, actual_key, actual_hash, requires = self._current(
            conn, row["task_id"], row["action_id"], row["lease_owner"], row["generation"]
        )
        valid = conn.execute(
            "SELECT id FROM business.execution_grants WHERE id=%s AND revoked_at IS NULL "
            "AND expires_at>clock_timestamp() FOR SHARE",
            (row["id"],),
        ).fetchone()
        if not valid or any(
            (
                task["employee_id"] != employee_id,
                action["digest"] != row["action_digest"],
                row["request_id"] != actual_key,
                row["input_hash"] != actual_hash,
                row["requires_confirmation"] != requires,
                row["scope_revision"] != task["scope_revision"],
                request_id is not None and request_id != actual_key,
                input_hash is not None and input_hash != actual_hash,
            )
        ):
            raise AccessError("invalid_grant", "Agent execution binding changed")
        if operation and any(
            (
                operation["source"] != "decagent",
                operation["agent_task_id"] != task["id"],
                operation["agent_action_id"] != action["id"],
                operation["request_id"] != actual_key,
                operation["input_hash"] != actual_hash,
                operation["scope_id"] != task["business_scope_id"],
                operation["contract_digest"] != self.spec.digest,
            )
        ):
            raise AccessError("invalid_grant", "Agent operation binding changed")
        return row

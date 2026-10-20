"""Employee business control; MCP queries and the shared approval core authorize work."""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec, validate_schema
from decagent.business.decision_policy import PolicyContext
from decagent.business.interventions import SemanticInterventions, decision_evidence
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import digest, json_value
from decagent.storage.safe_record import exact_safe_record


class BusinessControl:
    def __init__(
        self, database, *, read=None, employee_read=None, conditional_workflows_enabled=False
    ):
        if type(conditional_workflows_enabled) is not bool:
            raise ValueError("Conditional workflow rollout flag must be a boolean")
        self.database = database
        self.conditional_workflows_enabled = conditional_workflows_enabled
        self.read_callback = employee_read
        self.operations = OperationService(database)
        self.semantic = SemanticInterventions(database, refresh=self.refresh_semantic)

    def refresh_semantic(self, conn, task, pending, identity):
        """Rebuild current choices through the original employee's trusted MCP reads."""
        self._employee(task, identity)
        spec = self.spec(task)
        state = json_value(pending["binding"]["evidence"])
        state["directory"] = self.read(spec, "directory", {}, identity=identity)
        if state.get("ticket") is not None:
            state["ticket"] = self.read(
                spec, "get", {"ticket_id": state["ticket"]["id"]}, identity=identity
            )["ticket"]
        context = PolicyContext(**pending["binding"]["snapshot"]["context"])
        return decision_evidence(
            spec,
            task,
            pending["binding"]["node"],
            state,
            context=context,
            scope_version=task["scope_revision"],
            account_version=identity.account_revision,
        )

    def reply_semantic(self, conn, task, approval, body, identity, response_id):
        self._employee(task, identity)
        return self.semantic.reply_in_transaction(conn, task, approval, body, identity, response_id)

    def plugins(self, identity=None):
        allowed = None
        if identity is not None:
            with self.database.connect() as conn:
                allowed = access.allowed_commands(
                    conn, identity.employee_id, workspace_id=identity.workspace_id
                )
        return [
            {
                "scope_id": str(row["id"]),
                "plugin_id": row["plugin_id"],
                "version": row["plugin_version"],
                "digest": row["manifest_digest"],
                "enabled": row["enabled"],
                "level": row["manifest"]["level"],
                "commands": [
                    {
                        "name": name,
                        **{key: command[key] for key in ("description", "effect", "confirmation")},
                    }
                    for name, command in row["manifest"]["commands"].items()
                    if allowed is None or name in allowed
                ],
            }
            for row in PluginRegistry(self.database).list()
        ]

    def spec(self, task):
        return PluginRegistry(self.database).get(
            task["business_scope_id"], task["authorization_snapshot"]["plugin_digest"]
        )

    def read(self, spec, command, params, *, identity):
        spec.validate(command, params)
        if spec.commands[command].effect != "read":
            raise ValueError("Control plane may only query registered read capabilities")
        if self.read_callback is None:
            raise AccessError("service_unavailable", "Employee MCP connection is unavailable")
        data = self.read_callback(identity, command, params)
        validate_schema(spec.commands[command].result_schema, data)
        return data

    def current_assessment(self, task, snapshot, *, identity):
        self._employee(task, identity)
        spec = self.spec(task)
        evidence = {"directory": self.read(spec, "directory", {}, identity=identity)}
        params = snapshot["params"]
        if "ticket_id" in params:
            evidence["ticket"] = self.read(
                spec, "get", {"ticket_id": params["ticket_id"]}, identity=identity
            )["ticket"]
        return assess_action(
            spec,
            str(task["id"]),
            str(task["business_scope_id"]),
            snapshot["instruction"],
            snapshot["command"],
            params,
            evidence,
        )

    @staticmethod
    def _employee(task, identity):
        if (
            identity.source != "employee_session"
            or task["kind"] != "business"
            or task["employee_id"] != identity.employee_id
            or task["workspace_id"] != identity.workspace_id
        ):
            raise AccessError("not_found", "Employee task unavailable")

    @staticmethod
    def _credential(conn, identity, credential_ref):
        if identity.source != "employee_session":
            raise AccessError("unauthorized", "Employee browser session required")
        credential = conn.execute(
            "SELECT c.id FROM business.mcp_credentials c "
            "JOIN business.employee_accounts a ON a.id=c.employee_id "
            "AND a.workspace_id=c.workspace_id "
            "WHERE c.id=%s AND c.employee_id=%s AND c.workspace_id=%s "
            "AND c.account_revision=%s "
            "AND c.revoked_at IS NULL AND c.expires_at>now() AND a.active "
            "AND a.credential_revision=c.account_revision FOR SHARE OF c,a",
            (
                UUID(str(credential_ref)),
                identity.employee_id,
                identity.workspace_id,
                identity.account_revision,
            ),
        ).fetchone()
        if credential is None:
            raise AccessError("unauthorized", "Employee MCP credential unavailable")
        return credential["id"]

    def submit_employee(self, conn, body, identity, credential_ref):
        credential_ref = self._credential(conn, identity, credential_ref)
        scope = conn.execute(
            "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (UUID(body.scope_id),)
        ).fetchone()
        if not scope or not scope["enabled"]:
            raise AccessError("contract_changed", "Business plugin is missing or disabled")
        spec = PluginSpec.from_mapping(scope["manifest"])
        if (
            spec.connection is None
            or spec.digest != scope["manifest_digest"]
            or spec.version != scope["plugin_version"]
        ):
            raise AccessError("contract_changed", "Employee MCP contract integrity changed")
        task_id = uuid4()
        snapshot = {
            "id": str(uuid4()),
            "version": "2",
            "task_id": str(task_id),
            "kind": "business",
            "scope_id": str(scope["id"]),
            "plugin_id": spec.id,
            "plugin_version": spec.version,
            "plugin_digest": spec.digest,
            "instruction": body.instruction,
            "strategy": body.strategy,
            "active": True,
            "provenance": {
                "employee_id": str(identity.employee_id),
                "source": "employee_session",
                "authenticated": True,
            },
        }
        if body.workflow is not None:
            from decagent.business.workflow_registry import WorkflowRegistry

            if not self.conditional_workflows_enabled:
                raise ValueError("Conditional workflow entry is disabled")
            inputs = {**body.workflow.inputs, "instruction": body.instruction}
            resolution = WorkflowRegistry(self.database)._match(
                conn,
                identity,
                scope["id"],
                inputs,
                body.workflow.workflow_id,
                body.workflow.version,
                body.workflow.goal_id,
            )
            if not resolution.matched:
                missing = ", ".join(resolution.missing_fields)
                raise ValueError(
                    "Workflow requires correction: "
                    + resolution.reason
                    + ("; missing: " + missing if missing else "")
                )
            workflow = resolution.workflow
            report = conn.execute(
                "SELECT report_digest FROM app.workflow_validation_reports WHERE report_id=%s",
                (workflow["validation_report_id"],),
            ).fetchone()
            snapshot["workflow"] = {
                "workflow_id": workflow["workflow_id"],
                "version": workflow["version"],
                "definition_digest": workflow["definition_digest"],
                "report_digest": report["report_digest"],
                "inputs": inputs,
            }
        return conn.execute(
            "INSERT INTO app.tasks(id,kind,business_scope_id,workspace_id,authorization_snapshot,"
            "budget,graph_version,employee_id,credential_ref) "
            "VALUES(%s,'business',%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                task_id,
                scope["id"],
                identity.workspace_id,
                Jsonb(exact_safe_record(snapshot, self.database.redactor)),
                Jsonb(body.budget.model_dump()),
                "business-3" if self.conditional_workflows_enabled else "business-2",
                identity.employee_id,
                credential_ref,
            ),
        ).fetchone()

    def reauthenticate(self, conn, task, identity, credential_ref):
        self._employee(task, identity)
        if task["lease_owner"] is not None or task["state"] == "running":
            raise AccessError("task_changed", "Wait for the current task invocation to stop")
        credential_ref = self._credential(conn, identity, credential_ref)
        if task["graph_version"] not in {"business-2", "business-3"}:
            raise AccessError("upgrade_required", "Historical CLI task cannot resume on MCP")
        return conn.execute(
            "UPDATE app.tasks SET credential_ref=%s WHERE id=%s RETURNING *",
            (credential_ref, task["id"]),
        ).fetchone()

    def reply_employee(self, conn, task, approval, body, identity, operation_id):
        self._employee(task, identity)
        if body.expected_digest != approval["digest"]:
            raise AccessError("stale_approval", "Reply does not match the current question")
        fingerprint = digest(
            {
                "approval_id": str(approval["id"]),
                "employee_id": str(identity.employee_id),
                "reply": body.model_dump(mode="json"),
            }
        )
        prior = (approval["reply_payload"] or {}).get("policy_record", {})
        if approval["status"] != "pending":
            if prior.get("response_fingerprint") != fingerprint:
                raise AccessError("approval_conflict", "Question already has a different reply")
            return {"approval_id": str(approval["id"]), "policy_record": prior}
        if (
            task["state"] not in {"awaiting_input", "awaiting_approval"}
            or task["lease_owner"] is not None
            or task["graph_version"] not in {"business-2", "business-3"}
        ):
            raise AccessError("task_changed", "Task is not waiting for this employee response")
        status, action_digest, service_id, input_hash = "clarified", None, None, None
        if approval["kind"] == "action":
            action = conn.execute(
                "SELECT * FROM app.actions WHERE id=%s AND task_id=%s FOR UPDATE",
                (approval["action_id"], task["id"]),
            ).fetchone()
            if (
                not action
                or action["digest"] != approval["digest"]
                or not action["server_operation_id"]
            ):
                raise AccessError("approval_invalid", "Service operation is unavailable")
            row = conn.execute(
                "SELECT * FROM business.operations WHERE id=%s FOR UPDATE",
                (action["server_operation_id"],),
            ).fetchone()
            if not row or any(
                (
                    row["employee_id"] != identity.employee_id,
                    row["agent_task_id"] != task["id"],
                    row["agent_action_id"] != action["id"],
                    row["source"] != "decagent",
                )
            ):
                raise AccessError("approval_invalid", "Service operation belongs to another task")
            service_id, input_hash, action_digest = row["id"], row["input_hash"], action["digest"]
            if body.decision == "edit":
                if body.edited_params is None or body.edited_params == row["params"]:
                    raise ValueError("Edit requires complete changed parameters")
                self.spec(task).validate(row["command"], body.edited_params)
                self.operations.reply_in_transaction(
                    conn, identity, service_id, input_hash, "reject"
                )
                conn.execute("UPDATE app.actions SET status='denied' WHERE id=%s", (action["id"],))
                status = "edited"
            else:
                if body.edited_params is not None or body.confirmation is not None:
                    raise ValueError("Approval cannot silently replace operation input")
                result = self.operations.reply_in_transaction(
                    conn,
                    identity,
                    service_id,
                    input_hash,
                    "reject" if body.decision == "reject" else "approve",
                )
                status = (
                    "rejected"
                    if body.decision == "reject"
                    else "approved"
                    if result["state"] in {"ready", "succeeded"}
                    else "stale"
                )
        elif approval["kind"] == "clarification":
            if body.decision == "reject":
                status = "rejected"
            elif (
                body.decision != "confirm"
                or body.edited_params is not None
                or not body.confirmation
                or not body.confirmation.strip()
            ):
                raise ValueError("Clarification requires a concrete employee answer")
        else:
            raise AccessError("approval_invalid", "Unsupported employee response kind")
        record = exact_safe_record(
            {
                "business": True,
                "approval_id": str(approval["id"]),
                "employee_id": str(identity.employee_id),
                "source": "employee_session",
                "response_id": operation_id,
                "response_fingerprint": fingerprint,
                "status": status,
                "action_digest": action_digest,
                "operation_id": service_id,
                "input_hash": input_hash,
                "edited_params": body.edited_params,
                "confirmation": body.confirmation,
            },
            self.database.redactor,
        )
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply=%s,reply_payload=%s,"
            "version=version+1,replied_at=now() WHERE id=%s",
            (
                body.decision,
                Jsonb({**(approval["reply_payload"] or {}), "policy_record": record}),
                approval["id"],
            ),
        )
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (task["id"],))
        return {"approval_id": str(approval["id"]), "policy_record": record}

    @staticmethod
    def submit(*_args, **_kwargs):
        raise AccessError("employee_auth_required", "Use the employee v2 task API")

    @staticmethod
    def reply(*_args, **_kwargs):
        raise AccessError("employee_auth_required", "Use the employee v2 reply API")

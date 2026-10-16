"""Bridge plan nodes to the existing employee-bound MCP governance and journal."""

from psycopg.types.json import Jsonb

from decagent.business.actions import assess_action
from decagent.business.execution import BusinessReadError
from decagent.business.mcp_client import MCPAuthenticationError, MCPTransportError

from .repository import PlanConflict
from .runtime import NodeFailure, NodePending, NodeResult


class BusinessNodeHandlers:
    def __init__(
        self,
        repository,
        services,
        *,
        after_action=None,
        decide=None,
        ask_user=None,
        before_dispatch=None,
    ):
        self.repository, self.services = repository, services
        self.after_action, self.decide, self.ask_user = after_action, decide, ask_user
        self.before_dispatch = before_dispatch

    def _boundary(self, node, context, saved):
        if self.before_dispatch:
            self.before_dispatch(node, context, saved)

    def _record(self, node, inputs, context, data, *, action_id=None):
        value = {
            "kind": node["type"],
            "verified": True,
            "data": data,
            "inputs": inputs,
            "employee_id": context.employee_id,
            "scope_revision": context.scope_revision,
            "account_revision": context.account_revision,
        }
        if action_id is not None:
            value["action_id"] = str(action_id)
        return NodeResult(value)

    def execute(self, node, inputs, context, saved):
        self.repository.guard()
        if node["type"] == "read_tool":
            return self._read(node, inputs, context, saved)
        if node["type"] == "governed_write":
            return self._write(node, inputs, context, saved)
        callback = {"decide": self.decide, "ask_user": self.ask_user}.get(node["type"])
        if callback is None:
            raise NodeFailure("No registered handler for this node type")
        self._boundary(node, context, saved)
        return callback(node, inputs, context, saved)

    def _read(self, node, inputs, context, saved):
        # A read may be repeated once after an interrupted transport. A write may
        # only consult its original durable action; these paths are deliberately separate.
        self._boundary(node, context, saved)
        if saved["dispatch_attempts"] >= 2:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "reason": "Bounded read attempts exhausted",
                }
            )
        if saved["status"] == "waiting":
            self.repository.resume_read(
                node["id"], saved["input_digest"], generation=saved["execution_generation"]
            )
        self.repository.dispatch(
            node["id"], max_attempts=2, generation=saved["execution_generation"]
        )
        self._boundary(node, context, saved)
        try:
            data = self.services.executor.read(node["tool"]["command"], inputs)
        except BusinessReadError as exc:
            raise NodeFailure(str(exc)) from exc
        except MCPAuthenticationError:
            raise
        except MCPTransportError as exc:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "reason": "Read transport failed; explicit recovery required",
                }
            ) from exc
        return self._record(node, inputs, context, data)

    def _action(self, node, inputs, context, saved):
        key = f"plan:{node['id']}:{saved['execution_generation']}"
        with self.repository.database.connect() as conn:
            self.repository.guard(conn)
            action = conn.execute(
                "SELECT * FROM app.actions WHERE task_id=%s AND logical_key=%s",
                (self.repository.task_id, key),
            ).fetchone()
        command = node["command"]["command"]
        if action is None:
            instruction = self.repository.instruction()
            if self.repository.current().revision > 1:
                with self.repository.database.connect() as conn:
                    self.repository.guard(conn)
                    duplicate = conn.execute(
                        "SELECT id FROM app.actions WHERE task_id=%s "
                        "AND status IN ('succeeded','running','unknown') "
                        "AND snapshot->>'command'=%s AND snapshot->'params'=%s LIMIT 1",
                        (self.repository.task_id, command, Jsonb(inputs)),
                    ).fetchone()
                if duplicate:
                    raise NodeFailure("Local revision would repeat an existing write operation")
            evidence = self.services._evidence(
                {"instruction": instruction},
                {"command": command, "params": inputs},
            )
            assessment = assess_action(
                self.repository.spec,
                str(self.repository.task_id),
                str(self.services.task["business_scope_id"]),
                instruction,
                command,
                inputs,
                evidence,
            )
            if assessment["outcome"] not in {"allow", "confirm"}:
                raise NodeFailure(assessment["reason"])
            if saved["execution_generation"] > 1:
                assessment = {
                    **assessment,
                    "outcome": "confirm",
                    "reason": "Corrected draft requires a new concrete confirmation",
                }
            action = self.services.executor.plan(key, assessment)
            if self.after_action:
                self.after_action("action_planned", action)
        if action["snapshot"]["command"] != command or action["snapshot"]["params"] != inputs:
            raise PlanConflict("Stable plan action has different command or inputs")
        self.repository.bind_action(
            node["id"], action["id"], generation=saved["execution_generation"]
        )
        return action

    def _write(self, node, inputs, context, saved):
        self._boundary(node, context, saved)
        action = self._action(node, inputs, context, saved)
        if saved["dispatch_attempts"] == 0:
            self.repository.dispatch(node["id"], generation=saved["execution_generation"])
        self._boundary(node, context, saved)
        kwargs = (
            {"before_dispatch": lambda: self._boundary(node, context, saved)}
            if self.before_dispatch
            else {}
        )
        result = self.services.executor.execute(action["id"], action["digest"], **kwargs)
        if self.after_action:
            self.after_action("action_finished", action)
        if result["status"] == "awaiting_approval":
            snapshot = action["snapshot"]
            assessment = {**snapshot["assessment"], "snapshot": snapshot}
            request = self.services.human_request(
                {
                    "instruction": context.task_input["instruction"],
                    "human_kind": "action",
                    "action_id": str(action["id"]),
                    "assessment": assessment,
                    "operation_id": result["operation_id"],
                    "operation": result["operation"],
                    "question": "请核对服务端保存的具体操作并确认。",
                }
            )
            raise NodePending({**request, "node_id": node["id"], "action_id": str(action["id"])})
        if result["status"] == "unknown":
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "action_id": str(action["id"]),
                    "operation_id": result.get("operation_id"),
                    "reason": result.get(
                        "reason", "Original operation outcome requires verification"
                    ),
                },
                unknown=True,
            )
        if result["status"] != "succeeded":
            raise NodeFailure(result.get("reason", "Business action was not completed"))
        return self._record(node, inputs, context, result["data"], action_id=action["id"])

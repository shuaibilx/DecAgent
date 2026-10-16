"""Verify authenticated draft edits before allowing one immutable node successor."""

import json
from copy import deepcopy

from decagent.business.actions import literal_request, matches_literal
from decagent.decisions.contracts import digest


def correction_fact(repository, conn, task, prior, approval_id):
    row = conn.execute(
        "SELECT p.*,a.status AS action_status,a.server_operation_id,a.snapshot,"
        "a.digest AS action_digest,o.state AS operation_state,o.input_hash,"
        "o.employee_id AS operation_employee,o.agent_task_id,o.agent_action_id,o.source "
        "FROM app.approvals p JOIN app.actions a ON a.id=p.action_id AND a.task_id=p.task_id "
        "JOIN business.operations o ON o.id=a.server_operation_id "
        "WHERE p.id=%s AND p.task_id=%s FOR UPDATE OF p,a,o",
        (approval_id, repository.task_id),
    ).fetchone()
    record = (row["reply_payload"] or {}).get("policy_record", {}) if row else {}
    if not row or any(
        (
            row["kind"] != "action",
            row["status"] not in {"replied", "consumed"},
            row["reply"] != "edit",
            record.get("business") is not True,
            record.get("source") != "employee_session",
            record.get("status") != "edited",
            record.get("employee_id") != str(task["employee_id"]),
            row["operation_employee"] != task["employee_id"],
            row["agent_task_id"] != task["id"],
            row["agent_action_id"] != row["action_id"],
            row["source"] != "decagent",
        )
    ):
        raise ValueError("Original employee correction provenance is invalid")
    if any(
        (
            row["operation_state"] != "rejected",
            row["action_status"] != "denied",
            str(record.get("operation_id")) != row["server_operation_id"],
            record.get("input_hash") != row["input_hash"],
            record.get("action_digest") != row["action_digest"],
            row["digest"] != row["action_digest"],
        )
    ):
        raise ValueError("Old prepared operation must be rejected before parameter correction")
    saved = conn.execute(
        "SELECT * FROM app.business_plan_nodes WHERE task_id=%s AND action_id=%s FOR UPDATE",
        (repository.task_id, row["action_id"]),
    ).fetchone()
    node = next((n for n in prior.nodes if saved and n["id"] == saved["node_id"]), None)
    if not node or node["type"] != "governed_write" or saved["status"] != "waiting":
        raise ValueError("Correction requires the original waiting prepared node")
    if saved["node_digest"] != digest(node):
        raise ValueError("Prepared node revision changed")
    command = node["command"]["command"]
    params = record.get("edited_params")
    repository.spec.validate(command, params)
    if row["snapshot"]["command"] != command or params == row["snapshot"]["params"]:
        raise ValueError("Correction must contain changed parameters for the original command")
    updated = deepcopy(node)
    types = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
        list: "array",
        type(None): "null",
    }
    updated["inputs"] = {
        key: {
            "source": "literal",
            "value": val,
            "value_type": types[type(val)],
            "nullable": val is None,
            "provenance": "plan_literal",
        }
        for key, val in params.items()
    }
    return {
        "approval_id": str(approval_id),
        "node_id": node["id"],
        "generation": saved["execution_generation"],
        "old_digest": saved["node_digest"],
        "new_digest": digest(updated),
        "old_action_id": str(saved["action_id"]),
        "old_operation_id": row["server_operation_id"],
        "command": command,
        "old_params": row["snapshot"]["params"],
        "params": params,
        "employee_id": str(task["employee_id"]),
        "source": "employee_session",
        "node": updated,
    }


def effective_instruction(original, facts):
    """Fold authenticated edits into literal goals without replacing original history."""
    request = literal_request(original)
    if request is None:
        return original
    for fact in facts:
        if matches_literal(request, fact["command"], fact["old_params"]):
            request = {"command": fact["command"], "params": fact["params"]}
    return json.dumps(request, ensure_ascii=False)

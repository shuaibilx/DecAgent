"""Determine business completion from actual results, never model completion prose."""

from decagent.business.actions import explicit_command, literal_request, matches_literal
from decagent.business.contracts import validate_schema
from decagent.decisions.contracts import digest

from .bindings import evaluate_condition, resolve_binding
from .workflow_goals import satisfied_without_write


def _failure(reason):
    return {"valid": False, "reason": reason, "summary": "业务目标尚未核实", "model_requests": 0}


def _observations(compiled, context, spec):
    observations = []
    for node_id, record in context.node_outputs.items():
        node = compiled.node(node_id)
        if node["type"] not in {"read_tool", "governed_write"}:
            continue
        if (
            record.get("verified") is not True
            or record.get("employee_id") != context.employee_id
            or record.get("scope_revision") != context.scope_revision
            or (
                context.account_revision is not None
                and record.get("account_revision") != context.account_revision
            )
            or record.get("stale", False) is not False
        ):
            continue
        command = node["tool" if node["type"] == "read_tool" else "command"]["command"]
        spec.validate(command, record["inputs"])
        validate_schema(spec.commands[command].result_schema, record["data"])
        observations.append(
            {"node_id": node_id, "command": command, "params": record["inputs"], **record}
        )
    return observations


def _writes_verified(observations, actions, spec, context):
    rows = {str(row["id"]): row for row in actions}
    claimed = set()
    for observation in observations:
        if spec.commands[observation["command"]].effect != "write":
            continue
        action_id = str(observation.get("action_id", ""))
        row = rows.get(action_id)
        receipt = (row or {}).get("receipt") or {}
        verification = receipt.get("verification", {})
        if (
            not row
            or row["status"] != "succeeded"
            or row["snapshot"]["command"] != observation["command"]
            or row["snapshot"]["params"] != observation["params"]
            or receipt.get("status") != "succeeded"
            or verification.get("server_verified") is not True
            or verification.get("input_bound") is not True
            or digest(receipt.get("data")) != digest(observation["data"])
        ):
            return False
        business_receipt = observation["data"].get("receipt", {})
        if business_receipt.get("employee_id") != context.employee_id:
            return False
        claimed.add(action_id)
    executed = {
        str(row["id"])
        for row in actions
        if row["status"] == "succeeded"
        and spec.commands[row["snapshot"]["command"]].effect == "write"
    }
    return executed <= claimed


def verify_completion(compiled, complete_node, context, *, instruction, spec, actions=()):
    """Caller supplies task-scoped durable action rows and trusted working state."""
    node = compiled.node(complete_node)
    if node["type"] != "complete":
        return _failure("not_completion_node")
    for row in actions:
        command = row["snapshot"].get("command")
        if command not in spec.commands:
            return _failure("unknown_executed_capability")
        if spec.commands[command].effect == "write" and row["status"] in {"unknown", "running"}:
            return _failure("unresolved_write")
    try:
        observations = _observations(compiled, context, spec)
        if not _writes_verified(observations, actions, spec, context):
            return _failure("write_evidence_missing")
        evidence = {item["id"]: item for item in compiled.plan.goal["evidence"]}
        consumed = []
        actual = False
        for evidence_id in node["evidence"]:
            item = evidence[evidence_id]
            if "condition" in item and not evaluate_condition(item["condition"], context):
                continue
            resolved = resolve_binding(item["source"], context)
            if resolved.value is None:
                return _failure("evidence_missing")
            consumed.append(evidence_id)
            actual = actual or item["kind"] in {"tool_result", "write_receipt"}
        if not consumed or not actual:
            return _failure("evidence_missing")
    except (KeyError, TypeError, ValueError):
        return _failure("evidence_invalid")
    literal = literal_request(instruction)
    required = (
        {literal["command"]}
        if literal and literal["command"] in spec.commands
        else {name for name in spec.commands if explicit_command(instruction, name)}
    )
    commands = {observation["command"] for observation in observations}
    already_satisfied = satisfied_without_write(compiled, complete_node, context, instruction)
    if not required or not required <= commands | already_satisfied:
        return _failure("goal_unproven")
    if literal and not any(
        matches_literal(literal, observation["command"], observation["params"])
        for observation in observations
    ):
        return _failure("goal_unproven")
    if {"create", "assign"} <= required:
        created = {
            observation["data"].get("ticket", {}).get("id")
            for observation in observations
            if observation["command"] == "create"
        }
        assigned = {
            observation["params"].get("ticket_id")
            for observation in observations
            if observation["command"] == "assign"
        }
        if not created & assigned:
            return _failure("goal_unproven")
    return {
        "valid": True,
        "summary": f"已核实 {len(observations)} 项业务结果",
        "evidence": consumed,
        "nodes": [item["node_id"] for item in observations],
        "actions": [item["action_id"] for item in observations if item.get("action_id")],
        "model_requests": 0,
        "satisfied_without_write": sorted((required - commands) & already_satisfied),
    }

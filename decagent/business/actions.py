"""Deterministic action governance shared by the fast path and the primary planner."""

import re

from decagent.business.codec import strict_json
from decagent.business.questions import POLICY_VERSION, legal_commands
from decagent.decisions.contracts import digest

ALIASES = {
    "create": ("创建", "新建", "建一个工单", "建一条工单"),
    "assign": ("分配", "分派", "指派", "assign"),
    "start": ("开始处理", "开始工单", "启动处理"),
    "comment": ("添加评论", "补充评论", "追加评论", "留言"),
    "resolve": ("解决工单", "标记已解决", "resolve"),
    "close": ("关闭", "close"),
    "reopen": ("重开", "重新打开", "reopen"),
    "set_priority": ("修改优先级", "调整优先级", "更改优先级"),
    "list": ("列出", "查询工单", "查看工单列表", "list"),
    "get": ("查看详情", "查询详情", "get"),
    "directory": ("查看目录", "查询人员", "查询分类", "directory"),
}


def literal_request(instruction):
    try:
        value = strict_json(instruction)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and set(value) == {"command", "params"} else None


def matches_literal(request, command, params):
    def user_fields(value):
        return {key: item for key, item in value.items() if key != "expected_revision"}

    return (
        request["command"] == command
        and isinstance(request["params"], dict)
        and user_fields(request["params"]) == user_fields(params)
    )


def explicit_command(instruction, command):
    """Conservative literal requests; uncertain write intent requires confirmation."""
    value = literal_request(instruction)
    if isinstance(value, dict) and value.get("command") == command:
        return True
    aliases = (*ALIASES.get(command, ()), command)
    for segment in re.split(r"[。！？!?;；\n]", instruction.casefold()):
        if re.search(r"不要|不能|禁止|别|do not|don't|never", segment):
            continue
        # Quoted/reference text does not grant automatic authority.
        if any(mark in segment for mark in ('"', "“", "”", "‘", "’", "`")):
            continue
        for word in aliases:
            offset = segment.find(word.casefold())
            if offset < 0:
                continue
            prefix = segment[:offset].strip(" ,，:：")
            if not prefix or re.fullmatch(
                r"(?:请|帮我|为我|给我|麻烦|please|can you|把)[\w\s，,]{0,80}", prefix
            ):
                return True
    return False


def assess_action(spec, task_id, scope_id, instruction, command, params, evidence):
    ticket = evidence.get("ticket")
    resource = (
        {key: ticket.get(key) for key in ("id", "revision", "status", "assignee_id")}
        if ticket
        else None
    )
    snapshot = {
        "task_id": str(task_id),
        "scope_id": str(scope_id),
        "plugin_digest": spec.digest,
        "plugin_version": spec.version,
        "policy_version": POLICY_VERSION,
        "command": command,
        "params": params,
        "resource": resource,
        "instruction_digest": digest(instruction),
    }
    snapshot["instruction"] = instruction

    def result(outcome, reason):
        return {
            "outcome": outcome,
            "reason": reason,
            "digest": digest(snapshot),
            "snapshot": snapshot,
        }

    try:
        spec.validate(command, params)
    except (KeyError, ValueError) as error:
        return result("deny", "Plugin parameter contract rejected the action: " + str(error))
    definition = spec.commands[command]
    literal = literal_request(instruction)
    if literal and (definition.effect == "write" or command == literal["command"]):
        if not matches_literal(literal, command, params):
            return result("deny", "Literal operation parameters differ from the owner request")
    if "ticket_id" in params:
        if not ticket or str(ticket["id"]) != params["ticket_id"]:
            return result("clarify", "Select the exact existing ticket before continuing")
        if "expected_revision" in params and params["expected_revision"] != ticket["revision"]:
            return result("stale", "Resource revision changed; refresh and reassess the action")
    if command not in legal_commands(spec, ticket):
        return result("deny", "Operation is forbidden in the current business state")
    directory = evidence.get("directory", {})
    for parameter, rows in (("person_id", "people"), ("category_id", "categories")):
        if parameter not in params:
            continue
        actual = [
            row
            for row in directory.get(rows, [])
            if str(row["id"]) == params[parameter] and row.get("active", True) is True
        ]
        if len(actual) != 1:
            return result("deny", "Entity ID does not identify one active queried candidate")
        selected = actual[0]
        if parameter == "person_id":
            namesakes = [
                row
                for row in directory.get(rows, [])
                if row["name"] == selected["name"] and row.get("active", True) is True
            ]
            if len(namesakes) > 1:
                matches = [
                    row
                    for row in namesakes
                    if row.get("team") and row["team"].casefold() in instruction.casefold()
                ]
                explicit_id = params[parameter] in instruction
                if not explicit_id and (len(matches) != 1 or matches[0]["id"] != selected["id"]):
                    return result("clarify", "Several people share this name; specify team or ID")
    if definition.effect == "read":
        return result("allow", "Validated read within the registered business scope")
    if definition.confirmation == "confirm":
        return result("confirm", "This business write requires a concrete owner confirmation")
    if definition.confirmation == "initial_assignment" and ticket and ticket.get("assignee_id"):
        return result("confirm", "Changing an existing assignee requires confirmation")
    if not explicit_command(instruction, command):
        return result(
            "confirm", "Write intent is not an explicit literal request; confirm the draft"
        )
    return result("allow", "Explicit requested write satisfies the current plugin contract")

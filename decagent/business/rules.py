"""Conservative natural-language baseline over the same observed business facts."""

import re

from decagent.business.actions import explicit_command

REQUESTER_MARKERS = ("请求人", "请求者", "提单人", "requester")
CREATE_PARAMETER_MARKERS = (
    *REQUESTER_MARKERS,
    "优先级",
    "priority",
    "标题",
    "title",
    "描述为",
    "描述是",
    "description",
)


def _person_target(instruction):
    # An authenticated clarification can replace an earlier ambiguous target.
    text = instruction.rsplit("\n用户补充：", 1)[-1]
    targets = re.findall(
        r"(?:分配给|分派给|指派给|assign\s+(?:to\s+)?)([^,，。;；\n]+)",
        text,
        re.IGNORECASE,
    )
    return targets[0].strip() if len(targets) == 1 else None


def _person(rows, instruction):
    target = _person_target(instruction)
    if target is None:
        return None
    matches = []
    for row in rows:
        if row.get("active", True) is not True:
            continue
        forms = {str(row["id"]).casefold(), row["name"].casefold()}
        if row.get("team"):
            forms.add((row["team"] + " 的" + row["name"]).casefold())
            forms.add((row["team"] + " " + row["name"]).casefold())
        if target.casefold() in forms:
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def _category(rows, instruction):
    match = re.match(
        r"\s*(?:请|帮我|为我|麻烦)?(?:创建|新建)\s*(?:一个\s*)?"
        r"([^,，。;；\n]+?)\s*工单(?:[,，。;；\n]|$)",
        instruction,
    )
    if match is None:
        return None
    target = match[1].strip().casefold()
    matches = [row for row in rows if target in {str(row["id"]).casefold(), row["name"].casefold()}]
    return matches[0] if len(matches) == 1 else None


def simple_read_params(command, instruction):
    text = instruction.strip().rstrip("。.!！?？").strip()
    prefix = r"(?:请|帮我|为我|麻烦|please\s+)?"
    if command == "list" and re.fullmatch(
        prefix + r"(?:查看工单列表|列出工单|查询工单|list(?:\s+tickets)?)", text, re.IGNORECASE
    ):
        return {}
    if command == "directory":
        for phrase, params in (
            (r"查看目录|directory", {}),
            (r"查询人员|查看人员目录", {"kind": "people"}),
            (r"查询分类|查看分类目录", {"kind": "categories"}),
        ):
            if re.fullmatch(prefix + "(?:" + phrase + ")", text, re.IGNORECASE):
                return params
    return None


def proposal(spec, instruction, evidence):
    commands = [name for name in spec.commands if explicit_command(instruction, name)]
    if len(commands) != 1:
        return None
    command = commands[0]
    directory, ticket = evidence.get("directory", {}), evidence.get("ticket")
    params = {}
    if command in {"list", "directory"}:
        params = simple_read_params(command, instruction)
        if params is None:
            return None
    elif command in {"get", "start", "close", "reopen"} and ticket:
        params = {"ticket_id": ticket["id"]}
        if command != "get":
            params["expected_revision"] = ticket["revision"]
    elif command == "assign" and ticket:
        person = _person(directory.get("people", []), instruction)
        if not person:
            return None
        params = {
            "ticket_id": ticket["id"],
            "expected_revision": ticket["revision"],
            "person_id": person["id"],
        }
    elif command == "create":
        category = _category(directory.get("categories", []), instruction)
        if not category or any(word in instruction.casefold() for word in CREATE_PARAMETER_MARKERS):
            return None
        text = instruction.casefold()
        if re.search(r"没有|并非|不是|之前|以前|原来|\bnot\b|\bprevious\b", text):
            return None
        priority = "normal"
        if any(word in text for word in ("未受影响", "工作不受影响", "work unaffected")):
            pass
        elif any(word in text for word in ("绕过", "临时办法", "workaround")):
            priority = "high"
        elif any(
            word in text for word in ("被阻断", "工作中断", "不能工作", "work blocked", "urgent")
        ):
            priority = "urgent"
        maximum = spec.commands["create"].input_schema["properties"]["title"]["maxLength"]
        params = {
            "title": instruction[:maximum],
            "description": instruction,
            "category_id": category["id"],
            "priority": priority,
        }
    else:
        return None
    return {"command": command, "params": params}

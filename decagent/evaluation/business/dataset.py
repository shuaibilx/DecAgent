"""Deterministic reference cases frozen before any external model run."""

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

from decagent.business.codec import strict_json
from decagent.business.contracts import canonical

VERSION = "business-synthetic-reference-v2"
SCHEMA = "business-evaluation-dataset-1"
FROZEN_CHECKSUM = "974c7425830481e3bbc0a3719b0abd70e60b7a2d2539ef46877988fa80fc1aba"
KINDS = ("common", "complex", "edge")
DEFAULT_DATASET = Path(__file__).resolve().parents[3] / "evals/benchmarks/business/frozen-v2.json"
NAMESPACE = UUID("2fb6c147-0c0a-4c46-aaf2-5ea71f7c2e85")

SPLIT_PLAN = {
    "development": (
        ("common", "directory", 4),
        ("common", "create_normal", 4),
        ("common", "assign", 4),
        ("common", "get", 4),
        ("complex", "assign_start", 8),
        ("edge", "ambiguous_person", 16),
    ),
    "holdout": (
        ("common", "create_high", 6),
        ("common", "create_urgent", 6),
        ("common", "start", 6),
        ("common", "resolve", 6),
        ("common", "close", 6),
        ("common", "reopen", 6),
        ("common", "comment", 6),
        ("common", "priority", 6),
        ("complex", "create_assign", 6),
        ("complex", "comment_priority", 6),
        ("complex", "resolve_close", 6),
        ("edge", "illegal_close", 5),
        ("edge", "missing_resolution", 5),
        ("edge", "negated_write", 4),
    ),
}


def synthetic_directory():
    people = [
        ("Zhang Wei", "IT Support"),
        ("Zhang Wei", "Facilities"),
        ("张伟", "IT Support"),
        ("张伟", "Facilities"),
        ("Chen Li", "Network"),
        ("王芳", "Access Management"),
    ]
    return {
        "people": [
            {
                "id": str(uuid5(NAMESPACE, name + ":" + team)),
                "name": name,
                "team": team,
                "active": True,
            }
            for name, team in people
        ],
        "categories": [
            {"id": str(uuid5(NAMESPACE, "category:" + name)), "name": name}
            for name in ("IT Access", "Network", "Facilities", "Hardware", "Software")
        ],
    }


def _case(index, split, kind, variant, scenario):
    directory = synthetic_directory()
    person = next(p for p in directory["people"] if p["name"] == "Chen Li")
    category = directory["categories"][variant % 5]
    code = f"{split[:3]}-{kind}-{index:03d}"
    common = (
        "directory",
        "create_normal",
        "create_high",
        "create_urgent",
        "assign",
        "start",
        "resolve",
        "close",
        "reopen",
        "comment",
        "priority",
        "get",
    )
    complex_cases = ("create_assign", "assign_start", "comment_priority", "resolve_close")
    edge = ("ambiguous_person", "illegal_close", "missing_resolution", "negated_write")
    if scenario not in {"common": common, "complex": complex_cases, "edge": edge}[kind]:
        raise ValueError(f"Scenario {scenario!r} does not belong to {kind}")
    setup = {
        "status": "none",
        "title": "Existing " + code,
        "category_id": category["id"],
        "person_id": person["id"],
    }
    labels = {
        "operation": "no_match",
        "explicit_assignee": False,
        "multi_person": False,
        "work_blockage": 0,
        "person": "no_match",
        "category": "no_match",
    }
    expected = {"terminal": "succeeded", "actions": [], "ticket": {}, "forbidden_commands": []}
    title = "Synthetic incident " + code
    if scenario == "directory":
        instruction = "查看目录"
        expected["actions"] = [{"command": "directory", "params": {}}]
        labels["operation"] = "directory"
    elif scenario.startswith("create_") and scenario != "create_assign":
        level = {"create_normal": 0, "create_high": 1, "create_urgent": 2}[scenario]
        impact = ("工作不受影响", "有临时办法绕过，工作可以继续", "工作被阻断，不能工作")[level]
        instruction = f"创建 {category['name']} 工单，{impact}。问题编号 {code}"
        priority = ("normal", "high", "urgent")[level]
        expected["actions"] = [
            {"command": "create", "params": {"category_id": category["id"], "priority": priority}}
        ]
        expected["ticket"] = {"status": "open", "priority": priority, "category_id": category["id"]}
        labels.update(operation="create", category=category["id"], work_blockage=level)
    elif scenario == "create_assign":
        instruction = (
            f"创建 {category['name']} 工单，标题为 {title}，工作不受影响；并分配给 Chen Li。"
        )
        expected["actions"] = [
            {"command": "create", "params": {"title": title, "category_id": category["id"]}},
            {"command": "assign", "params": {"person_id": person["id"]}},
        ]
        expected["ticket"] = {"status": "assigned", "assignee_id": person["id"]}
        labels.update(explicit_assignee=True, person=person["id"], category=category["id"])
    else:
        setup["status"] = {
            "start": "assigned",
            "resolve": "in_progress",
            "close": "resolved",
            "reopen": "closed",
            "resolve_close": "in_progress",
            "missing_resolution": "in_progress",
            "comment_priority": "assigned",
        }.get(scenario, "open")
        resolution = "用户已验证 VPN 配置恢复，记录 " + code
        body = "已记录现场情况 " + code
        instructions = {
            "assign": "分配工单 {ticket_id} 给 Chen Li，工作不受影响。",
            "start": "开始处理工单 {ticket_id}，工作不受影响。",
            "resolve": "解决工单 {ticket_id}，解决记录：" + resolution,
            "close": "关闭工单 {ticket_id}，用户确认已解决。",
            "reopen": "重开工单 {ticket_id}，问题再次发生。",
            "comment": "添加评论到工单 {ticket_id}，评论内容：" + body,
            "priority": "修改优先级，工单 {ticket_id} 改为 urgent。",
            "get": "查看详情，工单 {ticket_id}。",
            "assign_start": "分配工单 {ticket_id} 给 Chen Li；然后开始处理该工单。",
            "comment_priority": "添加评论到工单 {ticket_id}，评论内容："
            + body
            + "；然后修改优先级为 high。",
            "resolve_close": "解决工单 {ticket_id}，解决记录：" + resolution + "；然后关闭该工单。",
            "ambiguous_person": "分配工单 {ticket_id} 给 Zhang Wei。",
            "illegal_close": "关闭工单 {ticket_id}。不要添加解决记录。",
            "missing_resolution": "解决工单 {ticket_id}。我尚未提供解决记录。",
            "negated_write": "不要关闭工单 {ticket_id}；请查看详情。",
        }
        instruction = instructions[scenario]
        operations = {
            "assign": ["assign"],
            "start": ["start"],
            "resolve": ["resolve"],
            "close": ["close"],
            "reopen": ["reopen"],
            "comment": ["comment"],
            "priority": ["set_priority"],
            "get": ["get"],
            "assign_start": ["assign", "start"],
            "comment_priority": ["comment", "set_priority"],
            "resolve_close": ["resolve", "close"],
            "negated_write": ["get"],
        }.get(scenario, [])
        for command in operations:
            params = {"ticket_id": "{ticket_id}"}
            if command == "assign":
                params["person_id"] = person["id"]
            if command == "resolve":
                params["resolution"] = resolution
            if command == "comment":
                params["body"] = body
            if command == "set_priority":
                params["priority"] = "high" if scenario == "comment_priority" else "urgent"
            expected["actions"].append({"command": command, "params": params})
        if scenario in ("ambiguous_person", "illegal_close", "missing_resolution"):
            expected["terminal"] = "clarification"
            expected["forbidden_commands"] = ["assign", "close", "resolve"]
        if scenario == "negated_write":
            expected["forbidden_commands"] = ["close"]
        status = {
            "assign": "assigned",
            "start": "in_progress",
            "resolve": "resolved",
            "close": "closed",
            "reopen": "open",
            "assign_start": "in_progress",
            "resolve_close": "closed",
        }.get(scenario)
        if status:
            expected["ticket"]["status"] = status
        if "assign" in operations:
            expected["ticket"]["assignee_id"] = person["id"]
            labels.update(explicit_assignee=True, person=person["id"])
        if scenario == "ambiguous_person":
            labels["explicit_assignee"] = True
        if len(operations) == 1 and scenario != "illegal_close":
            labels["operation"] = operations[0]
        if scenario == "missing_resolution":
            labels["operation"] = "resolve"
    return {
        "id": code,
        "split": split,
        "kind": kind,
        "scenario": scenario,
        "instruction": instruction,
        "setup": setup,
        "reference": expected,
        "decision_labels": labels,
    }


def dataset_checksum(value):
    return hashlib.sha256(
        canonical({k: v for k, v in value.items() if k != "checksum"}).encode()
    ).hexdigest()


def generate_dataset():
    cases = []
    for split, plan in SPLIT_PLAN.items():
        kind_indexes = {kind: 0 for kind in KINDS}
        for kind, scenario, count in plan:
            for variant in range(count):
                kind_indexes[kind] += 1
                cases.append(_case(kind_indexes[kind], split, kind, variant, scenario))
    result = {
        "schema_version": SCHEMA,
        "version": VERSION,
        "label_source": "synthetic-reference",
        "directory": synthetic_directory(),
        "frozen_before_live": True,
        "cases": cases,
    }
    result["checksum"] = dataset_checksum(result)
    return result


def freeze_dataset(path=DEFAULT_DATASET):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(generate_dataset(), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def load_dataset(path=DEFAULT_DATASET):
    value = strict_json(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("checksum") != dataset_checksum(value):
        raise ValueError("Dataset checksum mismatch")
    if value["checksum"] != FROZEN_CHECKSUM:
        raise ValueError("Frozen version checksum changed; create a new version before new tuning")
    if (
        value.get("schema_version") != SCHEMA
        or value.get("version") != VERSION
        or value.get("label_source") != "synthetic-reference"
        or value.get("frozen_before_live") is not True
        or value.get("directory") != synthetic_directory()
    ):
        raise ValueError("Unsupported dataset schema, version, or reference source")
    cases = value.get("cases", [])
    if len(cases) != 120 or len({c["id"] for c in cases}) != 120:
        raise ValueError("Dataset must contain 120 unique frozen cases")
    expected_by_kind = {
        "development": (16, 8, 16),
        "holdout": (48, 18, 14),
    }
    split_families = {}
    for split, counts in expected_by_kind.items():
        selected = select_cases(value, split)
        if len(selected) != sum(counts):
            raise ValueError(f"Dataset {split} split count changed")
        if tuple(sum(c["kind"] == kind for c in selected) for kind in KINDS) != counts:
            raise ValueError("Dataset split counts changed")
        split_families[split] = {case["scenario"] for case in selected}
    if not split_families["development"].isdisjoint(split_families["holdout"]):
        raise ValueError("Development and holdout must isolate scenario families")
    return value


def select_cases(dataset, split):
    if split not in {"development", "holdout"}:
        raise ValueError("Select development or holdout")
    return [case for case in dataset["cases"] if case["split"] == split]


def model_input(case):
    """Project one frozen case to only what the agent should receive."""
    return {"task_id": case["id"], "instruction": case["instruction"]}

"""Synthetic memory-decision cases with family-isolated splits and hidden labels."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from decagent.business.codec import strict_json
from decagent.decisions.contracts import QUESTIONS, canonical_json

SCHEMA = "business-memory-evaluation-dataset-1"
VERSION = "business-memory-reference-v1"
FROZEN_CHECKSUM = "820a732a3036aa1fa1d3bf354aedd5ab8b716ca4f33ea7867c228accea0b5989"
DEFAULT_DATASET = (
    Path(__file__).resolve().parents[3] / "evals/benchmarks/business/memory-decisions-v1.json"
)
DEVELOPMENT_FAMILIES = (
    "write_supported_preference",
    "write_redundant_preference",
    "read_active_preference_needed",
    "relevance_active_preference",
)
HOLDOUT_FAMILIES = (
    "write_unsupported_inference",
    "write_conflicting_preference",
    "write_sensitive_candidate",
    "read_memory_unneeded",
    "read_current_fact_unavailable",
    "relevance_stale_or_expired",
    "relevance_wrong_employee_scope",
    "relevance_wrong_workflow_version",
)

_ZH_PROMPTS = {
    "write_supported_preference": (
        "员工明确表示希望后续用简体中文简短汇报。",
        "员工确认以后优先收到中文的简洁摘要。",
        "员工明确要求后续回复使用简体中文并保持精炼。",
        "员工在确认记录中说明喜欢中文短摘要。",
        "员工明确确认后续优先采用中文简短回复。",
    ),
    "write_redundant_preference": (
        "员工再次确认偏好，但当前有效记忆已记录相同内容。",
        "新候选与当前已确认的回复偏好完全一致。",
        "本次表述重复了同一员工已有的有效偏好。",
        "新证据没有增加信息，现存记忆已包含相同偏好。",
        "候选内容与当前有效的同槽记忆重复。",
    ),
    "read_active_preference_needed": (
        "当前任务需要生成回复，员工已确认的语言偏好会影响输出。",
        "需要为员工准备摘要，已批准的格式偏好与任务直接相关。",
        "当前任务要求撰写说明，已确认的简洁偏好会改变结果。",
        "需要组织面向员工的答复，语言偏好可以直接改善内容。",
        "当前工作要求选择回复形式，批准的偏好与本次任务相关。",
    ),
    "relevance_active_preference": (
        "请按当前员工已确认的回复风格组织这次摘要。",
        "生成面向员工的说明时，检查已批准的语言偏好是否适用。",
        "当前任务需要一段答复，确认过的简洁偏好可能相关。",
        "为员工准备本次总结，判断有效的回复风格是否适用。",
        "请根据当前任务判断已确认的表达偏好是否有帮助。",
    ),
    "write_unsupported_inference": (
        "智能体根据员工的一次请求推断其长期偏好，没有明确确认。",
        "候选偏好来自模型猜测，来源证据不包含员工确认。",
        "员工只提出本次格式要求，系统将它推断为长期习惯。",
        "来源记录只是一项临时请求，没有持久偏好的证据。",
        "候选内容由智能体推断，员工没有确认它会持续适用。",
    ),
    "write_conflicting_preference": (
        "员工最近分别要求中文和英文长期回复，冲突尚未解决。",
        "两条有效来源对同一偏好给出不同值，尚无新确认。",
        "候选值与员工当前有效记忆相反，不能自动覆盖。",
        "同一记忆槽存在两条互相矛盾的确认记录。",
        "员工新旧表述不一致，当前证据无法确认应采用哪项。",
    ),
    "write_sensitive_candidate": (
        "候选包含虚构的访问口令文本，来源是被引用的故障日志。",
        "候选试图保存模拟的认证密钥，不属于可持久化偏好。",
        "被引用内容含有占位凭据 SECRET-EXAMPLE，不应写入记忆。",
        "候选是模拟个人身份号码，业务偏好模板不允许保存。",
        "来源文本包含虚构密码值，不应作为员工长期记忆。",
    ),
    "read_memory_unneeded": (
        "本次只需查询当前工单状态，所有必要事实都在工具结果中。",
        "任务只要求返回一个实时字段，不涉及员工偏好。",
        "当前问题可以直接用实时 API 数据回答，不需要历史偏好。",
        "只需核对一个当前状态，记忆不会增加所需信息。",
        "本次任务完全由当前工具结果决定，无需检索记忆。",
    ),
    "read_current_fact_unavailable": (
        "需要查询当前账户余额，这类事实必须来自实时业务系统。",
        "用户询问最新工单负责人，不能从历史偏好记忆推断。",
        "任务需要当前价格，已批准的个人记忆不能替代实时查询。",
        "需要核实当前订单状态，历史记忆不能提供权威状态。",
        "请求的是实时库存，必须查询业务 API 而不是记忆。",
    ),
    "relevance_stale_or_expired": (
        "候选记忆已经过期，当前任务需要依据最新偏好处理。",
        "当前版本为 v3，候选记忆绑定的是旧版本 v1。",
        "候选处于撤销状态，不应影响当前回复。",
        "记忆有效期已结束，当前任务不能继续使用它。",
        "当前工作流修订与候选所绑定的修订不一致。",
    ),
    "relevance_wrong_employee_scope": (
        "候选记忆属于另一名员工，当前员工没有授权访问。",
        "记忆的 owner_scope 与当前员工不同，即使内容相似也不可用。",
        "候选来自其他员工的私有偏好，不适用于当前请求。",
        "该记忆记录绑定到不同 employee_id，必须视为不相关。",
        "候选属于其他员工的业务空间，不能用于当前任务。",
    ),
    "relevance_wrong_workflow_version": (
        "候选只适用于旧版采购流程，当前任务使用新版流程。",
        "记忆绑定的工具和流程版本与当前业务版本不兼容。",
        "候选记录的是旧审批路径，当前流程已经升级。",
        "该经验针对旧版工单生命周期，不能直接套用到当前版本。",
        "当前工作流版本已变更，历史流程经验需要重新确认。",
    ),
}
_EN_PROMPTS = {
    "write_supported_preference": (
        "The employee explicitly confirmed a preference for concise English summaries.",
        "The employee confirmed that future replies should be brief and in English.",
        "A direct confirmation records the employee's preference for short English updates.",
        "The employee explicitly asked to keep future summaries concise and in English.",
        "A confirmed preference requests short English responses for later tasks.",
    ),
    "write_redundant_preference": (
        "The new candidate duplicates the employee's active, verified preference.",
        "The same preference is already stored in the current active memory slot.",
        "This evidence repeats a confirmed preference without adding new information.",
        "The candidate value is identical to the current verified memory.",
        "A valid memory already records this exact preference for this employee.",
    ),
    "read_active_preference_needed": (
        "The task requires writing a reply and the approved style preference affects it.",
        "An employee-facing summary is needed; the confirmed format preference is relevant.",
        "The current task asks for an explanation whose tone depends on the saved preference.",
        "A response must be prepared and the approved language preference changes the output.",
        "This task requires choosing a response format informed by the active preference.",
    ),
    "relevance_active_preference": (
        "Decide whether this active response preference applies to the current summary.",
        "The employee's verified style preference may help format this task's reply.",
        "Assess whether the approved language memory is relevant to this response.",
        "The task asks for a reply; check whether this current preference applies.",
        "Determine whether the active communication preference fits this summary task.",
    ),
    "write_unsupported_inference": (
        "The agent inferred a lasting preference from one request; "
        "the employee never confirmed it.",
        "The candidate is a model guess and the evidence contains no employee confirmation.",
        "Only a one-time format request exists, but the candidate claims a permanent preference.",
        "The source records a temporary request with no evidence of a continuing preference.",
        "The employee has not confirmed that this inferred preference should be retained.",
    ),
    "write_conflicting_preference": (
        "Two current confirmations disagree about the employee's long-term language preference.",
        "The candidate conflicts with an active memory and no newer confirmation resolves it.",
        "Two verified sources give different values for the same memory slot.",
        "The employee's latest and earlier statements conflict and need clarification.",
        "The source evidence does not establish which of two conflicting preferences is current.",
    ),
    "write_sensitive_candidate": (
        "The candidate contains a fictional password placeholder from a quoted support log.",
        "A simulated authentication secret is not an employee preference to retain.",
        "The quoted text includes SECRET-EXAMPLE and must not be stored as memory.",
        "The candidate is a synthetic personal identifier, outside the allowed memory scope.",
        "The source contains a fictional credential and must not be persisted.",
    ),
    "read_memory_unneeded": (
        "The task only needs a live ticket status returned by the current tool call.",
        "All facts needed to answer this request are in the fresh API result.",
        "The request concerns one current field and does not depend on personal preferences.",
        "A live system query fully determines the answer; memory adds no useful context.",
        "This task requires no historical employee preference to complete correctly.",
    ),
    "read_current_fact_unavailable": (
        "The user asks for the latest account balance, which must come from the live system.",
        "The current ticket owner is an authoritative business fact, not a memory preference.",
        "The task asks for today's price; approved memory cannot replace an API lookup.",
        "The current order status must be verified against the business API.",
        "Inventory is live data and cannot be inferred from employee memory.",
    ),
    "relevance_stale_or_expired": (
        "The candidate memory is expired and must not guide the current response.",
        "The task is on workflow v3 while the candidate is bound to v1.",
        "The memory is revoked, so it is no longer eligible for this task.",
        "The memory validity period ended before this task started.",
        "The current workflow revision differs from the candidate's bound revision.",
    ),
    "relevance_wrong_employee_scope": (
        "The candidate belongs to another employee and is outside the current access scope.",
        "The owner scope differs from the current employee, even though the text is similar.",
        "This private preference belongs to a different employee.",
        "The memory is bound to another employee_id and cannot be used here.",
        "The candidate belongs to a different employee business space.",
    ),
    "relevance_wrong_workflow_version": (
        "The preference applies to an older procurement workflow, not the current version.",
        "The candidate's tool and workflow versions are incompatible with this task.",
        "The stored experience describes an approval path that has since changed.",
        "This memory targets an older ticket lifecycle and cannot be reused directly.",
        "The workflow version changed, so this historical experience needs revalidation.",
    ),
}


def _input_for(family: str, variant: int) -> tuple[str, dict, str | bool]:
    is_zh = variant % 2 == 0
    prompt = (_ZH_PROMPTS if is_zh else _EN_PROMPTS)[family][variant // 2]
    if family.startswith("write_"):
        candidate = {
            "kind": "preference",
            "subject": "response_style",
            "value": "简短中文" if is_zh else "concise English",
            "employee_scope": "employee:current",
        }
        source = {"source_type": "employee_statement", "excerpt": prompt}
        refs = ["synthetic-confirmation-1"] if family == "write_supported_preference" else []
        conflicts = []
        expected = "retain"
        if family in {"write_redundant_preference", "write_unsupported_inference"}:
            expected = "discard"
        if family in {"write_conflicting_preference", "write_sensitive_candidate"}:
            expected = "review"
            if family == "write_conflicting_preference":
                conflicts = ["active memory has a conflicting value"]
            else:
                candidate.update(kind="credential", subject="credential", value="SECRET-EXAMPLE")
                source["source_type"] = "quoted_untrusted_log"
        elif family == "write_redundant_preference":
            candidate["duplicate_of"] = "active-memory-fixture"
        elif family == "write_unsupported_inference":
            source["source_type"] = "agent_inference"
        return (
            "memory_write",
            {
                "candidate": candidate,
                "source_evidence": source,
                "confirmation_refs": refs,
                "conflicts": conflicts,
            },
            expected,
        )
    if family.startswith("read_"):
        expected = family == "read_active_preference_needed"
        terms = ["response_style", "language"] if expected else ["current_business_fact"]
        return (
            "memory_read_needed",
            {
                "task_summary": prompt,
                "current_version": "workflow-v3",
                "search_terms": terms,
            },
            expected,
        )
    expected = family == "relevance_active_preference"
    summary = {
        "kind": "preference",
        "subject": "response_style",
        "value": "简短中文" if is_zh else "concise English",
        "status": "active",
        "verified": True,
        "owner_scope": "employee:current",
        "workflow_version": "workflow-v3",
    }
    if family == "relevance_stale_or_expired":
        if variant % 4 == 0:
            summary.update(status="expired", expires_at="2024-01-01T00:00:00Z")
        else:
            summary["workflow_version"] = "workflow-v1"
        expected = False
    elif family == "relevance_wrong_employee_scope":
        summary["owner_scope"] = "employee:other"
        expected = False
    elif family == "relevance_wrong_workflow_version":
        summary["workflow_version"] = "procurement-v1"
        expected = False
    return (
        "memory_relevant",
        {
            "task_summary": prompt,
            "current_version": "workflow-v3",
            "candidate_summary": summary,
        },
        expected,
    )


def dataset_checksum(value: dict) -> str:
    return hashlib.sha256(
        canonical_json({key: item for key, item in value.items() if key != "checksum"}).encode()
    ).hexdigest()


def generate_memory_dataset() -> dict:
    cases = []
    for split, families in (("development", DEVELOPMENT_FAMILIES), ("holdout", HOLDOUT_FAMILIES)):
        for family in families:
            for variant in range(10):
                question_id, state, expected = _input_for(family, variant)
                question = QUESTIONS[question_id]
                cases.append(
                    {
                        "id": "memory-"
                        + hashlib.sha256(f"{VERSION}:{family}:{variant}".encode()).hexdigest()[:12],
                        "split": split,
                        "scenario_family": family,
                        "language": "zh" if variant % 2 == 0 else "en",
                        "question_id": question_id,
                        "question_version": question.version,
                        "primitive": question.type,
                        "input": state,
                        "expected": expected,
                    }
                )
    result = {
        "schema_version": SCHEMA,
        "version": VERSION,
        "label_source": "synthetic-reference",
        "human_reviewed": False,
        "frozen_before_live": True,
        "families": {
            "development": list(DEVELOPMENT_FAMILIES),
            "holdout": list(HOLDOUT_FAMILIES),
        },
        "review_record": {
            "status": "pending",
            "reviewer": None,
            "reviewed_at": None,
            "required_checks": [
                "Verify each memory decision against the published Jev template "
                "and supplied evidence.",
                "Check employee, scope, workflow-version, expiry, and sensitive-data boundaries.",
                "Record reviewer, date, dataset checksum, and corrections in a new frozen version.",
            ],
        },
        "cases": cases,
    }
    result["checksum"] = dataset_checksum(result)
    return result


def validate_memory_dataset(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("checksum") != dataset_checksum(value):
        raise ValueError("Memory dataset checksum mismatch")
    if (
        value.get("schema_version") != SCHEMA
        or value.get("version") != VERSION
        or value.get("label_source") != "synthetic-reference"
        or value.get("human_reviewed") is not False
        or value.get("frozen_before_live") is not True
        or value.get("review_record", {}).get("status") != "pending"
    ):
        raise ValueError("Unsupported memory dataset version or provenance")
    cases = value.get("cases")
    if (
        not isinstance(cases, list)
        or len(cases) != 120
        or len({row.get("id") for row in cases}) != 120
    ):
        raise ValueError("Memory dataset requires 120 unique cases")
    families = defaultdict(set)
    counts = defaultdict(int)
    for case in cases:
        if not isinstance(case, dict) or case.get("split") not in {"development", "holdout"}:
            raise ValueError("Memory case split is invalid")
        family = case.get("scenario_family")
        families[family].add(case["split"])
        counts[(case["split"], family)] += 1
        question = QUESTIONS.get(case.get("question_id"))
        if (
            question is None
            or case.get("question_version") != question.version
            or case.get("primitive") != question.type
            or not isinstance(case.get("input"), dict)
            or case.get("expected") is None
        ):
            raise ValueError("Memory case differs from its trusted decision contract")
        if question.type == "choice" and case["expected"] not in question.criteria:
            raise ValueError("Choice label is outside the trusted candidate set")
        if question.type == "noul" and type(case["expected"]) is not bool:
            raise ValueError("Noul label must be boolean")
    if any(len(splits) != 1 for splits in families.values()):
        raise ValueError("A memory scenario family crosses evaluation splits")
    if sum(case["split"] == "development" for case in cases) != 40:
        raise ValueError("Memory dataset must have 40 development cases")
    if sum(case["split"] == "holdout" for case in cases) != 80:
        raise ValueError("Memory dataset must have 80 holdout cases")
    expected_families = {
        "development": list(DEVELOPMENT_FAMILIES),
        "holdout": list(HOLDOUT_FAMILIES),
    }
    if (
        set(families) != set(DEVELOPMENT_FAMILIES + HOLDOUT_FAMILIES)
        or value.get("families") != expected_families
    ):
        raise ValueError("Memory dataset scenario families are incomplete")
    if any(count != 10 for count in counts.values()):
        raise ValueError("Each isolated memory scenario family requires ten cases")
    if FROZEN_CHECKSUM and value["checksum"] != FROZEN_CHECKSUM:
        raise ValueError("Frozen memory dataset changed; create a new version")
    return value


def load_memory_dataset(path: Path = DEFAULT_DATASET) -> dict:
    return validate_memory_dataset(strict_json(Path(path).read_text(encoding="utf-8")))


def select_memory_cases(dataset: dict, split: str) -> list[dict]:
    validate_memory_dataset(dataset)
    if split not in {"development", "holdout"}:
        raise ValueError("Memory evaluation split must be development or holdout")
    return [case for case in dataset["cases"] if case["split"] == split]


def memory_model_input(case: dict) -> dict:
    """Project the frozen prompt/template only; never send split or expected label."""
    question = QUESTIONS[case["question_id"]]
    return {
        "sample_id": case["id"],
        "question_id": question.id,
        "question_version": question.version,
        "question": question.to_wire(),
        "state": case["input"],
    }


def write_frozen_memory_dataset(path: Path = DEFAULT_DATASET) -> Path:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = generate_memory_dataset()
    validate_memory_dataset(value)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path

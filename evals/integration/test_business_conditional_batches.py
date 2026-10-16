"""Batch results survive crashes and are gated only when a node consumes them."""

import importlib
from types import SimpleNamespace

import pytest

from decagent.business.plans.handlers import BusinessNodeHandlers
from decagent.business.plans.runtime import NodeResult, PlanRuntime
from decagent.decisions.contracts import NoulAnswer, evaluation
from evals.deterministic.test_business_decision_batches import decisions
from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks
from evals.integration.test_business_plan_repository import repository

pytestmark = pytest.mark.integration


class Adapter:
    def __init__(self):
        self.calls = 0

    def evaluate(self, request):
        self.calls += 1
        return evaluation(
            request,
            "jev",
            "jev-latest",
            actual_model="jev-1.13.0",
            answers={q.id: NoulAnswer(0.9) for q in request.questions},
        )


def setup(database, sample, *, after_evaluation=None, plan_override=None, input_schema=None):
    api = importlib.import_module("decagent.business.plans.batching")
    repo = repository(database, sample)
    plan, _ = decisions()
    repo.publish(plan_override or plan.plan, input_schema=input_schema)
    adapter, consumed = Adapter(), []

    def gate(node, raw, bound, batch):
        consumed.append(node["id"])
        return NodeResult(
            {
                "kind": "decide",
                "accepted": True,
                "value": raw,
                "employee_id": bound.employee_id,
                "scope_revision": bound.scope_revision,
                "account_revision": bound.account_revision,
            }
        )

    runner = api.DecisionBatchRunner(repo, adapter, gate, after_evaluation=after_evaluation)
    services = SimpleNamespace(executor=SimpleNamespace(read=sample["backend"].read))
    handlers = BusinessNodeHandlers(repo, services, decide=runner.consume)
    value = PlanRuntime(repo, handlers.execute, prime_decisions=runner.prime)
    return value, adapter, consumed, repo, runner


def test_one_batch_is_reused_as_each_ready_node_consumes_its_answer(database, mixed_business_tasks):
    value, adapter, consumed, repo, _ = setup(database, mixed_business_tasks)
    for _ in range(10):
        result = value.step()
        if result["status"] != "advanced":
            break
    assert result["completion"]["valid"]
    assert adapter.calls == 1 and set(consumed) == {"decision_0", "decision_1"}
    with database.connect() as conn:
        completed = conn.execute(
            "SELECT payload FROM app.events WHERE task_id=%s AND kind='business.decision_batch' "
            "AND payload->>'phase'='complete'",
            (repo.task_id,),
        ).fetchall()
    assert len(completed) == 1
    assert completed[0]["payload"]["result"]["answered_questions"] == 2
    assert repo.counters()["node_attempts"] == 4


def test_uncertain_batch_is_not_silently_requested_again(database, mixed_business_tasks):
    def crash(batch):
        raise SystemExit("Lost process after model response before batch save")

    value, adapter, consumed, repo, runner = setup(
        database, mixed_business_tasks, after_evaluation=crash
    )
    assert value.step()["status"] == "advanced"  # Directory is durable.
    with pytest.raises(SystemExit):
        value.step()
    runner.after_evaluation = None
    result = value.step()
    assert result["status"] == "waiting" and result["request"]["kind"] == "reconciliation"
    assert adapter.calls == 1 and consumed == []
    assert repo.counters()["node_attempts"] == 3


def test_unselected_decision_branch_neither_calls_jev_nor_triggers_gate(
    database, mixed_business_tasks
):
    from evals.deterministic.test_business_plan_contracts import literal, node

    compiled, names = decisions()
    plan = compiled.plan.to_mapping()
    plan.pop("content_digest")
    plan["nodes"][0]["next"] = ["choose"]
    plan["nodes"][1]["next"] = [names[1]]
    plan["nodes"].insert(
        1,
        node(
            "choose",
            "branch",
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": literal("inactive"),
                        "right": literal("active"),
                    },
                    "next": [names[0]],
                }
            ],
            on_no_match={"node_id": "done"},
        ),
    )
    value, adapter, consumed, _, _ = setup(database, mixed_business_tasks, plan_override=plan)
    for _ in range(10):
        result = value.step()
        if result["status"] != "advanced":
            break
    assert result["completion"]["valid"]
    assert adapter.calls == 0 and consumed == []


def test_gate_wait_preserves_raw_answer_without_another_request(database, mixed_business_tasks):
    from decagent.business.plans.runtime import NodePending

    value, adapter, consumed, _, runner = setup(database, mixed_business_tasks)
    real_gate = runner.gate
    waited = []

    def gate(node, raw, bound, batch):
        if not waited:
            waited.append(node["id"])
            raise NodePending({"kind": "clarification", "reason": "Scripted employee question"})
        return real_gate(node, raw, bound, batch)

    runner.gate = gate
    assert value.step()["status"] == "advanced"
    assert value.step()["status"] == "waiting"
    for _ in range(8):
        result = value.step()
        if result["status"] != "advanced":
            break
    assert result["completion"]["valid"]
    assert adapter.calls == 1 and len(consumed) == 2


def test_uncertain_request_keeps_identity_after_plan_revision(database, mixed_business_tasks):
    from evals.integration.test_business_plan_repository import revise

    def crash(batch):
        raise SystemExit("Exit before batch result persistence")

    value, adapter, consumed, repo, runner = setup(
        database, mixed_business_tasks, after_evaluation=crash
    )
    value.step()
    with pytest.raises(SystemExit):
        value.step()
    original = repo.current()
    repo.publish(revise(original, 2), parent_digest=original.digest)
    runner.after_evaluation = None
    result = value.step()
    assert result["status"] == "waiting" and result["request"]["kind"] == "reconciliation"
    assert adapter.calls == 1 and consumed == []


def test_missing_sibling_takes_its_own_exit_after_valid_decision(database, mixed_business_tasks):
    from evals.deterministic.test_business_plan_contracts import reference

    compiled, _ = decisions()
    names = [name for name in compiled.order if name.startswith("decision_")]
    plan = compiled.plan.to_mapping()
    plan.pop("content_digest")
    missing = next(item for item in plan["nodes"] if item["id"] == names[-1])
    missing["inputs"]["sources"] = reference("task_input", path=["extra"], value_type="object")
    schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "extra": {"type": "object"},
        },
    }
    value, adapter, consumed, repo, _ = setup(
        database, mixed_business_tasks, plan_override=plan, input_schema=schema
    )
    assert value.step()["status"] == "advanced"
    assert value.step()["status"] == "advanced"
    assert consumed == [names[0]]
    assert value.step()["status"] == "advanced"  # Persist that sibling's declared missing exit.
    assert value.step()["reason"] == "missing_input"
    assert adapter.calls == 1
    failed = next(row for row in repo.nodes() if row["node_id"] == names[-1])
    assert failed["status"] == "failed"


def test_oversize_question_returns_scope_wait_without_calling_model(database, mixed_business_tasks):
    from evals.deterministic.test_business_plan_contracts import literal

    compiled, _ = decisions()
    plan = compiled.plan.to_mapping()
    plan.pop("content_digest")
    for item in plan["nodes"]:
        if item["type"] == "decide":
            item["inputs"]["sources"] = literal({"long": "x" * 8000}, value_type="object")
    value, adapter, _, _, _ = setup(database, mixed_business_tasks, plan_override=plan)
    value.step()
    result = value.step()
    assert result["status"] == "waiting" and result["request"]["kind"] == "clarification"
    assert "scope" in result["request"]["reason"] and adapter.calls == 0

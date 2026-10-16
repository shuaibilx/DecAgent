"""Only independent ready questions with identical current state may share Jev."""

import importlib
from copy import deepcopy

import pytest

from decagent.business.plans.bindings import BindingContext
from decagent.business.plans.compiler import compile_plan
from decagent.decisions.contracts import NoulAnswer, evaluation
from evals.deterministic.test_business_plan_compiler import context, read_plan
from evals.deterministic.test_business_plan_contracts import node, reference


def decisions(count=2):
    spec, registry = context()
    definition = next(item for item in registry.describe() if item["type"] == "noul")
    plan = read_plan()
    names = ["decision_" + str(index) for index in range(count)]
    plan["nodes"][0]["next"] = names
    plan["nodes"][1:1] = [
        node(
            name,
            "decide",
            question={key: definition[key] for key in ("question_id", "version", "type")},
            policy={"policy_id": "test", "version": "1"},
            inputs={"instruction": reference("task_input", path=["instruction"])},
            next=["done"],
            on_no_match={"stop": "no_match"},
        )
        for name in names
    ]
    plan["nodes"][-1]["depends_on"] = names
    return compile_plan(plan, spec), names


def binding_context():
    return BindingContext({"instruction": "创建工单，VPN 无法连接"}, {}, {}, {}, "employee", 1, 1)


def batches(compiled, ready, bound=None, **kwargs):
    api = importlib.import_module("decagent.business.plans.batching")
    return api.build_batches(
        compiled,
        ready,
        bound or binding_context(),
        spec=context()[0],
        task_id="task",
        scope_id="scope",
        **kwargs,
    )


def test_independent_questions_share_one_request_without_primary():
    compiled, ready = decisions()
    result = batches(compiled, ready)
    assert len(result) == 1
    assert [q.id for q in result[0].request.questions] == ready
    assert result[0].request.state["instruction"] == binding_context().task_input["instruction"]


def test_batches_are_bounded_to_eight_questions():
    compiled, ready = decisions(10)
    assert [len(batch.node_ids) for batch in batches(compiled, ready)] == [8, 2]


def test_same_batch_answer_dependency_is_rejected_even_if_nullable():
    compiled, ready = decisions()
    # Compilation intentionally supports a nullable prior decision; batching must
    # still reject pretending that its answer exists within the same request.
    value = compiled.plan.to_mapping()
    value.pop("content_digest")
    value["nodes"][1]["next"] = [ready[1]]
    value["nodes"][2]["inputs"]["sources"] = reference(
        "accepted_decision",
        node_id=ready[0],
        path=[],
        value_type="object",
        nullable=True,
    )
    spec, _ = context()
    compiled = compile_plan(value, spec)
    with pytest.raises(ValueError, match="depend"):
        batches(compiled, ready)


def test_future_directory_result_is_required_before_dynamic_candidates():
    import json
    from importlib.resources import files

    plan = json.loads(
        files("decagent.business.plans").joinpath("examples.json").read_text(encoding="utf-8")
    )["examples"]["create_with_category"]
    compiled = compile_plan(plan["plan"], context()[0], input_schema=plan["input_schema"])
    with pytest.raises(ValueError, match="missing"):
        batches(compiled, ["category"])
    bound = binding_context()
    bound.node_outputs["directory"] = {
        "kind": "read_tool",
        "verified": True,
        "employee_id": "employee",
        "scope_revision": 1,
        "account_revision": 1,
        "data": {"people": [], "categories": [{"id": "current", "name": "IT"}]},
    }
    first = batches(compiled, ["category"], bound)[0]
    bound.node_outputs["directory"]["data"]["categories"] = [{"id": "later", "name": "IT"}]
    second = batches(compiled, ["category"], bound)[0]
    assert first.request.input_hash != second.request.input_hash
    assert set(second.request.questions[0].criteria) == {"later", "no_match"}


def test_oversize_input_requests_scope_reduction_without_truncating():
    compiled, ready = decisions()
    bound = binding_context()
    bound.task_input["instruction"] = "大" * 5000
    with pytest.raises(ValueError, match="input|scope"):
        batches(compiled, ready, bound)
    assert bound.task_input["instruction"] == "大" * 5000


def test_dispatch_retains_unaccepted_low_certainty_without_human_side_effect():
    api = importlib.import_module("decagent.business.plans.batching")
    compiled, ready = decisions()
    prepared = batches(compiled, ready)[0]

    class Adapter:
        calls = 0

        def evaluate(self, request):
            self.calls += 1
            return evaluation(
                request,
                "jev",
                "jev-latest",
                actual_model="jev-1.13.0",
                answers={name: NoulAnswer(0.5) for name in ready},
            )

    adapter = Adapter()
    result = api.evaluate_batch(adapter, prepared)
    assert adapter.calls == 1
    assert len(result["raw_answers"]) == 2
    assert result["answered_questions"] == 2
    assert all(row["accepted"] is False for row in result["raw_answers"].values())
    assert result["actual_requests"] == 0  # Scripted evaluation has no provider attempts.


def test_different_real_state_is_split_instead_of_merged():
    compiled, ready = decisions()
    value = compiled.plan.to_mapping()
    value.pop("content_digest")
    from evals.deterministic.test_business_plan_contracts import literal

    value["nodes"][2]["inputs"]["sources"] = literal({"different": "source"}, value_type="object")
    compiled = compile_plan(value, context()[0])
    assert len(batches(compiled, ready)) == 2


def test_requested_batch_does_not_mutate_plan_or_context():
    compiled, ready = decisions()
    prior = deepcopy(compiled.plan.to_mapping())
    batches(compiled, ready)
    assert prior == compiled.plan.to_mapping()

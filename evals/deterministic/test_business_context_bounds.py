"""Bounded business context is deterministic data, with no summarization model."""

from copy import deepcopy

import pytest

from decagent.business.context import (
    CONTEXT_INSTRUCTIONS,
    ContextBudgetError,
    ContextBuilder,
    WorkingMemory,
)
from decagent.decisions.contracts import digest
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import node, reference


def memory(**changes):
    state = {
        "format_version": "1",
        "task_id": "fixture",
        "instruction": "Read current directory",
        "boundary": {"employee_id": "employee", "scope_revision": 1, "account_revision": 1},
        "requested_constraints": [],
        "plan": read_plan(),
        "nodes": [],
        "completed_nodes": [],
        "pending_nodes": ["directory", "done"],
        "observations": [],
        "decisions": {},
        "corrections": [],
        "actions": [],
        "approvals": [],
        "budget": {"limits": {}, "active_seconds": "0"},
        "through_event_seq": 0,
        **changes,
    }
    state["source_digest"] = digest(state)
    return WorkingMemory.from_mapping(state)


def observation(value):
    return {
        "node_id": "directory",
        "kind": "read_tool",
        "classification": "verified",
        "current": True,
        "requires_refresh": False,
        "source_ref": {
            "kind": "node",
            "node_id": "directory",
            "generation": 1,
            "digest": digest(value),
        },
        "value_digest": digest(value),
        "value": value,
    }


def test_context_is_deterministic_and_snapshots_are_immutable():
    original = memory(observations=[observation({"categories": []})])
    first = ContextBuilder(original).planning()
    second = ContextBuilder(original).planning()
    assert first.to_mapping() == second.to_mapping()
    assert first.messages() == second.messages()
    changed = original.to_mapping()
    changed["instruction"] = "changed"
    assert original.to_mapping()["instruction"] == "Read current directory"
    changed = first.to_mapping()
    changed["boundary"].clear()
    assert first.to_mapping()["boundary"]["employee_id"] == "employee"


@pytest.mark.parametrize("changes", [{"max_tokens": 0}, {"max_tokens": True}, {"max_bytes": 0}])
def test_context_bounds_reject_zero_or_coerced_values(changes):
    with pytest.raises(ValueError):
        ContextBuilder(memory(), **changes)


def test_budget_accounts_for_utf8_and_system_message_framing():
    plain = ContextBuilder(memory(instruction="a" * 100)).planning()
    chinese = ContextBuilder(memory(instruction="工" * 100)).planning()
    assert chinese.token_upper_bound - plain.token_upper_bound == 200
    with pytest.raises(ContextBudgetError) as caught:
        ContextBuilder(
            memory(instruction="工" * 100), max_tokens=plain.token_upper_bound
        ).planning()
    assert caught.value.required_tokens == chinese.token_upper_bound


def test_mapping_only_context_does_not_charge_an_unused_system_prefix():
    original = memory()
    default = ContextBuilder(original).planning()
    mapped = ContextBuilder(original, system_prefix="").planning()
    assert mapped.to_mapping() == default.to_mapping()
    assert default.token_upper_bound - mapped.token_upper_bound == len(
        CONTEXT_INSTRUCTIONS.encode("utf-8")
    )
    assert mapped.messages() == [{"role": "user", "content": mapped._source}]
    assert len(default.messages()) == 2


def test_missing_referenced_fields_remain_explicitly_missing():
    context = ContextBuilder(memory(observations=[observation({"people": []})])).planning()
    assert context.to_mapping()["observations"][0]["fields"] == [
        {"path": ["categories"], "missing": True}
    ]


def test_unknown_submissions_cannot_be_discarded_to_make_context_fit():
    state = memory(
        actions=[
            {
                "status": "unknown",
                "operation_id": "original-operation",
                "params": {"title": "x" * 8000},
            }
        ]
    )
    with pytest.raises(ContextBudgetError):
        ContextBuilder(state, max_tokens=5000).planning()


def test_plain_literal_objects_cannot_invent_required_evidence_dependencies():
    plan = read_plan()
    plan["nodes"][0]["inputs"]["inert_data"] = {
        "source": "literal",
        "value_type": "object",
        "nullable": False,
        "provenance": "plan_literal",
        "value": {"source": "node_output", "node_id": "directory", "path": ["large"]},
    }
    context = ContextBuilder(
        memory(
            plan=plan,
            observations=[observation({"categories": [], "large": "x" * 20000})],
        )
    ).planning()
    assert context.to_mapping()["observations"][0]["fields"] == [
        {"path": ["categories"], "value": []}
    ]


def test_serialized_memory_rejects_credentials_even_without_a_repository():
    with pytest.raises(ValueError):
        memory(instruction="Bearer " + "Z" * 40)


def test_working_memory_integrity_is_verified_when_reconstructed_from_a_mapping():
    value = deepcopy(memory().to_mapping())
    value["instruction"] = "Changed without a matching source digest"
    with pytest.raises(ValueError):
        WorkingMemory.from_mapping(value)


def test_unknown_replan_reasons_and_missing_decision_nodes_are_rejected():
    builder = ContextBuilder(memory())
    with pytest.raises(ValueError):
        builder.planning(replan_reason="grant_permission")
    with pytest.raises(ValueError):
        builder.decision("missing")


def directory_decision_memory(*, status, inactive=False):
    plan = read_plan()
    plan["nodes"][0]["next"] = ["judge"]
    plan["nodes"].insert(
        1,
        node(
            "judge",
            "decide",
            inputs={
                "directory": reference("node_output", node_id="directory", value_type="object")
            },
            next=["done"],
        ),
    )
    return memory(
        plan=plan,
        nodes=[{"node_id": "judge", "status": status, "frozen": True, "active": True}],
        pending_nodes=["done"] if status in {"skipped", "succeeded"} or inactive else ["judge"],
        blocked_nodes=[] if status in {"skipped", "succeeded"} or inactive else ["done"],
        inactive_nodes=["judge"] if inactive else [],
        observations=[observation({"categories": [], "people": [{"name": "x" * 20000}]})],
    )


@pytest.mark.parametrize(
    "status,inactive", [("skipped", False), ("succeeded", False), ("running", True)]
)
def test_retired_or_inactive_decision_no_longer_makes_the_whole_directory_mandatory(
    status, inactive
):
    context = ContextBuilder(directory_decision_memory(status=status, inactive=inactive)).planning()
    observed = context.to_mapping()["observations"][0]
    assert observed["fields"] == [{"path": ["categories"], "value": []}]
    assert "people" not in str(observed)
    assert context.to_mapping()["nodes"][0]["status"] == status


def test_future_decision_still_requires_its_completed_source_without_truncation():
    with pytest.raises(ContextBudgetError):
        ContextBuilder(directory_decision_memory(status="waiting")).planning()


def test_goal_evidence_remains_required_after_all_nodes_settle():
    with pytest.raises(ContextBudgetError):
        ContextBuilder(
            memory(
                pending_nodes=[],
                blocked_nodes=[],
                observations=[observation({"categories": ["x" * 20000]})],
            )
        ).planning()

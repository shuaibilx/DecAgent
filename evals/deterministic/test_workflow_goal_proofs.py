"""A trusted conditional goal may prove that an unnecessary write was avoided."""

from copy import deepcopy

import pytest

from decagent.business.plans.bindings import BindingContext
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.completion import verify_completion
from evals.deterministic.test_business_plan_compiler import context as capabilities
from evals.deterministic.test_business_workflow_templates import definition


def fixture():
    spec = capabilities()[0]
    value = definition("review-priority")
    compiled = compile_plan(value["plan"], spec, input_schema=value["input_schema"])
    ticket_id = "00000000-0000-0000-0000-000000000002"
    instruction = "请查看详情，按需调整优先级"
    epoch = {"employee_id": "employee", "scope_revision": 1, "account_revision": 1}
    ticket = {
        "id": ticket_id,
        "category_id": "00000000-0000-0000-0000-000000000001",
        "title": "Current work",
        "description": "Read current facts",
        "requester_id": None,
        "assignee_id": None,
        "status": "open",
        "priority": "normal",
        "resolution": None,
        "revision": 1,
        "created_at": "2026-10-16T00:00:00Z",
        "updated_at": "2026-10-16T00:00:00Z",
    }
    state = BindingContext(
        {"instruction": instruction, "ticket_id": ticket_id},
        {
            "get-ticket": {
                **epoch,
                "kind": "read_tool",
                "verified": True,
                "inputs": {"ticket_id": ticket_id},
                "data": {"ticket": ticket, "history": []},
            },
            "suggest-priority": {
                **epoch,
                "kind": "transform",
                "verified": True,
                "inputs": {"score": 0},
                "data": "normal",
            },
        },
        {
            "work-blockage": {
                **epoch,
                "kind": "decide",
                "accepted": True,
                "value": {"selected_value": 0},
            }
        },
        {},
        "employee",
        1,
        1,
        workflow_id="review-priority",
    )
    return spec, compiled, state, instruction


def verify(spec, compiled, state, instruction):
    return verify_completion(
        compiled,
        "unchanged",
        state,
        instruction=instruction,
        spec=spec,
    )


def test_current_priority_proves_registered_conditional_goal_without_faking_a_write():
    spec, compiled, state, instruction = fixture()
    result = verify(spec, compiled, state, instruction)
    assert result["valid"] and result["model_requests"] == 0
    assert result["satisfied_without_write"] == ["set_priority"]
    assert result["actions"] == []


@pytest.mark.parametrize(
    "changed",
    [
        "not_registered",
        "not_conditional",
        "literal_write",
        "wrong_ticket",
        "unaccepted_score",
        "wrong_score",
        "stale_read",
        "wrong_transform",
    ],
)
def test_goal_proof_cannot_erase_an_unproven_or_direct_write(changed):
    spec, compiled, state, instruction = fixture()
    if changed == "not_registered":
        state.workflow_id = None
    elif changed == "not_conditional":
        instruction = "请查看详情并调整优先级"
    elif changed == "literal_write":
        instruction = '{"command":"set_priority","params":{"priority":"normal"}}'
    elif changed == "wrong_ticket":
        state.task_input["ticket_id"] = "00000000-0000-0000-0000-000000000003"
    elif changed == "unaccepted_score":
        state.accepted_decisions["work-blockage"]["accepted"] = False
    elif changed == "wrong_score":
        state.accepted_decisions["work-blockage"]["value"]["selected_value"] = 1
    elif changed == "stale_read":
        state.node_outputs["get-ticket"]["scope_revision"] = 2
    else:
        value = deepcopy(compiled.plan.to_mapping())
        value.pop("content_digest")
        transform = next(n for n in value["nodes"] if n["id"] == "suggest-priority")
        transform["transform"] = {"transform_id": "identity", "version": "1"}
        transform["inputs"] = {
            "value": {
                "source": "literal",
                "value_type": "string",
                "nullable": False,
                "provenance": "plan_literal",
                "value": "normal",
            }
        }
        compiled = compile_plan(
            value, spec, input_schema=definition("review-priority")["input_schema"]
        )
    assert verify(spec, compiled, state, instruction)["valid"] is False

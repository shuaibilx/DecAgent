"""Owner-authored workflow templates remain compiled data with fresh parameter slots."""

import importlib

import pytest

from decagent.business.plans.bindings import BindingContext, evaluate_condition, resolve_inputs
from decagent.business.plans.compiler import compile_plan
from decagent.business.workflow_registry import validate_definition
from evals.deterministic.test_business_plan_compiler import context

TEMPLATES = {
    "create-ticket": {"category"},
    "directory-assign": {"explicit_assignee", "person"},
    "review-priority": {"work_blockage"},
}


def definition(template_id, **overrides):
    values = {
        "spec": context()[0],
        "scope_id": "00000000-0000-0000-0000-000000000001",
        "scope_revision": 1,
        "policy_dependencies": {
            question: {
                "policy_id": "synthetic-" + question,
                "policy_version": "1",
                "definition_digest": "a" * 64,
            }
            for question in TEMPLATES[template_id]
        },
        **overrides,
    }
    module = importlib.import_module("decagent.business.workflow_templates")
    return module.build_workflow_template(template_id, **values)


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_templates_validate_under_owner_registry_with_exact_policy_pins(template_id):
    value = definition(template_id)
    normalized = validate_definition(value, context()[0])
    compiled = compile_plan(value["plan"], context()[0], input_schema=value["input_schema"])
    assert normalized["workflow_id"] == template_id
    assert len(compiled.order) <= 32
    questions = {
        node["question"]["question_id"] for node in compiled.plan.nodes if node["type"] == "decide"
    }
    assert questions == TEMPLATES[template_id]
    assert value["applicability"]["input_equals"] == {}
    for node in compiled.plan.nodes:
        for name, binding in node.get("inputs", {}).items():
            if name.endswith("_id") or name in {"title", "description"}:
                assert binding["source"] != "literal"
            if binding["source"] == "accepted_decision":
                assert binding["path"] == ["selected_value"]
            if name == "expected_revision":
                assert binding["source"] == "node_output"
                assert compiled.node(binding["node_id"])["tool"]["command"] == "get"


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_missing_policy_pins_fail_before_a_draft_can_be_built(template_id):
    with pytest.raises(ValueError, match="policy"):
        definition(template_id, policy_dependencies={})


def test_unknown_template_cannot_invent_a_business_capability():
    with pytest.raises(ValueError, match="template"):
        importlib.import_module("decagent.business.workflow_templates").build_workflow_template(
            "shell",
            spec=context()[0],
            scope_id="scope",
            scope_revision=1,
            policy_dependencies={},
        )


def bound(**decisions):
    current = BindingContext({}, {}, {}, {}, "employee", 1, 1)
    current.accepted_decisions = {
        name: {
            "accepted": True,
            "value": {"selected_value": value},
            "employee_id": "employee",
            "scope_revision": 1,
            "account_revision": 1,
        }
        for name, value in decisions.items()
    }
    return current


@pytest.mark.parametrize("explicit,expected", [(True, "select-person"), (False, None)])
def test_assignment_noul_controls_only_the_declared_person_branch(explicit, expected):
    value = definition("directory-assign")
    node = next(item for item in value["plan"]["nodes"] if item["id"] == "has-assignee")
    current = bound(**{"explicit-assignee": explicit})
    matches = [case for case in node["cases"] if evaluate_condition(case["condition"], current)]
    assert ([item for case in matches for item in case["next"]] or [None]) == [expected]
    assert node["on_no_match"] == {"stop": "missing_input"}


@pytest.mark.parametrize("same", [True, False])
def test_priority_comparison_routes_same_value_without_a_write(same):
    value = definition("review-priority")
    node = next(item for item in value["plan"]["nodes"] if item["id"] == "priority-changed")
    current = bound()
    for name, kind, data in (
        ("get-ticket", "read_tool", {"ticket": {"priority": "high"}}),
        ("suggest-priority", "transform", "high" if same else "urgent"),
    ):
        current.node_outputs[name] = {
            "kind": kind,
            "verified": True,
            "data": data,
            "employee_id": "employee",
            "scope_revision": 1,
            "account_revision": 1,
        }
    matches = [case for case in node["cases"] if evaluate_condition(case["condition"], current)]
    assert bool(matches) is same
    assert (
        matches[0]["next"] == ["unchanged"]
        if same
        else node["on_no_match"] == {"node_id": "set-priority"}
    )


def test_new_instance_uses_current_title_and_accepted_category_value():
    value = definition("create-ticket")
    node = next(item for item in value["plan"]["nodes"] if item["id"] == "create")
    current = bound(**{"select-category": "00000000-0000-0000-0000-000000000002"})
    current.task_input = {"title": "Fresh user title", "description": "Fresh description"}
    params, _ = resolve_inputs(node, current, context()[0])
    assert params == {
        "title": "Fresh user title",
        "description": "Fresh description",
        "category_id": "00000000-0000-0000-0000-000000000002",
    }


def test_returned_templates_do_not_share_mutable_plan_or_schema():
    first = definition("create-ticket")
    first["plan"]["nodes"].clear()
    first["input_schema"]["properties"].clear()
    second = definition("create-ticket")
    assert second["plan"]["nodes"] and second["input_schema"]["properties"]

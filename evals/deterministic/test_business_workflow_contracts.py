"""Workflow publication inputs carry slots and regression evidence, never authority."""

import importlib
from copy import deepcopy

import pytest

from decagent.decisions.contracts import digest
from evals.deterministic.test_business_plan_compiler import context, read_plan
from evals.deterministic.test_business_plan_contracts import literal, reference


def api():
    return importlib.import_module("decagent.business.workflow_registry")


def definition(scope_id="00000000-0000-0000-0000-000000000001"):
    return {
        "workflow_id": "read-directory",
        "version": "1",
        "goal_id": "directory-observation",
        "scope_id": scope_id,
        "scope_revision": 1,
        "plan": read_plan(),
        "input_schema": {
            "type": "object",
            "properties": {"instruction": {"type": "string", "minLength": 1}},
            "required": ["instruction"],
            "additionalProperties": False,
        },
        "applicability": {"roles": ["employee", "handler", "supervisor"], "input_equals": {}},
        "policy_dependencies": [],
    }


def report(value):
    normalized = api().validate_definition(value, context()[0])
    return {
        "format": "decagent.workflow-regression.v1",
        "report_id": "scripted-regression-1",
        "reference": "fixture://workflow-regression/1",
        "definition_digest": digest(normalized),
        "synthetic": True,
        "quality_claim": False,
        "cases": [
            {"case_id": "normal", "category": "success", "passed": True},
            {"case_id": "missing", "category": "missing_input", "passed": True},
            {"case_id": "denied", "category": "permission_denied", "passed": True},
            {"case_id": "failure", "category": "tool_failure", "passed": True},
        ],
        "covered_nodes": ["directory", "done"],
        "covered_edges": [["directory", "done"]],
    }


def test_definition_uses_existing_compiler_and_returns_an_independent_snapshot():
    value = definition()
    result = api().validate_definition(value, context()[0])
    assert result["plan"]["content_digest"]
    assert result["tool_fingerprint"] == context()[0].digest
    assert result["question_fingerprint"] == context()[1].fingerprint
    value["plan"]["nodes"].clear()
    assert len(result["plan"]["nodes"]) == 2
    broken = definition()
    broken["plan"]["nodes"][0]["next"] = ["unknown"]
    with pytest.raises(ValueError):
        api().validate_definition(broken, context()[0])


@pytest.mark.parametrize(
    "key",
    ["employee_id", "operation_id", "approval_id", "action_id", "grant", "token", "memory_id"],
)
def test_historical_authority_is_rejected_even_in_nested_literal_data(key):
    value = definition()
    value["plan"]["nodes"][0]["on_error"] = {"stop": "error"}
    value["plan"]["goal"]["description"] = "A safe description"
    value["applicability"]["input_equals"] = {key: "old-value"}
    with pytest.raises(ValueError, match="authority|historical"):
        api().validate_definition(value, context()[0])


def test_business_resource_constants_require_real_parameter_slots():
    value = definition()
    first = value["plan"]["nodes"][0]
    first["tool"]["command"] = "get"
    first["inputs"] = {"ticket_id": literal("previous-ticket")}
    value["plan"]["goal"]["evidence"][0]["source"] = reference(
        "node_output", node_id="directory", path=["ticket"], value_type="object"
    )
    with pytest.raises(ValueError, match="slot|historical"):
        api().validate_definition(value, context()[0])


def test_workflow_memory_slots_must_use_registered_kind_and_subject():
    validate_slots = api()._validate_memory_slots
    valid = {
        "source": "verified_memory",
        "memory_kind": "confirmed_preference",
        "subject": "response_language",
    }
    validate_slots(valid)
    for change in (
        {"memory_kind": "unknown", "subject": "response_language"},
        {"memory_kind": "confirmed_preference", "subject": "employee-specific-id"},
        {"memory_kind": "verified_workflow_experience", "subject": "workflow:unknown"},
    ):
        with pytest.raises(ValueError, match="stable slot"):
            validate_slots({**valid, **change})


def test_schema_cannot_publish_defaults_or_hidden_authority_as_future_input():
    for mutation in ("external", "default", "identity", "open"):
        value = definition()
        schema = value["input_schema"]
        if mutation == "external":
            schema["properties"]["instruction"] = {"$ref": "https://invalid.test/schema"}
        elif mutation == "default":
            schema["properties"]["ticket_id"] = {"type": "string", "default": "old-id"}
        elif mutation == "identity":
            schema["properties"]["employee_id"] = {"type": "string"}
        else:
            schema["additionalProperties"] = True
        with pytest.raises(ValueError):
            api().validate_definition(value, context()[0])


def test_scripted_report_is_explicit_regression_evidence_without_model_quality_claim():
    value = definition()
    evidence = report(value)
    normalized = api().validate_definition(value, context()[0])
    result = api().validate_workflow_report(evidence, digest(evidence), normalized)
    assert result["synthetic"] is True
    assert result["quality_claim"] is False
    for mutation in ("digest", "failed", "coverage", "quality", "missing_failure"):
        changed = deepcopy(evidence)
        if mutation == "digest":
            changed["definition_digest"] = "a" * 64
        elif mutation == "failed":
            changed["cases"][0]["passed"] = False
        elif mutation == "coverage":
            changed["covered_edges"] = []
        elif mutation == "quality":
            changed["quality_claim"] = True
        else:
            changed["cases"].pop()
        with pytest.raises(ValueError):
            api().validate_workflow_report(changed, digest(changed), normalized)


@pytest.mark.parametrize("field", ["ticket_id", "person_id", "category_id"])
def test_resource_input_cannot_reach_database_with_an_invalid_uuid(field):
    value = definition()
    value["input_schema"]["properties"][field] = {"type": "string"}
    reason, _ = api().WorkflowRegistry._input_check(
        value, {"instruction": "Read current resource", field: "not-a-resource-id"}
    )
    assert reason == "invalid_input"

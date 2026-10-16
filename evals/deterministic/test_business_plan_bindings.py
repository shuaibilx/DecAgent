"""Bind real observations into tool arguments without inventing future values."""

import importlib

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from evals.deterministic.test_business_plan_contracts import literal, node, reference


def api():
    return importlib.import_module("decagent.business.plans.bindings")


def context():
    return api().BindingContext(
        task_input={"title": "VPN 故障", "ticket_id": "new-ticket"},
        node_outputs={
            "created": {
                "kind": "governed_write",
                "verified": True,
                "employee_id": "employee-a",
                "scope_revision": 7,
                "data": {
                    "ticket": {"id": "new-ticket", "revision": 1},
                    "receipt": {"operation_id": "original-operation"},
                },
            }
        },
        accepted_decisions={
            "category": {
                "accepted": True,
                "employee_id": "employee-a",
                "scope_revision": 7,
                "value": {"choice": "category-it", "confidence": 0.99},
            }
        },
        memories={},
        employee_id="employee-a",
        scope_revision=7,
    )


def test_created_id_is_bound_from_verified_receipt_producer():
    binding = reference("node_output", node_id="created", path=["ticket", "id"])
    result = api().resolve_binding(binding, context())
    assert result.value == "new-ticket"
    assert result.provenance["node_id"] == "created"
    assert result.provenance["source"] == "node_output"


def test_write_receipt_reference_cannot_be_faked_by_inference():
    binding = reference("node_output", node_id="created", path=["receipt", "operation_id"])
    binding["provenance"] = "write_receipt"
    current = context()
    current.node_outputs["created"]["verified"] = False
    with pytest.raises(ValueError, match="verified"):
        api().resolve_binding(binding, current)


@pytest.mark.parametrize("change", ["employee", "scope", "stale", "not_accepted"])
def test_decision_binding_requires_current_accepted_authorized_source(change):
    current = context()
    record = current.accepted_decisions["category"]
    if change == "employee":
        record["employee_id"] = "employee-b"
    elif change == "scope":
        record["scope_revision"] = 6
    elif change == "stale":
        record["stale"] = True
    else:
        record["accepted"] = False
    binding = reference("accepted_decision", node_id="category", path=["choice"])
    with pytest.raises(ValueError):
        api().resolve_binding(binding, current)


def test_missing_value_is_nullable_only_when_explicitly_declared():
    binding = reference("task_input", path=["missing"], nullable=True)
    assert api().resolve_binding(binding, context()).value is None
    binding["nullable"] = False
    with pytest.raises(ValueError, match="missing"):
        api().resolve_binding(binding, context())


def test_wrong_actual_type_and_attributes_do_not_resolve():
    current = context()
    current.task_input["title"] = 12
    with pytest.raises(ValueError, match="type"):
        api().resolve_binding(reference("task_input", path=["title"]), current)
    with pytest.raises(ValueError):
        api().resolve_binding(reference("task_input", path=["__class__"]), current)


def test_verified_memory_requires_owner_active_status_and_freshness():
    current = context()
    slot = "confirmed_preference:response_language"
    current.memories[slot] = {
        "id": "employee-specific-record",
        "kind": "confirmed_preference",
        "subject": "response_language",
        "employee_id": "employee-a",
        "scope_revision": 7,
        "active": True,
        "verified": True,
        "expired": False,
        "data": {"summary": "中文"},
    }
    binding = reference("verified_memory", path=["summary"])
    result = api().resolve_binding(binding, current)
    assert result.value == "中文"
    assert result.provenance["memory_slot"] == slot
    assert result.provenance["memory_id"] == "employee-specific-record"
    current.memories[slot]["expired"] = True
    with pytest.raises(ValueError):
        api().resolve_binding(binding, current)


def test_verified_memory_slot_must_match_the_resolved_record():
    current = context()
    current.memories["confirmed_preference:response_language"] = {
        "kind": "confirmed_preference",
        "subject": "response_style",
        "employee_id": "employee-a",
        "scope_revision": 7,
        "active": True,
        "verified": True,
        "expired": False,
        "data": {"value": "formal"},
    }
    with pytest.raises(ValueError, match="slot"):
        api().resolve_binding(reference("verified_memory", path=["value"]), current)


def test_final_tool_schema_is_checked_before_dispatch():
    spec = PluginSpec.from_mapping(builtin_manifest())
    target = node(
        "create",
        "governed_write",
        command={"plugin_id": spec.id, "command": "create"},
        inputs={"title": literal("Title"), "category_id": literal("invented")},
        next=["done"],
    )
    with pytest.raises(ValueError):
        api().resolve_inputs(target, context(), spec)


def test_program_owned_identity_cannot_be_bound_into_command():
    spec = PluginSpec.from_mapping(builtin_manifest())
    target = node(
        "create",
        "governed_write",
        command={"plugin_id": spec.id, "command": "create"},
        inputs={"title": literal("Title"), "employee_id": literal("employee-b")},
        next=["done"],
    )
    with pytest.raises(ValueError):
        api().resolve_inputs(target, context(), spec)


def test_score_priority_is_registered_pure_computation():
    assert api().run_transform("priority_for_score", "1", {"score": 1.8}) == "urgent"
    assert api().run_transform("priority_for_score", "1", {"score": 0.2}) == "normal"
    with pytest.raises(ValueError):
        api().run_transform("eval", "1", {"value": "dangerous()"})


def test_conditions_are_closed_and_missing_is_not_success():
    missing = reference("task_input", path=["missing"], nullable=True)
    assert not api().evaluate_condition({"op": "exists", "value": missing}, context())
    assert api().evaluate_condition(
        {
            "op": "eq",
            "left": literal("same"),
            "right": literal("same"),
        },
        context(),
    )
    with pytest.raises(ValueError):
        api().evaluate_condition({"op": "python", "expression": "True"}, context())


def test_coalesce_merges_only_present_optional_branch_outputs():
    assert api().run_transform("coalesce", "1", {"left": None, "right": "actual"}) == "actual"
    with pytest.raises(ValueError):
        api().run_transform("coalesce", "1", {"left": None, "right": None})

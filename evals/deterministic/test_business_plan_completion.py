"""Completion requires the real requested effects and durable evidence."""

import importlib

from decagent.business.contracts import PluginSpec
from decagent.business.plans.bindings import BindingContext
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.tickets import builtin_manifest
from evals.deterministic.test_business_plan_contracts import node, reference


def fixture():
    spec = PluginSpec.from_mapping(builtin_manifest())
    registry = QuestionRegistry.from_spec(spec)
    value = {
        "schema_version": "1",
        "plan_id": "read-tickets",
        "revision": 1,
        "registry_fingerprint": registry.fingerprint,
        "entry": "lookup",
        "goal": {
            "description": "View tickets",
            "evidence": [
                {
                    "id": "read_result",
                    "kind": "tool_result",
                    "description": "Actual read",
                    "source": reference(
                        "node_output", node_id="lookup", path=["tickets"], value_type="array"
                    ),
                }
            ],
        },
        "nodes": [
            node(
                "lookup",
                "read_tool",
                tool={"plugin_id": spec.id, "command": "list"},
                inputs={},
                next=["done"],
            ),
            node("done", "complete", depends_on=["lookup"], evidence=["read_result"]),
        ],
    }
    compiled = compile_plan(value, spec)
    record = {
        "kind": "read_tool",
        "verified": True,
        "employee_id": "employee-a",
        "scope_revision": 1,
        "data": {"tickets": []},
        "inputs": {},
    }
    context = BindingContext({}, {"lookup": record}, {}, {}, "employee-a", 1)
    return compiled, spec, context


def verify(compiled, spec, context, *, instruction='{"command":"list","params":{}}', actions=()):
    module = importlib.import_module("decagent.business.plans.completion")
    return module.verify_completion(
        compiled, "done", context, instruction=instruction, spec=spec, actions=actions
    )


def test_verified_read_proves_literal_goal_without_model_generated_final_text():
    compiled, spec, context = fixture()
    result = verify(compiled, spec, context)
    assert result["valid"] is True
    assert result["model_requests"] == 0
    assert result["summary"]


def test_inference_flag_cannot_prove_tool_completion():
    compiled, spec, context = fixture()
    context.node_outputs["lookup"]["verified"] = False
    assert verify(compiled, spec, context)["valid"] is False


def test_unknown_submission_prevents_success_even_if_read_goal_has_evidence():
    compiled, spec, context = fixture()
    action = {
        "id": "uncertain",
        "status": "unknown",
        "snapshot": {"command": "create", "params": {}},
        "receipt": None,
    }
    result = verify(compiled, spec, context, actions=[action])
    assert not result["valid"]
    assert result["reason"] == "unresolved_write"


def test_invented_completion_goal_cannot_replace_users_literal_request():
    compiled, spec, context = fixture()
    result = verify(
        compiled, spec, context, instruction='{"command":"create","params":{"title":"requested"}}'
    )
    assert not result["valid"]


def test_unproven_natural_language_goal_is_not_declared_complete():
    compiled, spec, context = fixture()
    assert not verify(compiled, spec, context, instruction="Handle everything")["valid"]


def test_executed_write_not_in_plan_evidence_cannot_be_omitted():
    compiled, spec, context = fixture()
    action = {
        "id": "unexpected",
        "status": "succeeded",
        "snapshot": {"command": "create", "params": {}},
        "receipt": {"status": "succeeded"},
    }
    result = verify(compiled, spec, context, actions=[action])
    assert not result["valid"]
    assert result["reason"] == "write_evidence_missing"

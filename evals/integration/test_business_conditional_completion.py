"""Real governed receipt evidence is required by the new completion verifier."""

import json

import pytest

from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec
from decagent.business.execution import BusinessExecutor
from decagent.business.plans.bindings import BindingContext
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.completion import verify_completion
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from evals.deterministic.test_business_plan_contracts import literal, node, reference
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def test_completion_accepts_real_receipt_and_rejects_tampered_working_data(database):
    backend = FixtureTickets(database)
    spec = PluginSpec.from_mapping(builtin_manifest())
    scope = PluginRegistry(database).list()[0]
    directory = backend.read("directory", {})
    params = {
        "title": "Verified conditional completion",
        "category_id": directory["categories"][0]["id"],
    }
    instruction = json.dumps({"command": "create", "params": params})
    task = database.create_business_task(
        scope["id"],
        {"instruction": instruction, "plugin_digest": spec.digest},
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    task = database.claim(task["id"], "completion-test")
    executor = BusinessExecutor(
        database, spec, task, "completion-test", task["lease_generation"], mcp_client=backend
    )
    assessment = assess_action(
        spec,
        str(task["id"]),
        str(scope["id"]),
        instruction,
        "create",
        params,
        {"directory": directory},
    )
    action = executor.plan("create", assessment)
    result = executor.execute(action["id"], assessment["digest"])
    assert result["status"] == "succeeded"
    source = reference("node_output", node_id="create", path=["receipt", "operation_id"])
    source["provenance"] = "write_receipt"
    compiled = compile_plan(
        {
            "schema_version": "1",
            "plan_id": "receipt-proof",
            "revision": 1,
            "registry_fingerprint": QuestionRegistry.from_spec(spec).fingerprint,
            "entry": "create",
            "goal": {
                "description": "Create the requested ticket",
                "evidence": [
                    {
                        "id": "created",
                        "kind": "write_receipt",
                        "description": "Committed receipt",
                        "source": source,
                    }
                ],
            },
            "nodes": [
                node(
                    "create",
                    "governed_write",
                    command={"plugin_id": spec.id, "command": "create"},
                    inputs={key: literal(value) for key, value in params.items()},
                    next=["done"],
                ),
                node("done", "complete", depends_on=["create"], evidence=["created"]),
            ],
        },
        spec,
    )
    with database.connect() as conn:
        actions = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (task["id"],)
        ).fetchall()
    record = {
        "kind": "governed_write",
        "verified": True,
        "employee_id": str(task["employee_id"]),
        "scope_revision": scope["revision"],
        "data": result["data"],
        "inputs": params,
        "action_id": str(action["id"]),
    }
    context = BindingContext(
        {}, {"create": record}, {}, {}, str(task["employee_id"]), scope["revision"]
    )
    success = verify_completion(
        compiled, "done", context, instruction=instruction, spec=spec, actions=actions
    )
    assert success["valid"]
    record["data"]["ticket"]["title"] = "Different unverified content"
    denied = verify_completion(
        compiled, "done", context, instruction=instruction, spec=spec, actions=actions
    )
    assert not denied["valid"] and denied["reason"] == "write_evidence_missing"

"""Revoked inputs pause new dispatch while original operations remain inspectable."""

import importlib
import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.conditional_services import ConditionalBusinessServices
from decagent.business.memory_control import BusinessMemoryControl
from decagent.business.plans.repository import PlanRepository
from decagent.business.registry import PluginRegistry
from decagent.business.workflow_registry import WorkflowRegistry
from decagent.config import Settings
from decagent.decisions.contracts import digest
from decagent.runtime.config import RuntimeConfig
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import reference
from evals.integration import test_business_workflow_registry as workflow_cases
from evals.integration.test_business_conditional_mcp import create_plan
from evals.integration.test_business_memory import confirm, propose
from evals.integration.test_business_memory import sample as sample
from evals.integration.test_business_memory_gateway import published_memory_workflow

pytestmark = pytest.mark.integration


def harness(
    database,
    tmp_path,
    sample,
    plan,
    *,
    instruction=None,
    task=None,
    input_schema=None,
    task_input=None,
):
    backend = sample["backend"]
    if task is None and instruction is not None:
        task = database.create_business_task(
            sample["scope"]["id"],
            {"instruction": instruction, "plugin_digest": sample["scope"]["manifest_digest"]},
            graph_version="business-3",
            employee_id=backend.identity.employee_id,
            credential_ref=backend.identity.credential_ref,
        )
    elif task is None:
        task = sample["task"]
    task = database.claim(task["id"], "dependency-boundary", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(plan, input_schema=input_schema, task_input=task_input)
    value = ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=backend,
    )
    return value, repo


def memory_workflow(database, sample):
    return published_memory_workflow(database, sample)


def activate(sample, services, memory_id, monkeypatch, **preference):
    sample["context"] = BusinessMemoryControl(services.database).context(
        sample["backend"].identity, services.task["id"]
    )
    memory_module = importlib.import_module("decagent.business.memory")
    original, identifiers = memory_module.uuid4, [memory_id]
    monkeypatch.setattr(
        memory_module, "uuid4", lambda: identifiers.pop() if identifiers else original()
    )
    return confirm(sample, propose(sample, **preference))


def memory_write_workflow(database, sample, pending):
    backend = sample["backend"]
    scope = sample["scope"]
    category_id = backend.read("directory", {})["categories"][0]["id"]
    params = {"title": "中文", "category_id": category_id}
    instruction = (
        json.dumps({"command": "create", "params": params})
        if pending == "unknown"
        else "请处理这条请求"
    )
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    definition = workflow_cases.definition(str(scope["id"]))
    definition.update(
        workflow_id="memory-create-ticket",
        goal_id="memory-create-ticket",
        scope_revision=scope["revision"],
    )
    plan = create_plan(spec, params)
    title = reference("verified_memory", path=["value"])
    title.update(memory_kind="confirmed_preference", subject="response_language")
    plan["nodes"][0]["inputs"]["title"] = title
    plan["nodes"][0]["inputs"]["category_id"] = reference("task_input", path=["category_id"])
    definition["plan"] = plan
    definition["input_schema"]["properties"]["category_id"] = {
        "type": "string",
        "format": "uuid",
    }
    registry = WorkflowRegistry(database)
    registry.register(sample["owner"], definition)
    evidence = workflow_cases.report(definition)
    evidence.update(
        report_id="memory-write-" + uuid4().hex,
        covered_nodes=["create", "done"],
        covered_edges=[["create", "done"]],
    )
    registry.validate(
        sample["owner"],
        definition["workflow_id"],
        definition["version"],
        evidence,
        digest(evidence),
    )
    published = registry.publish(sample["owner"], definition["workflow_id"], definition["version"])
    task = database.create_business_task(
        scope["id"],
        {"instruction": instruction, "plugin_digest": scope["manifest_digest"]},
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    task_input = {"instruction": instruction, "category_id": category_id}
    instance = registry.instantiate(
        backend.identity,
        task["id"],
        definition["workflow_id"],
        definition["version"],
        task_input,
    )
    with database.connect() as conn:
        report_record = conn.execute(
            "SELECT report_digest FROM app.workflow_validation_reports WHERE report_id=%s",
            (published["validation_report_id"],),
        ).fetchone()
        snapshot = task["authorization_snapshot"]
        snapshot["workflow"] = {
            "workflow_id": definition["workflow_id"],
            "version": definition["version"],
            "definition_digest": published["definition_digest"],
            "report_digest": report_record["report_digest"],
            "inputs": task_input,
        }
        conn.execute(
            "UPDATE app.tasks SET authorization_snapshot=%s WHERE id=%s",
            (Jsonb(snapshot), task["id"]),
        )
    return task, instance, task_input, definition


def revoke(sample, memory):
    sample["store"].revoke(
        sample["backend"].browser, memory["id"], memory["content_digest"], "Withdraw input"
    )


def test_runtime_loads_current_memory_before_binding(database, tmp_path, sample, monkeypatch):
    memory_id = uuid4()
    instance, _ = memory_workflow(database, sample)
    value, repo = harness(database, tmp_path, sample, instance.plan)
    activate(sample, value, memory_id, monkeypatch)
    assert value.step({})["status"] == "advanced"
    assert repo.nodes()[0]["status"] == "succeeded"
    assert repo.nodes()[0]["output_snapshot"]["result"]["data"] == "中文"


@pytest.mark.parametrize("withdrawal", ["memory", "source", "scope"])
def test_unexecuted_published_memory_slot_pauses_after_invalidation(
    database, tmp_path, sample, monkeypatch, withdrawal
):
    instance, _ = memory_workflow(database, sample)
    value, repo = harness(database, tmp_path, sample, instance.plan)
    memory_id = uuid4()
    memory = activate(sample, value, memory_id, monkeypatch)
    if withdrawal == "memory":
        revoke(sample, memory)
    elif withdrawal == "source":
        sample["store"].revoke_source(
            sample["backend"].browser, sample["context"], sample["source"], "Withdraw source"
        )
    else:
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
                (sample["scope"]["id"],),
            )
    result = value.step({})
    assert result["status"] == "waiting"
    assert result["request"]["category"] in {
        "dependency_changed",
        "permission_changed",
        "workflow_changed",
    }
    assert repo.nodes() == []


def test_source_revoked_after_transform_blocks_unexecuted_descendant(
    database, tmp_path, sample, monkeypatch
):
    instance, _ = memory_workflow(database, sample)
    value, repo = harness(database, tmp_path, sample, instance.plan)
    memory_id = uuid4()
    activate(sample, value, memory_id, monkeypatch)
    assert value.step({})["status"] == "advanced"
    sample["store"].revoke_source(
        sample["backend"].browser, sample["context"], sample["source"], "Withdraw source"
    )
    result = value.step({})
    assert result["status"] == "waiting"
    assert result["request"]["category"] == "dependency_changed"
    assert [row["node_id"] for row in repo.nodes()] == ["remembered"]
    assert repo.nodes()[0]["status"] == "succeeded"


@pytest.mark.parametrize("boundary", ["begin", "dispatch"])
def test_revocation_between_binding_and_tool_dispatch_is_rechecked(
    database, tmp_path, sample, monkeypatch, boundary
):
    instance, _ = memory_workflow(database, sample)
    value, repo = harness(database, tmp_path, sample, instance.plan)
    memory_id = uuid4()
    memory = activate(sample, value, memory_id, monkeypatch)
    assert value.step({})["status"] == "advanced"
    operation = getattr(value.repository, boundary)

    def revoke_after_begin(*args, **kwargs):
        saved = operation(*args, **kwargs)
        revoke(sample, memory)
        return saved

    monkeypatch.setattr(value.repository, boundary, revoke_after_begin)
    result = value.step({})
    assert result["status"] == "waiting"
    row = next(row for row in repo.nodes() if row["node_id"] == "directory")
    assert row["dispatch_attempts"] == (0 if boundary == "begin" else 1)
    assert row["status"] == "waiting"


def operation_counts(database, task_id):
    with database.connect() as conn:
        return tuple(
            conn.execute(
                "SELECT count(*) AS n FROM " + table + " WHERE " + field + "=%s", (task_id,)
            ).fetchone()["n"]
            for table, field in (
                ("app.actions", "task_id"),
                ("business.operations", "agent_task_id"),
                ("business.execution_grants", "task_id"),
            )
        )


@pytest.mark.parametrize("pending", ["unknown", "approval"])
def test_invalid_dependency_only_verifies_original_operation_without_replaying_write(
    database, tmp_path, sample, monkeypatch, pending
):
    task, instance, task_input, definition = memory_write_workflow(database, sample, pending)
    value, repo = harness(
        database,
        tmp_path,
        sample,
        instance.plan,
        task=task,
        input_schema=definition["input_schema"],
        task_input=task_input,
    )
    memory = activate(sample, value, uuid4(), monkeypatch)
    sample["backend"].lose = pending == "unknown"
    first = value.step({})
    assert first["status"] == "waiting"
    original = repo.nodes()[0]
    counts = operation_counts(database, value.task["id"])
    revoke(sample, memory)
    result = value.step({})
    assert result["status"] == "waiting"
    request = result["request"]
    assert request["category"] == "dependency_changed"
    assert request["action_id"] == first["request"]["action_id"]
    assert request["operation_id"] == first["request"]["operation_id"]
    assert request["original_verification"]["status"] == (
        "succeeded" if pending == "unknown" else "awaiting_approval"
    )
    assert request["grants_write_authority"] is False
    if pending == "approval":
        assert request["original_approval_id"] == first["request"]["approval_id"]
        assert "approval_id" not in request
    assert operation_counts(database, value.task["id"]) == counts
    current = repo.nodes()[0]
    assert current["status"] == original["status"]
    assert current["input_digest"] == original["input_digest"]
    assert current["execution_generation"] == original["execution_generation"]


def test_memory_free_plan_does_not_query_memory_eligibility(
    database, tmp_path, sample, monkeypatch
):
    value, _ = harness(database, tmp_path, sample, read_plan())

    def unnecessary_read(*args, **kwargs):
        raise AssertionError("Memory-free execution queried memory eligibility")

    monkeypatch.setattr(value.memory_control, "check_dependencies", unnecessary_read)
    assert value.step({})["status"] == "advanced"


@pytest.fixture
def workflow_sample(database):
    return workflow_cases.sample.__wrapped__(database)


def test_workflow_revocation_pauses_remaining_nodes_without_replanning(
    database, tmp_path, workflow_sample
):
    sample = workflow_sample
    published = workflow_cases.publish(sample)
    task = workflow_cases.task(sample)
    instance = sample["store"].instantiate(
        sample["backend"].identity,
        task["id"],
        published["workflow_id"],
        published["version"],
        {"instruction": task["authorization_snapshot"]["instruction"]},
    )
    task = database.claim(task["id"], "dependency-boundary", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(instance.plan)
    value = ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=sample["backend"],
    )
    assert value.step({})["status"] == "advanced"
    before = repo.nodes()
    sample["store"].revoke(
        sample["owner"], published["workflow_id"], published["version"], "Withdraw workflow"
    )
    result = value.step({})
    assert result["status"] == "waiting" and result["request"]["category"] == "workflow_changed"
    assert repo.nodes() == before
    assert value.planner is None
    assert (
        sample["store"].instance(sample["backend"].identity, task["id"]).plan.digest
        == instance.plan.digest
    )


@pytest.mark.parametrize("pending", ["unknown", "approved_ready"])
def test_workflow_revocation_still_verifies_original_operation_without_commit(
    database, tmp_path, workflow_sample, pending
):
    from evals.deterministic.test_business_workflow_contracts import report

    sample = workflow_sample
    backend = sample["backend"]
    scope = sample["scope"]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    params = {
        "title": "Conditional withdrawal boundary",
        "category_id": backend.read("directory", {})["categories"][0]["id"],
    }
    definition = sample["definition"]
    definition["workflow_id"], definition["goal_id"] = "create-ticket", "create-ticket"
    definition["plan"] = create_plan(spec, params)
    definition["plan"]["nodes"][0]["inputs"] = {
        key: reference("task_input", path=[key]) for key in params
    }
    definition["input_schema"]["properties"].update({key: {"type": "string"} for key in params})
    definition["input_schema"]["required"] += list(params)
    sample["store"].register(sample["owner"], definition)
    evidence = report(definition)
    evidence["covered_nodes"], evidence["covered_edges"] = ["create", "done"], [["create", "done"]]
    sample["store"].validate(sample["owner"], "create-ticket", "1", evidence, digest(evidence))
    sample["store"].publish(sample["owner"], "create-ticket", "1")
    instruction = (
        json.dumps({"command": "create", "params": params})
        if pending == "unknown"
        else "请处理这条请求"
    )
    task = workflow_cases.task(sample, instruction=instruction)
    inputs = {"instruction": instruction, **params}
    instance = sample["store"].instantiate(
        backend.identity, task["id"], "create-ticket", "1", inputs
    )
    task = database.claim(task["id"], "dependency-boundary", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(instance.plan, input_schema=definition["input_schema"], task_input=inputs)
    value = ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=backend,
    )
    backend.lose = pending == "unknown"
    first = value.step({})
    assert first["status"] == "waiting"
    if pending == "approved_ready":
        operation = backend.query_operation(first["request"]["operation_id"])
        backend.operations.reply(
            backend.browser, operation["operation_id"], operation["input_hash"], "approve"
        )
    counts = operation_counts(database, task["id"])
    sample["store"].revoke(sample["owner"], "create-ticket", "1", "Withdraw workflow")
    result = value.step({})
    request = result["request"]
    assert result["status"] == "waiting" and request["category"] == "workflow_changed"
    assert request["operation_id"] == first["request"]["operation_id"]
    assert request["original_verification"]["status"] == (
        "succeeded" if pending == "unknown" else "unknown"
    )
    assert operation_counts(database, task["id"]) == counts
    assert backend.writes == (1 if pending == "unknown" else 0)
    assert repo.nodes()[0]["status"] == ("unknown" if pending == "unknown" else "waiting")
    assert value.planner is None


def test_read_only_verifier_rejects_foreign_operation_binding_without_new_authority(
    database, tmp_path, sample, monkeypatch
):
    from decagent.business.mcp_client import MCPProtocolError

    backend = sample["backend"]
    params = {
        "title": "Immutable original operation",
        "category_id": backend.read("directory", {})["categories"][0]["id"],
    }
    spec = PluginRegistry(database).get(sample["scope"]["id"], sample["scope"]["manifest_digest"])
    value, repo = harness(
        database,
        tmp_path,
        sample,
        create_plan(spec, params),
        instruction=json.dumps({"command": "create", "params": params}),
    )
    backend.lose = True
    assert value.step({})["status"] == "waiting"
    with database.connect() as conn:
        action = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (value.task["id"],)
        ).fetchone()
    counts = operation_counts(database, value.task["id"])
    query = backend.query_operation

    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only verification attempted to authorize a new write")

    monkeypatch.setattr(backend, "prepare", forbidden)
    monkeypatch.setattr(backend, "commit", forbidden)
    monkeypatch.setattr(value.executor, "_grant", forbidden)
    for field, replacement in (
        ("input_hash", "a" * 64),
        ("request_id", "different-request"),
        ("employee_id", str(uuid4())),
        ("command", "resolve"),
        ("operation_id", "different-operation"),
    ):

        def tampered(operation_id, field=field, replacement=replacement):
            return {**query(operation_id), field: replacement}

        monkeypatch.setattr(backend, "query_operation", tampered)
        result = value.executor.verify_original(
            action["id"], action["digest"], expected_inputs=params
        )
        assert result["status"] == "unknown" and "data" not in result
    with pytest.raises(MCPProtocolError):
        value.executor.verify_original(action["id"], action["digest"], expected_inputs={})
    with pytest.raises(MCPProtocolError):
        value.executor.verify_original(action["id"], "a" * 64, expected_inputs=params)
    with pytest.raises(ValueError):
        value.executor.verify_original(uuid4(), action["digest"], expected_inputs=params)
    monkeypatch.setattr(backend, "query_operation", query)
    assert (
        value.executor.verify_original(action["id"], action["digest"], expected_inputs=params)[
            "status"
        ]
        == "succeeded"
    )
    monkeypatch.setattr(
        backend,
        "query_operation",
        lambda operation_id: {**query(operation_id), "input_hash": "a" * 64},
    )
    assert value.executor.verify_original(action["id"], action["digest"])["status"] == "unknown"
    monkeypatch.setattr(backend, "query_operation", lambda operation_id: [])
    assert value.executor.verify_original(action["id"], action["digest"])["status"] == "unknown"
    monkeypatch.setattr(
        backend,
        "query_operation",
        lambda operation_id: {
            key: data for key, data in query(operation_id).items() if key != "state"
        },
    )
    assert value.executor.verify_original(action["id"], action["digest"])["status"] == "unknown"
    with database.connect() as conn:
        assert (
            conn.execute("SELECT status FROM app.actions WHERE id=%s", (action["id"],)).fetchone()[
                "status"
            ]
            == "succeeded"
        )
    assert operation_counts(database, value.task["id"]) == counts
    assert repo.nodes()[0]["status"] == "unknown"


@pytest.mark.parametrize("stage", ["scope_clarification", "pending_replan"])
def test_workflow_boundary_also_blocks_special_semantic_replan_paths(
    database, tmp_path, sample, monkeypatch, stage
):
    from decagent.business.plans.runtime import NodePending
    from evals.integration.test_business_conditional_batches import Adapter
    from evals.integration.test_business_conditional_services import choice_plan

    spec = PluginRegistry(database).get(sample["scope"]["id"], sample["scope"]["manifest_digest"])
    value, repo = harness(database, tmp_path, sample, choice_plan(spec))
    value.adapter = Adapter()
    assert value.step({})["status"] == "advanced"
    if stage == "scope_clarification":
        monkeypatch.setattr("decagent.business.plans.batching.MAX_INPUT_TOKENS", 100)
        waiting = value.step({})
        assert waiting["request"]["category"] == "scope_limit"
    else:
        repo.save_context(
            {
                "pending_replan": {
                    "parent_digest": repo.current().digest,
                    "parent_revision": 1,
                    "node_id": "directory",
                }
            }
        )

    def withdrawn():
        raise NodePending(
            {"kind": "reconciliation", "category": "workflow_changed", "reason": "workflow_revoked"}
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("Withdrawn workflow consumed a clarification or replanned")

    class NeverPlanner:
        plan = staticmethod(forbidden)

    monkeypatch.setattr(value, "_workflow_boundary", withdrawn)
    monkeypatch.setattr(value, "_decide", forbidden)
    value.planner = NeverPlanner()
    result = value.step({})
    assert result["status"] == "waiting" and result["request"]["category"] == "workflow_changed"


def test_binding_epoch_drift_recovers_original_unknown_write_without_new_dispatch(
    database, tmp_path, sample
):
    backend = sample["backend"]
    params = {
        "title": "Frozen directory binding",
        "category_id": backend.read("directory", {})["categories"][0]["id"],
    }
    spec = PluginRegistry(database).get(sample["scope"]["id"], sample["scope"]["manifest_digest"])
    plan = create_plan(spec, params)
    plan["entry"] = "directory"
    plan["nodes"].insert(0, read_plan()["nodes"][0])
    plan["nodes"][0]["next"] = ["create"]
    plan["nodes"][1]["inputs"]["category_id"] = reference(
        "node_output",
        node_id="directory",
        path=["categories", 0, "id"],
    )
    value, repo = harness(
        database,
        tmp_path,
        sample,
        plan,
        instruction=json.dumps({"command": "create", "params": params}),
    )
    assert value.step({})["status"] == "advanced"
    backend.lose = True
    original = value.step({})
    assert original["status"] == "waiting"
    counts = operation_counts(database, value.task["id"])
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (sample["scope"]["id"],),
        )
    waiting = value.step({})
    assert (
        waiting["status"] == "waiting"
        and waiting["request"]["category"] == "binding_source_changed"
    )
    assert waiting["request"]["operation_id"] == original["request"]["operation_id"]
    assert waiting["request"]["original_verification"]["status"] == "succeeded"
    assert operation_counts(database, value.task["id"]) == counts
    assert next(row for row in repo.nodes() if row["node_id"] == "create")["status"] == "unknown"

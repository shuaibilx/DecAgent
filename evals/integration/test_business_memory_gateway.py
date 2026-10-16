"""Authenticated employee memory management uses real disposable PostgreSQL cases."""

import importlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.business.memory import BusinessMemoryStore
from decagent.business.memory_control import BusinessMemoryControl
from decagent.business.plans.bindings import BindingContext, BindingMissing, resolve_binding
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.workflow_registry import WorkflowRegistry
from decagent.config import Settings
from decagent.decisions.contracts import digest
from decagent.gateway import create_app
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import node, reference
from evals.integration import test_business_workflow_registry as workflow_cases
from evals.integration.business_support import FixtureTickets
from evals.integration.test_business_memory import confirm, experience, propose, receipt, rule
from evals.integration.test_business_memory import sample as sample
from evals.integration.test_business_semantic_gateway import client_for

pytestmark = pytest.mark.integration
BASE = "/api/v2/business/memories"


@pytest.fixture
def workflow_sample(database):
    return workflow_cases.sample.__wrapped__(database)


@pytest.fixture
def api(database, tmp_path, sample):
    task = database.claim(sample["task"]["id"], "memory-api", seconds=120)
    repo = PlanRepository(database, task["id"], "memory-api", task["lease_generation"])
    plan = repo.publish(read_plan())
    sample["context"] = sample["store"].context(
        sample["backend"].identity,
        sample["scope"]["id"],
        question_fingerprint=QuestionRegistry.from_spec(repo.spec).fingerprint,
        plan_fingerprint=plan.digest,
    )
    return client_for(database, tmp_path, sample), sample, repo


def mutation(client, path, body, key="memory-change"):
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def published_memory_workflow(
    database,
    sample,
    *,
    memory_kind="confirmed_preference",
    subject="response_language",
):
    value = workflow_cases.definition(str(sample["scope"]["id"]))
    value["scope_revision"] = sample["scope"]["revision"]
    plan = read_plan()
    binding = reference("verified_memory", path=["value"])
    binding.update(memory_kind=memory_kind, subject=subject)
    plan["entry"] = "remembered"
    plan["nodes"].insert(
        0,
        node(
            "remembered",
            "transform",
            transform={"transform_id": "identity", "version": "1"},
            inputs={"value": binding},
            next=["directory"],
        ),
    )
    plan["nodes"][1]["depends_on"] = ["remembered"]
    value["plan"] = plan
    registry = WorkflowRegistry(database)
    registry.register(sample["owner"], value)
    evidence = workflow_cases.report(value)
    evidence.update(
        report_id="memory-slot-" + uuid4().hex,
        covered_nodes=["remembered", "directory", "done"],
        covered_edges=[["remembered", "directory"], ["directory", "done"]],
    )
    registry.validate(
        sample["owner"], value["workflow_id"], value["version"], evidence, digest(evidence)
    )
    registry.publish(sample["owner"], value["workflow_id"], value["version"])
    task_input = {"instruction": sample["task"]["authorization_snapshot"]["instruction"]}
    instance = registry.instantiate(
        sample["backend"].identity,
        sample["task"]["id"],
        value["workflow_id"],
        value["version"],
        task_input,
    )
    sample["context"] = BusinessMemoryControl(database).context(
        sample["backend"].identity, sample["task"]["id"]
    )
    return instance, binding


def body(sample, memory, **updates):
    return {
        "task_id": str(sample["task"]["id"]),
        "expected_digest": memory["content_digest"],
        **updates,
    }


def test_history_is_own_inspection_and_only_server_context_can_mark_eligible(api):
    client, sample, _ = api
    active = confirm(sample, propose(sample))
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == active["id"]
    assert response.json()[0]["active"] is False and response.json()[0]["verified"] is False
    current = client.get(BASE, params={"task_id": str(sample["task"]["id"])})
    assert current.status_code == 200 and current.json()[0]["active"] is True
    assert current.json()[0]["grants_write_authority"] is False


def test_confirmation_is_authenticated_csrf_protected_and_idempotent(api, database, tmp_path):
    client, sample, _ = api
    pending = propose(sample)
    path, payload = BASE + "/" + pending["id"] + "/confirm", body(sample, pending)
    assert client.post(path, json=payload).status_code == 422
    assert client.post(path, json=payload, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert (
        client.post(path, json=payload, headers={"Origin": "http://attacker.invalid"}).status_code
        == 403
    )
    anonymous = TestClient(create_app(database, Settings(data_dir=tmp_path)))
    assert anonymous.get(BASE).status_code == 401
    assert anonymous.get(BASE, headers={"Authorization": "Bearer owner-token"}).status_code == 401
    assert (
        client.post(path, json=payload, headers={"Authorization": "Bearer mcp-token"}).status_code
        == 401
    )
    result = mutation(client, path, payload)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "active"
    assert mutation(client, path, payload).json() == result.json()
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.business_memory_audit "
                "WHERE event='preference_confirmed'"
            ).fetchone()["n"]
            == 1
        )


def test_foreign_employee_cannot_inspect_correct_confirm_or_revoke(api, database, tmp_path):
    client, sample, _ = api
    pending = propose(sample)
    other = FixtureTickets(database, login="employee-facilities")
    foreign = client_for(database, tmp_path, sample, other.identity)
    assert foreign.get(BASE).json() == []
    assert foreign.get(BASE + "/" + pending["id"]).status_code == 404
    for operation, changes in (
        ("confirm", {}),
        ("correct", {"value": "English"}),
        ("revoke", {"reason": "Reject foreign change"}),
    ):
        result = mutation(
            foreign,
            BASE + "/" + pending["id"] + "/" + operation,
            body(sample, pending, **changes),
            operation,
        )
        assert result.status_code == 404, result.text
    assert client.get(BASE + "/" + pending["id"]).status_code == 200


@pytest.mark.parametrize("field,value", [("scope_revision", 999), ("plan_fingerprint", "a" * 64)])
def test_client_cannot_declare_current_epochs_or_fingerprints(api, field, value):
    client, sample, _ = api
    pending = propose(sample)
    result = mutation(
        client,
        BASE + "/" + pending["id"] + "/confirm",
        body(sample, pending, **{field: value}),
    )
    assert result.status_code == 422
    assert sample["store"].get(sample["backend"].browser, pending["id"])["status"] == (
        "pending_confirmation"
    )


def test_correction_is_new_pending_same_subject_with_old_and_new_evidence(api, database):
    client, sample, _ = api
    old = confirm(sample, propose(sample))
    path = BASE + "/" + old["id"] + "/correct"
    payload = body(sample, old, value="English")
    result = mutation(client, path, payload, "correction")
    assert result.status_code == 201, result.text
    replacement = result.json()
    assert replacement["id"] != old["id"] and replacement["supersedes"] == old["id"]
    assert replacement["subject"] == old["subject"]
    assert replacement["status"] == "pending_confirmation" and not replacement["active"]
    assert {item["key"] for item in old["evidence_refs"]} < {
        item["key"] for item in replacement["evidence_refs"]
    }
    assert mutation(client, path, payload, "correction").json() == replacement
    assert sample["store"].get(sample["backend"].browser, old["id"])["status"] == "active"
    activated = mutation(
        client,
        BASE + "/" + replacement["id"] + "/confirm",
        body(sample, replacement),
        "replacement-confirmation",
    )
    assert activated.status_code == 200, activated.text
    assert sample["store"].get(sample["backend"].browser, old["id"])["status"] == "superseded"
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.business_memories").fetchone()["n"] == 2


def test_correction_and_idempotency_response_rollback_together(api, database, monkeypatch):
    client, sample, _ = api
    old = confirm(sample, propose(sample))
    original = BusinessMemoryStore._audit

    def fail_after_write(self, conn, event, actor, payload, memory_id=None):
        original(self, conn, event, actor, payload, memory_id)
        if event == "preference_proposed":
            raise ValueError("Injected transactional failure")

    monkeypatch.setattr(BusinessMemoryStore, "_audit", fail_after_write)
    result = mutation(
        client,
        BASE + "/" + old["id"] + "/correct",
        body(sample, old, value="English"),
        "atomic-correction",
    )
    assert result.status_code == 422
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.business_memories").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.idempotency_keys WHERE key='atomic-correction'"
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.events "
                "WHERE kind='business.memory_correction_proposed'"
            ).fetchone()["n"]
            == 0
        )


def test_source_revocation_is_idempotent_and_removes_prompt_eligibility(api):
    client, sample, _ = api
    active = confirm(sample, propose(sample))
    payload = {
        "task_id": str(sample["task"]["id"]),
        "reference": sample["source"],
        "reason": "Original source withdrawn",
    }
    result = mutation(client, BASE + "/sources/revoke", payload, "source-withdrawal")
    assert result.status_code == 200, result.text
    assert mutation(client, BASE + "/sources/revoke", payload, "source-withdrawal").json() == (
        result.json()
    )
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []
    view = client.get(BASE + "/" + active["id"], params={"task_id": payload["task_id"]})
    assert view.status_code == 200 and not view.json()["active"]


@pytest.mark.parametrize("change", ["scope", "plan", "ttl", "source"])
def test_server_derived_scope_and_plan_versions_reject_stale_confirmation(
    api, database, change, monkeypatch
):
    client, sample, repo = api
    pending = propose(sample)
    if change == "scope":
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
                (sample["scope"]["id"],),
            )
    elif change == "plan":
        plan = repo.current()
        value = plan.to_mapping()
        value.pop("content_digest")
        value["revision"] = 2
        value["goal"]["description"] = "Revised task context"
        repo.publish(value, parent_digest=plan.digest)
    elif change == "ttl":
        from decagent.business.memory_control import BusinessMemoryControl

        original_store = BusinessMemoryControl._store

        def expired_store(control, conn):
            store = original_store(control, conn)
            store.clock = lambda: pending["expires_at"]
            return store

        monkeypatch.setattr(BusinessMemoryControl, "_store", expired_store)
    else:
        sample["store"].revoke_source(
            sample["backend"].browser, sample["context"], sample["source"], "Withdraw source"
        )
    result = mutation(client, BASE + "/" + pending["id"] + "/confirm", body(sample, pending))
    assert result.status_code == 409 and result.json()["code"] in {"stale", "source_revoked"}
    history = client.get(BASE, params={"task_id": str(sample["task"]["id"])})
    assert history.status_code == 200 and not history.json()[0]["active"]


def test_pending_bindings_revalidate_revoked_source_and_preserve_completed_history(
    database, tmp_path, sample, monkeypatch
):
    instance, binding = published_memory_workflow(database, sample)
    task = database.claim(sample["task"]["id"], "memory-dependency", seconds=120)
    assert task["id"] == sample["task"]["id"]
    active = confirm(sample, propose(sample))
    control = BusinessMemoryControl(database)
    current = control.check_dependencies(sample["backend"].identity, task["id"])
    slot = "confirmed_preference:response_language"
    assert current["valid"] and current["memories"][slot]["active"]
    bound = instance.binding_context
    bound.memories = current["memories"]
    resolved = resolve_binding(binding, bound)
    assert resolved.value == "中文" and resolved.provenance["memory_id"] == active["id"]
    sample["store"].revoke_source(
        sample["backend"].browser, sample["context"], sample["source"], "Source withdrawn"
    )
    invalid = control.check_dependencies(sample["backend"].identity, task["id"])
    assert not invalid["valid"] and invalid["status"] == "revalidation_required"
    assert invalid["memories"] == {}
    assert {item["node_id"] for item in invalid["invalid_dependencies"]} == {
        "remembered",
        "directory",
        "done",
    }
    assert (
        WorkflowRegistry(database).instance(sample["backend"].identity, task["id"]).plan.digest
        == instance.plan.digest
    )
    assert "中文" not in str(invalid)
    bound = BindingContext(
        {},
        {},
        {},
        invalid["memories"],
        str(sample["backend"].identity.employee_id),
        sample["context"].scope_revision,
        sample["context"].account_revision,
    )
    with pytest.raises(BindingMissing):
        resolve_binding(binding, bound)


def test_runtime_clears_cached_memory_before_each_dependent_dispatch(database, tmp_path, sample):
    from decagent.business.conditional_services import ConditionalBusinessServices
    from decagent.business.plans.runtime import NodePending
    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig

    instance, binding = published_memory_workflow(database, sample)
    confirm(sample, propose(sample))
    task = database.claim(sample["task"]["id"], "memory-dispatch", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(
        instance.plan,
        input_schema=workflow_cases.definition(str(sample["scope"]["id"]))["input_schema"],
        task_input=instance.task_input,
    )
    service = ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=sample["backend"],
    )
    bound = instance.binding_context
    node = next(item for item in instance.plan.nodes if item["id"] == "remembered")
    service._before_node(node, bound)
    assert bound.memories["confirmed_preference:response_language"]["active"]
    sample["store"].revoke_source(
        sample["backend"].browser, sample["context"], sample["source"], "Withdraw before dispatch"
    )
    with pytest.raises(NodePending):
        service._before_node(node, bound)
    assert bound.memories == {}


def test_revocation_requires_exact_digest_and_never_deletes_history(api):
    client, sample, _ = api
    active = confirm(sample, propose(sample))
    path = BASE + "/" + active["id"] + "/revoke"
    result = mutation(
        client,
        path,
        body(sample, active, expected_digest="a" * 64, reason="Withdraw memory"),
        "wrong-digest",
    )
    assert result.status_code == 409
    revoked = mutation(client, path, body(sample, active, reason="Withdraw memory"), "revoke")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert not revoked.json()["active"] and not revoked.json()["verified"]
    assert client.get(BASE + "/" + active["id"]).json()["evidence_refs"] == active["evidence_refs"]
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []


def test_warm_workflow_context_uses_stable_definition_and_current_registry(
    database, tmp_path, workflow_sample
):
    published = workflow_cases.publish(workflow_sample)
    task = workflow_cases.task(workflow_sample)
    instance = workflow_sample["store"].instantiate(
        workflow_sample["backend"].identity,
        task["id"],
        published["workflow_id"],
        published["version"],
        {"instruction": "Read directory"},
    )
    module = importlib.import_module("decagent.gateway.business_memory")
    ctx = module.BusinessMemoryControl(database).context(
        workflow_sample["backend"].identity, task["id"]
    )
    assert ctx.plan_fingerprint == published["definition_digest"]
    assert ctx.plan_fingerprint != instance.plan.digest
    store = BusinessMemoryStore(database)
    observed = database.event(task["id"], "business.preference_observed", {"value": "中文"})
    pending = store.propose_preference(
        workflow_sample["backend"].identity,
        ctx,
        subject="response_language",
        value="中文",
        evidence_refs=[{"kind": "task_event", "task_id": str(task["id"]), "seq": observed["seq"]}],
    )
    client = client_for(database, tmp_path, workflow_sample)
    result = mutation(
        client,
        BASE + "/" + pending["id"] + "/confirm",
        {"task_id": str(task["id"]), "expected_digest": pending["content_digest"]},
    )
    assert result.status_code == 200 and result.json()["active"]


def test_memory_bindings_are_rejected_outside_published_workflows(database, sample):
    from decagent.business.access import AccessError
    from decagent.business.memory_control import BusinessMemoryControl
    from evals.deterministic.test_business_plan_compiler import branching_plan

    plan = branching_plan()
    plan["nodes"][0]["cases"][0]["next"] = ["unused_memory"]
    plan["nodes"].insert(
        1,
        node(
            "unused_memory",
            "transform",
            transform={"transform_id": "identity", "version": "1"},
            inputs={"value": reference("verified_memory", path=["value"])},
            next=["read_ticket"],
        ),
    )
    next(n for n in plan["nodes"] if n["id"] == "read_ticket")["depends_on"] = ["unused_memory"]
    held = database.claim(sample["task"]["id"], "memory-inactive", seconds=120)
    repo = PlanRepository(database, held["id"], "memory-inactive", held["lease_generation"])
    repo.publish(
        plan,
        input_schema={
            "type": "object",
            "properties": {"instruction": {"type": "string"}, "ticket_id": {"type": "string"}},
        },
    )
    with pytest.raises(AccessError, match="published workflow"):
        BusinessMemoryControl(database).check_dependencies(sample["backend"].identity, held["id"])


def test_published_memory_slot_without_value_pauses_before_any_binding(database, sample):
    _, _ = published_memory_workflow(database, sample)
    checked = BusinessMemoryControl(database).check_dependencies(
        sample["backend"].identity, sample["task"]["id"]
    )
    assert not checked["valid"] and checked["requires_replan"]
    assert checked["memories"] == {}
    assert {item["reason"] for item in checked["invalid_dependencies"]} == {"memory_unavailable"}


def test_expired_memory_slot_is_not_resolved_for_a_published_workflow(
    database, sample, monkeypatch
):
    published_memory_workflow(database, sample)
    active = confirm(sample, propose(sample))
    original_store = BusinessMemoryControl._store

    def expired_store(control, conn):
        store = original_store(control, conn)
        store.clock = lambda: active["expires_at"]
        return store

    monkeypatch.setattr(BusinessMemoryControl, "_store", expired_store)
    checked = BusinessMemoryControl(database).check_dependencies(
        sample["backend"].identity, sample["task"]["id"]
    )
    assert not checked["valid"] and checked["memories"] == {}
    assert all(item["reason"] == "memory_unavailable" for item in checked["invalid_dependencies"])


def test_scope_revision_change_invalidates_a_published_memory_slot(database, tmp_path, sample):
    from decagent.business.conditional_services import ConditionalBusinessServices
    from decagent.business.plans.runtime import NodePending
    from decagent.runtime.config import RuntimeConfig

    instance, _ = published_memory_workflow(database, sample)
    confirm(sample, propose(sample))
    task = database.claim(sample["task"]["id"], "memory-scope-change", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(
        instance.plan,
        input_schema=workflow_cases.definition(str(sample["scope"]["id"]))["input_schema"],
        task_input=instance.task_input,
    )
    service = ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=sample["backend"],
    )
    node = next(item for item in instance.plan.nodes if item["id"] == "remembered")
    bound = instance.binding_context
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (sample["scope"]["id"],),
        )

    with pytest.raises(NodePending) as pending:
        service._before_node(node, bound)
    assert pending.value.request["category"] in {"workflow_changed", "permission_changed"}
    assert pending.value.request["requires_replan"]
    assert bound.memories == {}


def test_ambiguous_experience_slot_pauses_instead_of_picking_one(database, sample):
    published_memory_workflow(
        database,
        sample,
        memory_kind="verified_workflow_experience",
        subject="workflow:create",
    )
    first = receipt(sample)
    rule(sample, first)
    experience(sample, first)
    experience(sample, receipt(sample))
    checked = BusinessMemoryControl(database).check_dependencies(
        sample["backend"].identity, sample["task"]["id"]
    )
    assert not checked["valid"] and checked["memories"] == {}
    assert {item["reason"] for item in checked["invalid_dependencies"]} == {"memory_ambiguous"}

"""Employee trace views rebuild durable facts without taking an execution lease."""

from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings
from decagent.gateway import create_app
from decagent.storage.usage import UsageLedger
from evals.integration.business_support import FixtureTickets
from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks
from evals.integration.test_business_context import memory_for, save_directory
from evals.integration.test_business_semantic_gateway import client_for

pytestmark = pytest.mark.integration


@pytest.fixture
def trace_case(database, tmp_path, mixed_business_tasks):
    sample = mixed_business_tasks
    repo, _ = memory_for(database, sample)
    sample["task"] = sample["tasks"]["business-3"]
    client = client_for(database, tmp_path, sample)
    return sample, repo, client


def endpoint(sample, suffix, version="business-3"):
    return f"/api/v2/business/tasks/{sample['tasks'][version]['id']}/{suffix}"


def test_plan_view_recovers_actual_path_without_a_worker_lease_or_cache(trace_case, database):
    sample, repo, client = trace_case
    observed = save_directory(repo, sample)
    database.event(repo.task_id, "task.waiting", {"kind": "clarification", "reason": "old_wait"})
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s",
            (repo.task_id,),
        )
        before = conn.execute(
            "SELECT * FROM app.business_plan_heads WHERE task_id=%s", (repo.task_id,)
        ).fetchone()
    response = client.get(endpoint(sample, "plan"))
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["plan"]["revision"] == 1
    assert value["completed_nodes"] == ["directory"]
    assert value["ready_nodes"] == ["done"]
    assert value["observations"][0]["value"] == observed
    assert value["task"]["state"] == "running"
    assert value["waiting"] is None
    with database.connect() as conn:
        after = conn.execute(
            "SELECT * FROM app.business_plan_heads WHERE task_id=%s", (repo.task_id,)
        ).fetchone()
        assert after == before


@pytest.mark.parametrize("suffix", ["plan", "trace", "export"])
def test_trace_requires_original_browser_and_rejects_owner_or_mcp(
    trace_case, database, tmp_path, suffix
):
    sample, _, client = trace_case
    path = endpoint(sample, suffix)
    anonymous = TestClient(create_app(database, Settings(data_dir=tmp_path)))
    for token in (None, "owner-token", "mcp-token"):
        assert (
            anonymous.get(
                path, headers={"Authorization": "Bearer " + token} if token else {}
            ).status_code
            == 401
        )
    other = FixtureTickets(database, login="employee-facilities")
    assert client_for(database, tmp_path, sample, other.identity).get(path).status_code == 404
    assert client.get(path).status_code == 200
    # A caller cannot use the direct control with an MCP identity either.
    from decagent.business.access import AccessError
    from decagent.gateway.business_trace import BusinessTraceControl

    with pytest.raises(AccessError):
        BusinessTraceControl(database).snapshot(sample["backend"].identity, sample["task"]["id"])


def test_legacy_is_explicit_and_does_not_fabricate_a_conditional_plan(trace_case):
    sample, _, client = trace_case
    result = client.get(endpoint(sample, "plan", "business-2"))
    assert result.status_code == 200
    assert result.json()["legacy"] is True
    assert result.json()["plan"] is None and result.json()["nodes"] == []


def test_usage_counts_requests_keeps_unknown_holds_and_target_separate(trace_case, database):
    sample, repo, client = trace_case
    ledger = UsageLedger(database, repo.task_id)
    not_sent = ledger.reserve_model(
        str(uuid4()),
        0,
        "jev",
        40,
        Decimal("0.04"),
        model_family="jev",
        purpose="decide",
        plan_revision=1,
        node_ids=["directory"],
    )
    ledger.mark_not_sent(not_sent, reason="local_failure")
    reserved = ledger.reserve_model(
        str(uuid4()),
        0,
        "jev",
        40,
        Decimal("0.04"),
        model_family="jev",
        purpose="decide",
        plan_revision=1,
        node_ids=["directory"],
    )
    for index in range(4):
        receipt = ledger.reserve_model(
            str(uuid4()),
            0,
            "primary",
            100,
            Decimal("0.1"),
            model_family="primary",
            purpose="plan",
            plan_revision=1,
            exception_reason="invalid_format" if index == 3 else None,
        )
        ledger.mark_dispatched(receipt)
        if index < 3:
            ledger.settle_model(receipt, 30, Decimal("0.03"), response_state="responded")
    ledger.settle_model(receipt, None, None, response_state="unknown")
    result = client.get(endpoint(sample, "plan"))
    assert result.status_code == 200, result.text
    usage = result.json()["usage"]
    assert usage["actual_requests"] == 4
    assert usage["families"]["primary"]["actual_requests"] == 4
    assert usage["families"]["jev"]["actual_requests"] == 0
    assert usage["limits"]["primary_target"] == 3 and usage["limits"]["max_primary_calls"] == 6
    unknown = next(row for row in usage["requests"] if row["id"] == str(receipt))
    assert unknown["actual_tokens"] is None and unknown["actual_cost"] is None
    assert unknown["held_tokens"] == 100 and unknown["held_cost"] == "0.1"
    assert unknown["exception_reason"] == "invalid_format"
    assert usage["unknown_cost"] is True and usage["actual_cost"] is None
    assert (
        next(row for row in usage["requests"] if row["id"] == str(reserved))["dispatch_state"]
        == "reserved"
    )


def test_export_correlates_events_usage_original_operation_without_raw_payloads(
    trace_case, database
):
    sample, repo, client = trace_case
    action = database.plan_action(
        repo.task_id,
        "trace-unknown",
        "a" * 64,
        {"command": "create", "params": {}, "operation_id": "original-request"},
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.actions SET status='unknown',server_operation_id=%s WHERE id=%s",
            (sample["operation"]["operation_id"], action["id"]),
        )
    ledger = UsageLedger(database, repo.task_id)
    request_id = ledger.reserve_model(
        str(uuid4()),
        0,
        "primary",
        100,
        Decimal("0.1"),
        model_family="primary",
        purpose="replan",
        plan_revision=2,
    )
    ledger.mark_dispatched(request_id)
    database.event(
        repo.task_id,
        "business.planner_failed",
        {
            "request_key": "a" * 64,
            "target_revision": 2,
            "parent_digest": repo.current().digest,
            "purpose": "replan",
            "outcome": "unknown",
            "reason": {"raw_api_payload": "nested-transport-private"},
            "usage": [{"id": str(request_id)}],
            "headers": {"Authorization": "Bearer secret-access-123"},
            "raw_response": {"private": "not-for-export"},
            "message": "Operation completed successfully",
        },
    )
    response = client.get(endpoint(sample, "export"))
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["Content-Disposition"]
    value = response.json()
    event = next(row for row in value["timeline"] if row["kind"] == "business.planner_failed")
    assert event["links"]["model_request_ids"] == [str(request_id)]
    assert event["links"]["plan_revision"] == 2
    assert event["details"]["outcome"] == "unknown"
    assert value["snapshot"]["actions"][0]["verified"] is False
    assert value["snapshot"]["actions"][0]["operation_id"] == sample["operation"]["operation_id"]
    assert all(
        secret not in response.text
        for secret in (
            "secret-access-123",
            "not-for-export",
            "nested-transport-private",
            "Authorization",
            "raw_response",
            "credential_ref",
            "token_hash",
        )
    )
    assert client.get(endpoint(sample, "trace")).json()["timeline"] == value["timeline"]


def test_scope_epoch_change_hides_old_business_values_but_keeps_references(trace_case, database):
    sample, repo, client = trace_case
    save_directory(repo, sample, extra={"sentinel": "old-scope-private-value"})
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (sample["task"]["business_scope_id"],),
        )
    response = client.get(endpoint(sample, "export"))
    assert response.status_code == 200, response.text
    assert "old-scope-private-value" not in response.text
    observation = response.json()["snapshot"]["observations"][0]
    assert observation["current"] is False and "value" not in observation
    assert observation["source_ref"]["kind"] == "node"


def test_readonly_projection_adapter_has_no_execution_mutators(trace_case):
    from decagent.gateway.business_trace import ReadOnlyPlanSource

    assert not any(
        hasattr(ReadOnlyPlanSource, name)
        for name in ("publish", "begin", "finish", "start_batch", "apply_correction")
    )


def test_export_preserves_long_timeline_with_no_silent_redactor_truncation(trace_case, database):
    sample, repo, client = trace_case
    for index in range(105):
        database.event(
            repo.task_id, "business.trace_fixture", {"status": "recorded", "ordinal": index}
        )
    result = client.get(endpoint(sample, "export"))
    assert result.status_code == 200
    entries = [
        item for item in result.json()["timeline"] if item["kind"] == "business.trace_fixture"
    ]
    assert len(entries) == 105


def test_decision_trace_links_actual_model_requests_and_policy_node(trace_case, database):
    from evals.integration.test_business_conditional_batches import setup

    sample, _, client = trace_case
    # A separate task allows the real runner to publish its decision plan.
    task = sample["tasks"]["business-3"]
    task = database.create_business_task(
        task["business_scope_id"],
        task["authorization_snapshot"],
        graph_version="business-3",
        employee_id=task["employee_id"],
        credential_ref=task["credential_ref"],
    )
    sample["tasks"]["business-3"] = task
    runtime, _, _, repo, _ = setup(database, sample)
    runtime.step()
    runtime.step()
    ledger = UsageLedger(database, repo.task_id)
    request_id = ledger.reserve_model(
        str(uuid4()),
        0,
        "jev",
        100,
        Decimal("0.1"),
        model_family="jev",
        purpose="decide",
        plan_revision=1,
        node_ids=["decision_0", "decision_1"],
    )
    ledger.mark_dispatched(request_id)
    result = client.get(endpoint(sample, "trace"))
    assert result.status_code == 200
    batch = next(
        item for item in result.json()["timeline"] if item["kind"] == "business.decision_batch"
    )
    assert batch["links"]["model_request_ids"] == [str(request_id)]
    node = next(
        item
        for item in result.json()["timeline"]
        if item["kind"] == "node.state" and item["links"]["node_id"] == "decision_0"
    )
    assert node["links"]["policy_id"]


def test_semantic_approval_trace_keeps_original_node_and_binding(database, tmp_path):
    from evals.integration.test_business_interventions import setup, waiting

    sample = setup.__wrapped__(database)()
    pending = waiting(database, sample)
    client = client_for(database, tmp_path, sample)
    result = client.get(f"/api/v2/business/tasks/{sample['task']['id']}/trace")
    assert result.status_code == 200, result.text
    approval = next(item for item in result.json()["timeline"] if item["kind"] == "approval.state")
    assert approval["links"]["node_id"] == "judge"
    assert approval["links"]["binding_digest"] == pending["digest"]
    assert approval["details"]["kind"] == "decision"


def test_transport_objects_in_current_observations_are_never_exported(trace_case):
    sample, repo, client = trace_case
    save_directory(
        repo,
        sample,
        extra={
            "headers": {"x-internal": "private-transport-header"},
            "sdk_object": {"debug": "private-sdk-state"},
            "raw_api_payload": {"debug": "private-provider-payload"},
        },
    )
    result = client.get(endpoint(sample, "export"))
    assert result.status_code == 200, result.text
    assert all(
        value not in result.text
        for value in (
            "private-transport-header",
            "private-sdk-state",
            "private-provider-payload",
        )
    )
    assert result.json()["snapshot"]["observations"][0]["value"]["people"]

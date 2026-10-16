from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings
from decagent.policy import (
    Action,
    CodeOrigin,
    CodeSnapshot,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
    action_digest,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def api(database, tmp_path):
    from decagent.gateway import GatewayCallbacks, create_app, initialize_owner

    token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    contexts = {}
    callbacks = GatewayCallbacks(current_policy_context=lambda task_id: contexts[task_id])
    client = TestClient(create_app(database, Settings(data_dir=tmp_path), callbacks=callbacks))
    client.headers.update({"Authorization": "Bearer " + token})
    return database, client, contexts, tmp_path


def submit(api):
    _, client, _, _ = api
    project = client.post(
        "/api/v1/projects",
        json={"repository": "fixture/gateway"},
        headers={"Idempotency-Key": "project-1"},
    ).json()
    body = {
        "project_id": project["id"],
        "base_ref": "a" * 40,
        "issue": "Fix bounded range",
        "publish_draft_pr": False,
        "budget": {"max_calls": 3, "max_tokens": 1000},
    }
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "task-1"})
    assert response.status_code == 201, response.text
    return project, response.json(), body


def test_intervention_history_is_bounded_ordered_and_owner_scoped(api):
    db, client, _, _ = api
    _, task, _ = submit(api)
    for number in range(55):
        db.event(task["id"], "intervention.checked", {"route": "replan", "number": number})
    view = client.get("/api/v1/tasks/" + task["id"]).json()
    assert "interventions" in view, "Task detail lacks intervention history"
    assert len(view["interventions"]) == 50
    assert [e["payload"]["number"] for e in view["interventions"]] == list(range(5, 55))
    client.headers.pop("Authorization")
    assert client.get("/api/v1/tasks/" + task["id"]).status_code == 401


def test_submission_is_atomic_idempotent_owner_scoped_and_binds_task_authorization(api):
    db, client, _, _ = api
    project, task, body = submit(api)
    duplicate = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "task-1"})
    assert duplicate.status_code == 201 and duplicate.json() == task
    body["issue"] = "Different request"
    assert (
        client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "task-1"}).status_code
        == 409
    )
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.tasks").fetchone()["n"] == 1
    saved = db.get_task(task["id"])
    auth = saved["authorization_snapshot"]
    assert auth["task_id"] == task["id"] and auth["project_id"] == project["id"]
    assert auth["publish_draft"] is False and auth["active"] is True
    assert auth["provenance"]["authenticated"] is True
    assert auth["provenance"]["source"] == "owner"
    assert saved["state"] == "queued"
    assert client.get("/api/v1/tasks/" + task["id"]).json()["state"] == "queued"
    assert client.get("/api/v1/tasks").json()[0]["requested_issue"] == "Fix bounded range"


def test_sse_reconnect_replays_only_later_durable_events_and_does_not_restart(api):
    db, client, _, _ = api
    _, task, _ = submit(api)
    db.event(task["id"], "progress", {"phase": "reproduce"})
    db.event(task["id"], "progress", {"phase": "repair"})
    response = client.get(
        "/api/v1/tasks/" + task["id"] + "/events?follow=false", headers={"Last-Event-ID": "2"}
    )
    assert response.status_code == 200
    assert "id: 3\n" in response.text and "id: 2\n" not in response.text
    assert db.get_task(task["id"])["state"] == "queued"
    assert (
        client.get(
            "/api/v1/tasks/" + task["id"] + "/events?follow=false", headers={"Last-Event-ID": "-1"}
        ).status_code
        == 422
    )


def install_context(api, project, task):
    db, _, contexts, tmp_path = api
    auth = db.get_task(task["id"])["authorization_snapshot"]
    provenance = Provenance(Source.OWNER, "owner", auth["provenance"]["session_id"], True)
    context = PolicyContext(
        task["id"],
        project["id"],
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 64, "main", CodeOrigin.OWNER),
        OwnerAuthorization(
            auth["id"],
            auth["version"],
            task["id"],
            project["id"],
            project["repository"],
            provenance,
        ),
        "1",
    )
    contexts[task["id"]] = context
    return context


@pytest.mark.parametrize("decision", ["approve", "reject", "edit"])
def test_approval_actions_are_durable_and_edits_regoverned(api, decision):
    db, client, _, _ = api
    project, task, _ = submit(api)
    context = install_context(api, project, task)
    action = Action("write_file", {"path": "hello.py", "content": "hello"})
    digest = action_digest(action, context)
    action_row = db.plan_action(
        task["id"], "hello", digest, {"tool": action.tool, "params": dict(action.params)}
    )
    approval = db.ensure_approval(task["id"], action_row["id"], digest, "action")
    body = {"expected_digest": digest, "decision": decision}
    if decision == "edit":
        body["edited_params"] = {"path": "../escape.py", "content": "bad"}
    response = client.post(
        f"/api/v1/approvals/{approval['id']}/reply",
        json=body,
        headers={"Idempotency-Key": "reply-1"},
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["approval"]["status"]
        == {"approve": "approved", "reject": "rejected", "edit": "edited"}[decision]
    )
    if decision == "edit":
        assert response.json()["decision"]["hard_denial"] is True
    from decagent.storage.approval_service import DurableApprovals

    assert DurableApprovals(db).read(approval["id"]).provenance.trusted_owner


def test_stale_approval_and_terminal_resume_conflict(api):
    db, client, contexts, _ = api
    project, task, _ = submit(api)
    context = install_context(api, project, task)
    action = Action("write_file", {"path": "hello.py", "content": "hello"})
    digest = action_digest(action, context)
    row = db.plan_action(
        task["id"], "hello", digest, {"tool": action.tool, "params": dict(action.params)}
    )
    approval = db.ensure_approval(task["id"], row["id"], digest, "action")
    contexts[task["id"]] = replace(context, policy_version="2")
    response = client.post(
        f"/api/v1/approvals/{approval['id']}/reply",
        json={"expected_digest": digest, "decision": "approve"},
        headers={"Idempotency-Key": "stale"},
    )
    assert response.status_code == 409
    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='succeeded' WHERE id=%s", (task["id"],))
    assert (
        client.post(
            f"/api/v1/tasks/{task['id']}/resume", json={}, headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 409
    )


def test_missing_idempotency_is_rejected_and_errors_are_redacted(api):
    _, client, _, _ = api
    assert (
        client.post("/api/v1/projects", json={"repository": "fixture/missing"}).status_code == 422
    )
    response = client.post(
        "/api/v1/projects",
        json={"repository": "sk-" + "x" * 32},
        headers={"Idempotency-Key": "bad"},
    )
    assert response.status_code == 422
    assert "x" * 32 not in response.text


def test_resume_rechecks_approved_action_and_never_executes_stale_approval(api):
    db, client, contexts, _ = api
    project, task, _ = submit(api)
    context = install_context(api, project, task)
    action = Action("write_file", {"path": "hello.py", "content": "hello"})
    digest = action_digest(action, context)
    row = db.plan_action(
        task["id"], "hello", digest, {"tool": action.tool, "params": dict(action.params)}
    )
    approval = db.ensure_approval(task["id"], row["id"], digest, "action")
    response = client.post(
        f"/api/v1/approvals/{approval['id']}/reply",
        json={"expected_digest": digest, "decision": "approve"},
        headers={"Idempotency-Key": "reply-current"},
    )
    assert response.status_code == 200
    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='awaiting_approval' WHERE id=%s", (task["id"],))
    contexts[task["id"]] = replace(
        context, snapshot=replace(context.snapshot, tree_digest="c" * 64)
    )
    response = client.post(
        f"/api/v1/tasks/{task['id']}/resume", json={}, headers={"Idempotency-Key": "resume-stale"}
    )
    assert response.status_code == 409
    assert db.get_task(task["id"])["state"] == "awaiting_approval"


def test_budget_resume_requeues_and_queued_cancel_needs_no_executor(api):
    db, client, _, _ = api
    project, task, _ = submit(api)
    install_context(api, project, task)
    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='paused_budget' WHERE id=%s", (task["id"],))
    response = client.post(
        f"/api/v1/tasks/{task['id']}/resume",
        json={"budget": {"max_calls": 6, "max_tokens": 2000}},
        headers={"Idempotency-Key": "budget-1"},
    )
    assert response.status_code == 200 and response.json()["state"] == "queued"
    assert db.get_task(task["id"])["budget"]["max_calls"] == 6
    response = client.post(
        f"/api/v1/tasks/{task['id']}/cancel", headers={"Idempotency-Key": "cancel-1"}
    )
    assert response.status_code == 200 and response.json()["state"] == "cancelled"


def test_pure_code_trust_approval_is_issued_and_consumed_transactionally(api):
    from psycopg.types.json import Jsonb

    from decagent.policy import CodeTrustRequest, TrustKind

    db, client, _, _ = api
    project, task, _ = submit(api)
    context = install_context(api, project, task)
    request = CodeTrustRequest(project["repository"], TrustKind.EXACT_COMMIT, "a" * 40)
    digest = request.digest(context)
    row = db.ensure_approval(task["id"], None, digest, "code_trust")
    with db.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET reply_payload=%s WHERE id=%s",
            (
                Jsonb(
                    {
                        "code_trust_request": {
                            "repository": request.repository,
                            "kind": "exact_commit",
                            "sha": request.sha,
                            "branch": None,
                        }
                    }
                ),
                row["id"],
            ),
        )
    response = client.post(
        f"/api/v1/approvals/{row['id']}/reply",
        json={"expected_digest": digest, "decision": "approve"},
        headers={"Idempotency-Key": "trust-1"},
    )
    assert response.status_code == 200, response.text
    saved = db.get_task(task["id"])["authorization_snapshot"]["code_trust"]
    assert saved["sha"] == "a" * 40 and saved["provenance"]["authenticated"] is True
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM app.approvals WHERE id=%s", (row["id"],)).fetchone()[
                "status"
            ]
            == "consumed"
        )


def test_clarification_binds_current_question_and_persists_owner_confirmation(api):
    from psycopg.types.json import Jsonb

    from decagent.policy import ApprovalKind
    from decagent.policy.approvals import confirmation_digest

    db, client, _, _ = api
    project, task, _ = submit(api)
    context = install_context(api, project, task)
    subject = {"question": "What inclusive range should be supported?"}
    digest = confirmation_digest(ApprovalKind.CLARIFICATION, subject, context)
    row = db.ensure_approval(task["id"], None, digest, "clarification")
    with db.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET reply_payload=%s WHERE id=%s",
            (Jsonb({"subject": subject}), row["id"]),
        )
        conn.execute("UPDATE app.tasks SET state='awaiting_input' WHERE id=%s", (task["id"],))
    response = client.post(
        f"/api/v1/tasks/{task['id']}/responses",
        json={
            "question_id": str(row["id"]),
            "expected_digest": digest,
            "content": "Both endpoints must be included.",
        },
        headers={"Idempotency-Key": "clarify-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["approval"]["status"] == "confirmed"


def test_workbench_assets_are_served_with_security_headers(api):
    _, client, _, _ = api
    page = client.get("/")
    assert page.status_code == 200
    assert "DecAgent" in page.text and 'id="approvals"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "Last-Event-ID" in script.text or "after=" in script.text
    assert "localStorage" not in script.text

"""Employee workbench shares the server operation core and isolates browser data."""

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.employee_accounts import seed_employee_accounts
from decagent.business.grants import GrantAuthority
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore
from decagent.config import Settings
from decagent.gateway import create_app, initialize_owner
from decagent.gateway.app import GatewayCallbacks

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def workbench(database, tmp_path):
    assert TicketStore(database.url).execute("seed", {}, "workbench-seed")["ok"]
    directory = tmp_path / "demo-auth"
    seed_employee_accounts(database, directory)
    scope = PluginRegistry(database).register(builtin_manifest())

    def read(identity, command, params):
        with database.connect() as conn:
            if command == "list":
                return {"tickets": access.list_tickets(conn, identity.employee_id, params)}
            if command == "get":
                ticket_id = params["ticket_id"]
                return {
                    "ticket": access.get_ticket(conn, identity.employee_id, ticket_id),
                    "history": access.get_history(conn, identity.employee_id, ticket_id),
                }
            raise AssertionError(f"Unexpected read: {command}")

    app = create_app(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        callbacks=GatewayCallbacks(business_employee_read=read),
    )
    return database, TestClient(app), app.state.employee_auth, directory, scope


def _login(client, directory, login):
    response = client.post(
        "/api/v2/business/auth/session",
        headers={"Origin": ORIGIN},
        json={"login": login, "password": (directory / f"{login}.password").read_text().strip()},
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def _identity(client, auth, directory, login):
    csrf = _login(client, directory, login)
    issued = client.post("/api/v2/business/auth/mcp-credentials", headers=csrf)
    assert issued.status_code == 201, issued.text
    token = (directory / f"mcp-{issued.json()['credential_ref']}.token").read_text().strip()
    return auth.authenticate_mcp_token(token), csrf


def _ticket(database, employee_id, person_id, category_id, *, status="resolved"):
    ticket_id = uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,employee_id,requester_id,status) "
            "VALUES(%s,'employee private ticket',%s,%s,%s,%s)",
            (ticket_id, category_id, employee_id, person_id, status),
        )
    return ticket_id


def test_operation_inbox_requires_original_browser_approval_and_preserves_original_commit(
    workbench,
):
    database, client, auth, directory, _ = workbench
    employee, csrf = _identity(client, auth, directory, "employee-it")
    with database.connect() as conn:
        category_id = conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"]
    ticket_id = _ticket(database, employee.employee_id, employee.person_id, category_id)
    operation = OperationService(database).prepare(
        employee,
        "employee-close-1",
        "close",
        {"ticket_id": str(ticket_id), "expected_revision": 1},
    )
    assert operation["state"] == "awaiting_approval"
    path = f"/api/v2/business/operations/{operation['operation_id']}"

    listed = client.get("/api/v2/business/operations")
    assert listed.status_code == 200, listed.text
    assert [item["operation_id"] for item in listed.json()] == [operation["operation_id"]]
    detail = client.get(path)
    assert detail.status_code == 200
    assert detail.json()["source"] == "external_mcp"
    assert detail.json()["expected_revision"] == 1
    assert detail.json()["input_hash"] == operation["input_hash"]
    assert detail.json()["resource_snapshot"]["status"] == "resolved"

    body = {"expected_hash": operation["input_hash"], "decision": "approve"}
    assert client.post(path + "/reply", json=body).status_code == 403
    csrf_other = _login(client, directory, "employee-facilities")
    assert client.get(path).status_code == 404
    assert client.get("/api/v2/business/operations").json() == []
    assert client.post(path + "/reply", headers=csrf_other, json=body).status_code == 404
    csrf_supervisor = _login(client, directory, "supervisor")
    assert client.get(path).status_code == 200
    assert client.post(path + "/reply", headers=csrf_supervisor, json=body).status_code == 404

    csrf = _login(client, directory, "employee-it")
    approved = client.post(path + "/reply", headers=csrf, json=body)
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "ready"
    assert client.post(path + "/reply", headers=csrf, json=body).json()["state"] == "ready"
    with database.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 0
        )
    committed = OperationService(database).commit(employee, operation["operation_id"])
    assert committed["state"] == "succeeded"
    assert client.get(path).json()["state"] == "succeeded"
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT status FROM business.tickets WHERE id=%s", (ticket_id,)
            ).fetchone()["status"]
            == "closed"
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )


def test_operation_edit_creates_linked_replacement_and_reject_preserves_resource(workbench):
    database, client, auth, directory, _ = workbench
    supervisor, csrf = _identity(client, auth, directory, "supervisor")
    with database.connect() as conn:
        category_id = conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"]
    ticket_id = _ticket(
        database, supervisor.employee_id, supervisor.person_id, category_id, status="open"
    )
    with database.connect() as conn:
        receipts_before = conn.execute(
            "SELECT count(*) AS n FROM business.operation_receipts"
        ).fetchone()["n"]
    operation = OperationService(database).prepare(
        supervisor,
        "priority-1",
        "set_priority",
        {"ticket_id": str(ticket_id), "expected_revision": 1, "priority": "high"},
    )
    path = f"/api/v2/business/operations/{operation['operation_id']}"
    edited = client.post(
        path + "/reply",
        headers=csrf,
        json={
            "expected_hash": operation["input_hash"],
            "decision": "edit",
            "edited_params": {
                "ticket_id": str(ticket_id),
                "expected_revision": 1,
                "priority": "urgent",
            },
        },
    )
    assert edited.status_code == 200, edited.text
    replacement = edited.json()
    assert replacement["operation_id"] != operation["operation_id"]
    assert replacement["supersedes"] == operation["operation_id"]
    assert replacement["state"] == "awaiting_approval"
    assert client.get(path).json()["state"] == "rejected"
    rejected = client.post(
        f"/api/v2/business/operations/{replacement['operation_id']}/reply",
        headers=csrf,
        json={"expected_hash": replacement["input_hash"], "decision": "reject"},
    )
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT priority FROM business.tickets WHERE id=%s", (ticket_id,)
            ).fetchone()["priority"]
            == "normal"
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()["n"]
            == receipts_before
        )


def test_employee_task_events_and_ticket_views_do_not_cross_account(workbench):
    database, client, auth, directory, scope = workbench
    first, _ = _identity(client, auth, directory, "employee-it")
    second, _ = _identity(client, auth, directory, "employee-facilities")
    with database.connect() as conn:
        category_id = conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"]
    first_ticket = _ticket(database, first.employee_id, first.person_id, category_id)
    second_ticket = _ticket(database, second.employee_id, second.person_id, category_id)
    tasks = []
    for actor, name in ((first, "first private task"), (second, "second private task")):
        task = database.create_business_task(
            scope["id"], {"instruction": name, "plugin_digest": scope["manifest_digest"]}
        )
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET employee_id=%s WHERE id=%s", (actor.employee_id, task["id"])
            )
        database.event(task["id"], "task.private", {"instruction": name})
        tasks.append(task)

    _login(client, directory, "employee-it")
    visible = client.get("/api/v2/business/tickets")
    assert visible.status_code == 200, visible.text
    assert {item["id"] for item in visible.json()["tickets"]} == {str(first_ticket)}
    assert client.get(f"/api/v2/business/tickets/{second_ticket}").status_code == 404
    listed = client.get("/api/v2/business/tasks")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [str(tasks[0]["id"])]
    assert client.get(f"/api/v2/business/tasks/{tasks[1]['id']}").status_code == 404
    assert (
        client.get(f"/api/v2/business/tasks/{tasks[1]['id']}/events?follow=false").status_code
        == 404
    )
    events = client.get(f"/api/v2/business/tasks/{tasks[0]['id']}/events?follow=false")
    assert events.status_code == 200
    assert "first private task" in events.text and "second private task" not in events.text

    _login(client, directory, "supervisor")
    supervisor_tickets = client.get("/api/v2/business/tickets")
    assert {item["id"] for item in supervisor_tickets.json()["tickets"]} == {
        str(first_ticket),
        str(second_ticket),
    }
    assert client.get(f"/api/v2/business/tasks/{tasks[0]['id']}").status_code == 404


def test_default_business_page_uses_employee_entry_and_keeps_repair_link(workbench):
    _, client, _, _, _ = workbench
    html = client.get("/").text
    script = client.get("/assets/business.js").text
    assert "员工账号" in html and "员工密码" in html
    assert 'href="/repair"' in html
    assert 'id="operation-list"' in html and 'id="ticket-list"' in html
    assert 'id="approvals"' in html
    assert 'id="workflow-trace"' in html and 'id="plan-nodes"' in html
    assert 'id="plan-policies"' in html and 'id="trace-timeline"' in html
    assert 'id="memory-panel"' in html and 'id="memory-dependencies"' in html
    assert "/api/v2/business" in script
    assert "/decisions/" in script and "/decision-thresholds" in script
    assert "/memories/" in script and "certainty" in script
    assert "/approvals/${approval.id}/reply" in script
    assert "/api/v1/business/tasks" not in script


def test_owner_v1_cannot_start_or_resume_business_writes(workbench):
    database, client, _, directory, scope = workbench
    owner_token = initialize_owner(database, directory.parent / "owner.token").read_text().strip()
    login = client.post(
        "/api/v1/auth/session", headers={"Origin": ORIGIN}, json={"token": owner_token}
    )
    assert login.status_code == 200
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": login.json()["csrf_token"],
        "Idempotency-Key": "old-business-1",
    }
    submitted = client.post(
        "/api/v1/business/tasks",
        headers=headers,
        json={"scope_id": str(scope["id"]), "instruction": "create a request"},
    )
    assert submitted.status_code == 410
    task = database.create_business_task(
        scope["id"], {"instruction": "old task", "plugin_digest": scope["manifest_digest"]}
    )
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='recovering' WHERE id=%s", (task["id"],))
    resumed = client.post(
        f"/api/v1/tasks/{task['id']}/resume",
        headers={**headers, "Idempotency-Key": "old-business-2"},
        json={},
    )
    assert resumed.status_code == 410
    cancelled = client.post(
        f"/api/v1/tasks/{task['id']}/cancel",
        headers={**headers, "Idempotency-Key": "old-business-3"},
        json={},
    )
    assert cancelled.status_code == 410


def test_employee_submission_and_clarification_are_scoped_idempotent_and_durable(workbench):
    database, client, _, directory, scope = workbench
    csrf = _login(client, directory, "employee-it")
    headers = {**csrf, "Idempotency-Key": "employee-submit-1"}
    body = {"scope_id": str(scope["id"]), "instruction": "请帮我查清工单"}
    submitted = client.post("/api/v2/business/tasks", headers=headers, json=body)
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    replayed = client.post("/api/v2/business/tasks", headers=headers, json=body)
    assert replayed.status_code == 201 and replayed.json()["id"] == task_id
    assert "credential_ref" not in submitted.json()
    with database.connect() as conn:
        task = conn.execute("SELECT * FROM app.tasks WHERE id=%s", (task_id,)).fetchone()
        assert task["graph_version"] == "business-2"
        assert task["employee_id"] is not None and task["credential_ref"] is not None
        assert (
            conn.execute("SELECT count(*) AS n FROM app.tasks WHERE kind='business'").fetchone()[
                "n"
            ]
            == 1
        )
        conn.execute("UPDATE app.tasks SET state='awaiting_input' WHERE id=%s", (task_id,))
        approval_id = uuid4()
        digest = hashlib.sha256(b"clarify-task").hexdigest()
        conn.execute(
            "INSERT INTO app.approvals(id,task_id,digest,kind,reply_payload) "
            "VALUES(%s,%s,%s,'clarification',%s)",
            (approval_id, task_id, digest, Jsonb({"question": "请补充部门"})),
        )
    reply_url = f"/api/v2/business/tasks/{task_id}/approvals/{approval_id}/reply"
    reply = {"expected_digest": digest, "decision": "confirm", "confirmation": "IT Support"}
    assert client.post(reply_url, json=reply).status_code == 403
    csrf_other = _login(client, directory, "employee-facilities")
    assert (
        client.post(
            reply_url, headers={**csrf_other, "Idempotency-Key": "other-reply"}, json=reply
        ).status_code
        == 404
    )
    csrf = _login(client, directory, "employee-it")
    replied = client.post(reply_url, headers={**csrf, "Idempotency-Key": "clarify-1"}, json=reply)
    assert replied.status_code == 200, replied.text
    assert replied.json()["policy_record"]["status"] == "clarified"
    assert (
        client.post(reply_url, headers={**csrf, "Idempotency-Key": "clarify-1"}, json=reply).json()
        == replied.json()
    )
    with database.connect() as conn:
        task = conn.execute("SELECT state FROM app.tasks WHERE id=%s", (task_id,)).fetchone()
        approval = conn.execute(
            "SELECT status FROM app.approvals WHERE id=%s", (approval_id,)
        ).fetchone()
    assert task["state"] == "queued" and approval["status"] == "replied"


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_operation"),
    [
        ("approve", "approved", "ready"),
        ("reject", "rejected", "rejected"),
        ("edit", "edited", "rejected"),
    ],
)
def test_agent_action_reply_uses_original_employee_and_server_operation(
    workbench, decision, expected_status, expected_operation
):
    database, client, auth, directory, scope = workbench
    actor, csrf = _identity(client, auth, directory, "employee-it")
    submitted = client.post(
        "/api/v2/business/tasks",
        headers={**csrf, "Idempotency-Key": "agent-action-submit"},
        json={"scope_id": str(scope["id"]), "instruction": "创建员工工单"},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    leased = database.claim(task_id, "action-test-worker", seconds=120)
    assert leased is not None
    with database.connect() as conn:
        category_id = conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"]
    params = {"title": "my service request", "category_id": str(category_id)}
    request_id = "agent_" + uuid4().hex
    digest = "a" * 64
    snapshot = {
        "task_id": task_id,
        "scope_id": str(scope["id"]),
        "plugin_digest": scope["manifest_digest"],
        "command": "create",
        "params": params,
        "operation_id": request_id,
        "assessment": {"outcome": "confirm", "digest": digest},
    }
    action = database.plan_action(task_id, "create-1", digest, snapshot)
    grant = GrantAuthority(database).issue(
        task_id, action["id"], "action-test-worker", leased["lease_generation"]
    )
    operation = OperationService(database).prepare(actor, request_id, "create", params, grant=grant)
    assert operation["source"] == "decagent"
    approval = database.ensure_approval(task_id, action["id"], digest, "action")
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_approval',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (task_id,),
        )
    task = client.get(f"/api/v2/business/tasks/{task_id}")
    assert task.status_code == 200, task.text
    assert task.json()["approvals"][0]["action"]["params"] == params
    reply = {"expected_digest": digest, "decision": decision}
    if decision == "edit":
        reply["edited_params"] = {**params, "title": "revised service request"}
    reply_url = f"/api/v2/business/tasks/{task_id}/approvals/{approval['id']}/reply"
    stale = client.post(
        reply_url,
        headers={**csrf, "Idempotency-Key": "stale-action"},
        json={**reply, "expected_digest": "b" * 64},
    )
    assert stale.status_code == 409
    resolved = client.post(
        reply_url,
        headers={**csrf, "Idempotency-Key": "agent-action-reply"},
        json=reply,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["policy_record"]["status"] == expected_status
    assert (
        client.get(f"/api/v2/business/operations/{operation['operation_id']}").json()["state"]
        == expected_operation
    )
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT state FROM app.tasks WHERE id=%s", (task_id,)).fetchone()["state"]
            == "queued"
        )


def test_employee_resume_reauthenticates_same_actor_and_cancel_increments_generation(workbench):
    database, client, _, directory, scope = workbench
    csrf = _login(client, directory, "employee-it")
    task = client.post(
        "/api/v2/business/tasks",
        headers={**csrf, "Idempotency-Key": "resume-task"},
        json={"scope_id": str(scope["id"]), "instruction": "请创建工单"},
    ).json()
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='paused_budget' WHERE id=%s", (task["id"],))
        before = conn.execute(
            "SELECT credential_ref,lease_generation FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone()
    resume_url = f"/api/v2/business/tasks/{task['id']}/resume"
    missing_budget = client.post(
        resume_url, headers={**csrf, "Idempotency-Key": "missing-budget"}, json={}
    )
    assert missing_budget.status_code == 422 and missing_budget.json()["code"] == "budget_required"
    csrf_other = _login(client, directory, "employee-facilities")
    assert (
        client.post(
            resume_url,
            headers={**csrf_other, "Idempotency-Key": "other-resume"},
            json={"budget": {}},
        ).status_code
        == 404
    )
    csrf = _login(client, directory, "employee-it")
    resumed = client.post(
        resume_url,
        headers={**csrf, "Idempotency-Key": "resume-1"},
        json={"budget": {"max_calls": 40}},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "queued"
    with database.connect() as conn:
        after = conn.execute(
            "SELECT credential_ref,lease_generation FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone()
    assert after["credential_ref"] != before["credential_ref"]
    assert after["lease_generation"] == before["lease_generation"] + 1
    cancelled = client.post(
        f"/api/v2/business/tasks/{task['id']}/cancel",
        headers={**csrf, "Idempotency-Key": "cancel-1"},
        json={},
    )
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    with database.connect() as conn:
        final = conn.execute(
            "SELECT lease_generation FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone()
    assert final["lease_generation"] == after["lease_generation"] + 1


def test_pending_operation_and_original_session_survive_workbench_restart(workbench):
    database, client, auth, directory, _ = workbench
    employee, csrf = _identity(client, auth, directory, "employee-it")
    with database.connect() as conn:
        category = conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"]
    ticket = _ticket(database, employee.employee_id, employee.person_id, category)
    operation = OperationService(database).prepare(
        employee,
        "restart-pending",
        "close",
        {
            "ticket_id": str(ticket),
            "expected_revision": 1,
        },
    )
    restarted = TestClient(
        create_app(
            database,
            Settings(
                database_url=database.url,
                data_dir=directory.parent,
            ),
        )
    )
    restarted.cookies.update(client.cookies)
    path = "/api/v2/business/operations/" + operation["operation_id"]
    assert restarted.get(path).json()["state"] == "awaiting_approval"
    approved = restarted.post(
        path + "/reply",
        headers=csrf,
        json={
            "expected_hash": operation["input_hash"],
            "decision": "approve",
        },
    )
    assert approved.status_code == 200 and approved.json()["state"] == "ready"
    assert (
        OperationService(database).commit(employee, operation["operation_id"])["state"]
        == "succeeded"
    )

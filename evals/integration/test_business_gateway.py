"""Employee API and real graph regressions use the shared operation boundary."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from decagent.business.employee_accounts import seed_employee_accounts
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


@pytest.fixture
def business_api(database, tmp_path):
    TicketStore(database.url).seed_directory("seed")
    directory = tmp_path / "demo-auth"
    seed_employee_accounts(database, directory)
    store = FixtureTickets(database)
    store.data_dir = tmp_path
    scope = PluginRegistry(database).register(builtin_manifest())
    callbacks = GatewayCallbacks(
        business_employee_read=lambda identity, command, params: store.read(command, params)
    )
    client = TestClient(create_app(database, Settings(data_dir=tmp_path), callbacks=callbacks))
    origin = "http://127.0.0.1:8000"
    login = client.post(
        "/api/v2/business/auth/session",
        json={
            "login": "supervisor",
            "password": (directory / "supervisor.password").read_text().strip(),
        },
        headers={"Origin": origin},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]})
    return database, client, scope, store


def submit_business(api, instruction="查看工单列表", key="business-1", strategy="hybrid"):
    _, client, scope, _ = api
    response = client.post(
        "/api/v2/business/tasks",
        json={"scope_id": str(scope["id"]), "instruction": instruction, "strategy": strategy},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_business_submission_is_atomic_idempotent_frozen_and_requires_employee(business_api):
    db, client, scope, _ = business_api
    plugins = client.get("/api/v2/business/plugins")
    assert plugins.status_code == 200 and plugins.json()[0]["scope_id"] == str(scope["id"])
    task = submit_business(business_api)
    duplicate = submit_business(business_api)
    assert duplicate == task
    saved = db.get_task(task["id"])
    assert saved["kind"] == "business" and saved["project_id"] is None and saved["base_sha"] is None
    assert saved["graph_version"] == "business-2"
    assert saved["authorization_snapshot"]["plugin_digest"] == scope["manifest_digest"]
    assert saved["authorization_snapshot"]["provenance"]["authenticated"] is True
    assert saved["authorization_snapshot"]["strategy"] == "hybrid"
    assert len(db.events(task["id"])) == 1
    view = client.get("/api/v2/business/tasks/" + task["id"]).json()
    assert view["requested_issue"] == "查看工单列表" and "actions" in view
    client.cookies.clear()
    assert client.get("/api/v2/business/plugins").status_code == 401
    assert client.post("/api/v2/business/tasks", json={}).status_code == 401


def test_disabled_plugin_and_forged_authorization_are_rejected(business_api):
    db, client, scope, _ = business_api
    PluginRegistry(db).disable(scope["id"])
    response = client.post(
        "/api/v2/business/tasks",
        json={"scope_id": str(scope["id"]), "instruction": "查询"},
        headers={"Idempotency-Key": "disabled"},
    )
    assert response.status_code == 409 and response.json()["code"] == "contract_changed"
    response = client.post(
        "/api/v2/business/tasks",
        json={
            "scope_id": str(scope["id"]),
            "instruction": "查询",
            "provenance": {"authenticated": True},
        },
        headers={"Idempotency-Key": "forged"},
    )
    assert response.status_code == 422
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.tasks").fetchone()["n"] == 0


def test_business_cancel_resume_and_sse_do_not_require_broker(business_api):
    db, client, _, _ = business_api
    task = submit_business(business_api)
    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='paused_budget' WHERE id=%s", (task["id"],))
    missing = client.post(
        "/api/v2/business/tasks/" + task["id"] + "/resume",
        json={},
        headers={"Idempotency-Key": "missing"},
    )
    assert missing.status_code == 422
    resumed = client.post(
        "/api/v2/business/tasks/" + task["id"] + "/resume",
        json={"budget": {"max_calls": 5}},
        headers={"Idempotency-Key": "resume"},
    )
    assert resumed.status_code == 200 and resumed.json()["state"] == "queued"
    claimed = db.claim(task["id"], "worker", seconds=120)
    cancelled = client.post(
        "/api/v2/business/tasks/" + task["id"] + "/cancel", headers={"Idempotency-Key": "cancel"}
    )
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert db.get_task(task["id"])["lease_generation"] > claimed["lease_generation"]
    with pytest.raises(ValueError):
        db.assert_lease(task["id"], "worker", claimed["lease_generation"])
    events = client.get(
        "/api/v2/business/tasks/" + task["id"] + "/events?follow=false",
        headers={"Last-Event-ID": "1"},
    )
    assert "id: 1\n" not in events.text and "task.cancelled" in events.text


def test_employee_budget_change_records_exact_authorized_values_once(business_api):
    db, client, _, _ = business_api
    task = submit_business(business_api)
    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='paused_budget' WHERE id=%s", (task["id"],))
    path = "/api/v2/business/tasks/" + task["id"] + "/resume"
    body = {"budget": {"primary_target": 3, "max_primary_calls": 8, "max_jev_calls": 24}}
    response = client.post(path, json=body, headers={"Idempotency-Key": "budget-change"})
    assert response.status_code == 200
    replay = client.post(path, json=body, headers={"Idempotency-Key": "budget-change"})
    assert replay.json() == response.json()
    changed = [event for event in db.events(task["id"]) if event["kind"] == "task.budget_changed"]
    assert len(changed) == 1
    assert changed[0]["payload"]["before"]["max_primary_calls"] == 6
    assert changed[0]["payload"]["after"]["max_primary_calls"] == 8
    assert changed[0]["payload"]["employee_id"] == str(db.get_task(task["id"])["employee_id"])
    client.cookies.clear()
    assert client.post(path, json=body, headers={"Idempotency-Key": "unauthed"}).status_code == 401
    assert (
        len([event for event in db.events(task["id"]) if event["kind"] == "task.budget_changed"])
        == 1
    )


def pending_action(api):
    db, client, _, store = api
    directory = store.read("directory", {})
    ticket = store.execute(
        "create", {"title": "Fixture", "category_id": directory["categories"][0]["id"]}, "create"
    )["data"]["ticket"]
    task = submit_business(
        api,
        json.dumps(
            {
                "command": "set_priority",
                "params": {"ticket_id": ticket["id"], "expected_revision": 1, "priority": "high"},
            }
        ),
        key="confirm-task",
        strategy="rule",
    )
    assert real_business_worker(api, store.data_dir).run_once()
    assert client.get("/api/v2/business/tasks/" + task["id"]).json()["state"] == "awaiting_approval"
    with db.connect() as conn:
        action = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (task["id"],)
        ).fetchone()
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE task_id=%s", (task["id"],)
        ).fetchone()
    return task, ticket, action, approval


def test_employee_view_hides_pending_action_until_task_is_released_to_wait(business_api):
    db, client, _, _ = business_api
    task, _, _, approval = pending_action(business_api)
    path = "/api/v2/business/tasks/" + task["id"]

    with db.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='running',lease_owner='test-worker',"
            "lease_until=clock_timestamp()+interval '30 seconds' WHERE id=%s",
            (task["id"],),
        )
    running = client.get(path).json()
    assert running["state"] == "running"
    assert not any(item["status"] == "pending" for item in running["approvals"])

    with db.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='awaiting_approval' WHERE id=%s", (task["id"],))
    leased_wait = client.get(path).json()
    assert leased_wait["state"] == "awaiting_approval"
    assert not any(item["status"] == "pending" for item in leased_wait["approvals"])

    with db.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_owner=NULL,lease_until=NULL WHERE id=%s", (task["id"],)
        )
    released_wait = client.get(path).json()
    assert released_wait["state"] == "awaiting_approval"
    assert [item["id"] for item in released_wait["approvals"] if item["status"] == "pending"] == [
        str(approval["id"])
    ]


def test_business_confirmation_is_authenticated_exact_and_duplicate_safe(business_api):
    db, client, _, _ = business_api
    task, _, action, approval = pending_action(business_api)
    path = "/api/v2/business/tasks/" + task["id"] + "/approvals/" + str(approval["id"]) + "/reply"
    body = {"expected_digest": approval["digest"], "decision": "approve"}
    first = client.post(path, json=body, headers={"Idempotency-Key": "reply-1"})
    assert first.status_code == 200, first.text
    assert first.json()["policy_record"]["action_digest"] == action["digest"]
    duplicate = client.post(path, json=body, headers={"Idempotency-Key": "reply-2"})
    assert duplicate.status_code == 200 and duplicate.json() == first.json()
    conflict = client.post(
        path, json={**body, "decision": "reject"}, headers={"Idempotency-Key": "reply-3"}
    )
    assert conflict.status_code == 409
    assert db.get_task(task["id"])["state"] == "queued"
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM app.actions WHERE id=%s", (action["id"],)).fetchone()[
                "status"
            ]
            == "running"
        )


def test_employee_approval_and_memory_read_share_identity_then_task_lock_order(
    business_api, monkeypatch
):
    """Concurrent browser approval and task memory reads must not deadlock PostgreSQL."""
    from decagent.business.memory_control import BusinessMemoryControl
    from decagent.business.operations import OperationService
    from decagent.gateway.business import BusinessControl

    db, client, _, _ = business_api
    task, _, _, approval = pending_action(business_api)
    memory_client = TestClient(client.app)
    memory_client.cookies.update(client.cookies)
    memory_client.headers.update(client.headers)

    reply_started = threading.Event()
    allow_reply = threading.Event()
    identity_before_reply = threading.Event()
    memory_identity_locked = threading.Event()
    approval_identity_attempted = threading.Event()
    request_role = threading.local()

    original_identity = OperationService._identity
    original_reply = BusinessControl.reply_employee
    original_history = BusinessMemoryControl.history

    def tracked_identity(self, conn, identity, **kwargs):
        role = getattr(request_role, "value", None)
        if not reply_started.is_set():
            identity_before_reply.set()
        if role == "approval":
            approval_identity_attempted.set()
        actor = original_identity(self, conn, identity, **kwargs)
        if role == "memory":
            memory_identity_locked.set()
            if not identity_before_reply.is_set():
                approval_identity_attempted.wait(timeout=10)
        return actor

    def held_reply(self, conn, task_row, approval_row, body, identity, operation_id):
        request_role.value = "approval"
        reply_started.set()
        if not allow_reply.wait(timeout=10):
            raise AssertionError("Test did not release the approval request")
        try:
            return original_reply(self, conn, task_row, approval_row, body, identity, operation_id)
        finally:
            request_role.value = None

    def tracked_history(self, identity, *, task_id=None, limit=50):
        request_role.value = "memory"
        try:
            return original_history(self, identity, task_id=task_id, limit=limit)
        finally:
            request_role.value = None

    monkeypatch.setattr(OperationService, "_identity", tracked_identity)
    monkeypatch.setattr(BusinessControl, "reply_employee", held_reply)
    monkeypatch.setattr(BusinessMemoryControl, "history", tracked_history)

    reply_path = f"/api/v2/business/tasks/{task['id']}/approvals/{approval['id']}/reply"
    reply_body = {"expected_digest": approval["digest"], "decision": "approve"}
    memory_path = f"/api/v2/business/memories?task_id={task['id']}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        reply_future = pool.submit(
            client.post,
            reply_path,
            json=reply_body,
            headers={"Idempotency-Key": "lock-order-reply"},
        )
        assert reply_started.wait(timeout=10)
        memory_future = pool.submit(memory_client.get, memory_path)
        if not identity_before_reply.is_set():
            assert memory_identity_locked.wait(timeout=10)
        allow_reply.set()
        reply_response = reply_future.result(timeout=15)
        memory_response = memory_future.result(timeout=15)

    assert reply_response.status_code == 200, reply_response.text
    assert memory_response.status_code in {200, 409}, memory_response.text
    assert db.get_task(task["id"])["state"] == "queued"


def test_read_only_task_memory_lookup_does_not_wait_on_running_worker_task(business_api):
    """A memory GET must not invert the worker's task-to-employee lock order."""
    db, client, _, _ = business_api
    task = submit_business(business_api)
    memory_client = TestClient(client.app)
    memory_client.cookies.update(client.cookies)
    memory_client.headers.update(client.headers)
    memory_path = f"/api/v2/business/memories?task_id={task['id']}"

    with db.connect() as worker_connection:
        worker_connection.execute("SELECT id FROM app.tasks WHERE id=%s FOR UPDATE", (task["id"],))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(memory_client.get, memory_path)
            try:
                response = pending.result(timeout=3)
            except TimeoutError as error:
                worker_connection.rollback()
                pending.result(timeout=10)
                raise AssertionError(
                    "Read-only task memory lookup waited on the worker lock"
                ) from error

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "contract_changed"


def test_noop_edit_does_not_consume_or_orphan_the_current_approval(business_api):
    _, client, _, _ = business_api
    task, _, action, approval = pending_action(business_api)
    response = client.post(
        "/api/v2/business/tasks/" + task["id"] + "/approvals/" + str(approval["id"]) + "/reply",
        json={
            "expected_digest": approval["digest"],
            "decision": "edit",
            "edited_params": action["snapshot"]["params"],
        },
        headers={"Idempotency-Key": "noop-edit"},
    )
    assert response.status_code == 422
    view = client.get("/api/v2/business/tasks/" + task["id"]).json()
    assert view["state"] == "awaiting_approval"
    assert (
        next(row for row in view["approvals"] if row["id"] == str(approval["id"]))["status"]
        == "pending"
    )


def test_changed_resource_requires_reassessment_and_edited_fields_validate(business_api):
    db, client, _, store = business_api
    task, ticket, _, approval = pending_action(business_api)
    store.execute(
        "comment",
        {"ticket_id": ticket["id"], "expected_revision": 1, "body": "Concurrent change"},
        "comment",
    )
    path = "/api/v2/business/tasks/" + task["id"] + "/approvals/" + str(approval["id"]) + "/reply"
    invalid_edit = client.post(
        path,
        json={
            "expected_digest": approval["digest"],
            "decision": "edit",
            "edited_params": {"unknown": True},
        },
        headers={"Idempotency-Key": "invalid-edit"},
    )
    assert invalid_edit.status_code == 422
    response = client.post(
        path,
        json={"expected_digest": approval["digest"], "decision": "approve"},
        headers={"Idempotency-Key": "stale"},
    )
    assert response.status_code == 200 and response.json()["policy_record"]["status"] == "stale"
    assert db.get_task(task["id"])["state"] == "queued"
    assert (
        store.execute("get", {"ticket_id": ticket["id"]}, "get")["data"]["ticket"]["priority"]
        == "normal"
    )


def real_business_worker(api, tmp_path):
    from decagent.business.services import BusinessServices
    from decagent.business.worker import BusinessWorker

    database, _, _, store = api

    def factory(db, settings, config, task, owner, generation):
        return BusinessServices(db, settings, config, task, owner, generation, mcp_client=store)

    return BusinessWorker(database, Settings(data_dir=tmp_path), services_factory=factory)


def reply_waiting(client, view, decision, key, **extra):
    pending = [item for item in view["approvals"] if item["status"] == "pending"]
    assert len(pending) == 1, view
    approval = pending[0]
    response = client.post(
        "/api/v2/business/tasks/" + view["id"] + "/approvals/" + approval["id"] + "/reply",
        json={"expected_digest": approval["digest"], "decision": decision, **extra},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("decision", ["approve", "reject", "edit", "stale"])
def test_authenticated_reply_resumes_production_graph_and_verifies_real_receipt(
    business_api, tmp_path, decision
):
    db, client, _, store = business_api
    category = store.execute("directory", {}, "dir")["data"]["categories"][0]["id"]
    ticket = store.execute("create", {"title": "Fixture", "category_id": category}, "create")[
        "data"
    ]["ticket"]
    params = {"ticket_id": ticket["id"], "expected_revision": 1, "priority": "high"}
    task = submit_business(
        business_api, json.dumps({"command": "set_priority", "params": params}), "real-reply"
    )
    path = "/api/v2/business/tasks/" + task["id"]
    assert real_business_worker(business_api, tmp_path).run_once()
    view = client.get(path).json()
    assert view["state"] == "awaiting_approval"
    assert (
        store.execute("get", {"ticket_id": ticket["id"]}, "before")["data"]["ticket"]["revision"]
        == 1
    )
    if decision == "stale":
        assert store.execute(
            "comment",
            {"ticket_id": ticket["id"], "expected_revision": 1, "body": "Concurrent write"},
            "concurrent",
        )["ok"]
    extra = {"edited_params": {**params, "priority": "urgent"}} if decision == "edit" else {}
    response = reply_waiting(
        client, view, "approve" if decision == "stale" else decision, "first-reply", **extra
    )
    assert (
        response["policy_record"]["status"]
        == {"approve": "approved", "reject": "rejected", "edit": "edited", "stale": "stale"}[
            decision
        ]
    )
    assert real_business_worker(business_api, tmp_path).run_once()
    view = client.get(path).json()
    if decision in {"edit", "stale"}:
        assert view["state"] == "awaiting_approval"
        reply_waiting(client, view, "approve", "new-proposal-reply")
        assert real_business_worker(business_api, tmp_path).run_once()
        view = client.get(path).json()
    assert view["state"] == ("failed" if decision == "reject" else "succeeded"), view
    resource = store.execute("get", {"ticket_id": ticket["id"]}, "after")["data"]
    assert (
        resource["ticket"]["priority"]
        == {"reject": "normal", "edit": "urgent", "approve": "high", "stale": "high"}[decision]
    )
    assert len([entry for entry in resource["history"] if entry["command"] == "set_priority"]) == (
        0 if decision == "reject" else 1
    )
    if decision != "reject":
        assert view["business_result"]["completion"]["valid"] is True
        assert any(
            action["receipt"]["verification"]["server_verified"]
            for action in view["actions"]
            if action["status"] == "succeeded"
        )
    assert real_business_worker(business_api, tmp_path).run_once() is False


def test_clarification_keeps_namesakes_and_resumes_to_the_selected_real_person(
    business_api, tmp_path
):
    _, client, _, store = business_api
    directory = store.execute("directory", {}, "dir")["data"]
    person = next(
        row
        for row in directory["people"]
        if row["name"] == "Zhang Wei" and row["team"] == "IT Support"
    )
    ticket = store.execute(
        "create", {"title": "Fixture", "category_id": directory["categories"][0]["id"]}, "create"
    )["data"]["ticket"]
    task = submit_business(
        business_api, "请分配给Zhang Wei，工单 " + ticket["id"], "namesakes", "rule"
    )
    path = "/api/v2/business/tasks/" + task["id"]
    assert real_business_worker(business_api, tmp_path).run_once()
    view = client.get(path).json()
    assert view["state"] == "awaiting_input"
    reply_waiting(
        client, view, "confirm", "choose-team", confirmation="请分配给 IT Support 的Zhang Wei"
    )
    assert real_business_worker(business_api, tmp_path).run_once()
    view = client.get(path).json()
    # Use the rule route without a paid model for this directory disambiguation test.
    assert view["state"] == "succeeded", view
    current = store.execute("get", {"ticket_id": ticket["id"]}, "get")["data"]["ticket"]
    assert current["assignee_id"] == person["id"] and current["status"] == "assigned"


def test_business_browser_session_requires_exact_origin_and_csrf(business_api):
    _, client, scope, _ = business_api
    csrf = client.headers.pop("X-CSRF-Token")
    origin = "http://127.0.0.1:8000"
    body = {"scope_id": str(scope["id"]), "instruction": "查看工单列表"}
    endpoint = "/api/v2/business/tasks"
    assert (
        client.post(endpoint, json=body, headers={"Idempotency-Key": "no-csrf"}).status_code == 403
    )
    headers = {"Origin": origin, "X-CSRF-Token": csrf, "Idempotency-Key": "with-csrf"}
    assert (
        client.post(
            endpoint, json=body, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert client.post(endpoint, json=body, headers=headers).status_code == 201


def test_create_then_clarify_preserves_verified_ticket_and_action_ids(business_api, tmp_path):
    from decimal import Decimal

    from decagent.business.services import BusinessServices
    from decagent.business.worker import BusinessWorker
    from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    db, client, _, store = business_api
    directory = store.execute("directory", {}, "dir")["data"]
    person = next(
        row
        for row in directory["people"]
        if row["name"] == "Zhang Wei" and row["team"] == "IT Support"
    )
    category = next(row["id"] for row in directory["categories"] if row["name"] == "Network")
    task = submit_business(
        business_api, "请创建 Network 工单，并分配给Zhang Wei", "create-clarify", "main"
    )
    calls, saved, writes = [], {}, []

    class Backend:
        def __getattr__(self, name):
            return getattr(store, name)

        def commit(self, operation_id, *, grant=None):
            result = store.commit(operation_id, grant=grant)
            writes.append(result["command"])
            return result

    def primary_factory(settings, protocol):
        names = {command: name for name, command in protocol.commands.items()}

        class Model:
            def respond(self, messages, **kwargs):
                tool_results = [
                    json.loads(row["content"]) for row in messages if row["role"] == "tool"
                ]
                turn = len(calls)
                calls.append(messages)
                if turn == 0:
                    name, params = (
                        names["create"],
                        {"title": "VPN unavailable", "category_id": category},
                    )
                elif turn == 1:
                    saved.update(
                        action_id=tool_results[0]["action_id"],
                        ticket_id=tool_results[0]["data"]["ticket"]["id"],
                    )
                    name, params = "decagent_ask_user", {"question": "请选择Zhang Wei的部门。"}
                elif turn == 2:
                    text = json.dumps(messages, ensure_ascii=False)
                    assert saved["action_id"] in text and saved["ticket_id"] in text
                    assert "Previously verified operations" in text and "IT Support" in text
                    name, params = (
                        names["assign"],
                        {
                            "ticket_id": saved["ticket_id"],
                            "expected_revision": 1,
                            "person_id": person["id"],
                        },
                    )
                else:
                    assert turn == 3
                    name, params = (
                        "decagent_finish",
                        {
                            "summary": "Created and assigned to the clarified person",
                            "completed_actions": [
                                saved["action_id"],
                                tool_results[-1]["action_id"],
                            ],
                        },
                    )
                protocol.validate(name, params)
                return ModelTurn(
                    "",
                    (ToolProposal("call-" + str(turn), name, params),),
                    ModelUsage(100, 20, 120),
                    "offline-fixture",
                )

        return Model()

    def factory(database, settings, config, row, owner, generation):
        return BusinessServices(
            database,
            settings,
            config,
            row,
            owner,
            generation,
            mcp_client=Backend(),
            primary_factory=primary_factory,
        )

    def instance():
        return BusinessWorker(
            db,
            Settings(data_dir=tmp_path),
            services_factory=factory,
            runtime_config=RuntimeConfig(
                main_price=TokenPrice("offline-fixture", Decimal(0), Decimal(0))
            ),
        )

    path = "/api/v2/business/tasks/" + task["id"]
    assert instance().run_once()
    view = client.get(path).json()
    assert view["state"] == "awaiting_input" and writes == ["create"]
    reply_waiting(
        client,
        view,
        "confirm",
        "clarify-after-create",
        confirmation="请分配给 IT Support 的Zhang Wei",
    )
    assert instance().run_once()
    view = client.get(path).json()
    assert view["state"] == "succeeded", view
    assert writes == ["create", "assign"] and len(calls) == 4
    assert view["business_result"]["completion"]["valid"] is True
    current = store.execute("get", {"ticket_id": saved["ticket_id"]}, "get")["data"]["ticket"]
    assert current["assignee_id"] == person["id"] and current["status"] == "assigned"

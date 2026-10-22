"""Persistent guest task queue limits, fairness, cancellation and lease recovery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from decagent.business.guest_auth import COOKIE_NAME as GUEST_COOKIE_NAME
from decagent.business.guest_queue import GuestExecutionQueue, GuestQueueFull
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.trial_budget import (
    FxSnapshot,
    TrialBudgetLedger,
    TrialBudgetPolicy,
)
from decagent.config import Settings
from decagent.gateway import create_app

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"


def _guest(database, slug):
    workspace_id = uuid4()
    employee_id, person_id, credential_id = uuid4(), uuid4(), uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.workspaces(id,slug,kind) VALUES(%s,%s,'guest')",
            (workspace_id, slug),
        )
        conn.execute(
            "INSERT INTO business.people(id,name,team,workspace_id) "
            "VALUES(%s,'Queue Fixture','IT',%s)",
            (person_id, workspace_id),
        )
        conn.execute(
            "INSERT INTO business.employee_accounts"
            "(id,login,person_id,role,password_hash,workspace_id) "
            "VALUES(%s,%s,%s,'employee',%s,%s)",
            (employee_id, f"queue-{workspace_id.hex[:12]}", person_id, "x" * 32, workspace_id),
        )
        conn.execute(
            "INSERT INTO business.mcp_credentials"
            "(id,token_hash,employee_id,workspace_id,account_revision,expires_at) "
            "VALUES(%s,%s,%s,%s,1,now()+interval '1 hour')",
            (
                credential_id,
                sha256(uuid4().bytes).hexdigest(),
                employee_id,
                workspace_id,
            ),
        )
    return workspace_id, employee_id, credential_id


def _task(database, scope_id, guest, instruction="Offline queue task"):
    workspace_id, employee_id, credential_id = guest
    spec = PluginRegistry(database).get(scope_id)
    return database.create_business_task(
        scope_id,
        {"instruction": instruction, "plugin_digest": spec.digest},
        graph_version="business-2",
        employee_id=employee_id,
        credential_ref=credential_id,
        workspace_id=workspace_id,
    )


def _queue(database):
    policy = TrialBudgetPolicy(
        daily_cost_usd=Decimal("5.00"),
        daily_qwen_tokens=5_000_000,
        guest_daily_tasks=30,
        guest_daily_qwen_tokens=500_000,
        fx_snapshot=FxSnapshot(
            source_date=date(2026, 10, 20),
            cny_per_usd=Decimal("6.7200"),
            source_url="https://www.chinamoney.com.cn/english/",
        ),
    )
    ledger = TrialBudgetLedger(
        database,
        policy,
        clock=lambda: datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    return GuestExecutionQueue(database, ledger)


def _enqueue(database, queue, guest, task):
    with database.connect() as conn:
        return queue.enqueue(conn, workspace_id=guest[0], task_id=task["id"])


def test_guest_waiting_queue_is_bounded_and_counts_only_successful_enqueues(database):
    scope = PluginRegistry(database).register(builtin_manifest())
    guest = _guest(database, "queue-full")
    queue = _queue(database)
    tasks = [_task(database, scope["id"], guest) for _ in range(21)]

    for task in tasks[:20]:
        _enqueue(database, queue, guest, task)
    with pytest.raises(GuestQueueFull):
        _enqueue(database, queue, guest, tasks[20])

    with database.connect() as conn:
        admission = conn.execute(
            "SELECT task_count FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=DATE '2026-10-22' AND workspace_id=%s",
            (guest[0],),
        ).fetchone()
        queue_size = conn.execute(
            "SELECT count(*) AS total FROM business.guest_trial_task_admissions "
            "WHERE queue_state='waiting'",
        ).fetchone()["total"]
        rejected_admission = conn.execute(
            "SELECT 1 FROM business.guest_trial_task_admissions WHERE task_id=%s",
            (tasks[20]["id"],),
        ).fetchone()
    assert admission["task_count"] == 20
    assert queue_size == 20
    assert rejected_admission is None


def test_guest_cancellation_releases_waiting_capacity_without_refunding_task_count(database):
    scope = PluginRegistry(database).register(builtin_manifest())
    guest = _guest(database, "queue-cancel")
    queue = _queue(database)
    tasks = [_task(database, scope["id"], guest) for _ in range(21)]
    for task in tasks[:20]:
        _enqueue(database, queue, guest, task)

    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='cancelled' WHERE id=%s", (tasks[0]["id"],))
    _enqueue(database, queue, guest, tasks[20])

    with database.connect() as conn:
        states = conn.execute(
            "SELECT task_id,queue_state FROM business.guest_trial_task_admissions "
            "WHERE workspace_id=%s AND task_id=ANY(%s)",
            (guest[0], [task["id"] for task in tasks]),
        ).fetchall()
        admitted = conn.execute(
            "SELECT task_count FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=DATE '2026-10-22' AND workspace_id=%s",
            (guest[0],),
        ).fetchone()["task_count"]
    assert {row["task_id"]: row["queue_state"] for row in states}[tasks[0]["id"]] == "cancelled"
    assert admitted == 21


def test_guest_claims_are_fair_and_enforce_workspace_and_global_active_limits(database):
    scope = PluginRegistry(database).register(builtin_manifest())
    guests = [_guest(database, f"queue-fair-{index}") for index in range(3)]
    first = _task(database, scope["id"], guests[0], "workspace A first")
    same_workspace = _task(database, scope["id"], guests[0], "workspace A second")
    second_workspace = _task(database, scope["id"], guests[1], "workspace B first")
    third_workspace = _task(database, scope["id"], guests[2], "workspace C first")
    queue = _queue(database)
    for guest, task in (
        (guests[0], first),
        (guests[0], same_workspace),
        (guests[1], second_workspace),
        (guests[2], third_workspace),
    ):
        _enqueue(database, queue, guest, task)

    claimed_first = queue.claim("worker-a", lease_seconds=60)
    claimed_second = queue.claim("worker-b", lease_seconds=60)
    claimed_over_limit = queue.claim("worker-c", lease_seconds=60)
    assert claimed_first["id"] == first["id"]
    assert claimed_second["id"] == second_workspace["id"]
    assert claimed_over_limit is None

    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (first["id"],),
        )
    resumed = queue.claim("worker-c", lease_seconds=60)
    assert resumed["id"] == same_workspace["id"]


def test_expired_guest_lease_is_fenced_and_only_reclaimed_once(database):
    scope = PluginRegistry(database).register(builtin_manifest())
    guest = _guest(database, "queue-reclaim")
    task = _task(database, scope["id"], guest)
    queue = _queue(database)
    _enqueue(database, queue, guest, task)
    original = queue.claim("old-worker", lease_seconds=60)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (task["id"],),
        )

    def reclaim(worker):
        result = queue.claim(worker, lease_seconds=60)
        return result["id"] if result else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reclaim, [f"recovery-{index}" for index in range(8)]))

    assert results.count(task["id"]) == 1
    recovered = database.get_task(task["id"])
    assert recovered["lease_generation"] == original["lease_generation"] + 1
    with pytest.raises(ValueError, match="lease"):
        database.assert_lease(task["id"], "old-worker", original["lease_generation"])
    assert database.assert_lease(
        task["id"], recovered["lease_owner"], original["lease_generation"] + 1
    )


def test_guest_gateway_submission_atomically_enqueues_and_exposes_position(database, tmp_path):
    scope = PluginRegistry(database).register(builtin_manifest())
    client = TestClient(
        create_app(
            database,
            Settings(
                data_dir=tmp_path,
                guest_trial_enabled=True,
                guest_rate_limit_hmac_key=b"Q" * 32,
            ),
        ),
        base_url=ORIGIN,
    )
    session = client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
    assert session.status_code == 201, session.text
    csrf = session.json()["csrf_token"]
    guest_token = client.cookies.get(GUEST_COOKIE_NAME)
    assert guest_token
    with database.connect() as conn:
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (sha256(guest_token.encode()).hexdigest(),),
        ).fetchone()["workspace_id"]
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "guest-queue-submission-1",
    }
    response = client.post(
        "/api/v2/business/tasks",
        headers=headers,
        json={"scope_id": str(scope["id"]), "instruction": "Read the ticket directory"},
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["execution_queue"] == {
        "state": "waiting",
        "position": 1,
        "reason": "guest_execution_capacity",
        "active_per_workspace_limit": 1,
        "active_global_limit": 2,
        "waiting_global_limit": 20,
    }
    replay = client.post(
        "/api/v2/business/tasks",
        headers=headers,
        json={"scope_id": str(scope["id"]), "instruction": "Read the ticket directory"},
    )
    assert replay.status_code == 201 and replay.json()["id"] == task["id"]
    listed = client.get("/api/v2/business/tasks")
    assert listed.status_code == 200
    assert listed.json()[0]["execution_queue"]["position"] == 1

    with database.connect() as conn:
        usage = conn.execute(
            "SELECT task_count FROM business.guest_trial_workspace_usage WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()
        admission = conn.execute(
            "SELECT queue_state FROM business.guest_trial_task_admissions WHERE task_id=%s",
            (task["id"],),
        ).fetchone()
    assert usage["task_count"] == 1
    assert admission["queue_state"] == "waiting"


def test_guest_gateway_full_queue_returns_429_and_rolls_back_task_and_admission(database, tmp_path):
    scope = PluginRegistry(database).register(builtin_manifest())
    app = create_app(
        database,
        Settings(
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=b"Q" * 32,
        ),
    )
    responses = []
    for index in range(21):
        client = TestClient(
            app,
            base_url=ORIGIN,
            client=(f"198.51.100.{index % 5 + 1}", 50000),
        )
        session = client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
        assert session.status_code == 201, session.text
        responses.append(
            client.post(
                "/api/v2/business/tasks",
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": session.json()["csrf_token"],
                    "Idempotency-Key": f"guest-queue-fill-{index}",
                },
                json={"scope_id": str(scope["id"]), "instruction": f"Read task {index}"},
            )
        )

    assert [response.status_code for response in responses[:20]] == [201] * 20
    assert responses[20].status_code == 429
    assert responses[20].json()["code"] == "guest_queue_full"
    assert responses[20].headers["Retry-After"] == "30"
    with database.connect() as conn:
        task_count = conn.execute(
            "SELECT count(*) AS total FROM app.tasks t JOIN business.workspaces w "
            "ON w.id=t.workspace_id WHERE t.kind='business' AND w.kind='guest'",
        ).fetchone()["total"]
        admitted = conn.execute(
            "SELECT coalesce(sum(u.task_count),0) AS total "
            "FROM business.guest_trial_workspace_usage u",
        ).fetchone()["total"]
        credentials = conn.execute(
            "SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS active,"
            "count(*) FILTER (WHERE revoked_at IS NOT NULL) AS revoked "
            "FROM business.mcp_credentials c JOIN business.workspaces w "
            "ON w.id=c.workspace_id WHERE w.kind='guest'",
        ).fetchone()
    assert task_count == 20
    assert admitted == 20
    assert credentials == {"active": 20, "revoked": 1}

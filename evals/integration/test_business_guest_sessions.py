"""Anonymous guest sessions create and restore isolated simulated companies."""

import hashlib
import multiprocessing
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings
from decagent.gateway import create_app

pytestmark = pytest.mark.integration
LOCAL_ORIGIN = "http://127.0.0.1:8000"
GUEST_COOKIE = "decagent_guest_session"
CSRF_COOKIE = "decagent_guest_csrf"


def _hold_task_lock(database_url, task_id, acquired):
    from decagent.storage.database import Database

    with Database(database_url).connect(autocommit=True) as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,46126))", (str(task_id),)
        )
        acquired.set()
        threading.Event().wait(30)


def _client(database, tmp_path, *, public_url=LOCAL_ORIGIN, guest_trial_enabled=True):
    settings = Settings(
        database_url=database.url,
        public_url=public_url,
        data_dir=tmp_path,
        guest_trial_enabled=guest_trial_enabled,
        guest_rate_limit_hmac_key=b"G" * 32 if guest_trial_enabled else None,
    )
    return TestClient(create_app(database, settings), base_url=public_url)


def _create_guest_task(database, employee_id, workspace_id, credential_ref=None):
    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest

    scope = PluginRegistry(database).register(builtin_manifest())
    if credential_ref is None:
        credential_ref = uuid4()
        credential_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        with database.connect() as conn:
            account_revision = conn.execute(
                "SELECT credential_revision FROM business.employee_accounts WHERE id=%s",
                (employee_id,),
            ).fetchone()["credential_revision"]
            conn.execute(
                "INSERT INTO business.mcp_credentials"
                "(id,token_hash,employee_id,workspace_id,account_revision,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,now()+interval '1 hour')",
                (credential_ref, credential_hash, employee_id, workspace_id, account_revision),
            )
    return database.create_business_task(
        scope["id"],
        {
            "instruction": "Disposable guest event stream fixture",
            "plugin_digest": scope["manifest_digest"],
        },
        employee_id=employee_id,
        credential_ref=credential_ref,
        workspace_id=workspace_id,
    )


def test_parallel_guest_starts_create_atomic_isolated_companies(database, tmp_path):
    clients = [_client(database, tmp_path / str(index)) for index in range(2)]

    def start(client):
        return client.post(
            "/api/v3/guest/session",
            headers={"Origin": LOCAL_ORIGIN},
            json={"workspace_id": "00000000-0000-0000-0000-000000000001"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(start, clients))

    assert [response.status_code for response in responses] == [201, 201]
    for response in responses:
        assert response.json()["is_guest"] is True
        assert "workspace_id" not in response.json()
        assert response.json()["identity"]["role"] == "employee"
        session = response.cookies[GUEST_COOKIE]
        csrf = response.cookies[CSRF_COOKIE]
        assert len(session) >= 48
        assert len(csrf) >= 32
        assert response.headers["set-cookie"].lower().count("httponly") == 1
        assert "samesite=strict" in response.headers["set-cookie"].lower()

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT s.workspace_id,s.token_hash,s.csrf_hash,w.kind,w.active "
            "FROM business.guest_sessions s "
            "JOIN business.workspaces w ON w.id=s.workspace_id ORDER BY s.created_at"
        ).fetchall()
        assert len(rows) == 2
        assert len({row["workspace_id"] for row in rows}) == 2
        assert all(row["kind"] == "guest" and row["active"] for row in rows)
        assert {row["token_hash"] for row in rows} == {
            hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
            for client in clients
        }
        assert {row["csrf_hash"] for row in rows} == {
            hashlib.sha256(client.cookies[CSRF_COOKIE].encode()).hexdigest()
            for client in clients
        }
        assert conn.execute(
            "SELECT count(*) AS count FROM business.employee_sessions"
        ).fetchone()["count"] == 2
        for row in rows:
            people = conn.execute(
                "SELECT count(*) AS count FROM business.people WHERE workspace_id=%s",
                (row["workspace_id"],),
            ).fetchone()["count"]
            categories = conn.execute(
                "SELECT count(*) AS count FROM business.categories WHERE workspace_id=%s",
                (row["workspace_id"],),
            ).fetchone()["count"]
            employees = conn.execute(
                "SELECT count(*) AS count FROM business.employee_accounts "
                "WHERE workspace_id=%s",
                (row["workspace_id"],),
            ).fetchone()["count"]
            assert (people, categories, employees) == (6, 5, 6)


def test_guest_session_restores_identity_and_rejects_forged_or_expired_cookie(
    database, tmp_path
):
    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert created.status_code == 201
    identity = created.json()["identity"]
    csrf = created.cookies[CSRF_COOKIE]

    restored = client.get("/api/v3/guest/session")
    assert restored.status_code == 200
    assert restored.json()["identity"] == identity
    assert restored.json()["csrf_token"] == csrf
    assert restored.json()["is_guest"] is True
    assert restored.json()["current_role_key"] == "employee-it"
    assert (
        client.post(
            "/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN}
        ).status_code
        == 200
    )
    assert client.get("/api/v3/guest/session").json()["identity"] == identity

    assert (
        client.post(
            "/api/v3/guest/role",
            json={"role_key": "owner"},
            headers={"Origin": LOCAL_ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    for headers in (
        {"Origin": LOCAL_ORIGIN},
        {"Origin": LOCAL_ORIGIN, "X-CSRF-Token": "forged"},
        {"Origin": "https://attacker.invalid", "X-CSRF-Token": csrf},
    ):
        assert client.post("/api/v3/guest/role", headers=headers).status_code == 403

    client.cookies.delete(CSRF_COOKIE)
    recovered = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert recovered.status_code == 200
    assert recovered.json()["identity"] == identity
    assert recovered.json()["csrf_token"] != csrf
    assert client.get("/api/v3/guest/session").json()["csrf_token"] == recovered.json()[
        "csrf_token"
    ]
    csrf = recovered.json()["csrf_token"]

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.guest_sessions SET expires_at=now()-interval '1 second' "
            "WHERE token_hash=%s",
            (hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest(),),
        )
    assert client.get("/api/v3/guest/session").status_code == 401

    client.cookies.set(GUEST_COOKIE, "forged-session-token")
    assert client.get("/api/v3/guest/session").status_code == 401


def test_guest_start_requires_exact_origin_and_loopback_host(database, tmp_path):
    client = _client(database, tmp_path)
    assert (
        client.post(
            "/api/v3/guest/session", headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v3/guest/session",
            headers={"Origin": LOCAL_ORIGIN, "Host": "attacker.invalid"},
        ).status_code
        == 403
    )


def test_guest_entry_is_closed_by_default(database, tmp_path):
    client = _client(database, tmp_path, guest_trial_enabled=False)
    status = client.get("/api/v3/guest/status")
    assert status.status_code == 200
    assert status.json() == {"enabled": False}
    response = client.post(
        "/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN}
    )
    assert response.status_code == 404
    with database.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM business.guest_sessions"
        ).fetchone()["count"] == 0


def test_guest_status_announces_enabled_trial_without_creating_a_session(database, tmp_path):
    client = _client(database, tmp_path, guest_trial_enabled=True)
    response = client.get("/api/v3/guest/status")
    assert response.status_code == 200
    assert response.json() == {"enabled": True}
    with database.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM business.guest_sessions"
        ).fetchone()["count"] == 0


def test_guest_logout_revokes_both_browser_session_rows_and_clears_cookies(database, tmp_path):
    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert created.status_code == 201
    response = client.delete(
        "/api/v3/guest/session",
        headers={
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": created.json()["csrf_token"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"logged_out": True}
    assert response.cookies.get(GUEST_COOKIE) is None
    assert response.cookies.get(CSRF_COOKIE) is None
    assert client.get("/api/v3/guest/session").status_code == 401
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT revoked_at FROM business.guest_sessions"
        ).fetchall()
        employee_rows = conn.execute(
            "SELECT revoked_at FROM business.employee_sessions"
        ).fetchall()
        assert len(rows) == len(employee_rows) == 1
        assert rows[0]["revoked_at"] is not None
        assert employee_rows[0]["revoked_at"] is not None


def test_guest_user_activity_renews_seven_day_idle_window_but_restore_does_not(
    database, tmp_path
):
    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.guest_sessions SET last_activity_at=now()-interval '6 days 23 hours',"
            "expires_at=now()+interval '1 hour' WHERE token_hash=%s",
            (token_hash,),
        )

    restored = client.get("/api/v3/guest/session")
    assert restored.status_code == 200
    with database.connect() as conn:
        before_activity = conn.execute(
            "SELECT last_activity_at FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()["last_activity_at"]
        assert before_activity < datetime.now(UTC) - timedelta(days=6)

    switched = client.post(
        "/api/v3/guest/role",
        json={"role_key": "supervisor"},
        headers={
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": created.json()["csrf_token"],
        },
    )
    assert switched.status_code == 200
    with database.connect() as conn:
        after_activity = conn.execute(
            "SELECT last_activity_at,expires_at FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        now = conn.execute("SELECT now() AS now").fetchone()["now"]
        assert after_activity["last_activity_at"] >= now - timedelta(minutes=1)
        assert after_activity["expires_at"] >= now + timedelta(days=6, hours=23)


def test_guest_session_is_rejected_after_seven_idle_days(database, tmp_path):
    from decagent.business.employee_auth import EmployeeAuth, EmployeeIdentity

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    employee_id = UUID(created.json()["identity"]["employee_id"])
    with database.connect() as conn:
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (employee_id,),
        ).fetchone()
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()["workspace_id"]
    employee_auth = EmployeeAuth(database, LOCAL_ORIGIN, False, tmp_path / "credentials")
    employee_identity = EmployeeIdentity(
        account["id"], account["person_id"], account["login"], account["role"],
        account["credential_revision"], "employee_session", token_hash,
        workspace_id=workspace_id,
    )
    issued = employee_auth.issue_mcp(employee_identity)
    mcp_token = Path(issued["credential_path"]).read_text(encoding="utf-8").strip()
    task = _create_guest_task(
        database, employee_id, workspace_id, UUID(issued["credential_ref"])
    )
    assert employee_auth.authenticate_mcp_token(mcp_token) is not None
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.guest_sessions SET last_activity_at=now()-interval '7 days 1 second',"
            "expires_at=now()+interval '1 day' WHERE token_hash=%s",
            (token_hash,),
        )
    assert client.get("/api/v3/guest/session").status_code == 401
    assert client.get("/api/v2/business/auth/session").status_code == 401
    assert employee_auth.authenticate_mcp_token(mcp_token) is None
    with pytest.raises(ValueError, match="Employee credential unavailable for task"):
        employee_auth.resolve_credential_ref(task["id"])
    with database.connect() as conn:
        row = conn.execute(
            "SELECT last_activity_at,revoked_at FROM business.guest_sessions "
            "WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        assert row["last_activity_at"] < datetime.now(UTC) - timedelta(days=7)
        assert row["revoked_at"] is None


def test_guest_background_event_stream_does_not_extend_idle_window(database, tmp_path):
    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()["workspace_id"]
    task = _create_guest_task(database, UUID(identity["employee_id"]), workspace_id)
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.guest_sessions SET last_activity_at=now()-interval '6 days',"
            "expires_at=now()+interval '1 day' WHERE token_hash=%s",
            (token_hash,),
        )

    response = client.get(
        f"/api/v2/business/tasks/{task['id']}/events?follow=false"
    )
    assert response.status_code == 200
    with database.connect() as conn:
        last_activity = conn.execute(
            "SELECT last_activity_at FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()["last_activity_at"]
        now = conn.execute("SELECT now() AS now").fetchone()["now"]
        assert last_activity < now - timedelta(days=5)


def test_guest_workspace_clear_requires_csrf_and_deactivates_only_that_workspace(
    database, tmp_path
):
    from decagent.business.employee_auth import EmployeeAuth, EmployeeIdentity

    first = _client(database, tmp_path / "first")
    second = _client(database, tmp_path / "second")
    first_session = first.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    second.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    first_hash = hashlib.sha256(first.cookies[GUEST_COOKIE].encode()).hexdigest()
    second_hash = hashlib.sha256(second.cookies[GUEST_COOKIE].encode()).hexdigest()
    first_identity = first_session.json()["identity"]
    with database.connect() as conn:
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(first_identity["employee_id"]),),
        ).fetchone()
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (first_hash,),
        ).fetchone()["workspace_id"]
    employee_auth = EmployeeAuth(database, LOCAL_ORIGIN, False, tmp_path / "credentials")
    employee_identity = EmployeeIdentity(
        account["id"],
        account["person_id"],
        account["login"],
        account["role"],
        account["credential_revision"],
        "employee_session",
        first_hash,
        workspace_id=workspace_id,
    )
    issued = employee_auth.issue_mcp(employee_identity)
    credential_token = Path(issued["credential_path"]).read_text(encoding="utf-8").strip()
    assert employee_auth.authenticate_mcp_token(credential_token) is not None
    task = _create_guest_task(
        database,
        employee_identity.employee_id,
        workspace_id,
        UUID(issued["credential_ref"]),
    )

    rejected = first.delete(
        "/api/v3/guest/workspace",
        headers={"Origin": LOCAL_ORIGIN, "X-CSRF-Token": "invalid"},
    )
    assert rejected.status_code == 403
    with database.connect() as conn:
        assert conn.execute(
            "SELECT active FROM business.workspaces WHERE id=(SELECT workspace_id "
            "FROM business.guest_sessions WHERE token_hash=%s)",
            (first_hash,),
        ).fetchone()["active"] is True

    cleared = first.delete(
        "/api/v3/guest/workspace",
        headers={
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": first_session.json()["csrf_token"],
        },
    )
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": True}
    assert first.get("/api/v3/guest/session").status_code == 401
    assert employee_auth.authenticate_mcp_token(credential_token) is None
    with pytest.raises(ValueError, match="Employee credential unavailable for task"):
        employee_auth.resolve_credential_ref(task["id"])
    with database.connect() as conn:
        tombstone = conn.execute(
            "SELECT workspace_id,cleared_at FROM business.guest_workspace_tombstones "
            "WHERE token_hash=%s",
            (first_hash,),
        ).fetchone()
        assert tombstone is not None
        assert conn.execute(
            "SELECT cleanup_completed_at FROM business.workspaces WHERE id=%s",
            (tombstone["workspace_id"],),
        ).fetchone()["cleanup_completed_at"] is not None
        assert conn.execute(
            "SELECT count(*) AS n FROM app.tasks WHERE workspace_id=%s",
            (tombstone["workspace_id"],),
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(*) AS n FROM business.employee_accounts WHERE workspace_id=%s",
            (tombstone["workspace_id"],),
        ).fetchone()["n"] == 0
        cleared_at_before_retry = tombstone["cleared_at"]
    first.cookies.set(GUEST_COOKIE, first_session.cookies[GUEST_COOKIE])
    first.cookies.set(CSRF_COOKIE, first_session.cookies[CSRF_COOKIE])
    rejected_replay = first.delete(
        "/api/v3/guest/workspace",
        headers={
            "Origin": "https://attacker.invalid",
            "X-CSRF-Token": first_session.json()["csrf_token"],
        },
    )
    assert rejected_replay.status_code == 401
    replayed_clear = first.delete(
        "/api/v3/guest/workspace",
        headers={
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": first_session.json()["csrf_token"],
        },
    )
    assert replayed_clear.status_code == 200
    assert replayed_clear.json() == {"cleared": True}
    with database.connect() as conn:
        cleared = conn.execute(
            "SELECT w.active,t.cleared_at FROM business.guest_workspace_tombstones t "
            "JOIN business.workspaces w ON w.id=t.workspace_id WHERE t.token_hash=%s",
            (first_hash,),
        ).fetchone()
        active_second = conn.execute(
            "SELECT w.active,s.revoked_at FROM business.guest_sessions s "
            "JOIN business.workspaces w ON w.id=s.workspace_id WHERE s.token_hash=%s",
            (second_hash,),
        ).fetchone()
        assert cleared["active"] is False
        assert cleared["cleared_at"] == cleared_at_before_retry
        assert active_second["active"] is True
        assert active_second["revoked_at"] is None


def test_inactive_guest_workspace_is_not_claimed_by_business_worker(database, tmp_path):
    from decagent.business.guest_auth import GuestAuth
    from decagent.business.worker import BusinessWorker

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        session = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(identity["employee_id"]),),
        ).fetchone()
    from decagent.business.employee_auth import EmployeeIdentity

    employee_identity = EmployeeIdentity(
        account["id"], account["person_id"], account["login"], account["role"],
        account["credential_revision"], "employee_session", token_hash,
        workspace_id=session["workspace_id"],
    )
    _create_guest_task(database, employee_identity.employee_id, employee_identity.workspace_id)

    assert GuestAuth(database, LOCAL_ORIGIN, False).clear_workspace(employee_identity)
    worker = BusinessWorker(database, Settings(database_url=database.url, data_dir=tmp_path))

    assert worker.claim() is None


def test_guest_cleanup_waits_for_expired_worker_lease_and_advisory_lock(database, tmp_path):
    from decagent.business.guest_auth import GuestAuth
    from decagent.business.guest_cleanup import GuestWorkspaceCleaner
    from decagent.business.worker import BusinessWorker

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity_data = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        session = conn.execute(
            "SELECT workspace_id,csrf_hash FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(identity_data["employee_id"]),),
        ).fetchone()
    from decagent.business.employee_auth import EmployeeIdentity

    identity = EmployeeIdentity(
        account["id"],
        account["person_id"],
        account["login"],
        account["role"],
        account["credential_revision"],
        "employee_session",
        token_hash,
        workspace_id=session["workspace_id"],
    )
    task = _create_guest_task(database, identity.employee_id, identity.workspace_id)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='running',lease_owner='worker-test',"
            "lease_generation=1,lease_until=clock_timestamp()+interval '1 minute' WHERE id=%s",
            (task["id"],),
        )
    from datetime import UTC, datetime

    checkpoint_id = str(uuid4())
    with database.checkpointer() as saver:
        saver.put(
            {"configurable": {"thread_id": str(task["id"]), "checkpoint_ns": ""}},
            {
                "v": 1,
                "id": checkpoint_id,
                "ts": datetime.now(UTC).isoformat(),
                "channel_values": {"private": "guest-checkpoint"},
                "channel_versions": {"private": "1"},
                "versions_seen": {},
                "pending_sends": [],
            },
            {"source": "input", "step": 0, "writes": {}},
            {},
        )
    assert GuestAuth(database, LOCAL_ORIGIN, False).clear_workspace(identity)

    cleaner = GuestWorkspaceCleaner(database)
    assert cleaner.clean_workspace(identity.workspace_id).status == "pending"
    with database.connect() as conn:
        assert conn.execute(
            "SELECT id FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone() is not None

    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (task["id"],),
        )
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    lock_process = context.Process(
        target=_hold_task_lock, args=(database.url, task["id"], acquired)
    )
    lock_process.start()
    try:
        assert acquired.wait(10)
        assert cleaner.clean_workspace(identity.workspace_id).status == "pending"
    finally:
        lock_process.terminate()
        lock_process.join(10)
    assert not lock_process.is_alive()

    worker = BusinessWorker(database, Settings(database_url=database.url, data_dir=tmp_path))
    assert worker.claim() is None
    restarted_cleaner = GuestWorkspaceCleaner(database)
    assert restarted_cleaner.clean_workspace(identity.workspace_id).status == "complete"
    with database.connect() as conn:
        assert conn.execute(
            "SELECT id FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone() is None
        cleanup = conn.execute(
            "SELECT cleanup_completed_at FROM business.workspaces WHERE id=%s",
            (identity.workspace_id,),
        ).fetchone()
        assert cleanup["cleanup_completed_at"] is not None
        assert conn.execute(
            "SELECT count(*) AS n FROM checkpoint.checkpoints WHERE thread_id=%s",
            (str(task["id"]),),
        ).fetchone()["n"] == 0


def test_guest_cleanup_preserves_ambiguous_operation_and_is_safe_after_restart(
    database, tmp_path
):
    from decagent.business.employee_auth import EmployeeIdentity
    from decagent.business.guest_auth import GuestAuth
    from decagent.business.guest_cleanup import GuestWorkspaceCleaner
    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity_data = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        session = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(identity_data["employee_id"]),),
        ).fetchone()
    identity = EmployeeIdentity(
        account["id"], account["person_id"], account["login"], account["role"],
        account["credential_revision"], "employee_session", token_hash,
        workspace_id=session["workspace_id"],
    )
    scope = PluginRegistry(database).register(builtin_manifest())
    operation_id = "guest-unknown-operation"
    digest = "a" * 64
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.operations"
            "(id,employee_id,workspace_id,request_id,scope_id,command,params,effective_params,"
            "input_hash,contract_digest,source,state,confirmation_required) "
            "VALUES(%s,%s,%s,%s,%s,'create','{}','{}',%s,%s,'external_mcp','unknown',false)",
            (operation_id, identity.employee_id, identity.workspace_id, operation_id,
             scope["id"], digest, digest),
        )
        conn.execute(
            "INSERT INTO business.operation_receipts"
            "(operation_id,workspace_id,input_hash,command,employee_id,contract_digest,result) "
            "VALUES(%s,%s,%s,'create',%s,%s,'{}')",
            (operation_id, identity.workspace_id, "b" * 64, identity.employee_id, digest),
        )
    assert GuestAuth(database, LOCAL_ORIGIN, False).clear_workspace(identity)

    first = GuestWorkspaceCleaner(database).clean_workspace(identity.workspace_id)
    second = GuestWorkspaceCleaner(database).clean_workspace(identity.workspace_id)

    assert first.status == second.status == "pending"
    with database.connect() as conn:
        operation = conn.execute(
            "SELECT state FROM business.operations WHERE id=%s", (operation_id,)
        ).fetchone()
        assert operation["state"] == "unknown"
        assert conn.execute(
            "SELECT count(*) AS n FROM business.operation_receipts WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()["n"] == 1


def test_guest_cleanup_removes_unknown_only_after_receipt_verifies(database, tmp_path):
    from decagent.business.employee_auth import EmployeeIdentity
    from decagent.business.guest_auth import GuestAuth
    from decagent.business.guest_cleanup import GuestWorkspaceCleaner
    from decagent.business.operations import OperationService
    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity_data = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        session = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(identity_data["employee_id"]),),
        ).fetchone()
        category = conn.execute(
            "SELECT id FROM business.categories WHERE workspace_id=%s ORDER BY name LIMIT 1",
            (session["workspace_id"],),
        ).fetchone()["id"]
    identity = EmployeeIdentity(
        account["id"], account["person_id"], account["login"], account["role"],
        account["credential_revision"], "employee_session", token_hash,
        workspace_id=session["workspace_id"],
    )
    PluginRegistry(database).register(builtin_manifest())
    operations = OperationService(database)
    prepared = operations.prepare(
        identity,
        "guest-cleanup-verified-unknown",
        "create",
        {"title": "cleanup receipt proof", "category_id": str(category)},
    )
    if prepared["state"] == "awaiting_approval":
        operations.reply(identity, prepared["operation_id"], prepared["input_hash"], "approve")
    committed = operations.commit(identity, prepared["operation_id"])
    assert committed["state"] == "succeeded"
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.operations SET state='unknown',result=NULL,error_code='lost_response' "
            "WHERE id=%s",
            (prepared["operation_id"],),
        )
        ticket_id = committed["result"]["ticket"]["id"]
        revision = conn.execute(
            "SELECT revision FROM business.tickets WHERE id=%s AND workspace_id=%s",
            (UUID(ticket_id), identity.workspace_id),
        ).fetchone()["revision"]
    assert GuestAuth(database, LOCAL_ORIGIN, False).clear_workspace(identity)

    cleaner = GuestWorkspaceCleaner(database)
    assert cleaner.clean_workspace(identity.workspace_id).status == "complete"
    restarted_cleaner = GuestWorkspaceCleaner(database)
    assert restarted_cleaner.clean_workspace(identity.workspace_id).status == "complete"
    with database.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM business.tickets WHERE id=%s", (UUID(ticket_id),)
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT revision FROM business.tickets WHERE id=%s", (UUID(ticket_id),)
        ).fetchone() is None
    assert revision == 1


def test_guest_cleanup_expires_idle_workspace_without_waiting_for_login(database, tmp_path):
    from decagent.business.guest_cleanup import GuestWorkspaceCleaner

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert created.status_code == 201
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()["workspace_id"]
        conn.execute(
            "UPDATE business.guest_sessions SET last_activity_at=now()-interval '8 days' "
            "WHERE token_hash=%s",
            (token_hash,),
        )

    result = GuestWorkspaceCleaner(database).run_once()

    assert len(result) == 1 and result[0].status == "complete"
    with database.connect() as conn:
        workspace = conn.execute(
            "SELECT active,cleanup_completed_at FROM business.workspaces WHERE id=%s",
            (workspace_id,),
        ).fetchone()
        assert workspace["active"] is False
        assert workspace["cleanup_completed_at"] is not None
        assert conn.execute(
            "SELECT count(*) AS n FROM business.guest_sessions WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()["n"] == 0


def test_clear_during_worker_execution_waits_before_removing_late_checkpoint(
    database, tmp_path
):
    from types import SimpleNamespace

    from decagent.business.employee_auth import EmployeeIdentity
    from decagent.business.guest_auth import GuestAuth
    from decagent.business.guest_cleanup import GuestWorkspaceCleaner
    from decagent.business.guest_queue import GuestExecutionQueue
    from decagent.business.trial_budget import TrialBudgetLedger, TrialBudgetPolicy
    from decagent.business.worker import BusinessWorker

    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    identity_data = created.json()["identity"]
    token_hash = hashlib.sha256(client.cookies[GUEST_COOKIE].encode()).hexdigest()
    with database.connect() as conn:
        session = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE token_hash=%s",
            (token_hash,),
        ).fetchone()
        account = conn.execute(
            "SELECT id,person_id,login,role,credential_revision "
            "FROM business.employee_accounts WHERE id=%s",
            (UUID(identity_data["employee_id"]),),
        ).fetchone()
    identity = EmployeeIdentity(
        account["id"], account["person_id"], account["login"], account["role"],
        account["credential_revision"], "employee_session", token_hash,
        workspace_id=session["workspace_id"],
    )
    task = _create_guest_task(database, identity.employee_id, identity.workspace_id)
    with database.connect() as conn:
        GuestExecutionQueue(
            database, TrialBudgetLedger(database, TrialBudgetPolicy())
        ).enqueue(
            conn, workspace_id=identity.workspace_id, task_id=task["id"]
        )
    entered = threading.Event()
    release = threading.Event()
    failures = []

    class BlockingGraph:
        def __init__(self, saver):
            self.saver = saver

        def get_state(self, _config):
            return SimpleNamespace(values={}, next=(), tasks=())

        def invoke(self, _argument, config):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test graph was not released")
            self.saver.put(
                {"configurable": {"thread_id": str(task["id"]), "checkpoint_ns": ""}},
                {
                    "v": 1,
                    "id": str(uuid4()),
                    "ts": datetime.now(UTC).isoformat(),
                    "channel_values": {"private": "late-worker-checkpoint"},
                    "channel_versions": {"private": "1"},
                    "versions_seen": {},
                    "pending_sends": [],
                },
                {"source": "input", "step": 0, "writes": {}},
                {},
            )
            return {"completion": {"valid": True}}

    worker = BusinessWorker(
        database,
        Settings(
            database_url=database.url,
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=b"G" * 32,
        ),
        services_factory=lambda *_args: SimpleNamespace(close=lambda: None),
        graph_factory=lambda _services, saver: BlockingGraph(saver),
        heartbeat_seconds=0.02,
        lease_seconds=0.2,
    )

    def run_worker():
        try:
            worker.run_once()
        except Exception as exc:  # surfaced in the test thread
            failures.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    assert entered.wait(5)
    assert GuestAuth(database, LOCAL_ORIGIN, False).clear_workspace(identity)
    cleaner = GuestWorkspaceCleaner(database)
    assert cleaner.clean_workspace(identity.workspace_id).status == "pending"
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert failures == []
    time.sleep(0.25)

    assert cleaner.clean_workspace(identity.workspace_id).status == "complete"
    with database.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM checkpoint.checkpoints WHERE thread_id=%s",
            (str(task["id"]),),
        ).fetchone()["n"] == 0


def test_guest_cookies_are_secure_for_https_and_local_http_is_not_secure(
    database, tmp_path
):
    https_origin = "https://agent.example.test"
    client = _client(database, tmp_path, public_url=https_origin)
    response = client.post(
        "/api/v3/guest/session", headers={"Origin": https_origin}
    )
    assert response.status_code == 201
    cookies = response.headers.get_list("set-cookie")
    assert any(GUEST_COOKIE in cookie and "secure" in cookie.lower() for cookie in cookies)
    assert any(CSRF_COOKIE in cookie and "secure" in cookie.lower() for cookie in cookies)

    local = _client(database, tmp_path / "local")
    response = local.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert response.status_code == 201
    cookies = response.headers.get_list("set-cookie")
    assert all("secure" not in cookie.lower() for cookie in cookies)


def test_guest_role_switch_is_scoped_and_keeps_tasks_bound_to_original_employee(
    database, tmp_path
):
    client = _client(database, tmp_path)
    created = client.post("/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN})
    assert created.status_code == 201
    original = created.json()["identity"]
    original_employee = UUID(original["employee_id"])
    csrf = created.json()["csrf_token"]
    with database.connect() as conn:
        workspace_id = conn.execute(
            "SELECT workspace_id FROM business.guest_sessions WHERE employee_id=%s",
            (original_employee,),
        ).fetchone()["workspace_id"]

    from decagent.business.registry import PluginRegistry
    from decagent.business.tickets import builtin_manifest

    scope = PluginRegistry(database).register(builtin_manifest())
    credential_ref = uuid4()
    credential_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.mcp_credentials"
            "(id,token_hash,employee_id,workspace_id,account_revision,expires_at) "
            "VALUES(%s,%s,%s,%s,1,now()+interval '1 hour')",
            (credential_ref, credential_hash, original_employee, workspace_id),
        )
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "Keep this simulated task bound to its creating employee",
            "plugin_digest": scope["manifest_digest"],
        },
        employee_id=original_employee,
        credential_ref=credential_ref,
        workspace_id=workspace_id,
    )
    approval_id, approval_digest = uuid4(), "f" * 64
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_approval',phase='human' WHERE id=%s",
            (task["id"],),
        )
        conn.execute(
            "INSERT INTO app.approvals(id,task_id,digest,kind,workspace_id) "
            "VALUES(%s,%s,%s,'clarification',%s)",
            (approval_id, task["id"], approval_digest, workspace_id),
        )
    task_url = f"/api/v2/business/tasks/{task['id']}"
    assert client.get(task_url).status_code == 200

    switched = client.post(
        "/api/v3/guest/role",
        json={"role_key": "supervisor"},
        headers={"Origin": LOCAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert switched.status_code == 200
    assert switched.json()["identity"]["role"] == "supervisor"
    assert switched.json()["current_role_key"] == "supervisor"
    assert switched.json()["identity"]["employee_id"] != original["employee_id"]
    assert switched.json()["identity"]["source"] == "employee_session"
    assert "workspace_id" not in switched.json()["identity"]
    assert switched.json()["csrf_token"] != csrf
    assert len(switched.json()["roles"]) == 6
    assert client.get(task_url).status_code == 404
    denied_reply = client.post(
        f"/api/v2/business/tasks/{task['id']}/approvals/{approval_id}/reply",
        json={"expected_digest": approval_digest, "decision": "approve"},
        headers={
            "Origin": LOCAL_ORIGIN,
            "X-CSRF-Token": switched.json()["csrf_token"],
            "Idempotency-Key": "guest-role-cannot-consume-old-approval",
        },
    )
    assert denied_reply.status_code == 404
    with database.connect() as conn:
        approval = conn.execute(
            "SELECT status FROM app.approvals WHERE id=%s", (approval_id,)
        ).fetchone()
        assert approval["status"] == "pending"
    assert (
        client.post(
            "/api/v3/guest/role",
            json={"role_key": "employee-it"},
            headers={"Origin": LOCAL_ORIGIN, "X-CSRF-Token": csrf},
        ).status_code
        == 403
    )

    switched_back = client.post(
        "/api/v3/guest/role",
        json={"role_key": "employee-it"},
        headers={"Origin": LOCAL_ORIGIN, "X-CSRF-Token": switched.json()["csrf_token"]},
    )
    assert switched_back.status_code == 200
    assert switched_back.json()["identity"]["employee_id"] == original["employee_id"]
    assert client.get(task_url).status_code == 200
    restored_task = client.get(task_url).json()
    assert restored_task["approvals"][0]["id"] == str(approval_id)
    assert restored_task["approvals"][0]["status"] == "pending"
    with database.connect() as conn:
        row = conn.execute(
            "SELECT employee_id,workspace_id FROM app.tasks WHERE id=%s", (task["id"],)
        ).fetchone()
        assert row["employee_id"] == original_employee
        assert row["workspace_id"] == workspace_id


def test_guest_company_initialization_rolls_back_as_one_transaction(database, tmp_path):
    with database.connect() as conn:
        conn.execute(
            "CREATE FUNCTION business.reject_guest_session_insert() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected guest failure'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER reject_guest_session_insert BEFORE INSERT "
            "ON business.guest_sessions FOR EACH ROW "
            "EXECUTE FUNCTION business.reject_guest_session_insert()"
        )

    app = create_app(
        database,
        Settings(
            database_url=database.url,
            public_url=LOCAL_ORIGIN,
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=b"G" * 32,
        ),
    )
    with TestClient(app, base_url=LOCAL_ORIGIN, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v3/guest/session", headers={"Origin": LOCAL_ORIGIN}
        )
    assert response.status_code == 503
    with database.connect() as conn:
        assert conn.execute(
            "SELECT count(*) AS count FROM business.workspaces WHERE kind='guest'"
        ).fetchone()["count"] == 0
        assert (
            conn.execute("SELECT count(*) AS count FROM business.people").fetchone()["count"]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) AS count FROM business.categories").fetchone()["count"]
            == 0
        )
        assert conn.execute(
            "SELECT count(*) AS count FROM business.employee_accounts"
        ).fetchone()["count"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS count FROM business.guest_sessions"
            ).fetchone()["count"]
            == 0
        )

"""Employee browser and MCP credentials have distinct, database-backed audiences."""

import hashlib
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.employee_accounts import hash_employee_password, seed_employee_accounts
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore
from decagent.business.workspaces import LEGACY_WORKSPACE_ID
from decagent.config import Settings
from decagent.gateway import create_app, initialize_owner
from decagent.storage.usage import UsageLedger

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def employee_api(database, tmp_path, request):
    assert TicketStore(database.url).execute("seed", {}, "employee-auth-seed")["ok"]
    directory = tmp_path / "demo-auth"
    seed_employee_accounts(database, directory)
    scope = PluginRegistry(database).register(builtin_manifest())
    owner_token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    guest_trial_enabled = bool(getattr(request, "param", False))
    app = create_app(
        database,
        Settings(
            database_url=database.url,
            data_dir=tmp_path,
            guest_trial_enabled=guest_trial_enabled,
            guest_rate_limit_hmac_key=b"G" * 32 if guest_trial_enabled else None,
        ),
    )
    return (
        database,
        TestClient(app),
        getattr(app.state, "employee_auth", None),
        directory,
        scope,
        owner_token,
    )


def _login(client, directory, login="employee-it", password=None):
    password = password or (directory / f"{login}.password").read_text().strip()
    return client.post(
        "/api/v2/business/auth/session",
        json={"login": login, "password": password},
        headers={"Origin": ORIGIN},
    )


def _csrf(response):
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_employee_login_uses_account_identity_and_rejects_bad_or_forged_credentials(employee_api):
    database, client, _, directory, _, owner_token = employee_api
    password = (directory / "employee-it.password").read_text().strip()
    for login, candidate in (("employee-it", "wrong password"), ("missing", password)):
        response = _login(client, directory, login, candidate)
        assert response.status_code == 401
        assert password not in response.text and candidate not in response.text
    assert _login(client, directory, password=owner_token).status_code == 401
    assert (
        client.post(
            "/api/v2/business/auth/session",
            json={"login": "employee-it", "password": password, "role": "supervisor"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 422
    )
    signed_in = _login(client, directory)
    assert signed_in.status_code == 200
    response = client.get("/api/v2/business/auth/session")
    assert response.status_code == 200
    with database.connect() as conn:
        account = conn.execute(
            "SELECT id,person_id,role FROM business.employee_accounts WHERE login='employee-it'"
        ).fetchone()
        session = conn.execute("SELECT * FROM business.employee_sessions").fetchone()
    assert response.json() == {
        "employee_id": str(account["id"]),
        "person_id": str(account["person_id"]),
        "login": "employee-it",
        "role": account["role"],
        "source": "employee_session",
    }
    assert session["employee_id"] == account["id"]
    assert session["expires_at"] - session["created_at"] == timedelta(hours=12)
    assert password not in str(session)
    assert "httponly" in signed_in.headers["set-cookie"].lower()
    assert "samesite=strict" in signed_in.headers["set-cookie"].lower()


def test_business_worker_rejects_recovered_credential_from_another_workspace():
    from decagent.business.mcp_client import MCPAuthenticationError
    from decagent.business.worker import BusinessWorker

    employee_id, task_workspace, credential_workspace = uuid4(), uuid4(), uuid4()

    class Auth:
        def resolve_credential_ref(self, task_id):
            assert task_id == "task-id"
            return "protected-token"

        def authenticate_mcp_token(self, token):
            assert token == "protected-token"
            return SimpleNamespace(employee_id=employee_id, workspace_id=credential_workspace)

    class Manager:
        def client(self, identity, token):
            return (identity, token)

    worker = object.__new__(BusinessWorker)
    worker.employee_auth = Auth()
    worker.mcp_manager = Manager()
    task = {
        "id": "task-id",
        "employee_id": employee_id,
        "workspace_id": task_workspace,
    }
    with pytest.raises(MCPAuthenticationError, match="Original employee"):
        worker._client(task)


def test_browser_session_enforces_origin_csrf_expiry_and_account_state(employee_api):
    database, client, _, directory, _, owner_token = employee_api
    assert _login(client, directory).status_code == 200
    assert client.get("/api/v2/business/auth/session").status_code == 200
    assert (
        client.get(
            "/api/v2/business/auth/session", headers={"Authorization": "Bearer " + owner_token}
        ).status_code
        == 401
    )
    assert (
        client.post("/api/v2/business/auth/mcp-credentials", headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v2/business/auth/mcp-credentials",
            headers={"Origin": "https://evil.invalid", "X-CSRF-Token": "forged"},
        ).status_code
        == 403
    )
    with database.connect() as conn:
        conn.execute("UPDATE business.employee_sessions SET expires_at=now()-interval '1 second'")
    assert client.get("/api/v2/business/auth/session").status_code == 401
    assert _login(client, directory).status_code == 200
    with database.connect() as conn:
        conn.execute("UPDATE business.employee_accounts SET active=false WHERE login='employee-it'")
    assert client.get("/api/v2/business/auth/session").status_code == 401
    assert _login(client, directory).status_code == 401
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET active=true,"
            "credential_revision=credential_revision+1 WHERE login='employee-it'"
        )
    assert _login(client, directory).status_code == 200
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE login='employee-it'"
        )
    assert client.get("/api/v2/business/auth/session").status_code == 401


def test_mcp_token_has_independent_audience_lifetime_and_revocation(employee_api):
    database, client, auth, directory, _, owner_token = employee_api
    login = _login(client, directory)
    assert login.status_code == 200, login.text
    issued = client.post("/api/v2/business/auth/mcp-credentials", headers=_csrf(login))
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert set(body) == {"credential_ref", "credential_path", "expires_at"}
    credential_ref = UUID(body["credential_ref"])
    path = directory / f"mcp-{credential_ref}.token"
    assert body["credential_path"] == str(path)
    token = path.read_text().strip()
    assert token.startswith("dcm_")
    assert token not in issued.text
    assert auth.authenticate_mcp_token(token).employee_id
    assert auth.authenticate_mcp_token(owner_token) is None
    assert auth.authenticate_mcp_token(client.cookies["decagent_employee_session"]) is None
    assert (
        client.get(
            "/api/v2/business/auth/session", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    with database.connect() as conn:
        saved = conn.execute(
            "SELECT * FROM business.mcp_credentials WHERE id=%s", (credential_ref,)
        ).fetchone()
    assert saved["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert saved["expires_at"] - saved["created_at"] == timedelta(hours=1)
    assert token not in str(saved)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    logout = client.delete("/api/v2/business/auth/session", headers=_csrf(login))
    assert logout.status_code == 200
    assert auth.authenticate_mcp_token(token).employee_id == saved["employee_id"]
    login = _login(client, directory)
    assert login.status_code == 200
    revoked = client.delete(
        f"/api/v2/business/auth/mcp-credentials/{credential_ref}", headers=_csrf(login)
    )
    assert revoked.status_code == 200
    assert not path.exists()
    assert auth.authenticate_mcp_token(token) is None
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT revoked_at FROM business.mcp_credentials WHERE id=%s", (credential_ref,)
            ).fetchone()["revoked_at"]
            is not None
        )


def test_credential_resolver_requires_matching_task_employee_and_current_credential(employee_api):
    database, client, auth, directory, scope, _ = employee_api
    login = _login(client, directory)
    issued = client.post("/api/v2/business/auth/mcp-credentials", headers=_csrf(login))
    ref = UUID(issued.json()["credential_ref"])
    token = (directory / f"mcp-{ref}.token").read_text().strip()
    with database.connect() as conn:
        employee_id = conn.execute(
            "SELECT id FROM business.employee_accounts WHERE login='employee-it'"
        ).fetchone()["id"]
        other_id = conn.execute(
            "SELECT id FROM business.employee_accounts WHERE login='supervisor'"
        ).fetchone()["id"]
    task = database.create_business_task(
        scope["id"], {"instruction": "read a ticket", "plugin_digest": scope["manifest_digest"]}
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET employee_id=%s,credential_ref=%s WHERE id=%s",
            (employee_id, ref, task["id"]),
        )
    assert auth.resolve_credential_ref(task["id"]) == token
    with pytest.raises(psycopg.errors.CheckViolation, match="employee is immutable"):
        with database.connect() as conn:
            conn.execute("UPDATE app.tasks SET employee_id=%s WHERE id=%s", (other_id, task["id"]))
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.mcp_credentials SET expires_at=now()-interval '1 second' WHERE id=%s",
            (ref,),
        )
    with pytest.raises(ValueError, match="unavailable"):
        auth.resolve_credential_ref(task["id"])
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.mcp_credentials SET expires_at=now()+interval '1 hour' WHERE id=%s",
            (ref,),
        )
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE id=%s",
            (employee_id,),
        )
    with pytest.raises(ValueError, match="unavailable"):
        auth.resolve_credential_ref(task["id"])


@pytest.mark.parametrize("employee_api", [True], indirect=True)
def test_authenticated_employee_workspace_is_database_bound_and_request_cannot_override(
    employee_api,
):
    database, client, auth, directory, scope, _ = employee_api
    workspace_id, person_id, employee_id = uuid4(), uuid4(), uuid4()
    supervisor_person_id, supervisor_id = uuid4(), uuid4()
    handler_person_id, handler_id = uuid4(), uuid4()
    ticket_id, category_id = uuid4(), uuid4()
    password = "workspace-test-password"
    fixture_password_hash = hash_employee_password("fixture-only-password")
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO business.workspaces(id,slug,kind) VALUES(%s,'guest-auth-test','guest')",
            (workspace_id,),
        )
        connection.execute(
            "INSERT INTO business.people(id,name,team,workspace_id) "
            "VALUES(%s,'Guest employee','Support',%s)",
            (person_id, workspace_id),
        )
        connection.execute(
            "INSERT INTO business.employee_accounts"
            "(id,login,person_id,role,password_hash,workspace_id) "
            "VALUES(%s,'guest-auth-test',%s,'employee',%s,%s)",
            (employee_id, person_id, hash_employee_password(password), workspace_id),
        )
        connection.execute(
            "INSERT INTO business.people(id,name,team,workspace_id) "
            "VALUES(%s,'Guest supervisor','Support',%s),(%s,'Guest handler','Support',%s)",
            (supervisor_person_id, workspace_id, handler_person_id, workspace_id),
        )
        connection.execute(
            "INSERT INTO business.employee_accounts"
            "(id,login,person_id,role,password_hash,workspace_id) "
            "VALUES(%s,'guest-supervisor',%s,'supervisor',%s,%s),"
            "(%s,'guest-handler',%s,'handler',%s,%s)",
            (
                supervisor_id,
                supervisor_person_id,
                fixture_password_hash,
                workspace_id,
                handler_id,
                handler_person_id,
                fixture_password_hash,
                workspace_id,
            ),
        )
        legacy_employee_id = connection.execute(
            "SELECT id FROM business.employee_accounts WHERE login='employee-it'"
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO business.categories(id,name,workspace_id) VALUES(%s,'Guest category',%s)",
            (category_id, workspace_id),
        )
        connection.execute(
            "INSERT INTO business.tickets"
            "(id,title,category_id,requester_id,employee_id,workspace_id) "
            "VALUES(%s,'Guest-only ticket',%s,%s,%s,%s)",
            (ticket_id, category_id, person_id, employee_id, workspace_id),
        )

    login = _login(client, directory, "guest-auth-test", password)
    assert login.status_code == 200, login.text
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO business.guest_sessions"
            "(token_hash,workspace_id,employee_id,account_revision,csrf_hash,expires_at) "
            "SELECT %s,a.workspace_id,a.id,a.credential_revision,%s,now()+interval '7 days' "
            "FROM business.employee_accounts a WHERE a.id=%s AND a.workspace_id=%s",
            (
                hashlib.sha256(client.cookies["decagent_employee_session"].encode()).hexdigest(),
                hashlib.sha256(login.json()["csrf_token"].encode()).hexdigest(),
                employee_id,
                workspace_id,
            ),
        )
    browser_identity = auth.authenticate_browser(
        SimpleNamespace(headers={}, cookies=dict(client.cookies))
    )
    assert browser_identity is not None
    assert browser_identity.workspace_id == workspace_id
    assert "workspace_id" not in browser_identity.public()
    with database.connect() as connection:
        guest_directory = TicketStore(database.url)._read(
            connection, "directory", {}, workspace_id=browser_identity.workspace_id
        )
        assert {person["id"] for person in guest_directory["people"]} == {
            str(person_id),
            str(supervisor_person_id),
            str(handler_person_id),
        }
        assert [category["id"] for category in guest_directory["categories"]] == [str(category_id)]
        assert (
            access.get_ticket(connection, employee_id, ticket_id, workspace_id=workspace_id)["id"]
            == ticket_id
        )
        assert [
            ticket["id"]
            for ticket in access.list_tickets(
                connection, employee_id, {}, workspace_id=workspace_id
            )
        ] == [ticket_id]
        with pytest.raises(AccessError, match="unavailable"):
            access.get_ticket(
                connection, legacy_employee_id, ticket_id, workspace_id=LEGACY_WORKSPACE_ID
            )
        assert not access.list_tickets(
            connection, legacy_employee_id, {}, workspace_id=LEGACY_WORKSPACE_ID
        )
        assert access.authorize_write(
            connection,
            supervisor_id,
            "assign",
            {
                "ticket_id": str(ticket_id),
                "expected_revision": 1,
                "person_id": str(handler_person_id),
            },
            workspace_id=workspace_id,
        )["person_id"] == str(handler_person_id)
        legacy_person_id = connection.execute(
            "SELECT person_id FROM business.employee_accounts WHERE id=%s", (legacy_employee_id,)
        ).fetchone()["person_id"]
        with pytest.raises(AccessError, match="Assignable employee unavailable"):
            access.authorize_write(
                connection,
                supervisor_id,
                "assign",
                {
                    "ticket_id": str(ticket_id),
                    "expected_revision": 1,
                    "person_id": str(legacy_person_id),
                },
                workspace_id=workspace_id,
            )

    issued = client.post("/api/v2/business/auth/mcp-credentials", headers=_csrf(login))
    assert issued.status_code == 201
    credential_ref = UUID(issued.json()["credential_ref"])
    mcp_token = (directory / f"mcp-{credential_ref}.token").read_text().strip()
    mcp_identity = auth.authenticate_mcp_token(mcp_token)
    assert mcp_identity is not None and mcp_identity.workspace_id == workspace_id
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT workspace_id FROM business.employee_sessions WHERE employee_id=%s",
                (employee_id,),
            ).fetchone()["workspace_id"]
            == workspace_id
        )
        assert (
            connection.execute(
                "SELECT workspace_id FROM business.mcp_credentials WHERE id=%s",
                (credential_ref,),
            ).fetchone()["workspace_id"]
            == workspace_id
        )

    foreign_identity = replace(browser_identity, workspace_id=uuid4())
    forged_role_identity = replace(
        browser_identity,
        employee_id=supervisor_id,
        person_id=supervisor_person_id,
        role="supervisor",
    )
    stale_revision_identity = replace(
        browser_identity, account_revision=browser_identity.account_revision + 1
    )
    mutation_request = SimpleNamespace(
        headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
        cookies=dict(client.cookies),
    )
    assert not auth.valid_mutation(
        mutation_request,
        foreign_identity,
    )
    assert not auth.valid_mutation(mutation_request, forged_role_identity)
    assert not auth.valid_mutation(mutation_request, stale_revision_identity)
    with pytest.raises(ValueError, match="session unavailable"):
        auth.issue_mcp(foreign_identity)

    denied = client.post(
        "/api/v2/business/tasks",
        json={
            "scope_id": str(scope["id"]),
            "instruction": "Create a guest task",
            "workspace_id": str(UUID(int=0)),
        },
        headers=_csrf(login),
    )
    assert denied.status_code == 422

    submitted = client.post(
        "/api/v2/business/tasks",
        json={"scope_id": str(scope["id"]), "instruction": "Read my workspace tickets"},
        headers={**_csrf(login), "Idempotency-Key": "guest-workspace-task"},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = UUID(submitted.json()["id"])
    assert client.get(f"/api/v2/business/tasks/{task_id}").status_code == 200
    for route in (
        f"/api/v2/business/tasks/{task_id}/plan",
        f"/api/v2/business/tasks/{task_id}/trace",
        f"/api/v2/business/tasks/{task_id}/export",
    ):
        response = client.get(route)
        assert response.status_code == 200, (route, response.status_code, response.text)
    with database.connect() as connection:
        task = connection.execute(
            "SELECT id,employee_id,credential_ref,workspace_id FROM app.tasks WHERE id=%s",
            (task_id,),
        ).fetchone()
        task_credential = connection.execute(
            "SELECT workspace_id,employee_id FROM business.mcp_credentials WHERE id=%s",
            (task["credential_ref"],),
        ).fetchone()
        event = connection.execute(
            "SELECT workspace_id FROM app.events WHERE task_id=%s", (task_id,)
        ).fetchone()
    assert task["employee_id"] == employee_id
    assert task["workspace_id"] == workspace_id
    assert task_credential["workspace_id"] == workspace_id
    assert task_credential["employee_id"] == employee_id
    assert event["workspace_id"] == workspace_id

    from decagent.business.worker import BusinessWorker

    class CapturingManager:
        def client(self, identity, token):
            self.identity, self.token = identity, token
            return identity

    worker = object.__new__(BusinessWorker)
    worker.employee_auth = auth
    worker.mcp_manager = CapturingManager()
    restored_identity = worker._client(task)
    assert restored_identity.employee_id == employee_id
    assert restored_identity.workspace_id == workspace_id
    task_token = (directory / f"mcp-{task['credential_ref']}.token").read_text().strip()
    assert worker.mcp_manager.token == task_token

    # Task-owned runtime rows must inherit the trusted tenant from the task, even
    # when a legacy writer relies on the schema default for workspace_id.
    action = database.plan_action(task_id, "workspace-action", "a" * 64, {"operation": "read"})
    approval = database.ensure_approval(task_id, action["id"], action["digest"], "action")
    assert action["workspace_id"] == workspace_id
    assert approval["workspace_id"] == workspace_id
    with database.connect() as connection:
        attempt = connection.execute(
            "INSERT INTO app.action_attempts"
            "(id,action_id,generation,execution_id,status) "
            "VALUES(%s,%s,1,%s,'prepared') RETURNING workspace_id",
            (uuid4(), action["id"], "workspace-attempt-" + str(uuid4())),
        ).fetchone()
    assert attempt["workspace_id"] == workspace_id
    reservation_id = UsageLedger(database, task_id).reserve(
        "workspace-usage", 0, "test-provider", 1, Decimal("0")
    )
    with database.connect() as connection:
        usage = connection.execute(
            "SELECT workspace_id FROM app.usage_records WHERE id=%s", (reservation_id,)
        ).fetchone()
    assert usage["workspace_id"] == workspace_id

    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app.approvals(id,task_id,digest,kind,workspace_id) "
            "VALUES(%s,%s,%s,'clarification',%s)",
            (uuid4(), task_id, "b" * 64, LEGACY_WORKSPACE_ID),
        )
        approval_id = connection.execute(
            "SELECT id FROM app.approvals WHERE task_id=%s AND digest=%s",
            (task_id, "b" * 64),
        ).fetchone()["id"]
        assert (
            connection.execute(
                "SELECT workspace_id FROM app.approvals WHERE id=%s", (approval_id,)
            ).fetchone()["workspace_id"]
            == workspace_id
        )

    assert client.delete("/api/v2/business/auth/session", headers=_csrf(login)).status_code == 200
    legacy_login = _login(client, directory)
    assert legacy_login.status_code == 200
    legacy_identity = auth.authenticate_browser(
        SimpleNamespace(headers={}, cookies=dict(client.cookies))
    )
    assert legacy_identity is not None and legacy_identity.workspace_id == LEGACY_WORKSPACE_ID
    protected_routes = (
        f"/api/v2/business/tasks/{task_id}",
        f"/api/v2/business/tasks/{task_id}/events?follow=false",
        f"/api/v2/business/tasks/{task_id}/plan",
        f"/api/v2/business/tasks/{task_id}/trace",
        f"/api/v2/business/tasks/{task_id}/export",
    )
    for route in protected_routes:
        response = client.get(route)
        assert response.status_code == 404, (route, response.status_code, response.text)
    replay = client.post(
        f"/api/v2/business/tasks/{task_id}/approvals/{approval_id}/reply",
        json={"expected_digest": "b" * 64, "decision": "approve"},
        headers={
            **_csrf(legacy_login),
            "Idempotency-Key": "cross-workspace-approval-replay",
        },
    )
    assert replay.status_code == 404, replay.text
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM app.approvals WHERE id=%s", (approval_id,)
            ).fetchone()["status"]
            == "pending"
        )

    supervisor_login = _login(client, directory, "guest-supervisor", "fixture-only-password")
    assert supervisor_login.status_code == 200
    assert client.get(f"/api/v2/business/tasks/{task_id}").status_code == 404
    supervisor_replay = client.post(
        f"/api/v2/business/tasks/{task_id}/approvals/{approval_id}/reply",
        json={"expected_digest": "b" * 64, "decision": "approve"},
        headers={
            **_csrf(supervisor_login),
            "Idempotency-Key": "same-workspace-approval-replay",
        },
    )
    assert supervisor_replay.status_code == 404, supervisor_replay.text

    login = _login(client, directory, "guest-auth-test", password)
    assert login.status_code == 200
    browser_identity = auth.authenticate_browser(
        SimpleNamespace(headers={}, cookies=dict(client.cookies))
    )
    assert browser_identity is not None and browser_identity.workspace_id == workspace_id

    operations = OperationService(database)
    prepared = operations.prepare(
        mcp_identity,
        "guest-workspace-operation",
        "create",
        {"title": "Guest-created ticket", "category_id": str(category_id)},
    )
    if prepared["state"] == "awaiting_approval":
        with pytest.raises(AccessError, match="unavailable"):
            operations.reply(
                legacy_identity,
                prepared["operation_id"],
                prepared["input_hash"],
                "approve",
            )
        operations.reply(
            browser_identity, prepared["operation_id"], prepared["input_hash"], "approve"
        )
    committed = operations.commit(mcp_identity, prepared["operation_id"])
    assert committed["state"] == "succeeded", committed
    created_ticket = committed["result"]["ticket"]
    with database.connect() as connection:
        receipt = connection.execute(
            "SELECT workspace_id FROM business.operation_receipts WHERE operation_id=%s",
            (prepared["operation_id"],),
        ).fetchone()
        assert receipt["workspace_id"] == workspace_id
        assert (
            access.get_receipt(
                connection,
                legacy_employee_id,
                prepared["operation_id"],
                workspace_id=LEGACY_WORKSPACE_ID,
            )
            is None
        )
        with pytest.raises(AccessError, match="unavailable"):
            access.get_ticket(
                connection,
                legacy_employee_id,
                created_ticket["id"],
                workspace_id=LEGACY_WORKSPACE_ID,
            )

    stale = operations.prepare(
        mcp_identity,
        "guest-workspace-stale-write",
        "comment",
        {"ticket_id": str(ticket_id), "expected_revision": 1, "body": "stale comment"},
    )
    if stale["state"] == "awaiting_approval":
        operations.reply(browser_identity, stale["operation_id"], stale["input_hash"], "approve")
    with database.connect() as connection:
        connection.execute(
            "UPDATE business.tickets SET revision=revision+1 WHERE id=%s AND workspace_id=%s",
            (ticket_id, workspace_id),
        )
    stale_result = operations.commit(mcp_identity, stale["operation_id"])
    assert stale_result["state"] == "failed"
    assert stale_result["error_code"] == "revision_conflict"
    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM business.ticket_history WHERE operation_id=%s",
                (stale["operation_id"],),
            ).fetchone()["count"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM business.operation_receipts WHERE operation_id=%s",
                (stale["operation_id"],),
            ).fetchone()["count"]
            == 0
        )


@pytest.mark.parametrize("invalidate", ["revoked_at=now()", "expires_at=now()-interval '1 second'"])
def test_stale_browser_identity_cannot_issue_mcp_token(employee_api, invalidate):
    database, client, auth, directory, _, _ = employee_api
    assert _login(client, directory).status_code == 200
    request = SimpleNamespace(headers={}, cookies=dict(client.cookies))
    identity = auth.authenticate_browser(request)
    assert identity is not None
    with database.connect() as conn:
        conn.execute("UPDATE business.employee_sessions SET " + invalidate)
    with pytest.raises(ValueError, match="session unavailable"):
        auth.issue_mcp(identity)
    with database.connect() as conn:
        count = conn.execute("SELECT count(*) AS n FROM business.mcp_credentials").fetchone()["n"]
        assert count == 0
    assert list(directory.glob("mcp-*.token")) == []

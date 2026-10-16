"""Employee browser and MCP credentials have distinct, database-backed audiences."""

import hashlib
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from decagent.business.employee_accounts import seed_employee_accounts
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore
from decagent.config import Settings
from decagent.gateway import create_app, initialize_owner

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def employee_api(database, tmp_path):
    assert TicketStore(database.url).execute("seed", {}, "employee-auth-seed")["ok"]
    directory = tmp_path / "demo-auth"
    seed_employee_accounts(database, directory)
    scope = PluginRegistry(database).register(builtin_manifest())
    owner_token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    app = create_app(database, Settings(database_url=database.url, data_dir=tmp_path))
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
    assert login.status_code == 200
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

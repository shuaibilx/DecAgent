import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def workbench(database, tmp_path):
    from decagent.gateway import create_app, initialize_owner

    path = initialize_owner(database, tmp_path / "owner.token")
    settings = Settings(database_url=database.url, data_dir=tmp_path)
    app = create_app(database, settings)
    return database, TestClient(app), path.read_text().strip()


def test_initialization_stores_only_hash_and_refuses_rotation(database, tmp_path):
    from decagent.gateway import initialize_owner

    path = initialize_owner(database, tmp_path / "owner.token")
    token = path.read_text().strip()
    assert len(token) >= 43
    with database.connect() as conn:
        saved = conn.execute("SELECT * FROM app.owner_credentials").fetchone()
    assert saved["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(saved)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already initialized"):
        initialize_owner(database, tmp_path / "replacement.token")
    assert not (tmp_path / "replacement.token").exists()


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/projects",
        "/api/v1/tasks",
        "/api/v1/config",
        "/api/v1/tasks/00000000-0000-0000-0000-000000000001/events",
        "/api/v1/projects/00000000-0000-0000-0000-000000000001/memories",
    ],
)
def test_all_owner_data_reject_unauthenticated(workbench, url):
    _, client, _ = workbench
    response = client.get(url)
    assert response.status_code == 401
    assert set(response.json()) == {"code", "message", "task_id", "details"}
    assert client.get("/health").json() == {"status": "ok"}


def test_session_cookie_csrf_origin_logout_and_expiry(workbench):
    db, client, token = workbench
    login = client.post(
        "/api/v1/auth/session", json={"token": token}, headers={"Origin": "http://127.0.0.1:8000"}
    )
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/v1/projects").status_code == 200
    body = {"repository": "owner/repository"}
    assert client.post("/api/v1/projects", json=body).status_code == 403
    headers = {"X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": "project"}
    headers["Origin"] = "https://evil.invalid"
    assert client.post("/api/v1/projects", json=body, headers=headers).status_code == 403
    headers["Origin"] = "http://127.0.0.1:8000"
    assert client.post("/api/v1/projects", json=body, headers=headers).status_code == 201
    assert client.delete("/api/v1/auth/session", headers=headers).status_code == 200
    assert client.get("/api/v1/projects").status_code == 401
    client.post("/api/v1/auth/session", json={"token": token}, headers=headers)
    with db.connect() as conn:
        conn.execute("UPDATE app.owner_sessions SET expires_at=now()-interval '1 second'")
    assert client.get("/api/v1/projects").status_code == 401


def test_remote_cookie_requires_https_and_configuration_is_key_free(database, tmp_path):
    from decagent.gateway import create_app, initialize_owner

    path = initialize_owner(database, tmp_path / "owner.token")
    secret = "never-expose-this-main-key"
    settings = Settings(
        database_url=database.url,
        data_dir=tmp_path,
        public_url="https://workbench.example",
        main_api_key=secret,
    )
    client = TestClient(create_app(database, settings), base_url=settings.public_url)
    response = client.post(
        "/api/v1/auth/session",
        json={"token": path.read_text().strip()},
        headers={"Origin": settings.public_url},
    )
    assert "secure" in response.headers["set-cookie"].lower()
    config = client.get("/api/v1/config")
    assert config.status_code == 200 and secret not in config.text
    assert config.json()["credentials"]["main"] is True


def test_bearer_mutation_uses_same_api_and_local_diagnostics_never_run_live(workbench):
    _, client, token = workbench
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "diagnostics"}
    assert client.get("/api/v1/config", headers=headers).status_code == 200
    response = client.post("/api/v1/diagnostics", json={"mode": "local"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["external_requests"] == 0
    headers["Idempotency-Key"] = "live-no-callback"
    response = client.post(
        "/api/v1/diagnostics",
        json={"mode": "live", "target": "main", "confirm_external_request": True},
        headers=headers,
    )
    assert response.status_code == 503


def test_https_instance_refuses_plain_http_even_with_valid_bearer(database, tmp_path):
    from decagent.gateway import create_app, initialize_owner

    path = initialize_owner(database, tmp_path / "owner.token")
    client = TestClient(
        create_app(database, Settings(data_dir=tmp_path, public_url="https://workbench.example")),
        base_url="http://workbench.example",
    )
    response = client.get(
        "/api/v1/config", headers={"Authorization": "Bearer " + path.read_text().strip()}
    )
    assert response.status_code == 403


def test_owner_token_in_task_issue_cannot_enter_persisted_operational_record(workbench):
    db, client, token = workbench
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "project"}
    project = client.post(
        "/api/v1/projects", json={"repository": "fixture/credential-test"}, headers=headers
    ).json()
    headers["Idempotency-Key"] = "task"
    response = client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "base_ref": "a" * 40,
            "issue": "Accidental credential: " + token,
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert token not in response.text
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.tasks").fetchone()["n"] == 0

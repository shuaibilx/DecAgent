"""Gateway assembly uses actual SQL and free, controlled HTTP transports."""

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.gateway import initialize_owner
from decagent.runtime.application import create_workbench
from decagent.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration


def setup(
    database, tmp_path, *, login="owner", repository_owner="owner", broker=None, publication=None
):
    calls = []

    def github(request):
        calls.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path == "/user":
            data = {"login": login}
        elif path == f"/repos/{repository_owner}/repo":
            data = {
                "id": 123,
                "full_name": repository_owner + "/repo",
                "default_branch": "main",
                "owner": {"login": repository_owner},
                "permissions": {"push": True},
            }
        elif path.endswith("/commits/main"):
            data = {"sha": "a" * 40}
        elif publication and "/git/ref/heads/decagent/" in path:
            data = {"object": {"sha": "c" * 40}}
        elif publication and path.endswith("/git/commits/" + "c" * 40):
            data = {
                "tree": {"sha": "b" * 40},
                "parents": [{"sha": "a" * 40}],
                "message": "DecAgent-Task: " + publication["task_id"],
            }
        elif publication and path.endswith("/pulls"):
            data = [publication["pull"]] if publication.get("pull") else []
        else:
            data = []
        return httpx.Response(200, json=data)

    settings = Settings(
        database_url=database.url, data_dir=tmp_path, github_token="offline-fixture-github-token"
    )
    token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    app = create_workbench(
        settings,
        database=database,
        runtime_config=RuntimeConfig(broker_token="b" * 40, author_email="offline@example.invalid"),
        github_transport=httpx.MockTransport(github),
        broker_client=broker,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer " + token})
    return client, calls


def register(client, repository="owner/repo"):
    response = client.post(
        "/api/v1/projects", json={"repository": repository}, headers={"Idempotency-Key": "project"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, project, *, base_ref="main", branch="main"):
    return client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "base_ref": base_ref,
            "issue": "Offline controlled fixture",
            "trust_scope": {"kind": "own_branch", "branch": branch},
        },
        headers={"Idempotency-Key": base_ref + branch},
    )


def test_project_ownership_from_authenticated_login_not_write_permissions(database, tmp_path):
    client, calls = setup(database, tmp_path, login="owner", repository_owner="external")
    project = register(client, "external/repo")
    trust = database.get_project(project["id"])["trust"]
    assert trust["code_origin"] == "external" and trust["ownership_verified"] is True
    assert trust["authenticated_owner_login"] == "owner"
    assert trust["source"] == "github_authenticated_diagnostic"
    assert submit(client, project).status_code == 422
    assert any(request.url.path == "/user" for request in calls)


def test_owned_branch_registration_binds_branch_and_exact_resolved_sha(database, tmp_path):
    client, _calls = setup(database, tmp_path)
    project = register(client)
    assert project["trust"]["code_origin"] == "owner"
    response = submit(client, project)
    assert response.status_code == 201, response.text
    assert database.get_task(response.json()["id"])["base_sha"] == "a" * 40
    assert submit(client, project, base_ref="b" * 40).status_code == 422
    assert submit(client, project, branch="other").status_code == 422


def test_cancel_broker_row_lock_never_deadlocks_and_receipt_is_required(database, tmp_path):
    calls = []

    def broker(path, payload):
        calls.append((path, payload))
        with database.connect(options="-c statement_timeout=1000") as conn:
            row = conn.execute(
                "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (payload["task_id"],)
            ).fetchone()
            assert row["lease_generation"] == payload["lease_generation"]
            conn.execute(
                "UPDATE app.tasks SET state='recovering',lease_until=NULL WHERE id=%s",
                (payload["task_id"],),
            )
        return {
            "stopped": True,
            "lease_generation": payload["lease_generation"],
            "state": "recovering",
            "execution_ids": ["offline-receipt"],
        }

    client, _ = setup(database, tmp_path, broker=broker)
    response = submit(client, register(client))
    task_id = response.json()["id"]
    database.claim(task_id, "test-worker")
    cancel = client.post(f"/api/v1/tasks/{task_id}/cancel", headers={"Idempotency-Key": "cancel"})
    assert cancel.status_code == 200, cancel.text
    assert database.get_task(task_id)["state"] == "cancelled"
    assert calls[0][0] == "/v1/tasks/stop"
    assert database.events(task_id)[-1]["payload"]["receipt"]["execution_ids"] == [
        "offline-receipt"
    ]


def test_reconciliation_takes_broker_row_lock_then_resume_cas(database, tmp_path):
    def broker(path, payload):
        assert path == "/v1/tasks/reconcile"
        with database.connect(options="-c statement_timeout=1000") as conn:
            conn.execute("SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (payload["task_id"],))
        return {"reconciled": True, "lease_generation": payload["lease_generation"]}

    client, _ = setup(database, tmp_path, broker=broker)
    response = submit(client, register(client))
    task_id = response.json()["id"]
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='recovering' WHERE id=%s", (task_id,))
    resumed = client.post(
        f"/api/v1/tasks/{task_id}/resume", json={}, headers={"Idempotency-Key": "resume"}
    )
    assert resumed.status_code == 200, resumed.text
    assert database.get_task(task_id)["state"] == "queued"
    assert database.events(task_id)[-1]["payload"]["reconciliation"]["reconciled"] is True


def test_missing_credentials_do_not_claim_ownership_or_live_model_check(database, tmp_path):
    settings = Settings(database_url=database.url, data_dir=tmp_path)
    token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    client = TestClient(
        create_workbench(
            settings,
            database=database,
            runtime_config=RuntimeConfig(),
            broker_client=lambda _path, _payload: {},
        )
    )
    client.headers.update({"Authorization": "Bearer " + token})
    project = register(client)
    assert project["trust"] == {} and project["permissions"]["status"] == "unavailable"
    live = client.post(
        "/api/v1/diagnostics",
        json={"mode": "live", "target": "main", "confirm_external_request": True},
        headers={"Idempotency-Key": "live"},
    )
    assert live.status_code == 503


@pytest.mark.parametrize("found", [True, False])
def test_default_publication_reconcile_only_observes_a_bound_existing_draft(
    database, tmp_path, found
):
    publication = {}

    def broker(_path, payload):
        return {"reconciled": True, "lease_generation": payload["lease_generation"]}

    client, calls = setup(database, tmp_path, broker=broker, publication=publication)
    project = register(client)
    task_id = submit(client, project).json()["id"]
    publication["task_id"] = task_id
    branch = "decagent/" + task_id
    if found:
        publication["pull"] = {
            "number": 12,
            "html_url": "https://github.com/owner/repo/pull/12",
            "state": "open",
            "draft": True,
            "body": f"<!-- decagent-task:{task_id} -->",
            "head": {"sha": "c" * 40, "ref": branch, "repo": {"full_name": "owner/repo"}},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        }
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='recovering' WHERE id=%s", (task_id,))
        conn.execute(
            "INSERT INTO app.deliveries"
            "(id,task_id,branch,head_sha,tree_hash,status,base_sha,intent) "
            "VALUES(%s,%s,%s,%s,%s,'unknown',%s,%s)",
            (
                uuid4(),
                task_id,
                branch,
                "c" * 40,
                "f" * 64,
                "a" * 40,
                Jsonb({"base_branch": "main", "tree_sha": "b" * 40, "commit_sha": "c" * 40}),
            ),
        )
    response = client.post(
        f"/api/v1/tasks/{task_id}/resume",
        json={},
        headers={"Idempotency-Key": "publication-resume"},
    )
    assert response.status_code == (200 if found else 409), response.text
    with database.connect() as conn:
        delivery = conn.execute(
            "SELECT * FROM app.deliveries WHERE task_id=%s", (task_id,)
        ).fetchone()
    assert database.get_task(task_id)["state"] == ("queued" if found else "recovering")
    assert delivery["status"] == ("draft" if found else "unknown")
    assert all(request.method == "GET" for request in calls)
    if found:
        receipt = database.events(task_id)[-1]["payload"]["reconciliation"]["publication"]
        assert receipt["reconciled"] is True and receipt["pr_url"].endswith("/pull/12")

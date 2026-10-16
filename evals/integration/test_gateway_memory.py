from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_gateway_api import install_context, submit

from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app, initialize_owner
from decagent.memory import MemoryManager
from decagent.storage.evidence import EvidenceStore

pytestmark = pytest.mark.integration


@pytest.fixture
def memory_api(database, tmp_path):
    token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    contexts = {}
    store = EvidenceStore(tmp_path / "evidence")
    callbacks = GatewayCallbacks(
        current_policy_context=lambda task_id: contexts[task_id],
        memory_manager_factory=lambda project_id, scoped: MemoryManager(scoped, store),
    )
    client = TestClient(create_app(database, Settings(data_dir=tmp_path), callbacks=callbacks))
    client.headers.update({"Authorization": "Bearer " + token})
    api = database, client, contexts, tmp_path
    project, task, _ = submit(api)
    install_context(api, project, task)
    candidate_id = uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.memory_candidates(id,project_id,source_task_id,fingerprint,"
            "category,content,status,applicability) VALUES(%s,%s,%s,%s,'fact',%s,"
            "'pending_review',%s)",
            (
                candidate_id,
                project["id"],
                task["id"],
                "f" * 64,
                "Tenant id is required in cache keys",
                Jsonb({"_revision": 1, "key": "cache", "versions": ["a" * 40]}),
            ),
        )
    return api, candidate_id, project, task


def test_memory_review_rejection_uses_durable_confirmation_and_revocation_is_versioned(memory_api):
    api, candidate_id, project, _ = memory_api
    db, client, _, _ = api
    response = client.post(
        f"/api/v1/memories/{candidate_id}/review",
        json={
            "operation": "reject",
            "expected_revision": 1,
            "confirmation": "This fact lacks sufficient applicability evidence.",
        },
        headers={"Idempotency-Key": "review"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    memories = client.get(f"/api/v1/projects/{project['id']}/memories").json()
    assert memories[0]["revision"] == 2
    with db.connect() as conn:
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE kind='memory_conflict'"
        ).fetchone()
    assert approval["reply_payload"]["policy_record"]["provenance"]["authenticated"] is True
    response = client.post(
        f"/api/v1/memories/{candidate_id}/revoke",
        json={"expected_revision": 2},
        headers={"Idempotency-Key": "revoke"},
    )
    assert response.status_code == 200 and response.json()["status"] == "revoked"
    response = client.post(
        f"/api/v1/memories/{candidate_id}/revoke",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": "stale"},
    )
    assert response.status_code == 409


def test_memory_activation_cannot_bypass_missing_evidence_or_merge(memory_api):
    api, candidate_id, _, _ = memory_api
    db, client, _, _ = api
    response = client.post(
        f"/api/v1/memories/{candidate_id}/review",
        json={
            "operation": "activate",
            "expected_revision": 1,
            "confirmation": "Please activate this fact.",
        },
        headers={"Idempotency-Key": "activate"},
    )
    assert response.status_code == 409
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM app.memory_candidates WHERE id=%s", (candidate_id,)
        ).fetchone()
        assert row["status"] == "pending_review"
        assert conn.execute("SELECT count(*) AS n FROM app.memory_versions").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.idempotency_keys WHERE key='activate'"
            ).fetchone()["n"]
            == 0
        )

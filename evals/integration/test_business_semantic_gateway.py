"""The employee semantic endpoint keeps session, CSRF and action authority separate."""

import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.business.employee_auth import COOKIE_NAME
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from evals.integration.business_support import FixtureTickets
from evals.integration.test_business_interventions import body, waiting
from evals.integration.test_business_interventions import setup as setup

pytestmark = pytest.mark.integration


def client_for(database, tmp_path, sample, actor=None):
    actor = actor or sample["backend"].identity
    token, csrf = "dce_" + uuid4().hex + uuid4().hex, uuid4().hex
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.employee_sessions"
            "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
            "VALUES(%s,%s,%s,%s,now()+interval '1 hour')",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                actor.employee_id,
                actor.account_revision,
                hashlib.sha256(csrf.encode()).hexdigest(),
            ),
        )
    app = create_app(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        callbacks=GatewayCallbacks(
            business_employee_read=lambda identity, command, params: sample["backend"].read(
                command, params
            )
        ),
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)
    client.headers.update({"Origin": "http://127.0.0.1:8000", "X-CSRF-Token": csrf})
    return client


def test_semantic_route_is_actor_bound_idempotent_and_cannot_approve_action(
    database, tmp_path, setup
):
    sample = setup()
    pending = waiting(database, sample)
    path = f"/api/v2/business/tasks/{sample['task']['id']}/decisions/{pending['approval_id']}/reply"
    value = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    payload = body(pending, value=value).model_dump(mode="json")
    client = client_for(database, tmp_path, sample)
    assert client.post(path, headers={"X-CSRF-Token": "wrong"}, json=payload).status_code == 403
    other = FixtureTickets(database, login="employee-facilities")
    foreign = client_for(database, tmp_path, sample, other.identity)
    assert (
        foreign.post(path, headers={"Idempotency-Key": "foreign"}, json=payload).status_code == 404
    )
    assert (
        client.post(
            path, headers={"Idempotency-Key": "authority"}, json={**payload, "decision": "approve"}
        ).status_code
        == 422
    )
    result = client.post(path, headers={"Idempotency-Key": "semantic"}, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["semantic_record"]["selected_value"] == value
    assert "policy_record" not in result.json()
    assert not result.json()["grants_write_authority"]
    assert (
        client.post(path, headers={"Idempotency-Key": "semantic"}, json=payload).json()
        == result.json()
    )
    legacy = path.replace("/decisions/", "/approvals/")
    assert (
        client.post(
            legacy,
            headers={"Idempotency-Key": "wrong-kind"},
            json={"expected_digest": pending["digest"], "decision": "approve"},
        ).status_code
        == 409
    )


def test_semantic_route_refreshes_with_current_employee_read_before_accepting(
    database, tmp_path, setup
):
    sample = setup()
    pending = waiting(database, sample)
    selected = next(key for key in sample["evidence"].question.criteria if key != "no_match")
    client = client_for(database, tmp_path, sample)
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected,))
    path = f"/api/v2/business/tasks/{sample['task']['id']}/decisions/{pending['approval_id']}/reply"
    result = client.post(
        path,
        headers={"Idempotency-Key": "withdrawn"},
        json=body(pending, value=selected).model_dump(mode="json"),
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "stale"
    assert selected not in result.json()["request"]["question"]["criteria"]
    assert database.get_task(sample["task"]["id"])["state"] == "queued"

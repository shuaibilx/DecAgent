"""Persistent anonymous guest rate limits and trusted proxy boundaries."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from decagent.business.guest_rate_limit import GuestAdmissionRateLimiter
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.config import Settings
from decagent.gateway import create_app

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"
TEST_HMAC_KEY = bytes.fromhex("42" * 32)


def test_new_guest_creation_limit_survives_cookie_clearing_and_ignores_untrusted_xff(
    database, tmp_path
):
    client = TestClient(
        create_app(
            database,
            Settings(
                data_dir=tmp_path,
                guest_trial_enabled=True,
                guest_rate_limit_hmac_key=TEST_HMAC_KEY,
            ),
        ),
        base_url=ORIGIN,
    )

    responses = []
    for index in range(6):
        client.cookies.clear()
        responses.append(
            client.post(
                "/api/v3/guest/session",
                headers={
                    "Origin": ORIGIN,
                    "X-Forwarded-For": f"198.51.100.{index + 1}",
                },
            )
        )

    assert [response.status_code for response in responses[:5]] == [201] * 5
    limited = responses[5]
    assert limited.status_code == 429
    assert limited.json()["code"] == "guest_rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 3600

    with database.connect() as conn:
        created = conn.execute(
            "SELECT count(*) AS total FROM business.workspaces WHERE kind='guest' AND active"
        ).fetchone()["total"]
    assert created == 5


def test_guest_task_submission_rate_limit_returns_retry_after(database, tmp_path):
    scope = PluginRegistry(database).register(builtin_manifest())
    client = TestClient(
        create_app(
            database,
            Settings(
                data_dir=tmp_path,
                guest_trial_enabled=True,
                guest_rate_limit_hmac_key=TEST_HMAC_KEY,
            ),
        ),
        base_url=ORIGIN,
    )
    session = client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
    csrf = session.json()["csrf_token"]

    responses = [
        client.post(
            "/api/v2/business/tasks",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"guest-rate-task-{index}",
            },
            json={"scope_id": str(scope["id"]), "instruction": f"Read task {index}"},
        )
        for index in range(11)
    ]

    assert [response.status_code for response in responses[:10]] == [201] * 10
    limited = responses[10]
    assert limited.status_code == 429
    assert limited.json()["code"] == "guest_rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


def test_guest_manual_replies_are_rate_limited_before_route_mutation(database, tmp_path):
    client = TestClient(
        create_app(
            database,
            Settings(
                data_dir=tmp_path,
                guest_trial_enabled=True,
                guest_rate_limit_hmac_key=TEST_HMAC_KEY,
            ),
        ),
        base_url=ORIGIN,
    )
    session = client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
    csrf = session.json()["csrf_token"]
    task_id = "00000000-0000-4000-8000-000000000001"
    approval_id = "00000000-0000-4000-8000-000000000002"
    path = f"/api/v2/business/tasks/{task_id}/approvals/{approval_id}/reply"

    responses = [
        client.post(
            path,
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"guest-rate-reply-{index}",
            },
            json={},
        )
        for index in range(31)
    ]

    assert [response.status_code for response in responses[:30]] == [422] * 30
    limited = responses[30]
    assert limited.status_code == 429
    assert limited.json()["code"] == "guest_rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


def test_guest_usage_endpoint_reports_daily_caps_and_persisted_usage(database, tmp_path):
    client = TestClient(
        create_app(
            database,
            Settings(
                data_dir=tmp_path,
                guest_trial_enabled=True,
                guest_rate_limit_hmac_key=TEST_HMAC_KEY,
            ),
        ),
        base_url=ORIGIN,
    )
    session = client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
    usage = client.get("/api/v3/guest/usage")

    assert session.status_code == 201
    assert usage.status_code == 200
    assert usage.json()["guest"] == {
        "daily_task_limit": 30,
        "tasks_admitted": 0,
        "daily_qwen_token_limit": 500000,
        "qwen_tokens_reserved": 0,
    }
    assert usage.json()["global"]["daily_cost_limit_usd"] == "5.000000"
    assert usage.json()["global"]["daily_qwen_token_limit"] == 5000000


def test_guest_rate_window_is_atomic_and_shared_by_new_limiter_instances(database):
    from threading import Barrier

    limiter = GuestAdmissionRateLimiter(database, hmac_key=TEST_HMAC_KEY)
    barrier = Barrier(16)

    def attempt(_index):
        barrier.wait()
        return (
            GuestAdmissionRateLimiter(database, hmac_key=TEST_HMAC_KEY)
            .check(
                scope="guest_task_submit",
                client_ip="198.51.100.91",
                limit=10,
                window_seconds=60,
            )
            .allowed
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(16)))

    assert sum(results) == 10
    assert (
        limiter.check(
            scope="guest_task_submit",
            client_ip="198.51.100.91",
            limit=10,
            window_seconds=60,
        ).allowed
        is False
    )
    with database.connect() as conn:
        row = conn.execute(
            "SELECT subject_hash FROM business.guest_rate_limit_events LIMIT 1"
        ).fetchone()
    assert isinstance(row["subject_hash"], bytes)
    assert len(row["subject_hash"]) == 32


def test_rate_limit_database_does_not_store_the_hmac_secret(database):
    GuestAdmissionRateLimiter(database, hmac_key=TEST_HMAC_KEY).check(
        scope="guest_create",
        client_ip="198.51.100.23",
        limit=5,
        window_seconds=3600,
    )

    with database.connect() as conn:
        table = conn.execute(
            "SELECT to_regclass('business.guest_rate_limit_keys') AS key_table"
        ).fetchone()["key_table"]
        fingerprint = conn.execute(
            "SELECT key_fingerprint FROM business.guest_rate_limit_key_state"
        ).fetchone()["key_fingerprint"]

    assert table is None
    assert fingerprint not in {TEST_HMAC_KEY, None}


def test_rate_limit_key_rotation_waits_until_previous_windows_expire(database):
    original = GuestAdmissionRateLimiter(database, hmac_key=TEST_HMAC_KEY)
    replacement_key = bytes.fromhex("24" * 32)
    replacement = GuestAdmissionRateLimiter(database, hmac_key=replacement_key)
    original.check(
        scope="guest_create",
        client_ip="198.51.100.77",
        limit=5,
        window_seconds=3600,
    )

    # Simulate an older installation after migration 030 drops the key table.
    with database.connect() as conn:
        conn.execute("DELETE FROM business.guest_rate_limit_key_state")

    with pytest.raises(RuntimeError, match="rate-limit key rotation"):
        replacement.check(
            scope="guest_create",
            client_ip="198.51.100.77",
            limit=5,
            window_seconds=3600,
        )

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.guest_rate_limit_events "
            "SET occurred_at=clock_timestamp()-interval '2 hours'-interval '1 second'"
        )

    assert replacement.check(
        scope="guest_create",
        client_ip="198.51.100.77",
        limit=5,
        window_seconds=3600,
    ).allowed

    with pytest.raises(RuntimeError, match="rate-limit key rotation"):
        original.check(
            scope="guest_create",
            client_ip="198.51.100.77",
            limit=5,
            window_seconds=3600,
        )


def test_guest_admission_returns_retryable_unavailable_during_key_rotation(database, tmp_path):
    old_app = create_app(
        database,
        Settings(
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=TEST_HMAC_KEY,
        ),
    )
    with TestClient(old_app, base_url=ORIGIN) as old_client:
        response = old_client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})
        assert response.status_code == 201

    new_app = create_app(
        database,
        Settings(
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=bytes.fromhex("24" * 32),
        ),
    )
    with TestClient(new_app, base_url=ORIGIN) as new_client:
        response = new_client.post("/api/v3/guest/session", headers={"Origin": ORIGIN})

    assert response.status_code == 503
    assert response.json()["code"] == "guest_rate_limit_key_rotation_pending"
    assert 1 <= int(response.headers["Retry-After"]) <= 7200


def test_configured_proxy_supplies_https_scheme_and_stable_client_limit_key(database, tmp_path):
    app = create_app(
        database,
        Settings(
            public_url="https://agent.example",
            data_dir=tmp_path,
            guest_trial_enabled=True,
            guest_rate_limit_hmac_key=TEST_HMAC_KEY,
            guest_trusted_proxies=("192.0.2.0/24",),
        ),
    )
    responses = []
    for _ in range(6):
        client = TestClient(
            app,
            base_url="http://agent.example",
            client=("192.0.2.10", 50000),
        )
        responses.append(
            client.post(
                "/api/v3/guest/session",
                headers={
                    "Origin": "https://agent.example",
                    "X-Forwarded-For": "198.51.100.18",
                    "X-Forwarded-Proto": "https",
                },
            )
        )

    assert [response.status_code for response in responses[:5]] == [201] * 5
    assert responses[5].status_code == 429
    assert responses[5].headers["Retry-After"]

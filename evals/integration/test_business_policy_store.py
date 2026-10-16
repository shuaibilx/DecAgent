"""Disposable database tests use declared fixture metrics, never real evaluation claims."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from decagent.business.access import AccessError
from decagent.business.decision_policy import PolicyContext, ThresholdPolicy
from decagent.business.policy_store import DecisionPolicyStore
from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import digest
from decagent.gateway.auth import OwnerIdentity
from evals.deterministic.test_business_policy_reports import report
from evals.integration.business_support import FixtureTickets
from evals.integration.test_business_semantic_gateway import client_for

pytestmark = pytest.mark.integration


@pytest.fixture
def sample(database):
    backend = FixtureTickets(database, login="employee-it")
    scope = PluginRegistry(database).list()[0]
    task = database.create_business_task(
        scope["id"],
        {"instruction": "Fixture decision", "plugin_digest": scope["manifest_digest"]},
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    owner_hash = "f" * 64
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.owner_credentials(singleton,token_hash) VALUES(true,%s)", (owner_hash,)
        )
    record = report()
    record["binding"]["context"]["plugin_digest"] = scope["manifest_digest"]
    now = datetime.now(UTC)
    profile = ThresholdPolicy(
        policy_id="routing-policy",
        policy_version="1",
        question_id="routing",
        question_version="3",
        primitive="choice",
        actual_model="jev-1.2.3",
        context=PolicyContext(**record["binding"]["context"]),
        min_candidates=2,
        max_candidates=3,
        threshold=0.9,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
        validation_record=record["report_id"],
    )
    return {
        "store": DecisionPolicyStore(database),
        "backend": backend,
        "task": task,
        "owner": OwnerIdentity("bearer:" + owner_hash, True),
        "report": record,
        "policy": profile,
    }


def publish(sample):
    store, owner = sample["store"], sample["owner"]
    store.register_validation(owner, sample["report"], digest(sample["report"]))
    store.register(owner, sample["policy"])
    return store.publish(owner, "routing-policy", "1")


def resolve(sample, **kwargs):
    return sample["store"].resolve(
        sample["backend"].identity,
        sample["task"]["id"],
        "routing-policy",
        kwargs.pop("version", "1"),
        **kwargs,
    )


def test_empty_store_is_explicit_manual_without_primary_fallback(database, sample):
    assert "/case_" in database.url
    result = resolve(sample)
    assert result.policy is None
    assert result.reason == "policy_unavailable"
    assert not result.primary_fallback
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.decision_policies").fetchone()["n"] == 0


def test_published_policy_survives_restart_and_records_effective_history(database, sample):
    publish(sample)
    result = resolve(sample)
    assert result.policy.status == "published"
    assert result.policy.threshold == 0.9
    assert result.minimum_threshold == 0.9
    assert result.allowed_thresholds == (0.9, 0.95)
    assert result.effective_revision == 1
    sample["store"] = DecisionPolicyStore(database)
    assert resolve(sample).effective_revision == 1
    events = database.events(sample["task"]["id"])
    assert [event["kind"] for event in events] == ["decision.policy_effective"]
    assert events[0]["payload"]["policy_version"] == "1"


@pytest.mark.parametrize("threshold", [0.8, 0.99, 0.94])
def test_policy_cannot_publish_failed_insufficient_or_unmeasured_thresholds(sample, threshold):
    sample["store"].register_validation(sample["owner"], sample["report"], digest(sample["report"]))
    with pytest.raises(ValueError, match="qualified"):
        sample["store"].register(sample["owner"], replace(sample["policy"], threshold=threshold))


def test_publication_revalidates_report_binding_without_broadening_scope(sample):
    store, owner = sample["store"], sample["owner"]
    store.register_validation(owner, sample["report"], digest(sample["report"]))
    for changes in (
        {"max_candidates": 4},
        {"actual_model": "jev-1.2.4"},
        {"question_version": "4"},
        {"primitive": "noul"},
    ):
        with pytest.raises(ValueError, match="binding"):
            store.register(owner, replace(sample["policy"], **changes))
    with pytest.raises(ValueError, match="binding"):
        store.register(
            owner,
            replace(sample["policy"], context=replace(sample["policy"].context, risk="ordinary")),
        )


def test_task_override_is_original_employee_only_and_keeps_admin_floor(database, sample):
    publish(sample)
    store, browser, task = sample["store"], sample["backend"].browser, sample["task"]["id"]
    store.set_task_threshold(browser, task, "routing-policy", "1", 0.95)
    result = resolve(sample)
    assert result.policy.threshold == 0.95
    assert result.minimum_threshold == 0.9
    assert result.effective_revision == 1
    for threshold in (0.8, 0.94, 0.99):
        with pytest.raises(ValueError):
            store.set_task_threshold(browser, task, "routing-policy", "1", threshold)
    other = FixtureTickets(database, login="employee-facilities")
    with pytest.raises(AccessError):
        store.set_task_threshold(other.browser, task, "routing-policy", "1", 0.95)
    with pytest.raises(AccessError):
        store.set_task_threshold(sample["backend"].identity, task, "routing-policy", "1", 0.95)
    assert database.get_task(task)["state"] == "queued"
    assert not result.grants_write_authority


def test_employee_threshold_endpoint_exposes_only_qualified_choices_and_checks_session(
    database, tmp_path, sample
):
    publish(sample)
    resolve(sample)
    client = client_for(database, tmp_path, sample)
    task_id = sample["task"]["id"]
    path = f"/api/v2/business/tasks/{task_id}/decision-thresholds"
    body = {"policy_id": "routing-policy", "policy_version": "1", "threshold": 0.95}
    plan = client.get(f"/api/v2/business/tasks/{task_id}/plan")
    assert plan.status_code == 200, plan.text
    policy = plan.json()["policies"][0]
    assert policy["allowed_thresholds"] == [0.9, 0.95]
    assert policy["minimum_threshold"] == 0.9

    assert (
        client.post(
            path,
            headers={"X-CSRF-Token": "wrong", "Idempotency-Key": "wrong-csrf"},
            json=body,
        ).status_code
        == 403
    )
    other = FixtureTickets(database, login="employee-facilities")
    foreign = client_for(database, tmp_path, sample, other.identity)
    assert (
        foreign.post(
            path,
            headers={"Idempotency-Key": "foreign-threshold"},
            json=body,
        ).status_code
        == 404
    )

    updated = client.post(
        path,
        headers={"Idempotency-Key": "employee-threshold"},
        json=body,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["threshold"] == 0.95
    rejected = client.post(
        path,
        headers={"Idempotency-Key": "below-floor"},
        json={**body, "threshold": 0.8},
    )
    assert rejected.status_code == 422
    assert resolve(sample).policy.threshold == 0.95


def test_admin_floor_only_gets_stricter_and_task_setting_cannot_lower_it(database, sample):
    publish(sample)
    initial = resolve(sample)
    store, task = sample["store"], sample["task"]["id"]
    store.set_minimum(sample["owner"], "routing-policy", "1", 0.95)
    current = resolve(sample)
    assert initial.policy.threshold == 0.9
    assert current.policy.threshold == 0.95
    assert current.allowed_thresholds == (0.95,)
    assert current.effective_revision == 2
    with pytest.raises(ValueError, match="minimum"):
        store.set_minimum(sample["owner"], "routing-policy", "1", 0.9)
    with pytest.raises(ValueError, match="minimum"):
        store.set_task_threshold(sample["backend"].browser, task, "routing-policy", "1", 0.9)
    snapshots = [
        e["payload"] for e in database.events(task) if e["kind"] == "decision.policy_effective"
    ]
    assert [value["effective_threshold"] for value in snapshots] == [0.9, 0.95]


def test_waiting_task_history_preserves_old_settings_and_separate_policy_versions(database, sample):
    publish(sample)
    first = resolve(sample)
    task = sample["task"]["id"]
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='awaiting_input' WHERE id=%s", (task,))
    sample["store"].set_task_threshold(sample["backend"].browser, task, "routing-policy", "1", 0.95)
    second = resolve(sample)
    newer = replace(sample["policy"], policy_version="2", threshold=0.95)
    sample["store"].register(sample["owner"], newer)
    sample["store"].publish(sample["owner"], "routing-policy", "2")
    assert resolve(sample).policy.policy_version == "1"
    assert resolve(sample, version="2").policy.policy_version == "2"
    assert first.policy.threshold == 0.9
    assert second.policy.threshold == 0.95
    assert database.get_task(task)["state"] == "awaiting_input"
    assert second.effective_revision == 2


def test_revocation_is_permanent_and_preserves_reports_versions_and_history(database, sample):
    publish(sample)
    resolve(sample)
    sample["store"].revoke(sample["owner"], "routing-policy", "1", "validation withdrawn")
    assert resolve(sample).reason == "policy_revoked"
    with pytest.raises(ValueError):
        sample["store"].publish(sample["owner"], "routing-policy", "1")
    with database.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.decision_validation_reports").fetchone()[
                "n"
            ]
            == 1
        )
        assert conn.execute("SELECT count(*) AS n FROM app.decision_policies").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM app.decision_policy_events").fetchone()["n"]
            >= 4
        )


def test_untrusted_or_revoked_admin_and_employee_identities_are_rejected(database, sample):
    with pytest.raises(AccessError):
        sample["store"].register_validation(
            sample["backend"].browser, sample["report"], digest(sample["report"])
        )
    with database.connect() as conn:
        conn.execute("UPDATE app.owner_credentials SET token_hash=%s", ("e" * 64,))
    with pytest.raises(AccessError):
        sample["store"].register_validation(
            sample["owner"], sample["report"], digest(sample["report"])
        )
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE id=%s",
            (sample["backend"].identity.employee_id,),
        )
    with pytest.raises(AccessError):
        resolve(sample)


def test_report_policy_and_audit_history_cannot_be_rewritten(database, sample):
    publish(sample)
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute("UPDATE app.decision_validation_reports SET report='{}'::jsonb")
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute("UPDATE app.decision_policies SET definition='{}'::jsonb")
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute("DELETE FROM app.decision_policy_events")
    database.migrate()
    assert resolve(sample).policy.threshold == 0.9


def test_registration_publication_and_employee_retries_do_not_duplicate_history(database, sample):
    publish(sample)
    publish(sample)
    changed = {**sample["report"], "reference": "fixture://changed"}
    with pytest.raises(ValueError, match="immutable"):
        sample["store"].register_validation(sample["owner"], changed, digest(changed))
    with pytest.raises(ValueError, match="immutable"):
        sample["store"].register(sample["owner"], replace(sample["policy"], threshold=0.95))
    for _ in range(2):
        updated = sample["store"].set_task_threshold(
            sample["backend"].browser, sample["task"]["id"], "routing-policy", "1", 0.95
        )
        assert updated["revision"] == 1
        assert resolve(sample).effective_revision == 1
    with database.connect() as conn:
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM app.decision_policy_events ORDER BY id"
            ).fetchall()
        ]
    assert kinds.count("decision.policy_registered") == 1
    assert kinds.count("decision.validation_registered") == 1
    assert kinds.count("decision.policy_published") == 1
    assert kinds.count("decision.threshold_changed") == 1
    assert kinds.count("decision.policy_effective") == 1


def test_future_policy_cannot_be_used_to_change_task_settings(sample):
    now = datetime.now(UTC)
    sample["policy"] = replace(
        sample["policy"], valid_from=now + timedelta(days=1), valid_until=now + timedelta(days=2)
    )
    publish(sample)
    result = resolve(sample)
    assert result.reason == "policy_not_yet_valid"
    assert result.policy is None
    assert result.allowed_thresholds == ()
    with pytest.raises(ValueError):
        sample["store"].set_task_threshold(
            sample["backend"].browser, sample["task"]["id"], "routing-policy", "1", 0.95
        )


def test_persisted_policy_is_consumable_by_the_pure_gate_and_model_drift_remains_manual(sample):
    from decagent.business.decision_policy import DecisionSnapshot, gate_decision
    from decagent.decisions.contracts import QuestionSpec

    publish(sample)
    policy = resolve(sample).policy
    actor = sample["backend"].identity
    snap = DecisionSnapshot(
        policy.context,
        "d" * 64,
        "e" * 64,
        2,
        str(sample["task"]["business_scope_id"]),
        "1",
        str(actor.employee_id),
        str(actor.account_revision),
    )
    question = QuestionSpec(
        "routing",
        "3",
        "choice",
        "Trusted fixture question",
        {"a": "Authorized candidate", "no_match": "No match"},
        ("state",),
    )
    answer = {
        "type": "choice",
        "choice": "a",
        "probabilities": {"a": 0.98, "no_match": 0.02},
        "confidence": 0.96,
    }
    for actual_model, reason in (
        ("jev-1.2.3", "threshold_met"),
        ("jev-1.2.4", "model_version_mismatch"),
    ):
        result = gate_decision(
            question,
            answer,
            actual_model=actual_model,
            snapshot=snap,
            current_snapshot=snap,
            policy=policy,
            now=datetime.now(UTC),
        )
        assert result.reason == reason
        assert not result.grants_write_authority
        assert not result.primary_fallback

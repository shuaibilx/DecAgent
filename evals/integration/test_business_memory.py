"""Business memories have real sources, employee scope and bounded validity."""

import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.registry import PluginRegistry
from decagent.gateway.auth import OwnerIdentity
from evals.integration.business_support import FixtureTickets
from evals.integration.database_support import temporary_database

pytestmark = pytest.mark.integration


@pytest.fixture
def sample(database):
    assert find_spec("decagent.business.memory") is not None, "Business memory is not implemented"
    api = importlib.import_module("decagent.business.memory")
    backend = FixtureTickets(database, login="employee-it")
    scope = PluginRegistry(database).list()[0]
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "Please remember my language preference",
            "plugin_digest": scope["manifest_digest"],
        },
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    event = database.event(task["id"], "business.preference_observed", {"value": "中文"})
    clock = [datetime.now(UTC)]
    store = api.BusinessMemoryStore(database, clock=lambda: clock[0])
    context = store.context(
        backend.identity, scope["id"], question_fingerprint="b" * 64, plan_fingerprint="c" * 64
    )
    owner_hash = "f" * 64
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.owner_credentials(singleton,token_hash) VALUES(true,%s)", (owner_hash,)
        )
    return {
        "store": store,
        "backend": backend,
        "scope": scope,
        "task": task,
        "context": context,
        "clock": clock,
        "owner": OwnerIdentity("bearer:" + owner_hash, True),
        "source": {"kind": "task_event", "task_id": str(task["id"]), "seq": event["seq"]},
    }


def propose(sample, **updates):
    args = {"subject": "response_language", "value": "中文", "evidence_refs": [sample["source"]]}
    return sample["store"].propose_preference(
        sample["backend"].identity, sample["context"], **{**args, **updates}
    )


def confirm(sample, memory):
    return sample["store"].confirm_preference(
        sample["backend"].browser, sample["context"], memory["id"], memory["content_digest"]
    )


def receipt(sample):
    backend = sample["backend"]
    category = backend.read("directory", {})["categories"][0]["id"]
    result = backend.execute(
        "create",
        {"title": "Verified memory source", "category_id": category},
        "memory-source-" + uuid4().hex,
    )
    assert result["ok"]
    return {"kind": "operation_receipt", "operation_id": result["data"]["receipt"]["operation_id"]}


def rule(sample, source):
    return sample["store"].publish_experience_rule(
        sample["owner"],
        sample["context"],
        rule_id="verified-create",
        rule_version="1",
        command="create",
        lesson="创建工单以服务端成功回执为依据。",
        validation_refs=[source],
        valid_until=sample["clock"][0] + timedelta(days=30),
    )


def experience(sample, source):
    return sample["store"].record_experience(
        sample["backend"].identity,
        sample["context"],
        rule_id="verified-create",
        rule_version="1",
        receipt_ref=source,
    )


def test_preference_requires_original_browser_confirmation_and_has_30_day_ttl(sample):
    saved = propose(sample)
    store, actor, ctx = sample["store"], sample["backend"], sample["context"]
    assert saved["kind"] == "confirmed_preference" and saved["status"] == "pending_confirmation"
    assert store.candidates(actor.identity, ctx) == []
    with pytest.raises(AccessError, match="browser"):
        store.confirm_preference(actor.identity, ctx, saved["id"], saved["content_digest"])
    active = confirm(sample, saved)
    assert active["expires_at"] - active["verified_at"] == timedelta(days=30)
    candidates = store.candidates(actor.identity, ctx)
    assert [str(item["id"]) for item in candidates] == [str(saved["id"])]
    assert candidates[0]["verified"] is True and candidates[0]["grants_write_authority"] is False
    assert candidates[0]["data"] == {"value": "中文"}
    assert actor.identity.token_hash not in str(candidates)


def test_deduplication_does_not_extend_active_memory_ttl(sample):
    active = confirm(sample, propose(sample))
    sample["clock"][0] += timedelta(days=1)
    duplicate = propose(sample)
    assert duplicate["id"] == active["id"]
    assert confirm(sample, duplicate)["expires_at"] == active["expires_at"]
    with sample["store"].database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.business_memories").fetchone()["n"] == 1


def test_employee_scope_is_filtered_before_any_candidate_content_is_returned(database, sample):
    active = confirm(sample, propose(sample))
    other = FixtureTickets(database, login="employee-facilities")
    context = sample["store"].context(
        other.identity,
        sample["scope"]["id"],
        question_fingerprint="b" * 64,
        plan_fingerprint="c" * 64,
    )
    assert sample["store"].candidates(other.identity, context) == []
    with pytest.raises(AccessError, match="unavailable"):
        sample["store"].get(other.browser, active["id"])
    with pytest.raises(AccessError):
        sample["store"].confirm_preference(
            other.browser, context, active["id"], active["content_digest"]
        )


@pytest.mark.parametrize(
    "field,value", [("question_fingerprint", "d" * 64), ("plan_fingerprint", "e" * 64)]
)
def test_different_contract_fingerprints_exclude_memory(sample, field, value):
    confirm(sample, propose(sample))
    assert (
        sample["store"].candidates(
            sample["backend"].identity, replace(sample["context"], **{field: value})
        )
        == []
    )


def test_expiry_is_excluded_without_silent_renewal(sample):
    active = confirm(sample, propose(sample))
    sample["clock"][0] = active["expires_at"]
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []
    assert propose(sample)["id"] == active["id"]
    assert sample["store"].get(sample["backend"].browser, active["id"])["expired"] is True


def test_revoked_browser_and_account_revision_cannot_confirm(database, sample):
    pending = propose(sample)
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_sessions SET revoked_at=now() WHERE token_hash=%s",
            (sample["backend"].browser.token_hash,),
        )
    with pytest.raises(AccessError):
        confirm(sample, pending)
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
            "WHERE id=%s",
            (sample["backend"].identity.employee_id,),
        )
    with pytest.raises(AccessError):
        sample["store"].candidates(sample["backend"].identity, sample["context"])


def test_scope_change_excludes_old_memory_under_fresh_context(database, sample):
    confirm(sample, propose(sample))
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (sample["scope"]["id"],),
        )
    fresh = sample["store"].context(
        sample["backend"].identity,
        sample["scope"]["id"],
        question_fingerprint="b" * 64,
        plan_fingerprint="c" * 64,
    )
    assert sample["store"].candidates(sample["backend"].identity, fresh) == []
    with pytest.raises(AccessError, match="changed"):
        sample["store"].candidates(sample["backend"].identity, sample["context"])


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "task_event", "task_id": str(uuid4()), "seq": 1},
        {"kind": "operation_receipt", "operation_id": "missing"},
        {"kind": "model_claim", "verified": True},
    ],
)
def test_fabricated_source_references_are_rejected(sample, source):
    with pytest.raises((AccessError, ValueError)):
        propose(sample, evidence_refs=[source])


def test_another_employees_real_source_is_rejected(database, sample):
    other = FixtureTickets(database, login="employee-facilities")
    foreign = database.create_business_task(
        sample["scope"]["id"],
        {
            "instruction": "Other employee source",
            "plugin_digest": sample["scope"]["manifest_digest"],
        },
        employee_id=other.identity.employee_id,
        credential_ref=other.identity.credential_ref,
    )
    event = database.event(foreign["id"], "business.preference_observed", {"value": "English"})
    with pytest.raises(AccessError):
        propose(
            sample,
            evidence_refs=[
                {"kind": "task_event", "task_id": str(foreign["id"]), "seq": event["seq"]}
            ],
        )


@pytest.mark.parametrize(
    "subject", ["current_permissions", "active_ticket", "approval_authorization"]
)
def test_live_facts_and_authority_are_not_supported_long_term_memory_subjects(sample, subject):
    with pytest.raises(ValueError, match="subject"):
        propose(sample, subject=subject)


def test_source_revocation_and_source_content_change_exclude_memory(database, sample):
    active = confirm(sample, propose(sample))
    sample["store"].revoke_source(
        sample["backend"].browser,
        sample["context"],
        sample["source"],
        "Original statement withdrawn",
    )
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []
    assert sample["store"].get(sample["backend"].browser, active["id"])["status"] == "active"
    with pytest.raises(AccessError):
        propose(sample, value="English")
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.business_memory_audit WHERE event='source_revoked'"
            ).fetchone()["n"]
            == 1
        )


def test_mutated_event_evidence_is_not_reused(database, sample):
    confirm(sample, propose(sample))
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.events SET payload=%s WHERE task_id=%s AND seq=%s",
            (
                Jsonb({"value": "Different observation"}),
                sample["task"]["id"],
                sample["source"]["seq"],
            ),
        )
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []


def test_correction_activates_atomically_and_retains_superseded_history(sample):
    old = confirm(sample, propose(sample))
    replacement = propose(sample, value="English", supersedes=old["id"])
    assert (
        sample["store"].candidates(sample["backend"].identity, sample["context"])[0]["id"]
        == old["id"]
    )
    active = confirm(sample, replacement)
    assert active["supersedes"] == old["id"]
    assert sample["store"].get(sample["backend"].browser, old["id"])["status"] == "superseded"
    assert [
        row["id"]
        for row in sample["store"].candidates(sample["backend"].identity, sample["context"])
    ] == [active["id"]]
    revoked = sample["store"].revoke(
        sample["backend"].browser, active["id"], active["content_digest"], "Preference changed"
    )
    assert revoked["status"] == "revoked"
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []


def test_retry_of_confirmed_correction_keeps_the_same_memory_identity(sample):
    old = confirm(sample, propose(sample))
    replacement = propose(sample, value="English", supersedes=old["id"])
    active = confirm(sample, replacement)
    retry = propose(sample, value="English", supersedes=old["id"])
    assert retry["id"] == active["id"]
    assert retry["expires_at"] == active["expires_at"]


def test_experience_requires_published_rule_and_real_verified_receipt(sample):
    source = receipt(sample)
    with pytest.raises(ValueError, match="rule"):
        experience(sample, source)
    rule(sample, source)
    active = experience(sample, source)
    assert active["kind"] == "verified_workflow_experience" and active["status"] == "active"
    assert active["expires_at"] - active["verified_at"] == timedelta(days=7)
    assert active["content"] == {
        "command": "create",
        "outcome": "succeeded",
        "lesson": "创建工单以服务端成功回执为依据。",
    }
    assert experience(sample, source)["id"] == active["id"]
    sample["store"].revoke_experience_rule(sample["owner"], "verified-create", "1", "Rule retired")
    assert sample["store"].candidates(sample["backend"].identity, sample["context"]) == []


def test_unknown_receipt_and_mismatched_durable_history_cannot_support_experience(database, sample):
    source = receipt(sample)
    rule(sample, source)
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.ticket_history SET details='{}' WHERE operation_id=%s",
            (source["operation_id"],),
        )
    with pytest.raises((ValueError, AccessError), match="receipt|evidence"):
        experience(sample, source)


def test_rule_publication_requires_authenticated_owner(sample):
    source = receipt(sample)
    with pytest.raises(AccessError):
        sample["store"].publish_experience_rule(
            sample["backend"].browser,
            sample["context"],
            rule_id="not-owner",
            rule_version="1",
            command="create",
            lesson="No authority",
            validation_refs=[source],
            valid_until=sample["clock"][0] + timedelta(days=1),
        )


@pytest.mark.parametrize("state", ["ready", "unknown"])
def test_uncommitted_or_unknown_operation_is_not_completed_memory_evidence(database, sample, state):
    valid = receipt(sample)
    rule(sample, valid)
    backend = sample["backend"]
    category = backend.read("directory", {})["categories"][0]["id"]
    pending = backend.prepare(
        "uncommitted-memory-" + uuid4().hex,
        "create",
        {"title": "No completed receipt", "category_id": category},
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.operations SET state=%s WHERE id=%s", (state, pending["operation_id"])
        )
    with pytest.raises(AccessError, match="receipt"):
        experience(sample, {"kind": "operation_receipt", "operation_id": pending["operation_id"]})


def test_current_command_permission_is_rechecked_for_experience(database, sample):
    supervisor = FixtureTickets(database, login="supervisor")
    context = sample["store"].context(
        supervisor.identity,
        sample["scope"]["id"],
        question_fingerprint="b" * 64,
        plan_fingerprint="c" * 64,
    )
    directory = supervisor.read("directory", {})
    created = supervisor.execute(
        "create",
        {"title": "Permission evidence", "category_id": directory["categories"][0]["id"]},
        "memory-permission-create",
    )
    ticket = created["data"]["ticket"]
    target = next(
        item["id"]
        for item in directory["people"]
        if item["team"] == "IT Support" and item["name"] == "Zhang Wei"
    )
    assigned = supervisor.execute(
        "assign",
        {"ticket_id": ticket["id"], "person_id": target, "expected_revision": ticket["revision"]},
        "memory-permission-assign",
    )
    assert assigned["ok"]
    source = {
        "kind": "operation_receipt",
        "operation_id": assigned["data"]["receipt"]["operation_id"],
    }
    sample["store"].publish_experience_rule(
        sample["owner"],
        context,
        rule_id="verified-assign",
        rule_version="1",
        command="assign",
        lesson="分派以真实回执核实。",
        validation_refs=[source],
        valid_until=sample["clock"][0] + timedelta(days=1),
    )
    active = sample["store"].record_experience(
        supervisor.identity,
        context,
        rule_id="verified-assign",
        rule_version="1",
        receipt_ref=source,
    )
    assert active["active"] is True
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET role='employee' WHERE id=%s",
            (supervisor.identity.employee_id,),
        )
    assert sample["store"].candidates(supervisor.identity, context) == []
    with pytest.raises(AccessError, match="permission"):
        sample["store"].record_experience(
            supervisor.identity,
            context,
            rule_id="verified-assign",
            rule_version="1",
            receipt_ref=source,
        )


def test_memory_and_audit_content_are_immutable(database, sample):
    saved = confirm(sample, propose(sample))
    for command in (
        "UPDATE app.business_memories SET content='{}' WHERE id=%s",
        "DELETE FROM app.business_memories WHERE id=%s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
            conn.execute(command, (saved["id"],))
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute("DELETE FROM app.business_memory_audit")
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.memory_candidates").fetchone()["n"] == 0


def test_history_lookup_requires_explicit_current_context_to_claim_eligibility(sample):
    saved = confirm(sample, propose(sample))
    historical = sample["store"].get(sample["backend"].browser, saved["id"])
    assert historical["status"] == "active" and historical["active"] is False
    current = sample["store"].get(sample["backend"].browser, saved["id"], context=sample["context"])
    assert current["active"] is True


def test_own_history_listing_is_employee_filtered_and_bounded(database, sample):
    saved = confirm(sample, propose(sample))
    rows = sample["store"].list_own(sample["backend"].browser, context=sample["context"], limit=1)
    assert [row["id"] for row in rows] == [saved["id"]]
    assert rows[0]["active"] is True
    other = FixtureTickets(database, login="employee-facilities")
    assert sample["store"].list_own(other.browser) == []
    sample["clock"][0] = saved["expires_at"]
    history = sample["store"].list_own(sample["backend"].browser)
    assert history[0]["id"] == saved["id"] and history[0]["active"] is False
    with pytest.raises(ValueError, match="limit"):
        sample["store"].list_own(sample["backend"].browser, limit=True)


@pytest.fixture
def schema_15_database(database):
    with temporary_database(database.url, prefix="case_memory15_") as older:
        yield older


def test_schema_15_upgrade_preserves_business_operations_and_is_repeatable(
    schema_15_database, monkeypatch
):
    from decagent.storage import database as module
    from evals.integration.conditional_support import create_mixed_business_tasks

    older = schema_15_database
    with monkeypatch.context() as original:
        original.setattr(module, "MIGRATIONS", module.MIGRATIONS[:15])
        original.setattr(module, "SCHEMA_VERSION", 15)
        older.migrate()
        mixed = create_mixed_business_tasks(older)
        with older.connect() as conn:
            checksums = conn.execute(
                "SELECT * FROM app.schema_migrations ORDER BY version"
            ).fetchall()
    older.migrate()
    older.migrate()
    with older.connect() as conn:
        assert (
            conn.execute(
                "SELECT * FROM app.schema_migrations WHERE version<=15 ORDER BY version"
            ).fetchall()
            == checksums
        )
        assert conn.execute("SELECT count(*) AS n FROM app.business_memories").fetchone()["n"] == 0
        action = conn.execute(
            "SELECT status,server_operation_id FROM app.actions WHERE id=%s",
            (mixed["action"]["id"],),
        ).fetchone()
        assert action["status"] == "unknown"
        assert action["server_operation_id"] == mixed["operation"]["operation_id"]
    for version, saved in mixed["tasks"].items():
        assert older.get_task(saved["id"])["graph_version"] == version

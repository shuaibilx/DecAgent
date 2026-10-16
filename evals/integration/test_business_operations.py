"""Real PostgreSQL operation handles preserve identity, immutable input and effects."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.employee_accounts import _DEMO_ACCOUNTS
from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.grants import GrantAuthority
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore

pytestmark = pytest.mark.integration


@pytest.fixture
def operations(database):
    assert TicketStore(database.url).execute("seed", {}, "operations-seed")["ok"]
    scope = PluginRegistry(database).register(builtin_manifest())
    actors = {}
    with database.connect() as conn:
        people = {
            (p["name"], p["team"]): p["id"]
            for p in conn.execute("SELECT id,name,team FROM business.people").fetchall()
        }
        category = str(conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"])
        for login, name, team, role in _DEMO_ACCOUNTS:
            employee_id, ref = uuid4(), uuid4()
            token_hash = hashlib.sha256(ref.bytes).hexdigest()
            conn.execute(
                "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                "VALUES(%s,%s,%s,%s,%s)",
                (employee_id, login, people[name, team], role, "test-only-account-hash"),
            )
            conn.execute(
                "INSERT INTO business.mcp_credentials"
                "(id,token_hash,employee_id,account_revision,expires_at) "
                "VALUES(%s,%s,%s,1,now()+interval '1 hour')",
                (ref, token_hash, employee_id),
            )
            actors[login] = EmployeeIdentity(
                employee_id, people[name, team], login, role, 1, "mcp_token", token_hash, ref
            )
    return database, OperationService(database), actors, category, scope


def _counts(database):
    with database.connect() as conn:
        return tuple(
            conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        )


def test_prepare_has_no_ticket_history_or_receipt_side_effects_and_replays_same_input(operations):
    database, service, actors, category, _ = operations
    before = _counts(database)
    params = {"title": "VPN ; echo data", "category_id": category}
    first = service.prepare(actors["employee-it"], "stable-request", "create", params)
    assert first["state"] == "ready" and first["result"] is None
    assert first["source"] == "external_mcp"
    assert first["params"] == params
    assert first == service.prepare(actors["employee-it"], "stable-request", "create", params)
    assert _counts(database) == before
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM business.operations").fetchone()
    assert row["effective_params"]["requester_id"] == str(actors["employee-it"].person_id)
    assert row["employee_id"] == actors["employee-it"].employee_id
    assert row["input_hash"] == first["input_hash"]


def test_prepare_request_key_conflict_and_cross_employee_query_are_safe(operations):
    database, service, actors, category, _ = operations
    params = {"title": "private request", "category_id": category}
    first = service.prepare(actors["employee-it"], "same-key", "create", params)
    with pytest.raises(AccessError) as error:
        service.prepare(actors["employee-it"], "same-key", "create", {**params, "title": "changed"})
    assert error.value.code == "operation_conflict"
    other = service.prepare(actors["employee-facilities"], "same-key", "create", params)
    assert other["operation_id"] != first["operation_id"]
    with pytest.raises(AccessError) as error:
        service.get(actors["employee-facilities"], first["operation_id"])
    assert error.value.code == "not_found" and "private" not in str(error.value)
    assert (
        service.get(actors["supervisor"], first["operation_id"])["operation_id"]
        == first["operation_id"]
    )
    assert _counts(database)[0] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("requester_id", "00000000-0000-0000-0000-000000000000"),
        ("employee_id", "00000000-0000-0000-0000-000000000000"),
        ("task_id", "00000000-0000-0000-0000-000000000000"),
        ("source", "decagent"),
        ("approved", True),
        ("role", "supervisor"),
    ],
)
def test_prepare_rejects_source_and_identity_forgery(operations, field, value):
    database, service, actors, category, _ = operations
    with pytest.raises(AccessError):
        service.prepare(
            actors["employee-it"],
            "forged",
            "create",
            {"title": "attempt", "category_id": category, field: value},
        )
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 0


def test_prepare_rechecks_credential_and_rejects_fake_agent_grant(operations):
    database, service, actors, category, _ = operations
    identity = actors["employee-it"]
    params = {"title": "request", "category_id": category}
    with pytest.raises(AccessError, match="grant"):
        service.prepare(identity, "fake-agent", "create", params, grant="self-approved")
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s",
            (identity.credential_ref,),
        )
    with pytest.raises(AccessError) as error:
        service.prepare(identity, "revoked", "create", params)
    assert error.value.code == "unauthorized"
    assert _counts(database)[0] == 0


def test_concurrent_prepare_has_one_handle_and_database_rejects_input_mutation(operations):
    database, service, actors, category, _ = operations
    params = {"title": "one request", "category_id": category}
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(
                lambda _: service.prepare(actors["employee-it"], "concurrent", "create", params),
                range(2),
            )
        )
    assert replies[0] == replies[1]
    with pytest.raises(psycopg.IntegrityError):
        with database.connect() as conn:
            conn.execute("UPDATE business.operations SET params='{}'::jsonb")
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 1
    assert _counts(database)[0] == 0


def _ticket(database, actor, category, *, status="open", assignee=None):
    identifier = str(uuid4())
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,employee_id,requester_id,"
            "status,assignee_id) VALUES(%s,'existing ticket',%s,%s,%s,%s,%s)",
            (identifier, category, actor.employee_id, actor.person_id, status, assignee),
        )
    return identifier


def test_repeated_prepare_returns_original_handle_after_resource_changes(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    ticket_id = _ticket(database, actor, category)
    params = {"ticket_id": ticket_id, "expected_revision": 1, "body": "first note"}
    first = service.prepare(actor, "comment-key", "comment", params)
    with database.connect() as conn:
        conn.execute("UPDATE business.tickets SET revision=2 WHERE id=%s", (ticket_id,))
    assert service.prepare(actor, "comment-key", "comment", params) == first


def _browser(database, identity):
    token_hash = hashlib.sha256(uuid4().bytes).hexdigest()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.employee_sessions(token_hash,employee_id,account_revision,"
            "csrf_hash,expires_at) VALUES(%s,%s,1,%s,now()+interval '12 hours')",
            (token_hash, identity.employee_id, "c" * 64),
        )
    return replace(identity, source="employee_session", token_hash=token_hash, credential_ref=None)


def test_approval_requires_original_browser_session_and_is_specific_and_idempotent(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    ticket = _ticket(database, actor, category, status="resolved")
    prepared = service.prepare(
        actor, "close-confirm", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    assert prepared["state"] == "awaiting_approval" and prepared["approval_url"]
    for wrong in (
        actor,
        _browser(database, actors["supervisor"]),
        _browser(database, actors["employee-facilities"]),
    ):
        with pytest.raises(AccessError):
            service.reply(wrong, prepared["operation_id"], prepared["input_hash"], "approve")
    browser = _browser(database, actor)
    approved = service.reply(browser, prepared["operation_id"], prepared["input_hash"], "approve")
    assert approved["state"] == "ready" and approved["approval_url"] is None
    assert approved == service.reply(
        browser, prepared["operation_id"], prepared["input_hash"], "approve"
    )
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM business.operation_approvals").fetchone()
    assert row["employee_id"] == actor.employee_id
    assert row["expires_at"] - row["approved_at"] == timedelta(minutes=15)
    assert row["consumed_at"] is None
    with pytest.raises(AccessError):
        service.reply(browser, prepared["operation_id"], "f" * 64, "approve")
    assert _counts(database) == (1, 0, 1)


def test_reject_and_edit_do_not_mutate_prepared_input_or_create_business_receipts(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    ticket = _ticket(database, actor, category, status="resolved")
    another = _ticket(database, actor, category, status="resolved")
    original = service.prepare(
        actor, "edit-close", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    browser = _browser(database, actor)
    changed = service.reply(
        browser,
        original["operation_id"],
        original["input_hash"],
        "edit",
        edited_params={"ticket_id": another, "expected_revision": 1},
    )
    assert changed["operation_id"] != original["operation_id"]
    assert changed["state"] == "awaiting_approval"
    assert changed["params"]["ticket_id"] == another
    old = service.get(actor, original["operation_id"])
    assert old["state"] == "rejected" and old["params"]["ticket_id"] == ticket
    assert changed == service.reply(
        browser,
        original["operation_id"],
        original["input_hash"],
        "edit",
        edited_params=changed["params"],
    )
    rejected = service.reply(browser, changed["operation_id"], changed["input_hash"], "reject")
    assert rejected["state"] == "rejected" and rejected["result"] is None
    assert _counts(database) == (2, 0, 1)


def test_expired_approval_and_resource_drift_do_not_keep_old_authority(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    browser = _browser(database, actor)
    ticket = _ticket(database, actor, category, status="resolved")
    operation = service.prepare(
        actor, "expires", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    service.reply(browser, operation["operation_id"], operation["input_hash"], "approve")
    with database.connect() as conn:
        conn.execute("UPDATE business.operation_approvals SET expires_at=now()-interval '1 second'")
    assert service.get(actor, operation["operation_id"])["state"] == "expired"
    assert (
        service.reply(browser, operation["operation_id"], operation["input_hash"], "approve")[
            "state"
        ]
        == "expired"
    )
    stale = service.prepare(actor, "stale", "close", {"ticket_id": ticket, "expected_revision": 1})
    with database.connect() as conn:
        conn.execute("UPDATE business.tickets SET revision=2 WHERE id=%s", (ticket,))
    assert (
        service.reply(browser, stale["operation_id"], stale["input_hash"], "approve")["state"]
        == "failed"
    )
    assert _counts(database) == (1, 0, 1)


def test_commit_create_binds_employee_receipt_and_concurrent_replay_writes_once(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    prepared = service.prepare(
        actor, "commit-once", "create", {"title": "one persisted ticket", "category_id": category}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: service.commit(actor, prepared["operation_id"]), range(2))
        )
    assert results[0] == results[1]
    result = results[0]
    assert result["state"] == "succeeded"
    assert result["result"]["ticket"]["employee_id"] == str(actor.employee_id)
    assert result["result"]["ticket"]["requester_id"] == str(actor.person_id)
    receipt = result["result"]["receipt"]
    assert receipt["input_hash"] == prepared["input_hash"]
    assert receipt["employee_id"] == str(actor.employee_id)
    assert receipt["contract_digest"] == service.spec.digest
    assert _counts(database) == (1, 1, 2)
    with database.connect() as conn:
        history = conn.execute("SELECT * FROM business.ticket_history").fetchone()
    assert history["employee_id"] == actor.employee_id
    assert history["operation_id"] == prepared["operation_id"]
    assert history["source"] == "external_mcp"
    with pytest.raises(AccessError):
        service.commit(actors["supervisor"], prepared["operation_id"])


def test_commit_sensitive_operation_needs_approval_and_consumes_it_with_business_write(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    ticket = _ticket(database, actor, category, status="resolved")
    prepared = service.prepare(
        actor, "commit-close", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    assert service.commit(actor, prepared["operation_id"])["state"] == "awaiting_approval"
    assert _counts(database) == (1, 0, 1)
    service.reply(
        _browser(database, actor), prepared["operation_id"], prepared["input_hash"], "approve"
    )
    result = service.commit(actor, prepared["operation_id"])
    assert result["state"] == "succeeded" and result["result"]["ticket"]["status"] == "closed"
    with database.connect() as conn:
        assert conn.execute("SELECT consumed_at FROM business.operation_approvals").fetchone()[
            "consumed_at"
        ]
    assert service.commit(actor, prepared["operation_id"]) == result
    assert _counts(database) == (1, 1, 2)


def test_commit_role_or_revision_drift_does_not_write_and_domain_failure_rolls_back(
    operations, monkeypatch
):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    supervisor = actors["supervisor"]
    ticket = _ticket(database, actor, category)
    priority = service.prepare(
        supervisor,
        "role-drift",
        "set_priority",
        {"ticket_id": ticket, "expected_revision": 1, "priority": "high"},
    )
    service.reply(
        _browser(database, supervisor), priority["operation_id"], priority["input_hash"], "approve"
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET role='employee' WHERE id=%s",
            (supervisor.employee_id,),
        )
    assert service.commit(supervisor, priority["operation_id"])["state"] == "failed"
    created = service.prepare(
        actor, "rollback", "create", {"title": "rollback", "category_id": category}
    )
    original = TicketStore._write

    def broken(self, conn, *args, **kwargs):
        original(self, conn, *args, **kwargs)
        raise RuntimeError("injected failure after domain mutation")

    monkeypatch.setattr(TicketStore, "_write", broken)
    assert service.commit(actor, created["operation_id"])["state"] == "failed"
    assert _counts(database) == (1, 0, 1)


def test_failed_commit_rolls_back_receipt_history_and_approval_consumption(operations, monkeypatch):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    ticket = _ticket(database, actor, category, status="resolved")
    prepared = service.prepare(
        actor, "rollback-approval", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    service.reply(
        _browser(database, actor), prepared["operation_id"], prepared["input_hash"], "approve"
    )
    original = TicketStore.execute_in_transaction

    def broken(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("injected failure after receipt and approval consumption")

    monkeypatch.setattr(TicketStore, "execute_in_transaction", broken)
    assert service.commit(actor, prepared["operation_id"])["state"] == "failed"
    with database.connect() as conn:
        assert (
            conn.execute("SELECT consumed_at FROM business.operation_approvals").fetchone()[
                "consumed_at"
            ]
            is None
        )
        assert conn.execute(
            "SELECT status,revision FROM business.tickets WHERE id=%s", (ticket,)
        ).fetchone() == {"status": "resolved", "revision": 1}
    assert _counts(database) == (1, 0, 1)


def test_stale_approved_revision_and_expired_approval_cannot_commit(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    browser = _browser(database, actor)
    ticket = _ticket(database, actor, category, status="resolved")
    prepared = service.prepare(
        actor, "stale-approved", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    service.reply(browser, prepared["operation_id"], prepared["input_hash"], "approve")
    with database.connect() as conn:
        conn.execute("UPDATE business.tickets SET revision=2 WHERE id=%s", (ticket,))
    assert service.commit(actor, prepared["operation_id"])["state"] == "failed"
    expires = service.prepare(
        actor, "expired-commit", "close", {"ticket_id": ticket, "expected_revision": 2}
    )
    service.reply(browser, expires["operation_id"], expires["input_hash"], "approve")
    with database.connect() as conn:
        conn.execute("UPDATE business.operation_approvals SET expires_at=now()-interval '1 second'")
    assert service.commit(actor, expires["operation_id"])["state"] == "expired"
    assert _counts(database) == (1, 0, 1)


def _agent(operations, *, outcome="allow"):
    database, service, actors, category, scope = operations
    actor = actors["employee-it"]
    params = {"title": "agent request", "category_id": category}
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "create an agent request",
            "plugin_digest": service.spec.digest,
        },
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET employee_id=%s,credential_ref=%s WHERE id=%s",
            (actor.employee_id, actor.credential_ref, task["id"]),
        )
    leased = database.claim(task["id"], "worker-1", seconds=120)
    request_id = "agent_" + uuid4().hex
    snapshot = {
        "task_id": str(task["id"]),
        "scope_id": str(scope["id"]),
        "plugin_digest": service.spec.digest,
        "command": "create",
        "params": params,
        "operation_id": request_id,
        "assessment": {"outcome": outcome, "digest": "a" * 64},
    }
    action = database.plan_action(task["id"], "create-1", "a" * 64, snapshot)
    authority = GrantAuthority(database)
    token = authority.issue(task["id"], action["id"], "worker-1", leased["lease_generation"])
    return actor, leased, action, params, request_id, token, authority


def test_agent_prepare_and_commit_require_hidden_bound_grant(operations):
    database, service, actors, _, _ = operations
    actor, task, action, params, request_id, token, _ = _agent(operations)
    prepared = service.prepare(actor, request_id, "create", params, grant=token)
    assert prepared["source"] == "decagent" and token not in str(prepared)
    with database.connect() as conn:
        saved = conn.execute("SELECT * FROM business.execution_grants").fetchone()
    assert saved["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(saved)
    for forged in (None, "fake", {"task_id": str(task["id"])}):
        with pytest.raises(AccessError):
            service.commit(actor, prepared["operation_id"], grant=forged)
    with pytest.raises(AccessError):
        service.prepare(actors["employee-facilities"], request_id, "create", params, grant=token)
    assert _counts(database)[0] == 0
    result = service.commit(actor, prepared["operation_id"], grant=token)
    assert result["state"] == "succeeded"
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT server_operation_id FROM app.actions WHERE id=%s", (action["id"],)
            ).fetchone()["server_operation_id"]
            == prepared["operation_id"]
        )


@pytest.mark.parametrize("guard", ["cancelled", "generation", "budget", "plugin", "grant_expired"])
def test_agent_commit_rechecks_each_execution_guard(operations, guard):
    database, service, _, _, _ = operations
    actor, task, action, params, request_id, token, _ = _agent(operations)
    prepared = service.prepare(actor, request_id, "create", params, grant=token)
    with database.connect() as conn:
        if guard == "cancelled":
            conn.execute("UPDATE app.tasks SET state='cancelled' WHERE id=%s", (task["id"],))
        elif guard == "generation":
            conn.execute(
                "UPDATE app.tasks SET lease_generation=lease_generation+1 WHERE id=%s",
                (task["id"],),
            )
        elif guard == "budget":
            conn.execute(
                "UPDATE app.tasks SET budget=%s WHERE id=%s", (Jsonb({"max_calls": 0}), task["id"])
            )
        elif guard == "plugin":
            conn.execute(
                "UPDATE app.business_scopes SET enabled=false WHERE id=%s",
                (task["business_scope_id"],),
            )
        else:
            conn.execute(
                "UPDATE business.execution_grants SET expires_at=now()-interval '1 second'"
            )
    try:
        result = service.commit(actor, prepared["operation_id"], grant=token)
        assert result["state"] != "succeeded"
    except AccessError:
        pass
    assert _counts(database)[0] == 0


def test_agent_ambiguous_intent_requires_browser_approval_even_for_auto_domain_command(operations):
    database, service, _, _, _ = operations
    actor, _, _, params, request_id, token, _ = _agent(operations, outcome="confirm")
    prepared = service.prepare(actor, request_id, "create", params, grant=token)
    assert prepared["state"] == "awaiting_approval"
    assert (
        service.commit(actor, prepared["operation_id"], grant=token)["state"] == "awaiting_approval"
    )
    service.reply(
        _browser(database, actor), prepared["operation_id"], prepared["input_hash"], "approve"
    )
    assert service.commit(actor, prepared["operation_id"], grant=token)["state"] == "succeeded"


def test_disable_and_reregister_same_manifest_does_not_revive_old_approval(operations):
    database, service, actors, category, scope = operations
    actor = actors["employee-it"]
    ticket = _ticket(database, actor, category, status="resolved")
    operation = service.prepare(
        actor, "configuration-stamp", "close", {"ticket_id": ticket, "expected_revision": 1}
    )
    service.reply(
        _browser(database, actor), operation["operation_id"], operation["input_hash"], "approve"
    )
    registry = PluginRegistry(database)
    registry.disable(scope["id"])
    registry.register(builtin_manifest())
    result = service.commit(actor, operation["operation_id"])
    assert result["state"] == "failed" and result["error_code"] == "contract_changed"
    assert _counts(database) == (1, 0, 1)


def test_revoked_credential_denial_is_audited_and_can_be_seen_after_same_employee_login(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    browser = _browser(database, actor)
    operation = service.prepare(
        actor, "credential-block", "create", {"title": "reauthentication", "category_id": category}
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s",
            (actor.credential_ref,),
        )
    with pytest.raises(AccessError) as failure:
        service.commit(actor, operation["operation_id"])
    assert failure.value.code == "unauthorized"
    view = service.get(browser, operation["operation_id"])
    assert view["state"] == "ready" and view["error_code"] == "employee_credential_unavailable"
    with database.connect() as conn:
        audit = conn.execute(
            "SELECT * FROM business.employee_audit WHERE event='credential_rejected'"
        ).fetchone()
    assert audit["actor_employee_id"] == actor.employee_id and audit["outcome"] == "denied"
    assert _counts(database)[0] == 0


def test_lease_takeover_uses_new_grant_for_same_original_operation(operations):
    database, service, _, _, _ = operations
    actor, task, action, params, request_id, token, authority = _agent(operations)
    prepared = service.prepare(actor, request_id, "create", params, grant=token)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s", (task["id"],)
        )
    current = database.claim(task["id"], "worker-2", seconds=120)
    renewed = authority.issue(task["id"], action["id"], "worker-2", current["lease_generation"])
    with pytest.raises(AccessError):
        service.commit(actor, prepared["operation_id"], grant=token)
    assert service.commit(actor, prepared["operation_id"], grant=renewed)["state"] == "succeeded"
    assert _counts(database) == (1, 1, 2)


class SimulatedCrash(BaseException):
    """Escape the ordinary exception handler like a terminated worker."""


@pytest.mark.parametrize("phase", ["before_write", "after_write"])
def test_crash_before_transaction_commit_recovers_original_operation(
    operations, monkeypatch, phase
):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    prepared = service.prepare(
        actor, "crash-" + phase, "create", {"title": phase, "category_id": category}
    )
    before = _counts(database)
    original = TicketStore.execute_in_transaction

    def crash(self, *args, **kwargs):
        if phase == "before_write":
            raise SimulatedCrash()
        original(self, *args, **kwargs)
        raise SimulatedCrash()

    with monkeypatch.context() as change:
        change.setattr(TicketStore, "execute_in_transaction", crash)
        with pytest.raises(SimulatedCrash):
            service.commit(actor, prepared["operation_id"])
    assert _counts(database) == before
    recovered = service.get(actor, prepared["operation_id"])
    assert recovered["state"] == "ready"
    assert recovered["operation_id"] == prepared["operation_id"]
    assert service.commit(actor, prepared["operation_id"])["state"] == "succeeded"
    assert _counts(database) == (before[0] + 1, before[1] + 1, before[2] + 1)


def test_response_lost_after_commit_is_verified_from_receipt_without_rewrite(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    prepared = service.prepare(
        actor, "lost-response", "create", {"title": "receipt first", "category_id": category}
    )

    def transport_loses_response():
        assert service.commit(actor, prepared["operation_id"])["state"] == "succeeded"
        raise ConnectionError("response did not reach the caller")

    with pytest.raises(ConnectionError):
        transport_loses_response()
    before = _counts(database)
    verified = service.get(actor, prepared["operation_id"])
    assert verified["state"] == "succeeded"
    assert verified["result"]["receipt"]["operation_id"] == prepared["operation_id"]
    assert service.commit(actor, prepared["operation_id"]) == verified
    assert _counts(database) == before


def test_query_during_in_flight_commit_does_not_infer_uncommitted_effect(operations, monkeypatch):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    prepared = service.prepare(
        actor, "in-flight", "create", {"title": "still running", "category_id": category}
    )
    entered, release = Event(), Event()
    original = TicketStore.execute_in_transaction

    def held(self, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TicketStore, "execute_in_transaction", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        committing = pool.submit(service.commit, actor, prepared["operation_id"])
        try:
            assert entered.wait(timeout=10)
            viewing = pool.submit(service.get, actor, prepared["operation_id"])
            observed = viewing.result(timeout=3)
            assert observed["state"] in {"running", "unknown"}
            assert observed["result"] is None
        finally:
            release.set()
        assert committing.result(timeout=10)["state"] == "succeeded"
    assert service.get(actor, prepared["operation_id"])["state"] == "succeeded"


def test_inconsistent_receipt_remains_unknown_and_never_retries_write(operations):
    database, service, actors, category, _ = operations
    actor = actors["employee-it"]
    prepared = service.prepare(
        actor, "bad-receipt", "create", {"title": "one write", "category_id": category}
    )
    assert service.commit(actor, prepared["operation_id"])["state"] == "succeeded"
    before = _counts(database)
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.operation_receipts SET input_hash=%s WHERE operation_id=%s",
            ("0" * 64, prepared["operation_id"]),
        )
    observed = service.get(actor, prepared["operation_id"])
    assert observed["state"] == "unknown"
    assert observed["error_code"] == "receipt_mismatch"
    assert service.commit(actor, prepared["operation_id"])["state"] == "unknown"
    assert _counts(database) == before


def test_agent_crash_recovery_requires_current_lease_grant_for_original_id(operations, monkeypatch):
    database, service, _, _, _ = operations
    actor, task, action, params, request_id, token, authority = _agent(operations)
    prepared = service.prepare(actor, request_id, "create", params, grant=token)

    def crash(self, *args, **kwargs):
        raise SimulatedCrash()

    with monkeypatch.context() as change:
        change.setattr(TicketStore, "execute_in_transaction", crash)
        with pytest.raises(SimulatedCrash):
            service.commit(actor, prepared["operation_id"], grant=token)
    pending = service.get(actor, prepared["operation_id"])
    assert pending["state"] == "unknown"
    assert pending["error_code"] == "recovery_grant_required"
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s",
            (task["id"],),
        )
    new_lease = database.claim(task["id"], "replacement-worker", seconds=120)
    renewed = authority.issue(
        task["id"], action["id"], "replacement-worker", new_lease["lease_generation"]
    )
    stale = service.commit(actor, prepared["operation_id"], grant=token)
    assert stale["state"] == "unknown"
    assert _counts(database)[0] == 0
    completed = service.commit(actor, prepared["operation_id"], grant=renewed)
    assert completed["state"] == "succeeded"
    assert completed["operation_id"] == prepared["operation_id"]
    assert _counts(database) == (1, 1, 2)

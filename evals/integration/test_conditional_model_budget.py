"""Conditional model quotas are durable, classified and independent of tool quota."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from psycopg.types.json import Jsonb

from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger

pytestmark = pytest.mark.integration


def conditional_task(database, **budget):
    project = database.add_project("fixture/conditional-budget")
    task = database.create_task(
        project["id"],
        "a" * 40,
        {},
        {"max_tokens": 1_000_000, "max_cost": "10", **budget},
        graph_version="business-3",
    )
    return database.claim(task["id"], "budget-worker", seconds=120)


def reserve(ledger, key, family="primary", **metadata):
    return ledger.reserve(
        key,
        1,
        "arbitrary-route",
        10,
        Decimal(".01"),
        model_family=family,
        purpose="plan" if family == "primary" else "decision",
        **metadata,
    )


def rows(database, task):
    with database.connect() as conn:
        return conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s ORDER BY created_at,id",
            (task["id"],),
        ).fetchall()


def test_primary_reservations_are_atomic_and_preserve_node_purpose(database):
    task = conditional_task(database)
    barrier = Barrier(12)

    def attempt(index):
        barrier.wait()
        try:
            return reserve(
                UsageLedger(database, task["id"]),
                str(index),
                plan_revision=2,
                node_id="repair",
                exception_reason="new_evidence",
            )
        except BudgetExceeded as error:
            return error.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    saved = rows(database, task)
    assert len(saved) == 6
    assert results.count("primary_budget_exhausted") == 6
    assert all(row["model_family"] == "primary" for row in saved)
    assert all(row["provider"] == "arbitrary-route" for row in saved)
    assert all(row["purpose"] == "plan" and row["plan_revision"] == 2 for row in saved)
    assert all(row["node_id"] == "repair" and row["node_ids"] == ["repair"] for row in saved)
    assert all(row["exception_reason"] == "new_evidence" for row in saved)


def test_jev_and_total_limits_are_independent_and_count_each_request(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    for index in range(6):
        receipt = reserve(ledger, f"primary-{index}", exception_reason="new_evidence")
        ledger.settle(receipt, 10, Decimal(".01"))
    ledger.assert_action_available()
    for index in range(24):
        receipt = reserve(ledger, f"jev-{index}", "jev")
        ledger.settle(receipt, 10, Decimal(".01"))
    ledger.assert_action_available()
    with pytest.raises(BudgetExceeded) as caught:
        reserve(ledger, "jev-overflow", "jev")
    assert caught.value.code in {"jev_budget_exhausted", "model_request_budget_exhausted"}
    assert len(rows(database, task)) == 30


def test_primary_four_through_six_require_a_durable_exception_reason(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    for index in range(3):
        reserve(ledger, str(index))
    with pytest.raises(BudgetExceeded) as caught:
        reserve(ledger, "unexplained-fourth")
    assert caught.value.code == "primary_exception_reason_required"
    for index in range(3, 6):
        reserve(ledger, str(index), exception_reason="format_repair")
    assert [row["exception_reason"] for row in rows(database, task)] == [
        None,
        None,
        None,
        "format_repair",
        "format_repair",
        "format_repair",
    ]


def test_not_sent_attempt_retains_audit_but_releases_only_conditional_call_slot(database):
    task = conditional_task(database, max_calls=1, max_primary_calls=1, primary_target=1)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "local-failure")
    ledger.mark_not_sent(receipt, reason="local_validation")
    later = reserve(ledger, "actual-request")
    ledger.mark_dispatched(later)
    ledger.settle_model(
        later, 8, Decimal(".01"), response_state="responded", error_code="invalid_plan"
    )
    saved = rows(database, task)
    assert len(saved) == 2
    assert saved[0]["dispatch_state"] == "not_sent"
    assert saved[0]["actual_tokens"] == 0 and saved[0]["actual_cost"] == 0
    assert saved[1]["dispatch_state"] == "responded" and saved[1]["error_code"] == "invalid_plan"
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "overflow")


def test_unknown_response_survives_recovery_and_blocks_new_actions_and_models(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "lost-response")
    ledger.mark_dispatched(receipt)
    ledger.settle_model(
        receipt, None, None, response_state="unknown", error_code="transport_error",
        error_category="transport_error",
    )
    restored = UsageLedger(database, task["id"])
    for action in [restored.assert_action_available, lambda: reserve(restored, "new-key", "jev")]:
        with pytest.raises(BudgetExceeded) as caught:
            action()
        assert caught.value.code == "unknown_model_cost"
    saved = rows(database, task)[0]
    assert saved["status"] == "unknown" and saved["reserved_cost"] == Decimal(".01")
    with pytest.raises(ValueError):
        restored.mark_not_sent(receipt, reason="local_validation")


def test_unknown_primary_can_only_be_retried_as_a_separately_reserved_recovery(database):
    task = conditional_task(database, max_calls=8, max_primary_calls=6, max_cost="1")
    ledger = UsageLedger(database, task["id"])
    original = reserve(ledger, "qwen-first", exception_reason="provider_retry")
    ledger.mark_dispatched(original)
    ledger.settle_model(
        original, None, None, response_state="unknown", error_code="provider_timeout",
        error_category="timeout",
    )

    with pytest.raises(BudgetExceeded) as blocked:
        reserve(ledger, "qwen-unrelated", exception_reason="provider_retry")
    assert blocked.value.code == "unknown_model_cost"

    retry = ledger.reserve_model(
        "qwen-retry",
        1,
        "qwen",
        10,
        Decimal(".01"),
        model_family="primary",
        purpose="plan",
        exception_reason="provider_retry",
        recovery_of=original,
        request_role="plan",
        requested_model="qwen3.8-flash",
        endpoint="https://maas.qianwenaiapi.com/compatible-mode/v1",
        requested_output_tokens=5,
    )
    ledger.mark_dispatched(retry)
    ledger.settle_model(
        retry, 7, Decimal(".01"), response_state="responded", error_code=None,
        provider_status=200, server_request_id="provider-req-123",
    )

    another = reserve(ledger, "qwen-after-recovery", exception_reason="provider_retry")
    assert another != retry
    saved = rows(database, task)
    assert len(saved) == 3
    assert saved[0]["status"] == "unknown" and saved[0]["reserved_cost"] == Decimal(".01")
    assert saved[1]["recovery_of"] == original and saved[1]["status"] == "settled"
    assert saved[1]["requested_output_tokens"] == 5
    assert saved[1]["provider_status"] == 200
    assert saved[1]["server_request_id"] == "provider-req-123"
    assert saved[2]["recovery_of"] is None


def test_proven_not_sent_recovery_can_be_retried_without_dropping_original_unknown(database):
    task = conditional_task(database, max_calls=8, max_primary_calls=6, max_cost="1")
    ledger = UsageLedger(database, task["id"])
    original = reserve(ledger, "qwen-first", exception_reason="provider_retry")
    ledger.mark_dispatched(original)
    ledger.settle_model(
        original, None, None, response_state="unknown", error_code="provider_timeout",
        error_category="timeout",
    )
    first_recovery = ledger.reserve_model(
        "qwen-recovery-preflight-failure", 1, "qwen", 10, Decimal(".01"),
        model_family="primary", purpose="plan", exception_reason="provider_retry",
        recovery_of=original,
    )
    ledger.mark_not_sent(first_recovery, reason="before_dispatch_guard")

    retry = ledger.reserve_model(
        "qwen-recovery-after-preflight", 1, "qwen", 10, Decimal(".01"),
        model_family="primary", purpose="plan", exception_reason="provider_retry",
        recovery_of=first_recovery,
    )
    ledger.mark_dispatched(retry)
    ledger.settle_model(retry, 7, Decimal(".01"), response_state="responded")

    saved = rows(database, task)
    assert [row["dispatch_state"] for row in saved] == ["unknown", "not_sent", "responded"]
    assert saved[2]["recovery_of"] == first_recovery


def test_unknown_recovery_is_single_use_and_charges_both_reservations(database):
    task = conditional_task(
        database, max_calls=8, max_primary_calls=6, max_tokens=100, max_cost=".025"
    )
    ledger = UsageLedger(database, task["id"])
    original = reserve(ledger, "qwen-first")
    ledger.mark_dispatched(original)
    ledger.settle_model(
        original, None, None, response_state="unknown", error_code="provider_timeout",
        error_category="timeout",
    )

    retry = ledger.reserve_model(
        "qwen-retry",
        1,
        "qwen",
        10,
        Decimal(".01"),
        model_family="primary",
        purpose="plan",
        exception_reason="provider_retry",
        recovery_of=original,
    )
    ledger.mark_dispatched(retry)
    ledger.settle_model(
        retry, 7, Decimal(".01"), response_state="responded", error_code=None
    )
    with pytest.raises(BudgetExceeded) as cost_blocked:
        reserve(ledger, "beyond-token-budget", exception_reason="provider_retry")
    assert cost_blocked.value.code == "cost_budget_exhausted"

    with pytest.raises(BudgetExceeded):
        ledger.reserve_model(
            "second-recovery",
            1,
            "deepseek",
            10,
            Decimal(".02"),
            model_family="primary",
            purpose="plan",
            exception_reason="provider_retry",
            recovery_of=original,
        )

    saved = rows(database, task)
    assert len(saved) == 2
    assert saved[0]["recovery_of"] is None
    assert saved[1]["recovery_of"] == original


def test_jev_cannot_recover_an_unknown_primary_request(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    original = reserve(ledger, "qwen-first")
    ledger.mark_dispatched(original)
    ledger.settle_model(
        original, None, None, response_state="unknown", error_code="provider_timeout",
        error_category="timeout",
    )

    with pytest.raises(BudgetExceeded) as caught:
        ledger.reserve_model(
            "jev-recovery",
            1,
            "jev",
            10,
            Decimal(".02"),
            model_family="jev",
            purpose="decision",
            recovery_of=original,
        )
    assert caught.value.code == "unknown_model_cost"
    assert len(rows(database, task)) == 1


def test_unsettled_dispatch_stays_reserved_after_crash(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "crashed")
    ledger.mark_dispatched(receipt)
    with pytest.raises(BudgetExceeded) as caught:
        UsageLedger(database, task["id"]).assert_action_available()
    assert caught.value.code == "unknown_model_cost"
    assert rows(database, task)[0]["status"] == "reserved"


@pytest.mark.parametrize("boundary", ["cancel", "lease", "unknown"])
def test_dispatch_rechecks_hard_boundary_after_reservation(database, boundary):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "reserved-not-sent")
    if boundary == "unknown":
        other = reserve(ledger, "earlier", "jev")
        ledger.mark_dispatched(other)
        ledger.settle_model(other, None, None, response_state="unknown")
    else:
        with database.connect() as conn:
            if boundary == "cancel":
                conn.execute("UPDATE app.tasks SET state='cancelled' WHERE id=%s", (task["id"],))
            else:
                conn.execute(
                    "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s",
                    (task["id"],),
                )
    with pytest.raises(BudgetExceeded):
        ledger.mark_dispatched(receipt)
    ledger.mark_not_sent(receipt, reason="dispatch_guard")
    assert (
        next(row for row in rows(database, task) if row["id"] == receipt)["dispatch_state"]
        == "not_sent"
    )


def test_already_dispatching_reservation_requires_explicit_local_no_send_proof(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "dispatching")
    ledger.mark_dispatched(receipt)
    with pytest.raises(ValueError):
        ledger.mark_not_sent(receipt, reason="dispatch_guard")
    assert rows(database, task)[0]["dispatch_state"] == "dispatching"


def test_model_token_exhaustion_does_not_block_conditional_tools(database):
    task = conditional_task(database, max_tokens=10)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "all-tokens")
    ledger.settle(receipt, 10, Decimal("0"))
    ledger.assert_action_available()
    with pytest.raises(BudgetExceeded) as caught:
        reserve(ledger, "no-tokens", "jev")
    assert caught.value.code == "model_token_budget_exhausted"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"active_seconds": 1800}, "active_time_budget_exhausted"),
        ({"state": "cancelled"}, "task_not_active"),
        ({"expire": True}, "task_lease_expired"),
        ({"cost": "0.01"}, "cost_budget_exhausted"),
    ],
)
def test_overall_hard_limits_still_block_conditional_actions(database, change, code):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    receipt = reserve(ledger, "known")
    ledger.settle(receipt, 10, Decimal(".01"))
    with database.connect() as conn:
        if "active_seconds" in change:
            conn.execute(
                "UPDATE app.tasks SET active_seconds=%s WHERE id=%s",
                (change["active_seconds"], task["id"]),
            )
        elif "state" in change:
            conn.execute("UPDATE app.tasks SET state=%s WHERE id=%s", (change["state"], task["id"]))
        elif "expire" in change:
            conn.execute(
                "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s",
                (task["id"],),
            )
        else:
            conn.execute(
                "UPDATE app.tasks SET budget=budget||%s WHERE id=%s",
                (Jsonb({"max_cost": change["cost"]}), task["id"]),
            )
    with pytest.raises(BudgetExceeded) as caught:
        ledger.assert_action_available()
    assert caught.value.code == code


def test_legacy_rows_are_not_classified_from_provider_and_keep_original_call_semantics(database):
    project = database.add_project("fixture/legacy-budget")
    task = database.create_task(project["id"], "a" * 40, {}, {"max_calls": 1})
    ledger = UsageLedger(database, task["id"])
    receipt = ledger.reserve("legacy", 1, "jev", 10, Decimal("0"))
    ledger.settle(receipt, 0, Decimal("0"))
    saved = rows(database, task)[0]
    assert saved["model_family"] is None and saved["purpose"] is None
    assert saved["dispatch_state"] is None
    with pytest.raises(BudgetExceeded):
        ledger.assert_available()


def test_same_reservation_key_cannot_change_classification_or_node(database):
    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    original = reserve(ledger, "stable", node_id="one")
    assert reserve(ledger, "stable", node_id="one") == original
    for metadata in [{"node_id": "two"}, {"exception_reason": "new_evidence"}]:
        with pytest.raises(ValueError):
            reserve(ledger, "stable", **metadata)
    with pytest.raises(ValueError):
        reserve(ledger, "stable", "jev", node_id="one")


def test_migration_is_idempotent_and_does_not_reclassify_historical_usage(database):
    from importlib.resources import files

    project = database.add_project("fixture/legacy-migration")
    task = database.create_task(project["id"], "a" * 40, {}, {})
    ledger = UsageLedger(database, task["id"])
    receipt = ledger.reserve("old", 1, "primary", 10, Decimal(".1"))
    with database.connect() as conn:
        before = conn.execute("SELECT * FROM app.usage_records WHERE id=%s", (receipt,)).fetchone()
        source = files("decagent.storage").joinpath("013_model_request_budgets.sql").read_text()
        conn.execute(source)
        conn.execute(source)
        after = conn.execute("SELECT * FROM app.usage_records WHERE id=%s", (receipt,)).fetchone()
    assert after == before


@pytest.mark.parametrize("outcome", ["success", "invalid", "unknown", "not_sent", "fence"])
def test_primary_wrapper_accounts_for_classification_dispatch_and_invalid_responses(
    database, outcome
):
    from types import SimpleNamespace

    from decagent.models.primary import (
        ModelUsage,
        PrimaryRequestNotDispatched,
        PrimaryResponseRejected,
    )
    from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice

    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    calls = []

    class Model:
        def respond(self, messages, *, stream=False):
            if outcome == "not_sent":
                raise PrimaryRequestNotDispatched(reason="input_invalid")
            calls.append(True)
            if outcome == "invalid":
                raise PrimaryResponseRejected(reason="tool_schema", usage=ModelUsage(4, 2, 6))
            if outcome == "unknown":
                raise RuntimeError("Injected transport loss")
            return SimpleNamespace(usage=ModelUsage(4, 2, 6))

    def fence():
        if outcome == "fence":
            raise ValueError("Injected lease loss before provider")

    primary = BudgetedPrimary(
        Model(),
        ledger,
        price=TokenPrice("fixture", Decimal(1), Decimal(2)),
        purpose="format_repair",
        plan_revision=2,
        node_id="repair",
        before_dispatch=fence,
    )
    if outcome == "success":
        primary.respond([])
    else:
        with pytest.raises((ValueError, RuntimeError)):
            primary.respond([])
    saved = rows(database, task)[0]
    assert saved["model_family"] == "primary" and saved["purpose"] == "format_repair"
    assert saved["plan_revision"] == 2 and saved["node_ids"] == ["repair"]
    if outcome in {"not_sent", "fence"}:
        assert not calls and saved["dispatch_state"] == "not_sent"
        assert saved["actual_cost"] == 0 and saved["actual_tokens"] == 0
    elif outcome == "unknown":
        assert len(calls) == 1 and saved["dispatch_state"] == "unknown"
        assert saved["status"] == "unknown" and saved["actual_cost"] is None
    else:
        assert len(calls) == 1 and saved["dispatch_state"] == "responded"
        assert saved["actual_tokens"] == 6 and saved["actual_cost"] > 0
        assert saved["error_code"] == ("tool_schema" if outcome == "invalid" else None)


def test_jev_wrapper_classifies_each_retry_and_counts_questions_separately(database):
    import httpx

    from decagent.decisions import DecisionContext, JevAdapter, build_request
    from decagent.runtime.model_calls import LedgerAttemptBudget

    task = conditional_task(database)
    ledger = UsageLedger(database, task["id"])
    calls = []

    def response(request):
        calls.append(True)
        return httpx.Response(
            529 if len(calls) == 1 else 200,
            json={
                "model": "jev-1.0.0",
                "usage": {"input_tokens": 4, "output_tokens": 2, "cost": 0.001},
                "answers": {"memory_read_needed": {"type": "noul", "noul": 0.9}},
            },
        )

    request = build_request(
        ["memory_read_needed"],
        {"task_summary": "task", "current_version": "1"},
        DecisionContext(str(task["id"]), "project", "1", "1", "1", "1"),
    )
    budget = LedgerAttemptBudget(
        ledger,
        reservation_usd=0.01,
        provider="openrouter",
        purpose="memory_read",
        plan_revision=2,
        node_ids=["memory"],
    )
    with JevAdapter(
        api_key="fixture",
        provider="openrouter",
        model="jev-latest",
        budget=budget,
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(response)),
    ) as adapter:
        result = adapter.evaluate(request)
    assert result.available and len(calls) == 2
    saved = rows(database, task)
    assert len(saved) == 2
    assert [row["purpose"] for row in saved] == ["memory_read", "provider_retry"]
    assert all(row["provider"] == "openrouter" and row["model_family"] == "jev" for row in saved)
    assert all(row["question_count"] == 1 and row["node_ids"] == ["memory"] for row in saved)
    assert all(row["dispatch_state"] == "responded" for row in saved)

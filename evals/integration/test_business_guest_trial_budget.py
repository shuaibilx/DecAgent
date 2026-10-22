"""Atomic guest trial budget reservations in isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from threading import Event
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from psycopg.errors import CheckViolation

from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.trial_budget import (
    BudgetPolicyError,
    FxSnapshot,
    TrialBudgetExceeded,
    TrialBudgetLedger,
    TrialBudgetPolicy,
    make_task_usage_ledger,
)
from decagent.config import Settings
from decagent.decisions import DecisionContext, JevAdapter, build_request
from decagent.models.primary import PrimaryProviderFailure
from decagent.runtime.model_calls import BudgetedPrimary, LedgerAttemptBudget, TokenPrice
from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger

pytestmark = pytest.mark.integration
FIXED_NOW = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
FX = FxSnapshot(
    source_date=date(2026, 10, 20),
    cny_per_usd=Decimal("6.7200"),
    source_url="https://www.chinamoney.com.cn/english/",
)


def _policy(**overrides):
    values = {
        "daily_cost_usd": Decimal("5.00"),
        "daily_qwen_tokens": 5_000_000,
        "guest_daily_tasks": 30,
        "guest_daily_qwen_tokens": 500_000,
        "fx_snapshot": FX,
    }
    values.update(overrides)
    return TrialBudgetPolicy(**values)


def _workspace(database, slug):
    workspace_id = uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.workspaces(id,slug,kind) VALUES(%s,%s,'guest')",
            (workspace_id, slug),
        )
    return workspace_id


def _ledger(database, **policy):
    return TrialBudgetLedger(database, _policy(**policy), clock=lambda: FIXED_NOW)


def _guest_business_task(database, workspace_id, *, budget=None, graph_version="business-2"):
    registry = PluginRegistry(database)
    scope = registry.register(builtin_manifest())
    spec = registry.get(scope["id"])
    return database.create_business_task(
        scope["id"],
        {
            "instruction": "Read the ticket directory",
            "plugin_digest": spec.digest,
            "provenance": {"owner_id": "guest-test", "authenticated": True},
        },
        budget,
        graph_version=graph_version,
        workspace_id=workspace_id,
    )


def _reserve(ledger, workspace_id, *, request_key, cost=Decimal("3"), currency="USD", tokens=0):
    return ledger.reserve_request(
        workspace_id=workspace_id,
        task_id=uuid4(),
        request_key=request_key,
        provider="qwen" if tokens else "openrouter",
        model="fixture-model",
        cost_currency=currency,
        reserved_cost=cost,
        reserved_qwen_tokens=tokens,
        price_basis_id="fixture-price-v1",
    )


def test_concurrent_reservations_cannot_exceed_global_daily_cost(database):
    workspaces = [_workspace(database, f"budget-cost-{index}") for index in range(2)]
    ledger = _ledger(database)

    def run(index):
        try:
            _reserve(ledger, workspaces[index], request_key=f"cost-{index}")
            return True
        except TrialBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))

    assert results.count(True) == 1
    with database.connect() as conn:
        row = conn.execute(
            "SELECT reserved_cost_usd FROM business.guest_trial_budget_days "
            "WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        assert row["reserved_cost_usd"] == Decimal("3.000000")


def test_concurrent_qwen_usage_cannot_exceed_guest_daily_tokens(database):
    workspace_id = _workspace(database, "budget-guest-tokens")
    ledger = _ledger(database)

    def run(index):
        try:
            _reserve(
                ledger,
                workspace_id,
                request_key=f"tokens-{index}",
                cost=Decimal("1"),
                currency="CNY",
                tokens=300_000,
            )
            return True
        except TrialBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))

    assert results.count(True) == 1
    with database.connect() as conn:
        row = conn.execute(
            "SELECT qwen_tokens_reserved FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=%s AND workspace_id=%s",
            (FIXED_NOW.date(), workspace_id),
        ).fetchone()
        assert row["qwen_tokens_reserved"] == 300_000


def test_concurrent_qwen_usage_cannot_exceed_global_daily_tokens(database):
    workspaces = [_workspace(database, f"budget-global-tokens-{index}") for index in range(2)]
    ledger = _ledger(database, guest_daily_qwen_tokens=5_000_000)

    def run(index):
        try:
            _reserve(
                ledger,
                workspaces[index],
                request_key=f"global-tokens-{index}",
                cost=Decimal("1"),
                currency="CNY",
                tokens=3_000_000,
            )
            return True
        except TrialBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))

    assert results.count(True) == 1
    with database.connect() as conn:
        row = conn.execute(
            "SELECT qwen_tokens_reserved FROM business.guest_trial_budget_days "
            "WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        assert row["qwen_tokens_reserved"] == 3_000_000


def test_concurrent_task_admission_stops_at_per_guest_daily_limit(database):
    workspace_id = _workspace(database, "budget-guest-tasks")
    ledger = _ledger(database)

    def run(index):
        try:
            ledger.admit_task(workspace_id=workspace_id, task_id=uuid4())
            return True
        except TrialBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(run, range(40)))

    assert results.count(True) == 30
    assert results.count(False) == 10
    with database.connect() as conn:
        row = conn.execute(
            "SELECT task_count FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=%s AND workspace_id=%s",
            (FIXED_NOW.date(), workspace_id),
        ).fetchone()
        assert row["task_count"] == 30


def test_task_admission_is_idempotent(database):
    workspace_id = _workspace(database, "budget-task-idempotency")
    ledger = _ledger(database)
    task_id = uuid4()

    assert ledger.admit_task(workspace_id=workspace_id, task_id=task_id) is True
    assert ledger.admit_task(workspace_id=workspace_id, task_id=task_id) is False


def test_task_admission_idempotency_survives_beijing_midnight(database):
    workspace_id = _workspace(database, "budget-task-midnight-idempotency")
    task_id = uuid4()
    assert _ledger(database).admit_task(workspace_id=workspace_id, task_id=task_id) is True
    next_day = TrialBudgetLedger(
        database,
        _policy(),
        clock=lambda: datetime(2026, 10, 23, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert next_day.admit_task(workspace_id=workspace_id, task_id=task_id) is False
    with database.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (date(2026, 10, 23),),
        ).fetchone() is None


def test_daily_budget_limits_cannot_change_after_the_bucket_is_created(database):
    workspace_id = _workspace(database, "budget-limit-drift")
    _ledger(database).admit_task(workspace_id=workspace_id, task_id=uuid4())
    changed = _ledger(database, daily_cost_usd=Decimal("6.00"))

    with pytest.raises(BudgetPolicyError, match="cannot change"):
        changed.admit_task(workspace_id=workspace_id, task_id=uuid4())


def test_request_reservation_is_idempotent(database):
    workspace_id = _workspace(database, "budget-request-idempotency")
    ledger = _ledger(database)
    task_id = uuid4()
    arguments = {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "request_key": "stable-request-key",
        "provider": "qwen",
        "model": "fixture-model",
        "cost_currency": "CNY",
        "reserved_cost": Decimal("1"),
        "reserved_qwen_tokens": 100,
        "price_basis_id": "fixture-price-v1",
    }

    first = ledger.reserve_request(**arguments)
    second = ledger.reserve_request(**arguments)

    assert first["id"] == second["id"]
    with pytest.raises(BudgetPolicyError, match="different reservation"):
        ledger.reserve_request(**{**arguments, "reserved_qwen_tokens": 101})


def test_request_reservation_idempotency_survives_beijing_midnight(database):
    workspace_id = _workspace(database, "budget-request-midnight-idempotency")
    task_id = uuid4()
    arguments = {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "request_key": "midnight-stable-request",
        "provider": "qwen",
        "model": "fixture-model",
        "cost_currency": "CNY",
        "reserved_cost": Decimal("1"),
        "reserved_qwen_tokens": 100,
        "price_basis_id": "fixture-price-v1",
    }
    original = _ledger(database).reserve_request(**arguments)
    next_day = TrialBudgetLedger(
        database,
        _policy(),
        clock=lambda: datetime(2026, 10, 23, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    repeated = next_day.reserve_request(**arguments)

    assert repeated["id"] == original["id"]
    assert repeated["budget_date"] == date(2026, 10, 22)
    with database.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (date(2026, 10, 23),),
        ).fetchone() is None


def test_daily_fx_snapshot_is_immutable_and_cny_conversion_is_conservative(database):
    workspace_id = _workspace(database, "budget-fx")
    first = _ledger(database)
    receipt = _reserve(
        first,
        workspace_id,
        request_key="fx-first",
        cost=Decimal("2"),
        currency="CNY",
    )
    changed = _ledger(
        database,
        fx_snapshot=FxSnapshot(
            source_date=date(2026, 10, 22),
            cny_per_usd=Decimal("6.80"),
            source_url="https://www.chinamoney.com.cn/english/",
        ),
    )
    later = _reserve(
        changed,
        workspace_id,
        request_key="fx-second",
        cost=Decimal("2"),
        currency="CNY",
    )

    assert receipt["reserved_cost_usd"] == Decimal("0.331126")
    assert later["reserved_cost_usd"] == Decimal("0.331126")
    with database.connect() as conn:
        day = conn.execute(
            "SELECT fx_source_date,fx_cny_per_usd,fx_effective_cny_per_usd "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        assert day == {
            "fx_source_date": date(2026, 10, 20),
            "fx_cny_per_usd": Decimal("6.720000"),
            "fx_effective_cny_per_usd": Decimal("6.04"),
        }


def test_budget_day_rejects_partial_fx_snapshot(database):
    with pytest.raises(CheckViolation):
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO business.guest_trial_budget_days "
                "(budget_date,daily_cost_limit_usd,daily_qwen_token_limit,fx_source_date,"
                "fx_source_url,fx_cny_per_usd,fx_effective_cny_per_usd) "
                "VALUES(%s,5,5000000,%s,%s,NULL,NULL)",
                (date(2099, 1, 1), date(2098, 12, 31), "https://www.chinamoney.com.cn/"),
            )


def test_workspace_usage_foreign_key_has_lookup_index(database):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='business' "
            "AND tablename='guest_trial_workspace_usage'"
        ).fetchall()

    assert any("(workspace_id, budget_date" in row["indexdef"] for row in rows)


def test_cny_reservation_fails_closed_without_a_recent_verified_fx_rate(database):
    workspace_id = _workspace(database, "budget-missing-fx")
    ledger = _ledger(
        database,
        fx_snapshot=FxSnapshot(
            source_date=date(2026, 10, 14),
            cny_per_usd=Decimal("6.7200"),
            source_url="https://www.chinamoney.com.cn/english/",
        ),
    )

    with pytest.raises(BudgetPolicyError):
        _reserve(
            ledger,
            workspace_id,
            request_key="fx-stale",
            cost=Decimal("1"),
            currency="CNY",
            tokens=10,
        )


def test_unknown_price_and_non_guest_workspace_are_rejected(database):
    workspace_id = _workspace(database, "budget-validation")
    ledger = _ledger(database)
    with pytest.raises(BudgetPolicyError):
        ledger.reserve_request(
            workspace_id=workspace_id,
            task_id=uuid4(),
            request_key="missing-price",
            provider="qwen",
            model="fixture-model",
            cost_currency="CNY",
            reserved_cost=None,
            reserved_qwen_tokens=1,
            price_basis_id="fixture-price-v1",
        )
    with pytest.raises(BudgetPolicyError):
        ledger.admit_task(workspace_id=uuid4(), task_id=uuid4())


def test_each_provider_retry_gets_its_own_reservation(database):
    workspace_id = _workspace(database, "budget-retry-attempts")
    ledger = _ledger(database)
    task_id = uuid4()
    common = {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "request_key": "same-logical-model-call",
        "provider": "openrouter",
        "model": "fixture-model",
        "cost_currency": "USD",
        "reserved_cost": Decimal("0.10"),
        "reserved_qwen_tokens": 0,
        "price_basis_id": "fixture-price-v1",
    }

    first = ledger.reserve_request(**common, attempt=1)
    retry = ledger.reserve_request(**common, attempt=2)

    assert first["id"] != retry["id"]
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT attempt,status FROM business.guest_trial_usage_reservations "
            "WHERE workspace_id=%s AND request_key=%s ORDER BY attempt",
            (workspace_id, common["request_key"]),
        ).fetchall()
    assert rows == [
        {"attempt": 1, "status": "reserved"},
        {"attempt": 2, "status": "reserved"},
    ]


def test_settlement_charges_gross_fee_and_keeps_voucher_payment_separate(database):
    workspace_id = _workspace(database, "budget-settlement-voucher")
    ledger = _ledger(database)
    reservation = ledger.reserve_request(
        workspace_id=workspace_id,
        task_id=uuid4(),
        request_key="settlement-voucher",
        attempt=1,
        provider="qwen",
        model="fixture-model",
        cost_currency="CNY",
        reserved_cost=Decimal("2.00"),
        reserved_qwen_tokens=10_000,
        price_basis_id="qwen-tariff-v1",
    )
    ledger.mark_dispatched(reservation["id"])

    settled = ledger.settle_request(
        reservation["id"],
        response_state="responded",
        actual_input_tokens=700,
        actual_output_tokens=300,
        actual_cost_native=Decimal("1.00"),
        actual_cost_basis="provider_billed",
        voucher_credit_native=Decimal("1.00"),
        cash_paid_native=Decimal("0.00"),
        billing_evidence_source="invoice",
        server_request_id="server-request-1",
        latency_ms=240,
    )

    assert settled["status"] == "settled"
    assert settled["actual_cost_native"] == Decimal("1.000000000000")
    assert settled["actual_cost_usd"] == Decimal("0.165563")
    assert settled["voucher_credit_native"] == Decimal("1.000000000000")
    assert settled["cash_paid_native"] == Decimal("0E-12")
    with database.connect() as conn:
        day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        guest = conn.execute(
            "SELECT qwen_tokens_reserved FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=%s AND workspace_id=%s",
            (FIXED_NOW.date(), workspace_id),
        ).fetchone()
    assert day == {"reserved_cost_usd": Decimal("0.165563"), "qwen_tokens_reserved": 1000}
    assert guest == {"qwen_tokens_reserved": 1000}


def test_unknown_dispatched_request_keeps_full_reservation_after_restart(database):
    workspace_id = _workspace(database, "budget-unknown-restart")
    ledger = _ledger(database)
    reservation = ledger.reserve_request(
        workspace_id=workspace_id,
        task_id=uuid4(),
        request_key="unknown-restart",
        attempt=1,
        provider="qwen",
        model="fixture-model",
        cost_currency="CNY",
        reserved_cost=Decimal("2.00"),
        reserved_qwen_tokens=10_000,
        price_basis_id="qwen-tariff-v1",
    )
    ledger.mark_dispatched(reservation["id"])

    unknown = ledger.settle_request(
        reservation["id"],
        response_state="unknown",
        error_category="timeout",
        latency_ms=30_000,
    )
    restarted = TrialBudgetLedger(
        database,
        _policy(),
        clock=lambda: datetime(2026, 10, 23, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert unknown["status"] == "unknown"
    with database.connect() as conn:
        saved = conn.execute(
            "SELECT status,reserved_cost_usd,reserved_qwen_tokens,error_category "
            "FROM business.guest_trial_usage_reservations WHERE id=%s",
            (reservation["id"],),
        ).fetchone()
        old_day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        restarted.admit_task(workspace_id=workspace_id, task_id=uuid4())
        new_day = conn.execute(
            "SELECT budget_date FROM business.guest_trial_budget_days "
            "WHERE budget_date=%s",
            (date(2026, 10, 23),),
        ).fetchone()
    assert saved == {
        "status": "unknown",
        "reserved_cost_usd": Decimal("0.331126"),
        "reserved_qwen_tokens": 10_000,
        "error_category": "timeout",
    }
    assert old_day == {"reserved_cost_usd": Decimal("0.331126"), "qwen_tokens_reserved": 10_000}
    assert new_day is not None


def test_not_sent_attempt_requires_local_proof_and_releases_reservation(database):
    workspace_id = _workspace(database, "budget-not-sent")
    ledger = _ledger(database)
    reservation = ledger.reserve_request(
        workspace_id=workspace_id,
        task_id=uuid4(),
        request_key="not-sent",
        attempt=1,
        provider="qwen",
        model="fixture-model",
        cost_currency="CNY",
        reserved_cost=Decimal("2.00"),
        reserved_qwen_tokens=10_000,
        price_basis_id="qwen-tariff-v1",
    )
    ledger.mark_dispatched(reservation["id"])

    with pytest.raises(BudgetPolicyError, match="proof"):
        ledger.mark_not_sent(
            reservation["id"], reason="before_dispatch_guard", after_local_preflight=False
        )
    released = ledger.mark_not_sent(
        reservation["id"], reason="before_dispatch_guard", after_local_preflight=True
    )

    assert released["status"] == "not_sent"
    with database.connect() as conn:
        day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        guest = conn.execute(
            "SELECT qwen_tokens_reserved FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=%s AND workspace_id=%s",
            (FIXED_NOW.date(), workspace_id),
        ).fetchone()
    assert day == {"reserved_cost_usd": Decimal("0.000000"), "qwen_tokens_reserved": 0}
    assert guest == {"qwen_tokens_reserved": 0}


@pytest.mark.parametrize("provider", ["qwen", "dashscope"], ids=["qwen", "dashscope"])
def test_guest_model_request_reserves_task_and_trial_budget_in_one_transaction(
    database, provider
):
    workspace_id = _workspace(database, "budget-task-atomic")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 5, "max_tokens": 20_000, "max_cost": "1.00"},
    )
    usage = UsageLedger(
        database,
        task["id"],
        trial_budget=_ledger(database),
        guest_workspace_id=workspace_id,
    )

    receipt_id = usage.reserve_model(
        "task-atomic-qwen",
        1,
        provider,
        10_000,
        Decimal("2.00"),
        model_family="primary",
        purpose="plan",
        request_role="planning",
        requested_model="qwen3.8-flash",
        endpoint="https://maas.qianwenaiapi.com/compatible-mode/v1",
        requested_output_tokens=1024,
        trial_cost_currency="CNY",
        trial_cost_native=Decimal("2.00"),
        trial_price_basis_id="qwen-price-v1",
    )

    with database.connect() as conn:
        task_row = conn.execute(
            "SELECT id,reserved_cost FROM app.usage_records WHERE id=%s", (receipt_id,)
        ).fetchone()
        trial_row = conn.execute(
            "SELECT usage_record_id,reserved_cost_usd,reserved_qwen_tokens "
            "FROM business.guest_trial_usage_reservations WHERE usage_record_id=%s",
            (receipt_id,),
        ).fetchone()
    assert task_row == {"id": receipt_id, "reserved_cost": Decimal("0.331126")}
    assert trial_row == {
        "usage_record_id": receipt_id,
        "reserved_cost_usd": Decimal("0.331126"),
        "reserved_qwen_tokens": 10_000,
    }


def test_guest_trial_reservation_rolls_back_when_task_budget_rejects(database):
    workspace_id = _workspace(database, "budget-task-rollback")
    task = _guest_business_task(database, workspace_id, budget={"max_calls": 0})
    usage = UsageLedger(
        database,
        task["id"],
        trial_budget=_ledger(database),
        guest_workspace_id=workspace_id,
    )

    with pytest.raises(BudgetExceeded, match="Call budget"):
        usage.reserve_model(
            "task-budget-reject",
            1,
            "qwen",
            10_000,
            Decimal("2.00"),
            model_family="primary",
            purpose="plan",
            request_role="planning",
            requested_model="qwen3.8-flash",
            endpoint="https://maas.qianwenaiapi.com/compatible-mode/v1",
            requested_output_tokens=1024,
            trial_cost_currency="CNY",
            trial_cost_native=Decimal("2.00"),
            trial_price_basis_id="qwen-price-v1",
        )

    with database.connect() as conn:
        trial_rows = conn.execute(
            "SELECT count(*) AS n FROM business.guest_trial_usage_reservations "
            "WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()["n"]
        budget_days = conn.execute(
            "SELECT count(*) AS n FROM business.guest_trial_budget_days "
            "WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()["n"]
    assert trial_rows == 0
    assert budget_days == 0


@pytest.mark.parametrize("provider", ["qwen", "dashscope"], ids=["qwen", "dashscope"])
def test_guest_model_settlement_commits_actual_native_cost_and_usage_atomically(
    database, provider
):
    workspace_id = _workspace(database, "budget-task-settlement")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 5, "max_tokens": 20_000, "max_cost": "1.00"},
    )
    usage = UsageLedger(
        database,
        task["id"],
        trial_budget=_ledger(database),
        guest_workspace_id=workspace_id,
    )
    receipt_id = usage.reserve_model(
        "task-settlement-qwen",
        1,
        provider,
        10_000,
        Decimal("2.00"),
        model_family="primary",
        purpose="plan",
        request_role="planning",
        requested_model="qwen3.8-flash",
        endpoint="https://maas.qianwenaiapi.com/compatible-mode/v1",
        requested_output_tokens=1024,
        trial_cost_currency="CNY",
        trial_cost_native=Decimal("2.00"),
        trial_price_basis_id="qwen-price-v1",
    )

    usage.mark_dispatched(receipt_id)
    usage.settle_model(
        receipt_id,
        1000,
        Decimal("0.165563"),
        response_state="responded",
        actual_input_tokens=700,
        actual_output_tokens=300,
        trial_actual_cost_native=Decimal("1.00"),
        trial_actual_cost_basis="frozen_rate",
        server_request_id="qwen-req-1",
    )

    with database.connect() as conn:
        app_row = conn.execute(
            "SELECT status,actual_tokens,actual_cost FROM app.usage_records WHERE id=%s",
            (receipt_id,),
        ).fetchone()
        trial_row = conn.execute(
            "SELECT status,actual_input_tokens,actual_output_tokens,actual_cost_native,"
            "actual_cost_usd,reservation_exceeded,server_request_id "
            "FROM business.guest_trial_usage_reservations WHERE usage_record_id=%s",
            (receipt_id,),
        ).fetchone()
        day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()

    assert app_row == {
        "status": "settled",
        "actual_tokens": 1000,
        "actual_cost": Decimal("0.165563"),
    }
    assert trial_row == {
        "status": "settled",
        "actual_input_tokens": 700,
        "actual_output_tokens": 300,
        "actual_cost_native": Decimal("1.000000000000"),
        "actual_cost_usd": Decimal("0.165563"),
        "reservation_exceeded": False,
        "server_request_id": "qwen-req-1",
    }
    assert day == {
        "reserved_cost_usd": Decimal("0.165563"),
        "qwen_tokens_reserved": 1000,
    }


def test_guest_model_dispatch_factory_fails_closed_when_trial_is_disabled(database):
    workspace_id = _workspace(database, "budget-task-disabled")
    task = _guest_business_task(database, workspace_id)
    usage = make_task_usage_ledger(database, Settings(), task)

    with pytest.raises(BudgetExceeded) as caught:
        usage.reserve_model(
            "disabled-guest-qwen",
            1,
            "qwen",
            10_000,
            Decimal("2.00"),
            trial_cost_currency="CNY",
            trial_cost_native=Decimal("2.00"),
            trial_price_basis_id="qwen-price-v1",
        )

    assert caught.value.code == "guest_trial_disabled"


def test_guest_model_dispatch_factory_attaches_real_trial_ledger(database):
    workspace_id = _workspace(database, "budget-task-enabled")
    task = _guest_business_task(database, workspace_id)
    settings = Settings(
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
        guest_trial_fx_source_date=FX.source_date,
        guest_trial_fx_cny_per_usd=FX.cny_per_usd,
        guest_trial_fx_source_url=FX.source_url,
    )
    usage = make_task_usage_ledger(database, settings, task)

    receipt_id = usage.reserve_model(
        "enabled-guest-qwen",
        1,
        "qwen",
        10_000,
        Decimal("2.00"),
        trial_cost_currency="CNY",
        trial_cost_native=Decimal("2.00"),
        trial_price_basis_id="qwen-price-v1",
    )

    with database.connect() as conn:
        reservation = conn.execute(
            "SELECT usage_record_id,reserved_cost_usd FROM "
            "business.guest_trial_usage_reservations WHERE usage_record_id=%s",
            (receipt_id,),
        ).fetchone()
    assert reservation == {
        "usage_record_id": receipt_id,
        "reserved_cost_usd": Decimal("0.331126"),
    }


def test_guest_model_request_without_a_reliable_price_is_rejected(database):
    workspace_id = _workspace(database, "budget-task-unpriced")
    task = _guest_business_task(database, workspace_id)
    settings = Settings(
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
        guest_trial_fx_source_date=FX.source_date,
        guest_trial_fx_cny_per_usd=FX.cny_per_usd,
        guest_trial_fx_source_url=FX.source_url,
    )
    usage = make_task_usage_ledger(database, settings, task)

    with pytest.raises(BudgetExceeded) as caught:
        usage.reserve_model(
            "unpriced-guest-qwen",
            1,
            "qwen",
            10_000,
            Decimal("0.01"),
            model_family="primary",
            purpose="plan",
        )

    assert caught.value.code == "guest_trial_price_required"
    with database.connect() as conn:
        reservations = conn.execute(
            "SELECT count(*) AS n FROM business.guest_trial_usage_reservations "
            "WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()["n"]
    assert reservations == 0


def test_guest_model_timeout_stops_retry_and_keeps_unknown_reservation(database):
    workspace_id = _workspace(database, "budget-task-timeout")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 10, "max_tokens": 50_000, "max_cost": "5.00"},
        graph_version="business-3",
    )
    settings = Settings(
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
        guest_trial_fx_source_date=FX.source_date,
        guest_trial_fx_cny_per_usd=FX.cny_per_usd,
        guest_trial_fx_source_url=FX.source_url,
    )
    usage = make_task_usage_ledger(database, settings, task)
    claimed = database.claim(task["id"], "guest-budget-timeout", seconds=120)
    assert claimed["state"] == "running"

    class TimedOutModel:
        requested_model = "qwen3.8-flash"
        endpoint = "https://maas.qianwenaiapi.com/compatible-mode/v1"
        schema_bytes = 0
        calls = 0

        def respond(self, _messages, *, stream=False):
            self.calls += 1
            raise PrimaryProviderFailure(category="timeout")

    provider_model = TimedOutModel()
    model = BudgetedPrimary(
        provider_model,
        usage,
        price=TokenPrice("qwen-price-v1", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        purpose="plan",
        max_output_tokens=32,
        retry_sleep=lambda _delay: None,
    )

    with pytest.raises(BudgetExceeded) as caught:
        model.respond([{"role": "user", "content": "read only plan"}])
    assert caught.value.code == "unknown_model_cost"
    assert provider_model.calls == 1

    with database.connect() as conn:
        trial_rows = conn.execute(
            "SELECT attempt,status,reserved_cost_usd,reserved_qwen_tokens,error_category "
            "FROM business.guest_trial_usage_reservations WHERE workspace_id=%s "
            "ORDER BY attempt",
            (workspace_id,),
        ).fetchall()
        day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
        app_rows = conn.execute(
            "SELECT status,dispatch_state FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchall()

    assert len(trial_rows) == 1
    assert [row["attempt"] for row in trial_rows] == [1]
    assert all(row["status"] == "unknown" for row in trial_rows)
    assert all(row["error_category"] == "timeout" for row in trial_rows)
    assert all(row["reserved_cost_usd"] > 0 for row in trial_rows)
    assert all(row["reserved_qwen_tokens"] > 0 for row in trial_rows)
    assert day["reserved_cost_usd"] == sum(
        row["reserved_cost_usd"] for row in trial_rows
    )
    assert day["qwen_tokens_reserved"] == sum(
        row["reserved_qwen_tokens"] for row in trial_rows
    )
    assert app_rows == [{"status": "unknown", "dispatch_state": "unknown"}]


@pytest.mark.parametrize(
    ("category", "status_code", "trial_category"),
    [
        ("connection", None, "network_error"),
        ("permanent_http", 401, "provider_error"),
        ("transient_http", 408, "timeout"),
        ("transient_http", 429, "rate_limited"),
        ("transient_http", 503, "provider_error"),
    ],
)
def test_guest_provider_failures_commit_unknown_settlement(
    database, category, status_code, trial_category
):
    slug_category = category.replace("_", "-")
    workspace_id = _workspace(database, f"bp-{slug_category}-{status_code or 'none'}")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 10, "max_tokens": 50_000, "max_cost": "5.00"},
        graph_version="business-3",
    )
    settings = Settings(
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
        guest_trial_fx_source_date=FX.source_date,
        guest_trial_fx_cny_per_usd=FX.cny_per_usd,
        guest_trial_fx_source_url=FX.source_url,
    )
    usage = make_task_usage_ledger(database, settings, task)
    claimed = database.claim(task["id"], f"bp-worker-{slug_category}", seconds=120)
    assert claimed["state"] == "running"

    class FailedModel:
        requested_model = "qwen3.8-flash"
        endpoint = "https://maas.qianwenaiapi.com/compatible-mode/v1"
        schema_bytes = 0

        def respond(self, _messages, *, stream=False):
            raise PrimaryProviderFailure(category=category, status_code=status_code)

    model = BudgetedPrimary(
        FailedModel(),
        usage,
        price=TokenPrice("qwen-price-v1", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        purpose="plan",
        max_output_tokens=32,
        max_same_provider_attempts=1,
    )

    with pytest.raises(PrimaryProviderFailure):
        model.respond([{"role": "user", "content": "read only plan"}])

    with database.connect() as conn:
        trial_row = conn.execute(
            "SELECT status,reserved_cost_usd,reserved_qwen_tokens,error_category "
            "FROM business.guest_trial_usage_reservations WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()
        usage_row = conn.execute(
            "SELECT status,dispatch_state,error_category,provider_status "
            "FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchone()

    assert trial_row["status"] == "unknown"
    assert trial_row["error_category"] == trial_category
    assert trial_row["reserved_cost_usd"] > 0
    assert trial_row["reserved_qwen_tokens"] > 0
    assert usage_row == {
        "status": "unknown",
        "dispatch_state": "unknown",
        "error_category": category,
        "provider_status": status_code,
    }


def test_guest_unclassified_transport_failure_commits_unknown_settlement(database):
    workspace_id = _workspace(database, "budget-transport-error")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 10, "max_tokens": 50_000, "max_cost": "5.00"},
        graph_version="business-3",
    )
    settings = Settings(
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
        guest_trial_fx_source_date=FX.source_date,
        guest_trial_fx_cny_per_usd=FX.cny_per_usd,
        guest_trial_fx_source_url=FX.source_url,
    )
    usage = make_task_usage_ledger(database, settings, task)
    claimed = database.claim(task["id"], "transport-worker", seconds=120)
    assert claimed["state"] == "running"

    class BrokenTransport:
        requested_model = "qwen3.8-flash"
        endpoint = "https://maas.qianwenaiapi.com/compatible-mode/v1"
        schema_bytes = 0

        def respond(self, _messages, *, stream=False):
            raise RuntimeError("sanitized transport failure")

    model = BudgetedPrimary(
        BrokenTransport(),
        usage,
        price=TokenPrice("qwen-price-v1", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        purpose="plan",
        max_output_tokens=32,
        max_same_provider_attempts=1,
    )

    with pytest.raises(RuntimeError, match="sanitized transport failure"):
        model.respond([{"role": "user", "content": "read only plan"}])

    with database.connect() as conn:
        trial_row = conn.execute(
            "SELECT status,reserved_cost_usd,reserved_qwen_tokens,error_category "
            "FROM business.guest_trial_usage_reservations WHERE workspace_id=%s",
            (workspace_id,),
        ).fetchone()
        usage_row = conn.execute(
            "SELECT status,dispatch_state,error_category "
            "FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchone()

    assert trial_row["status"] == "unknown"
    assert trial_row["error_category"] == "unknown_error"
    assert trial_row["reserved_cost_usd"] > 0
    assert trial_row["reserved_qwen_tokens"] > 0
    assert usage_row == {
        "status": "unknown",
        "dispatch_state": "unknown",
        "error_category": "transport_error",
    }


def test_free_quota_still_consumes_guest_qwen_token_budget(database):
    workspace_id = _workspace(database, "budget-free-qwen")
    ledger = _ledger(database)
    reservation = ledger.reserve_request(
        workspace_id=workspace_id,
        task_id=uuid4(),
        request_key="free-quota-qwen",
        provider="qwen",
        model="fixture-free-model",
        cost_currency="CNY",
        reserved_cost=Decimal("0"),
        reserved_qwen_tokens=400,
        price_basis_id="free-quota-v1",
    )
    ledger.mark_dispatched(reservation["id"])
    settled = ledger.settle_request(
        reservation["id"],
        response_state="responded",
        actual_input_tokens=300,
        actual_output_tokens=100,
        actual_cost_native=Decimal("0"),
        actual_cost_basis="frozen_rate",
    )

    assert settled["status"] == "settled"
    with database.connect() as conn:
        day = conn.execute(
            "SELECT reserved_cost_usd,qwen_tokens_reserved "
            "FROM business.guest_trial_budget_days WHERE budget_date=%s",
            (FIXED_NOW.date(),),
        ).fetchone()
    assert day == {"reserved_cost_usd": Decimal("0.000000"), "qwen_tokens_reserved": 400}


def test_guest_jev_request_is_rejected_before_dispatch_without_fee_bound(database):
    workspace_id = _workspace(database, "budget-guest-jev")
    task = _guest_business_task(
        database,
        workspace_id,
        budget={"max_calls": 10, "max_tokens": 50_000, "max_cost": "5.00"},
        graph_version="business-3",
    )
    settings = Settings(guest_trial_enabled=True, guest_rate_limit_hmac_key=b"B" * 32)
    usage_ledger = make_task_usage_ledger(database, settings, task)
    claimed = database.claim(task["id"], "guest-budget-jev", seconds=120)
    assert claimed["state"] == "running"
    request = build_request(
        ["memory_read_needed"],
        {"task_summary": "route a service task", "current_version": "3"},
        DecisionContext(str(task["id"]), "guest-scope", "1", "1", "1", "1"),
    )

    provider_requests = []

    def respond(request):
        provider_requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "usage": {"input_tokens": 10, "output_tokens": 4, "cost": 0.0001},
                "answers": {"memory_read_needed": {"type": "noul", "noul": 0.9}},
            },
        )

    budget = LedgerAttemptBudget(
        usage_ledger,
        reservation_usd=Decimal("0.01"),
        provider="openrouter",
        purpose="memory_read",
    )
    with JevAdapter(
        api_key="fixture",
        provider="openrouter",
        model="jev-latest",
        budget=budget,
        cache_ttl=0,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as adapter:
        result = adapter.evaluate(request)

    assert not result.available
    assert result.error == "budget_exceeded"
    assert provider_requests == []
    with database.connect() as conn:
        app_row = conn.execute(
            "SELECT count(*) AS total FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchone()
        trial_row = conn.execute(
            "SELECT count(*) AS total FROM business.guest_trial_usage_reservations "
            "WHERE task_id=%s",
            (task["id"],),
        ).fetchone()

    assert app_row == {"total": 0}
    assert trial_row == {"total": 0}


def test_workspace_deactivation_serializes_before_guest_admission(database):
    workspace_id = _workspace(database, "budget-workspace-deactivation-race")
    ledger = _ledger(database)
    started = Event()

    with database.connect() as deactivation:
        deactivation.execute(
            "UPDATE business.workspaces SET active=false WHERE id=%s", (workspace_id,)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            def admit():
                started.set()
                return ledger.admit_task(workspace_id=workspace_id, task_id=uuid4())

            pending = pool.submit(admit)
            assert started.wait(timeout=1)
            sleep(0.1)
            assert not pending.done()
            deactivation.commit()
            with pytest.raises(BudgetPolicyError, match="active guest workspace"):
                pending.result(timeout=5)

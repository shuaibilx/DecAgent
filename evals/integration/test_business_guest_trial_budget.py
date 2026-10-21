"""Atomic guest trial budget reservations in isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from threading import Event
from time import sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from psycopg.errors import CheckViolation

from decagent.business.trial_budget import (
    BudgetPolicyError,
    FxSnapshot,
    TrialBudgetExceeded,
    TrialBudgetLedger,
    TrialBudgetPolicy,
)

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

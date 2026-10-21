from datetime import date
from decimal import Decimal

import pytest

from decagent.business.trial_budget import (
    BudgetPolicyError,
    FxSnapshot,
    TrialBudgetPolicy,
)
from decagent.config import Settings


def test_cny_cost_uses_frozen_conservative_decimal_rate():
    snapshot = FxSnapshot(
        source_date=date(2026, 10, 20),
        cny_per_usd=Decimal("6.7200"),
        source_url="https://www.chinamoney.com.cn/english/",
    )

    assert snapshot.effective_rate(as_of=date(2026, 10, 22)) == Decimal("6.04")
    assert snapshot.cny_to_usd(Decimal("2"), as_of=date(2026, 10, 22)) == Decimal("0.331126")


def test_trial_budget_policy_matches_approved_defaults_and_settings():
    policy = TrialBudgetPolicy.from_settings(Settings())

    assert policy.daily_cost_usd == Decimal("5.000000")
    assert policy.daily_qwen_tokens == 5_000_000
    assert policy.guest_daily_tasks == 30
    assert policy.guest_daily_qwen_tokens == 500_000
    assert policy.fx_snapshot is None


def test_settings_reject_incomplete_or_unsafe_fx_configuration():
    with pytest.raises(ValueError, match="must be set together"):
        Settings(guest_trial_fx_source_date=date(2026, 10, 20))
    with pytest.raises(ValueError, match="at least 10%"):
        Settings(guest_trial_fx_safety_margin_bps=999)


def test_settings_loads_trial_budget_and_fx_snapshot_from_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_DAILY_COST_USD", "5.00")
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_DAILY_QWEN_TOKENS", "5000000")
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_DAILY_TASKS_PER_GUEST", "30")
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_DAILY_QWEN_TOKENS_PER_GUEST", "500000")
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_FX_SOURCE_DATE", "2026-10-20")
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_FX_CNY_PER_USD", "6.7200")
    monkeypatch.setenv(
        "DECAGENT_GUEST_TRIAL_FX_SOURCE_URL", "https://www.chinamoney.com.cn/english/"
    )
    monkeypatch.setenv("DECAGENT_GUEST_TRIAL_FX_SAFETY_MARGIN_BPS", "1000")

    policy = TrialBudgetPolicy.from_settings(Settings.from_env())

    assert policy.fx_snapshot.source_date == date(2026, 10, 20)
    assert policy.fx_snapshot.effective_rate(as_of=date(2026, 10, 22)) == Decimal("6.04")


def test_trial_budget_policy_rejects_nonpositive_limits():
    with pytest.raises(BudgetPolicyError):
        TrialBudgetPolicy(daily_qwen_tokens=0)


@pytest.mark.parametrize(
    "source_date,as_of",
    [(date(2026, 10, 23), date(2026, 10, 22)), (date(2026, 10, 14), date(2026, 10, 22))],
)
def test_fx_snapshot_rejects_future_or_stale_quotes(source_date, as_of):
    snapshot = FxSnapshot(
        source_date=source_date,
        cny_per_usd=Decimal("6.7200"),
        source_url="https://www.chinamoney.com.cn/english/",
    )

    with pytest.raises(BudgetPolicyError):
        snapshot.effective_rate(as_of=as_of)


@pytest.mark.parametrize(
    "rate,source_url",
    [
        (Decimal("0"), "https://www.chinamoney.com.cn/english/"),
        (Decimal("NaN"), "https://www.chinamoney.com.cn/english/"),
        (Decimal("6.72"), "http://www.chinamoney.com.cn/english/"),
        (Decimal("6.72"), "https://example.com/fx"),
    ],
)
def test_fx_snapshot_rejects_invalid_rate_or_source(rate, source_url):
    with pytest.raises(BudgetPolicyError):
        FxSnapshot(
            source_date=date(2026, 10, 20),
            cny_per_usd=rate,
            source_url=source_url,
        )

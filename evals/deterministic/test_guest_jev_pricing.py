from decimal import Decimal

from decagent.business.guest_jev_pricing import (
    GUEST_JEV_ENDPOINT,
    GUEST_JEV_MAX_CONTEXT_TOKENS,
    GUEST_JEV_MAX_FEE_USD,
    GUEST_JEV_MODEL,
    GUEST_JEV_PRICE_BASIS_ID,
    guest_jev_profile_is_bounded,
    select_jev_profile,
)
from decagent.runtime.model_calls import LedgerAttemptBudget


def test_guest_jev_fee_ceiling_uses_full_pinned_model_context():
    assert GUEST_JEV_MAX_FEE_USD == Decimal("0.011010048")
    assert Decimal("0.02") > GUEST_JEV_MAX_FEE_USD
    assert GUEST_JEV_MAX_CONTEXT_TOKENS == 262_144


def test_guest_jev_profile_requires_exact_model_route_limit_and_reservation():
    profile = {
        "provider": "openrouter",
        "endpoint": GUEST_JEV_ENDPOINT,
        "model": GUEST_JEV_MODEL,
        "tokens": GUEST_JEV_MAX_CONTEXT_TOKENS,
        "cost_currency": "USD",
        "reserved_cost": Decimal("0.02"),
        "price_basis_id": GUEST_JEV_PRICE_BASIS_ID,
    }
    assert guest_jev_profile_is_bounded(**profile)
    assert not guest_jev_profile_is_bounded(**{**profile, "model": "~typesafe/jev-latest"})
    assert not guest_jev_profile_is_bounded(
        **{**profile, "tokens": GUEST_JEV_MAX_CONTEXT_TOKENS + 1}
    )
    assert not guest_jev_profile_is_bounded(
        **{**profile, "reserved_cost": Decimal("0.01")}
    )
    assert not guest_jev_profile_is_bounded(**{**profile, "provider": "typesafe"})
    assert not guest_jev_profile_is_bounded(**{**profile, "endpoint": "https://invalid.example/"})


def test_guest_jev_selects_pinned_model_and_minimum_reservation():
    assert select_jev_profile(
        is_guest=True,
        model="~typesafe/jev-latest",
        provider="openrouter",
        reservation_usd=Decimal("0.01"),
    ) == ("typesafe/jev-1.14", "openrouter", Decimal("0.02"))
    assert select_jev_profile(
        is_guest=False,
        model="~typesafe/jev-latest",
        provider="openrouter",
        reservation_usd=Decimal("0.01"),
    ) == ("~typesafe/jev-latest", "openrouter", Decimal("0.01"))
    assert select_jev_profile(
        is_guest=True,
        model="jev-1.13.0",
        provider="typesafe",
        reservation_usd=Decimal("0.01"),
    ) is None


def test_guest_jev_reservation_cannot_be_rebound_to_an_alias():
    class GuestLedger:
        trial_budget = object()

    budget = LedgerAttemptBudget(
        GuestLedger(),
        reservation_usd=Decimal("0.02"),
        provider="openrouter",
        requested_model=GUEST_JEV_MODEL,
    )
    budget.bind_model(
        model="~typesafe/jev-latest",
        provider="openrouter",
        endpoint=GUEST_JEV_ENDPOINT,
    )
    assert budget.reserve(object(), 1) is None


def test_employee_latest_alias_binds_to_openrouter_adapter_model():
    budget = LedgerAttemptBudget(
        object(),
        reservation_usd=Decimal("0.01"),
        provider="openrouter",
        requested_model="jev-latest",
    )

    budget.bind_model(
        model="~typesafe/jev-latest",
        provider="openrouter",
        endpoint=GUEST_JEV_ENDPOINT,
    )

    assert budget._model_binding_valid
    assert budget.requested_model == "~typesafe/jev-latest"

"""Frozen OpenRouter Jev profile used by anonymous trial dispatch."""

from decimal import Decimal

GUEST_JEV_PROVIDER = "openrouter"
GUEST_JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
GUEST_JEV_MODEL = "typesafe/jev-1.14"
GUEST_JEV_PRICE_BASIS_ID = "openrouter-typesafe-jev-1.14-2026-10-23"
GUEST_JEV_INPUT_USD_PER_MILLION = Decimal("0.042")
GUEST_JEV_OUTPUT_USD_PER_MILLION = Decimal("0")
GUEST_JEV_MAX_CONTEXT_TOKENS = 262_144
GUEST_JEV_MAX_FEE_USD = (
    GUEST_JEV_INPUT_USD_PER_MILLION
    * GUEST_JEV_MAX_CONTEXT_TOKENS
    / Decimal(1_000_000)
)
GUEST_JEV_RESERVATION_USD = Decimal("0.02")


def select_jev_profile(*, is_guest, model, provider, reservation_usd):
    """Use a fixed, priced Jev profile only for anonymous trial workspaces."""
    if not is_guest:
        return model, provider, Decimal(str(reservation_usd))
    if provider != GUEST_JEV_PROVIDER:
        return None
    return (
        GUEST_JEV_MODEL,
        GUEST_JEV_PROVIDER,
        max(Decimal(str(reservation_usd)), GUEST_JEV_RESERVATION_USD),
    )


def guest_jev_profile_is_bounded(
    *,
    provider,
    endpoint,
    model,
    tokens,
    cost_currency,
    reserved_cost,
    price_basis_id,
):
    """Require the exact priced route, provider-context bound, and adequate reserve."""
    return (
        provider == GUEST_JEV_PROVIDER
        and endpoint == GUEST_JEV_ENDPOINT
        and model == GUEST_JEV_MODEL
        and type(tokens) is int
        and 0 < tokens <= GUEST_JEV_MAX_CONTEXT_TOKENS
        and cost_currency == "USD"
        and reserved_cost is not None
        and isinstance(reserved_cost, Decimal)
        and reserved_cost.is_finite()
        and reserved_cost >= GUEST_JEV_MAX_FEE_USD
        and price_basis_id == GUEST_JEV_PRICE_BASIS_ID
    )

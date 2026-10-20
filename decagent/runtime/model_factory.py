"""Construct the same bounded primary/fallback profile for all general-model roles."""

from decagent.models.primary import PrimaryModel

from .model_calls import BudgetedPrimary


def build_budgeted_primary(
    settings,
    config,
    ledger,
    *,
    purpose,
    max_output_tokens=2048,
    model=None,
    tool_definitions=None,
    validator=None,
    before_dispatch=None,
    fallback_allowed=True,
    force_fallback=False,
    request_role=None,
):
    if type(fallback_allowed) is not bool or type(force_fallback) is not bool:
        raise ValueError("Model fallback controls must be boolean")
    use_fallback = fallback_allowed and (config.main_fallback_enabled or force_fallback)
    if use_fallback:
        if not settings.fallback_enabled and not force_fallback:
            raise ValueError("Fallback is enabled in runtime but disabled in model settings")
        if not settings.resolved_fallback_api_key:
            raise ValueError("OpenRouter fallback requires an API key")
        if not (config.fallback_price or config.unknown_fallback_reservation):
            raise ValueError("Fallback requires versioned pricing or a bounded fee reservation")
    primary = model or PrimaryModel.from_settings(
        settings,
        max_output_tokens=max_output_tokens,
        tool_definitions=tool_definitions,
        validator=validator,
    )
    fallback = None
    if use_fallback:
        fallback = PrimaryModel.from_profile(
            model=settings.fallback_model,
            base_url=settings.fallback_base_url,
            api_key=settings.resolved_fallback_api_key,
            max_output_tokens=max_output_tokens,
            tool_definitions=tool_definitions,
            validator=validator,
            known_secrets=(
                settings.main_api_key,
                settings.resolved_fallback_api_key,
                settings.jev_api_key,
                settings.github_token,
            ),
            usage_include=True,
        )
    return BudgetedPrimary(
        primary,
        ledger,
        price=config.main_price,
        max_output_tokens=max_output_tokens,
        unknown_cost_reservation=config.unknown_main_reservation,
        before_dispatch=before_dispatch,
        provider=config.main_provider,
        purpose=purpose,
        request_role=request_role
        or {"plan": "planning", "replan": "planning", "format_repair": "planning"}.get(
            purpose, purpose
        ),
        fallback_model=fallback,
        fallback_provider="openrouter",
        fallback_price=config.fallback_price,
        fallback_unknown_cost_reservation=config.unknown_fallback_reservation,
        fallback_requested_model=settings.fallback_model if fallback else None,
        fallback_endpoint=settings.fallback_base_url if fallback else None,
        max_same_provider_attempts=config.max_primary_attempts,
        max_retry_after_seconds=config.max_retry_after_seconds,
        max_input_tokens=config.max_model_input_tokens,
    )

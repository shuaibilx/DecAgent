"""Immutable admission limits for the approved first public-evaluation wave."""

from decimal import Decimal, InvalidOperation

FIRST_WAVE_BATCH_LIMITS = {
    "qwen_requests": 500,
    "qwen_tokens": 5_000_000,
    "agent_qwen_requests": 300,
    "agent_qwen_tokens": 3_000_000,
    "user_simulator_qwen_requests": 160,
    "user_simulator_qwen_tokens": 1_600_000,
    "judge_qwen_requests": 40,
    "judge_qwen_tokens": 400_000,
    "tool_calls": 1_600,
    "wall_seconds": 14_400,
}

_QWEN_BATCH_ROLES = ("agent", "user_simulator", "judge")
OPENROUTER_DIAGNOSTIC_LIMITS = {
    "openrouter_requests": 6,
    "openrouter_tokens": 600_000,
    "openrouter_cost_usd": "5",
    "agent_openrouter_requests": 5,
    "agent_openrouter_tokens": 500_000,
    "user_simulator_openrouter_requests": 0,
    "user_simulator_openrouter_tokens": 0,
    "judge_openrouter_requests": 1,
    "judge_openrouter_tokens": 65_946,
}
OPENROUTER_CSM_DIAGNOSTIC_BATCH_LIMITS = {
    "openrouter_requests": 5,
    "openrouter_tokens": 280_000,
    "openrouter_cost_usd": "0.30",
    "agent_openrouter_requests": 5,
    "agent_openrouter_tokens": 280_000,
    "user_simulator_openrouter_requests": 0,
    "user_simulator_openrouter_tokens": 0,
    "judge_openrouter_requests": 0,
    "judge_openrouter_tokens": 0,
    "tool_calls": 40,
    "wall_seconds": 1_800,
}
OPENROUTER_CSM_DIAGNOSTIC_RESOURCE_LIMITS = {
    "agent_model_requests": 5,
    "agent_tokens": 280_000,
    "tool_calls": 40,
    "agent_machine_seconds": 1_800,
    "tau_steps": 0,
    "user_simulator_requests": 0,
    "user_simulator_tokens": 0,
    "judge_requests": 0,
    "judge_tokens": 0,
}


def public_batch_limit_stop_reason(snapshot):
    """Return the first exhausted frozen batch limit, if any.

    This is a preflight check only. The ledger rechecks it transactionally before
    persisting a stop marker so stale snapshots cannot stop a changed batch.
    """
    if type(snapshot) is not dict:
        raise TypeError("A public batch snapshot is required")
    limits = snapshot.get("limits")
    if type(limits) is not dict:
        raise TypeError("A public batch snapshot must include frozen limits")

    def count(name):
        value = snapshot.get(name)
        if type(value) is not int or value < 0:
            raise TypeError(f"A non-negative integer {name} counter is required")
        return value

    if "qwen_requests" in limits and count("qwen_requests") >= limits["qwen_requests"]:
        return "qwen_requests"
    if "qwen_tokens" in limits and count("qwen_tokens") >= limits["qwen_tokens"]:
        return "qwen_tokens"
    if "openrouter_requests" in limits and count("openrouter_requests") >= limits[
        "openrouter_requests"
    ]:
        return "openrouter_requests"
    if "openrouter_tokens" in limits and count("openrouter_tokens") >= limits[
        "openrouter_tokens"
    ]:
        return "openrouter_tokens"

    by_role = snapshot.get("by_role")
    if type(by_role) is not dict:
        raise TypeError("A public batch snapshot must include per-role usage")
    for role, limit_prefix in (
        ("primary", "agent"),
        ("user_simulator", "user_simulator"),
        ("judge", "judge"),
    ):
        role_usage = by_role.get(role)
        if type(role_usage) is not dict:
            raise TypeError("A public batch snapshot is missing a model role")
        request_limit = limits.get(f"{limit_prefix}_qwen_requests")
        token_limit = limits.get(f"{limit_prefix}_qwen_tokens")
        if request_limit is not None:
            requests = count_role(role_usage, "requests")
            if requests > request_limit or (request_limit > 0 and requests >= request_limit):
                return f"{limit_prefix}_qwen_requests"
        if token_limit is not None:
            tokens = count_role(role_usage, "tokens")
            if tokens > token_limit or (token_limit > 0 and tokens >= token_limit):
                return f"{limit_prefix}_qwen_tokens"

    provider_roles = snapshot.get("by_provider_role", {})
    openrouter_roles = provider_roles.get("openrouter", {})
    if type(openrouter_roles) is not dict:
        raise TypeError("OpenRouter per-role usage must be a mapping")
    for role, limit_prefix in (
        ("primary", "agent"),
        ("user_simulator", "user_simulator"),
        ("judge", "judge"),
    ):
        role_usage = openrouter_roles.get(role, {})
        if type(role_usage) is not dict:
            raise TypeError("OpenRouter per-role usage must be a mapping")
        request_limit = limits.get(f"{limit_prefix}_openrouter_requests")
        token_limit = limits.get(f"{limit_prefix}_openrouter_tokens")
        if request_limit is not None:
            requests = count_role(role_usage, "requests")
            if requests > request_limit or (request_limit > 0 and requests >= request_limit):
                return f"{limit_prefix}_openrouter_requests"
        if token_limit is not None:
            tokens = count_role(role_usage, "tokens")
            if tokens > token_limit or (token_limit > 0 and tokens >= token_limit):
                return f"{limit_prefix}_openrouter_tokens"

    cost_limit = limits.get("qwen_cost_cny")
    if cost_limit is not None:
        unknown_costs = count("qwen_cost_unknown_attempts")
        current_cost = snapshot.get("qwen_cost_cny")
        if unknown_costs:
            return "qwen_cost_unknown"
        try:
            current_cost = Decimal(str(current_cost))
            cost_limit = Decimal(str(cost_limit))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TypeError("A valid Qwen cost snapshot is required") from exc
        if not current_cost.is_finite() or not cost_limit.is_finite():
            raise TypeError("Qwen costs must be finite")
        if current_cost >= cost_limit:
            return "qwen_cost_cny"

    openrouter_cost_limit = limits.get("openrouter_cost_usd")
    if openrouter_cost_limit is not None:
        unknown_costs = count("openrouter_cost_unknown_attempts")
        current_cost = snapshot.get("openrouter_cost_usd")
        if unknown_costs:
            return "openrouter_cost_unknown"
        try:
            current_cost = Decimal(str(current_cost))
            openrouter_cost_limit = Decimal(str(openrouter_cost_limit))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TypeError("A valid OpenRouter cost snapshot is required") from exc
        if not current_cost.is_finite() or not openrouter_cost_limit.is_finite():
            raise TypeError("OpenRouter costs must be finite")
        if current_cost >= openrouter_cost_limit:
            return "openrouter_cost_usd"

    if "tool_calls" in limits and count("tool_calls") >= limits["tool_calls"]:
        return "tool_calls"
    return None


def count_role(role_usage, name):
    value = role_usage.get(name)
    if type(value) is not int or value < 0:
        raise TypeError(f"A non-negative per-role {name} counter is required")
    return value


def validate_public_batch_limits(value):
    """Allow a frozen cohort to lower, but never raise, approved batch ceilings."""
    base_keys = set(FIRST_WAVE_BATCH_LIMITS)
    openrouter_keys = set(OPENROUTER_DIAGNOSTIC_LIMITS)
    openrouter_aggregate_keys = {
        "openrouter_requests", "openrouter_tokens", "openrouter_cost_usd"
    }
    if type(value) is not dict or frozenset(value) not in {
        frozenset(base_keys),
        frozenset(base_keys | openrouter_aggregate_keys),
        frozenset(openrouter_keys | {"tool_calls", "wall_seconds"}),
        frozenset(base_keys | openrouter_keys | {"tool_calls", "wall_seconds"}),
    }:
        raise ValueError("Public batch limits must contain the complete approved limit set")
    if base_keys <= set(value):
        for name, ceiling in FIRST_WAVE_BATCH_LIMITS.items():
            current = value[name]
            if type(current) is not int or current < 1:
                raise ValueError("Public batch limits must be positive integers")
            if current > ceiling:
                raise ValueError("Public batch limits exceed approved ceilings")

    for name, ceiling in (("tool_calls", 1_600), ("wall_seconds", 14_400)):
        if name in value and (
            type(value[name]) is not int or not 1 <= value[name] <= ceiling
        ):
            raise ValueError("Public batch resource limit exceeds approved ceilings")

    if openrouter_aggregate_keys <= set(value):
        for name in ("openrouter_requests", "openrouter_tokens"):
            current = value[name]
            if type(current) is not int or current < 1:
                raise ValueError("OpenRouter batch limits must be positive integers")
            if current > OPENROUTER_DIAGNOSTIC_LIMITS[name]:
                raise ValueError("OpenRouter batch limits exceed approved ceilings")
        try:
            current_cost = Decimal(str(value["openrouter_cost_usd"]))
            cost_ceiling = Decimal(OPENROUTER_DIAGNOSTIC_LIMITS["openrouter_cost_usd"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("OpenRouter cost ceiling must be a valid USD amount") from exc
        if (
            not current_cost.is_finite()
            or current_cost <= 0
            or current_cost > cost_ceiling
        ):
            raise ValueError("OpenRouter cost ceiling exceeds the approved $5 limit")

        role_names = (
            "agent", "user_simulator", "judge"
        )
        role_limit_keys = {
            f"{role}_openrouter_{kind}"
            for role in role_names
            for kind in ("requests", "tokens")
        }
        if role_limit_keys & set(value):
            if not role_limit_keys <= set(value):
                raise ValueError("OpenRouter role limits must be complete")
            for role in role_names:
                for kind in ("requests", "tokens"):
                    name = f"{role}_openrouter_{kind}"
                    current = value[name]
                    if type(current) is not int or current < 0:
                        raise ValueError("OpenRouter role limits must be nonnegative integers")
                    ceiling = OPENROUTER_DIAGNOSTIC_LIMITS[name]
                    if current > ceiling:
                        raise ValueError("OpenRouter role limit exceeds approved ceiling")
            if sum(value[f"{role}_openrouter_requests"] for role in role_names) != value[
                "openrouter_requests"
            ]:
                raise ValueError("OpenRouter role requests must sum to the total limit")
            if sum(value[f"{role}_openrouter_tokens"] for role in role_names) > value[
                "openrouter_tokens"
            ]:
                raise ValueError("OpenRouter role token limits cannot exceed the total limit")

    if base_keys <= set(value):
        if sum(value[f"{role}_qwen_requests"] for role in _QWEN_BATCH_ROLES) != value[
            "qwen_requests"
        ]:
            raise ValueError("Public batch request role limits must sum to the total request limit")
        if sum(value[f"{role}_qwen_tokens"] for role in _QWEN_BATCH_ROLES) != value["qwen_tokens"]:
            raise ValueError("Public batch token role limits must sum to the total token limit")
    return dict(value)

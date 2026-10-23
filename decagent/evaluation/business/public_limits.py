"""Immutable admission limits for the approved first public-evaluation wave."""

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


def validate_public_batch_limits(value):
    """Allow a frozen cohort to lower, but never raise, approved batch ceilings."""
    if type(value) is not dict or set(value) != set(FIRST_WAVE_BATCH_LIMITS):
        raise ValueError("Public batch limits must contain the complete approved limit set")
    for name, ceiling in FIRST_WAVE_BATCH_LIMITS.items():
        current = value[name]
        if type(current) is not int or current < 1:
            raise ValueError("Public batch limits must be positive integers")
        if current > ceiling:
            raise ValueError("Public batch limits exceed approved ceilings")

    if sum(value[f"{role}_qwen_requests"] for role in _QWEN_BATCH_ROLES) != value[
        "qwen_requests"
    ]:
        raise ValueError("Public batch request role limits must sum to the total request limit")
    if sum(value[f"{role}_qwen_tokens"] for role in _QWEN_BATCH_ROLES) != value["qwen_tokens"]:
        raise ValueError("Public batch token role limits must sum to the total token limit")
    return dict(value)

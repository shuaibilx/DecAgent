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

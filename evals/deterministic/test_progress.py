"""Observed progress is bounded evidence, never a model's success claim."""

from importlib import import_module
from importlib.util import find_spec

import pytest


def module():
    assert find_spec("decagent.runtime.progress") is not None, "Progress tracking is missing"
    return import_module("decagent.runtime.progress")


def step(progress=None, *, number=1, reason="assertion failed", action=None, **extra):
    return module().observe_progress(
        progress,
        action or {"tool": "run_command", "params": {"argv": ["python", "test.py"]}},
        {"status": "failed", "reason": reason, **extra},
        step_id=str(number),
    )


def test_repeated_failure_ignores_receipt_ids_elapsed_and_call_ids():
    first = step(execution_id="one", elapsed_seconds=1, stdout="1 failed in 0.12s")
    second = step(
        first, number=2, execution_id="two", elapsed_seconds=2, stdout="1 failed in 0.98s"
    )
    assert second["steps"][0]["result_hash"] == second["steps"][1]["result_hash"]
    assert module().progress_signals(second)["stalled"] is True
    assert module().route_progress(second)["route"] == "replan"


def test_distinct_failures_are_new_observations_and_not_automatic_help():
    first = step(reason="missing import")
    second = step(first, number=2, reason="assertion differs at upper boundary")
    signals = module().progress_signals(second)
    assert signals["needs_check"] and not signals["stalled"]
    assert module().route_progress(second)["route"] == "continue"
    assert "success" not in signals


def test_help_summary_does_not_claim_repetition_for_distinct_failures():
    from decagent.runtime.intervention import help_packet

    progress = step(step(reason="missing import"), number=2, reason="different assertion")
    decision = module().route_progress(progress, suggestion="escalate", calibrated=True)
    packet = help_packet("Fix boundary", progress, decision)
    assert "重复" not in packet["summary"]
    assert "协助" in packet["summary"]


def test_broker_command_receipts_ignore_transport_identity_but_keep_real_output():
    from uuid import uuid4

    from decagent.executor.protocol import ExecutionRequest

    progress = None
    for number in range(1, 3):
        request = ExecutionRequest(
            execution_id=str(uuid4()),
            task_id=str(uuid4()),
            action_id=str(uuid4()),
            action_digest="a" * 64,
            owner="worker",
            lease_generation=number,
            argv=["python", "test.py"],
        )
        progress = step(
            progress,
            number=number,
            execution_id=request.execution_id,
            payload_hash=request.payload_hash,
            container="decagent-run-" + request.execution_id,
            state="finished",
            exit_code=1,
            output="same assertion failure",
        )
    assert module().route_progress(progress)["route"] == "replan"
    changed = step(progress, number=3, output="new boundary assertion", exit_code=1)
    assert module().progress_signals(changed)["stalled"] is False


def test_nonzero_command_exit_is_failure_even_when_transport_succeeded():
    progress = step(status="succeeded", exit_code=1)
    assert progress["steps"][0]["outcome"] == "failed"
    assert module().progress_signals(progress)["failure_streak"] == 1


def test_unknown_receipt_never_becomes_retry_or_completed_progress():
    progress = step(status="unknown")
    assert progress["steps"][0]["outcome"] == "unknown"
    assert module().route_progress(progress)["route"] == "reconcile"


def test_repeated_safe_read_still_detects_stagnation():
    action = {"tool": "read_file", "params": {"path": "bug.py", "max_bytes": 2000}}
    first = step(action=action, status="succeeded", content="same source")
    second = step(first, number=2, action=action, status="succeeded", content="same source")
    assert module().route_progress(second)["route"] == "replan"
    newer = step(second, number=3, action=action, status="succeeded", content="new source")
    assert not module().progress_signals(newer)["stalled"]


def test_adjustments_are_bounded_and_feedback_starts_a_new_cycle_only_once():
    progress = step(step(), number=2)
    for _ in range(2):
        assert module().route_progress(progress)["route"] == "replan"
        progress = module().begin_replan(progress)
        for _ in range(2):
            progress = step(progress, number=progress["total"] + 1)
    assert module().route_progress(progress)["route"] == "escalate"
    answered = module().accept_feedback(progress, "reply-one")
    assert answered["replans"] == 0 and answered["episode_start"] == progress["total"]
    assert module().accept_feedback(answered, "reply-one") == answered
    assert answered["total"] == progress["total"]


@pytest.mark.parametrize("route", ["continue", "replan", "clarify", "escalate"])
def test_calibrated_routes_are_supported_but_uncalibrated_advice_is_shadow(route):
    progress = step(step(), number=2)
    selected = module().route_progress(progress, suggestion=route, calibrated=True)
    assert selected["route"] == route
    shadow = module().route_progress(progress, suggestion=route)
    assert shadow["route"] == "replan" and shadow["model_suggestion"] == route
    assert shadow["basis"] == "uncalibrated_shadow"


def test_model_cannot_override_adjustment_cap_or_expand_authorization():
    progress = step(step(), number=2)
    progress["replans"] = 2
    result = module().route_progress(progress, suggestion="continue", calibrated=True)
    assert result["route"] == "escalate"
    assert result["basis"] == "adjustment_limit"
    with pytest.raises(ValueError):
        module().route_progress(progress, suggestion="allow")


def test_calibrated_continue_cannot_repeat_identical_evidence_indefinitely():
    progress = None
    for number in range(1, 5):
        progress = step(progress, number=number)
    result = module().route_progress(progress, suggestion="continue", calibrated=True)
    assert result["route"] == "replan" and result["basis"] == "repeat_guard"


def test_window_is_bounded_and_replayed_observation_is_idempotent():
    progress = None
    for number in range(1, 51):
        progress = step(progress, number=number, reason="x" * 20_000)
    assert progress["total"] == 50 and len(progress["steps"]) <= 20
    assert len(progress["steps"][-1]["excerpt"]) <= 1600
    assert step(progress, number=50, reason="x" * 20_000) == progress


def test_model_failure_uses_explicit_bounded_fallback():
    progress = step(step(), number=2)
    result = module().route_progress(progress, error="decision_unavailable")
    assert result["route"] == "replan"
    assert result["basis"] == "decision_unavailable"


def test_question_carries_attempts_and_is_separate_from_action_permissions():
    from decagent.decisions.contracts import QUESTIONS

    assert "task_intervention" in QUESTIONS, "Independent intervention contract is missing"
    question = QUESTIONS["task_intervention"]
    assert set(question.criteria) == {"continue", "replan", "clarify", "escalate"}
    assert {"attempts", "progress_signals", "goal"} <= set(question.state_fields)
    assert "authorization" not in question.state_fields

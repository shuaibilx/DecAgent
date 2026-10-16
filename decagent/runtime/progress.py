"""Bounded observations and explicit loop policy; novelty is not verified task progress."""

from collections.abc import Mapping

from decagent.decisions.contracts import canonical_json, digest

WINDOW = 20
MAX_REPLANS = 2
ROUTES = frozenset({"continue", "replan", "clarify", "escalate"})
_VOLATILE = frozenset(
    {
        "id",
        "call_id",
        "execution_id",
        "payload_hash",
        "container",
        "action_id",
        "lease_generation",
        "owner",
        "elapsed_seconds",
        "duration_seconds",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        "timestamp",
        "timeout_seconds",
        "max_output_bytes",
        "max_bytes",
    }
)


def _stable(value, field=None):
    import re

    if isinstance(value, Mapping):
        return {k: _stable(v, k) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, (list, tuple)):
        return [_stable(v, field) for v in value[:100]]
    if isinstance(value, str):
        # pytest elapsed summaries are diagnostic metadata, not new failure evidence.
        text = value[:32_000]
        if field in {"stdout", "stderr", "output"}:
            text = re.sub(r"(?<= in )\d+(?:\.\d+)?s(?=\s*(?:=|$))", "<elapsed>", text)
        return text
    return value


def observe_progress(previous, action, observation, *, step_id):
    """Append one sanitized observation. Callers own redaction and durable step identity."""
    state = {
        "steps": [],
        "total": 0,
        "replans": 0,
        "episode_start": 0,
        "checked_total": 0,
        **(previous or {}),
    }
    if not isinstance(step_id, str) or not step_id:
        raise ValueError("Observation identity is required")
    if any(item["id"] == step_id for item in state["steps"]):
        return state
    stable_action = _stable({"tool": action["tool"], "params": action.get("params", {})})
    stable_result = _stable(observation)
    status = observation.get("status", "unknown")
    if status not in {"succeeded", "failed", "denied", "unknown"}:
        status = "unknown"
    if (
        status in {"succeeded", "failed"}
        and action["tool"] == "run_command"
        and observation.get("exit_code") not in {None, 0}
    ):
        status = "failed"
    stable_result["status"] = status
    number = state["total"] + 1
    entry = {
        "id": step_id,
        "number": number,
        "tool": action["tool"],
        "action_hash": digest(stable_action),
        "result_hash": digest(stable_result),
        "outcome": status,
        "target": canonical_json(stable_action["params"])[:400],
        "excerpt": canonical_json(stable_result)[:1600],
    }
    return {**state, "total": number, "steps": [*state["steps"], entry][-WINDOW:]}


def progress_signals(progress):
    state = progress or {}
    steps = [s for s in state.get("steps", []) if s["number"] > state.get("episode_start", 0)]
    latest = steps[-1] if steps else None
    recent = steps[-6:]
    repeated = (
        sum(
            s["action_hash"] == latest["action_hash"] and s["result_hash"] == latest["result_hash"]
            for s in recent
        )
        if latest
        else 0
    )
    streak = 0
    for item in reversed(steps):
        if item["outcome"] not in {"failed", "denied"}:
            break
        streak += 1
    same_failure = (
        len(recent) >= 3
        and all(s["outcome"] in {"failed", "denied"} for s in recent[-3:])
        and len({s["result_hash"] for s in recent[-3:]}) == 1
    )
    stalled = repeated >= 2 or same_failure
    fresh_window = state.get("total", 0) - state.get("checked_total", 0) >= 2
    return {
        "repeat_count": repeated,
        "failure_streak": streak,
        "stalled": stalled,
        "needs_check": fresh_window and (stalled or streak >= 2),
        "latest_outcome": latest["outcome"] if latest else None,
        "observed_steps": state.get("total", 0),
        "replans_used": state.get("replans", 0),
        "replans_remaining": max(0, MAX_REPLANS - state.get("replans", 0)),
        "meaning": "Fingerprint changes describe observation novelty, not verified progress",
    }


def route_progress(progress, *, suggestion=None, calibrated=False, error=None):
    if suggestion is not None and suggestion not in ROUTES:
        raise ValueError("Unknown intervention suggestion")
    signals = progress_signals(progress)
    route, basis = "continue", "observed_rule"
    if signals["latest_outcome"] == "unknown":
        route, basis = "reconcile", "unknown_receipt"
    elif signals["needs_check"]:
        if signals["stalled"] and signals["replans_remaining"] == 0:
            route, basis = "escalate", "adjustment_limit"
        elif calibrated and suggestion is not None:
            route, basis = suggestion, "calibrated"
            if (
                route == "continue"
                and signals["stalled"]
                and (signals["repeat_count"] >= 4 or signals["failure_streak"] >= 4)
            ):
                route, basis = "replan", "repeat_guard"
            if route == "replan" and signals["replans_remaining"] == 0:
                route, basis = "escalate", "adjustment_limit"
        else:
            route = "replan" if signals["stalled"] else "continue"
            basis = error or ("uncalibrated_shadow" if suggestion is not None else "observed_rule")
    return {"route": route, "basis": basis, "model_suggestion": suggestion, "signals": signals}


def begin_replan(progress):
    if progress.get("replans", 0) >= MAX_REPLANS:
        raise ValueError("Strategy adjustment limit reached")
    return {
        **progress,
        "replans": progress.get("replans", 0) + 1,
        "checked_total": progress["total"],
    }


def accept_feedback(progress, feedback_id):
    if not isinstance(feedback_id, str) or not feedback_id:
        raise ValueError("Durable feedback identity is required")
    if progress.get("feedback_id") == feedback_id:
        return progress
    return {
        **progress,
        "replans": 0,
        "episode_start": progress["total"],
        "checked_total": progress["total"],
        "feedback_id": feedback_id,
    }

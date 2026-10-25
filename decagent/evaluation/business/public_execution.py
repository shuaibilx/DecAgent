"""Fail-closed orchestration for one ledgered public benchmark route.

This module does not create model clients, benchmark environments, or a batch
scheduler. It joins the already-frozen route ledger to ``ControlledRoutes`` and
its injected, ledger-aware adapters so route lifecycle transitions have one
auditable entry point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from time import perf_counter

from decagent.business.plans.contracts import Plan
from decagent.decisions.contracts import digest

from .comparison import PublicComparisonTrial, Route
from .public_diagnostics import (
    DIAGNOSTIC_CODES,
    safe_diagnostic_code,
    safe_exception_type,
)
from .public_limits import public_batch_limit_stop_reason
from .public_resources import LedgeredModelAdapterFactory, LedgeredToolGateway
from .public_supervisor import PublicSupervisor, canonical_public_supervisor_lock_path
from .public_trials import PublicRunPlan, canonical_public_trial_ledger_path

_FIRST_WAVE_ROUTE_ORDER = ("react_skill_mcp", "plan_mcp")
_DIAGNOSTIC_ROUTE_ORDER = ("plan_mcp", "react_skill_mcp")
_SUPPORTED_PUBLIC_ROUTES = frozenset(route.value for route in Route)
_TERMINAL_ROUTE_STATUSES = frozenset(
    {"completed", "failed", "unknown", "scoring_error", "not_applicable"}
)
_ERROR_CATEGORY = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SCORE_SOURCES = {
    "enterpriseops-gym": "enterpriseops-gym-original-verifier",
    "tau-bench": "tau2-bench-original-evaluate-simulation",
}
_BATCH_RESOURCE_STOP_REASONS = frozenset(
    {
        "qwen_requests",
        "qwen_tokens",
        "qwen_cost_cny",
        "qwen_cost_unknown",
        "openrouter_requests",
        "openrouter_tokens",
        "openrouter_cost_usd",
        "openrouter_cost_unknown",
        "openrouter_limits_missing",
        "agent_qwen_requests",
        "agent_qwen_tokens",
        "agent_openrouter_requests",
        "agent_openrouter_tokens",
        "user_simulator_qwen_requests",
        "user_simulator_qwen_tokens",
        "user_simulator_openrouter_requests",
        "user_simulator_openrouter_tokens",
        "judge_qwen_requests",
        "judge_qwen_tokens",
        "judge_openrouter_requests",
        "judge_openrouter_tokens",
        "tool_calls",
    }
)


def _trial_matches_record(trial, record):
    return (
        isinstance(trial, PublicComparisonTrial)
        and trial.trial_id == record.get("trial_id")
        and trial.dataset == record.get("benchmark")
        and trial.dataset_version == record.get("version")
        and trial.case_id == record.get("case_id")
        and trial.exposure_group == record.get("exposure_group")
        and trial.resource_profile_digest == record.get("resource_profile_digest")
        and trial.response_policy_digest == record.get("response_policy_digest")
        and trial.model_profile_digest == record.get("model_profile_digest")
        and trial.jev_profile_digest == record.get("jev_profile_digest")
        and trial.user_simulator_profile_digest
        == record.get("user_simulator_profile_digest")
        and trial.judge_profile_digest == record.get("judge_profile_digest")
    )


@dataclass(frozen=True)
class _SemanticPlanReference:
    plan: Plan
    receipt: PublicRouteReceipt


@dataclass(frozen=True)
class PublicRouteReceipt:
    """Non-sensitive persisted outcome of one supervised route attempt."""

    trial_id: str
    route: str
    status: str
    score_status: str
    cleanup_verified: bool
    result_digest: str | None
    score_digest: str | None
    machine_seconds: float
    error_category: str | None
    tau_steps: int = 0
    failure_stage: str | None = None
    exception_type: str | None = None
    plan_digest: str | None = None
    decision_input_digest: str | None = None
    evidence_scope: str | None = None
    pair_status: str | None = None
    diagnostic_code: str | None = None


@dataclass(frozen=True)
class PublicPhaseReceipt:
    """Durable progress summary for the fixed first-wave route schedule."""

    phase: str
    cohort_digest: str
    scheduled_route_slots: int
    executed_routes: tuple[PublicRouteReceipt, ...]
    skipped_routes: tuple[tuple[str, str, str], ...]
    stopped_reason: str | None


def _failure_category(value):
    return value if type(value) is str and _ERROR_CATEGORY.fullmatch(value) else "route_failed"


def _comparison_evidence(result):
    run = getattr(result, "run", None)
    plan_digest = getattr(result, "plan_digest", None) or getattr(run, "plan_digest", None)
    decision_digest = getattr(result, "decision_input_digest", None) or getattr(
        run, "decision_input_digest", None
    )
    evidence_scope = getattr(result, "evidence_scope", None) or getattr(
        run, "evidence_scope", None
    )
    def valid_digest(value):
        return type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None

    return (
        plan_digest if valid_digest(plan_digest) else None,
        decision_digest if valid_digest(decision_digest) else None,
        evidence_scope if evidence_scope in {"full_workflow", "execution_only"} else None,
    )


def _mark_semantic_route_not_applicable(ledger, trial_id, route, reason):
    """Finalize a scheduled semantic slot without opening an environment or dispatching."""
    if reason not in {
        "semantic_decision_not_observed",
        "semantic_reference_unavailable",
    }:
        raise ValueError("Unsupported semantic non-applicability reason")
    ledger.start_route(trial_id, route)
    result_evidence = {
        "route": route,
        "status": "not_applicable",
        "reason": reason,
        "model_dispatched": False,
        "tool_dispatched": False,
        "environment_opened": False,
    }
    cleanup_evidence = {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
        "environment_opened": False,
        "cleanup_required": False,
    }
    result_hash = digest(result_evidence)
    ledger.finish_route(
        trial_id,
        route,
        status="not_applicable",
        result_digest=result_hash,
        error_category=reason,
        cleanup_verified=True,
        cleanup_evidence_digest=digest(cleanup_evidence),
        score_status="not_applicable",
    )
    return PublicRouteReceipt(
        trial_id=trial_id,
        route=route,
        status="not_applicable",
        score_status="not_applicable",
        cleanup_verified=True,
        result_digest=result_hash,
        score_digest=None,
        machine_seconds=0.0,
        error_category=reason,
        pair_status="not_applicable",
    )


def _mark_semantic_route_incomplete(ledger, trial_id, route):
    """Keep a scheduled comparison slot visible when its primary evidence failed."""
    ledger.start_route(trial_id, route)
    result_evidence = {
        "route": route,
        "status": "failed",
        "reason": "semantic_reference_incomplete",
        "model_dispatched": False,
        "tool_dispatched": False,
        "environment_opened": False,
    }
    cleanup_evidence = {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
        "environment_opened": False,
        "cleanup_required": False,
    }
    result_hash = digest(result_evidence)
    cleanup_hash = digest(cleanup_evidence)
    ledger.finish_route(
        trial_id,
        route,
        status="failed",
        result_digest=result_hash,
        error_category="semantic_reference_incomplete",
        cleanup_verified=True,
        cleanup_evidence_digest=cleanup_hash,
        score_status="not_attempted",
    )
    return PublicRouteReceipt(
        trial_id=trial_id,
        route=route,
        status="failed",
        score_status="not_attempted",
        cleanup_verified=True,
        result_digest=result_hash,
        score_digest=None,
        machine_seconds=0.0,
        error_category="semantic_reference_incomplete",
        pair_status="incomplete",
    )


def _score_evidence(trial, result):
    score = getattr(result, "score", None)
    source = _SCORE_SOURCES.get(getattr(trial, "dataset", None))
    if (
        score is None
        or source is None
        or getattr(score, "official", False) is not True
        or type(getattr(score, "benchmark_score", None)) not in {int, float}
    ):
        return None
    value = float(score.benchmark_score)
    if not isfinite(value) or not 0 <= value <= 1:
        return None
    success = value == 1.0
    if getattr(score, "success", None) is not success:
        return None
    forbidden = getattr(score, "forbidden_side_effects", None)
    if forbidden is not None and (type(forbidden) is not int or forbidden < 0):
        return None
    evidence = {
        "source": source,
        "value": value,
        "success": success,
        "forbidden_side_effects": forbidden,
    }
    components = getattr(score, "score_components", None)
    if components is not None:
        if source != "tau2-bench-original-evaluate-simulation":
            return None
        evidence["score_components"] = components
    return evidence


def _cleanup_evidence(result):
    evidence = getattr(result, "cleanup_evidence", None)
    verified = getattr(result, "cleanup_verified", None)
    if (
        type(verified) is not bool
        or type(evidence) is not dict
        or set(evidence) != {"verified", "session_closed", "environment_closed"}
        or type(evidence.get("verified")) is not bool
        or type(evidence.get("session_closed")) is not bool
        or type(evidence.get("environment_closed")) is not bool
        or evidence["verified"] is not verified
    ):
        return False, None
    return verified, digest(evidence) if verified else None


def _route_tau_steps(ledger, trial_id, route):
    snapshot = getattr(ledger, "resource_snapshot", None)
    if not callable(snapshot):
        return 0
    result = snapshot(trial_id, route)
    value = result.get("tau_steps") if type(result) is dict else None
    if type(value) is not int or value < 0:
        raise ValueError("Public ledger returned invalid τ-step evidence")
    return value


def _route_diagnostic_snapshot(ledger, trial_id, route):
    read = getattr(ledger, "route_lifecycle", None)
    if not callable(read):
        return None, None, None
    lifecycle = read(trial_id, route)
    if type(lifecycle) is not dict:
        return None, None, None
    return (
        lifecycle.get("failure_stage"),
        lifecycle.get("exception_type"),
        lifecycle.get("diagnostic_code"),
    )


def _unknown_route_score_status(ledger, trial_id, route):
    """Distinguish an unknown judge attempt from scoring never being started."""
    snapshot = getattr(ledger, "model_attempts_snapshot", None)
    if not callable(snapshot):
        return "not_attempted"
    try:
        attempts = snapshot(trial_id, route)
    except Exception:
        return "unknown"
    if type(attempts) not in {list, tuple}:
        return "unknown"
    for attempt in attempts:
        if type(attempt) is not dict:
            return "unknown"
        if attempt.get("role") == "judge" and attempt.get("status") not in {
            "not_sent",
            "reserved",
        }:
            return "unknown"
    return "not_attempted"


def _batch_stop_reason(ledger, batch):
    reason = batch.get("stopped_reason")
    if reason is not None:
        return reason
    remaining = batch.get("remaining_wall_seconds")
    if type(remaining) not in {int, float} or not isfinite(remaining):
        raise TypeError("A finite public batch wall-clock remainder is required")
    if remaining > 0:
        limit_reason = public_batch_limit_stop_reason(batch)
        if limit_reason is None:
            return None
        stop_for_limit = getattr(ledger, "stop_for_batch_limit", None)
        if not callable(stop_for_limit):
            raise TypeError("A durable public batch resource stop is required")
        persisted_reason = stop_for_limit(limit_reason)
        if persisted_reason is not None:
            return persisted_reason
        refreshed = ledger.batch_snapshot()
        return refreshed.get("stopped_reason")
    stop_for_deadline = getattr(ledger, "stop_for_batch_deadline", None)
    if not callable(stop_for_deadline):
        raise TypeError("A durable public batch deadline stop is required")
    stop_for_deadline()
    refreshed = ledger.batch_snapshot()
    return refreshed.get("stopped_reason") or "wall_seconds"


def _failure_diagnostics(result, failure):
    stages = {
        "environment_failure": "environment_setup",
        "agent_failure": "agent_execution",
        "tau_step_limit": "agent_execution",
        "model_profile_drift": "model_response_validation",
        "scoring_failure": "official_scoring",
        "cleanup_failure": "environment_cleanup",
        "resource_overrun": "route_deadline",
    }
    stage = getattr(result, "failure_stage", None) if result is not None else None
    exception_type = getattr(result, "exception_type", None) if result is not None else None
    diagnostic_code = (
        getattr(result, "diagnostic_code", None) if result is not None else None
    )
    if type(diagnostic_code) is not str or diagnostic_code not in DIAGNOSTIC_CODES:
        diagnostic_code = None
    if stage is None:
        stage = stages.get(failure)
    return stage, exception_type, diagnostic_code


def execute_public_route(
    *,
    root,
    supervisor,
    ledger,
    controlled_routes,
    trial_id,
    route,
    trial,
    max_machine_seconds,
    route_kwargs=None,
    ledger_phase=None,
):
    """Run and persist one ReAct/Plan first-wave route under its frozen ledger.

    ``controlled_routes`` must already receive ledgered model/gateway adapters
    through ``route_kwargs``. This function never creates an unmetered callback.
    Unknown routes are terminal and receive cleanup evidence only.
    """
    route_name = getattr(route, "value", route)
    if type(route_name) is not str or route_name not in _SUPPORTED_PUBLIC_ROUTES:
        raise ValueError("Public route is not registered")
    if type(trial_id) is not str or not trial_id.strip() or len(trial_id) > 256:
        raise ValueError("A bounded frozen trial ID is required")
    if (
        type(max_machine_seconds) not in {int, float}
        or not isfinite(max_machine_seconds)
        or max_machine_seconds <= 0
    ):
        raise ValueError("A positive finite route machine-time limit is required")
    if not isinstance(trial, PublicComparisonTrial) or trial.trial_id != trial_id:
        raise ValueError("Public route requires a comparison trial bound to the frozen manifest ID")
    plan = getattr(ledger, "plan", None)
    routes_for_trial = getattr(plan, "routes_for_trial", None)
    allowed_routes = (
        routes_for_trial(trial_id)
        if callable(routes_for_trial)
        else getattr(plan, "routes", _FIRST_WAVE_ROUTE_ORDER)
    )
    if route_name not in allowed_routes:
        raise ValueError("Public route is not scheduled for this frozen trial")
    if type(route_kwargs) is not dict:
        if route_kwargs is not None:
            raise TypeError("Route dependencies must be provided as a mapping")
        route_kwargs = {}
    candidate_sha = getattr(ledger, "candidate_sha", None)
    if (
        type(candidate_sha) is not str
        or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", candidate_sha) is None
    ):
        raise ValueError("A candidate SHA must be bound before route dispatch")
    if {"on_agent_start", "on_tau_step", "deadline_provider"} & set(route_kwargs):
        raise ValueError("Route lifecycle hooks are runner-owned and cannot be overridden")
    if (
        {"planner", "react", "semantic", "summarizer", "user_simulator", "official_judge"}
        & set(route_kwargs)
    ):
        raise ValueError("Model callbacks must come from the ledgered adapter factory")
    model_factory = route_kwargs.get("model_adapter_factory")
    if (
        not isinstance(model_factory, LedgeredModelAdapterFactory)
        or model_factory.ledger is not ledger
        or not callable(model_factory.trial_id_for)
        or model_factory.trial_id_for(trial) != trial_id
    ):
        raise ValueError("A trial-bound ledgered model adapter factory is required")
    upstream_gateway_factory = route_kwargs.get("gateway_factory")
    if not callable(upstream_gateway_factory):
        raise ValueError("A ledgered upstream tool gateway factory is required")

    def checked_gateway_factory(base_gateway, session, current_trial, current_route):
        gateway = upstream_gateway_factory(
            base_gateway, session, current_trial, current_route
        )
        if (
            not isinstance(gateway, LedgeredToolGateway)
            or gateway.ledger is not ledger
            or gateway.trial_id != trial_id
            or gateway.route != route_name
        ):
            raise ValueError("Tool gateway is not bound to this trial and route ledger")
        return gateway

    route_kwargs = {**route_kwargs, "gateway_factory": checked_gateway_factory}
    project_root = Path(root).resolve()
    if (
        not isinstance(supervisor, PublicSupervisor)
        or not supervisor.held
        or supervisor.path.resolve(strict=False)
        != canonical_public_supervisor_lock_path(project_root)
    ):
        raise ValueError("The canonical live-evaluation supervisor lock must be held")
    plan = getattr(ledger, "plan", None)
    ledger_path = getattr(ledger, "path", None)
    expected_ledger_path = (
        canonical_public_trial_ledger_path(
            project_root,
            cohort_digest=plan.cohort_digest,
            candidate_sha=candidate_sha,
            phase=ledger_phase,
        )
        if isinstance(plan, PublicRunPlan)
        else canonical_public_trial_ledger_path(project_root)
    )
    if (
        getattr(plan, "mode", None) not in {"smoke", "pilot"}
        or Path(getattr(plan, "root", "")).resolve() != project_root
        or ledger_path is None
        or Path(ledger_path).resolve(strict=False)
        != expected_ledger_path
    ):
        raise ValueError("The canonical first-wave public ledger and smoke plan are required")
    if trial.dataset == "tau-bench" and getattr(
        controlled_routes, "max_tau_steps", None
    ) != plan.resource_limits["tau_steps"]:
        raise ValueError("Controlled τ-step bound differs from the frozen trial profile")
    frozen_trials = getattr(plan, "trials", ())
    frozen_record = next(
        (
            item
            for item in frozen_trials
            if type(item) is dict and item.get("trial_id") == trial_id
        ),
        None,
    )
    if frozen_record is None or not _trial_matches_record(trial, frozen_record):
        raise ValueError("Public route trial differs from its frozen manifest record")
    if any(
        not callable(getattr(ledger, name, None))
        for name in ("route_status", "route_remaining_seconds", "batch_snapshot")
    ):
        raise TypeError("A durable public route ledger is required")
    if ledger.route_status(trial_id, route_name) != "pending":
        raise ValueError("Only an unstarted pending public route may be dispatched")

    reservation_id = ledger.start_route_with_machine_time(
        trial_id,
        route_name,
        max_seconds=max_machine_seconds,
    )
    ledger.mark_machine_time_dispatched(reservation_id)

    agent_started_at = None

    def mark_agent_started():
        nonlocal agent_started_at
        if agent_started_at is not None:
            raise ValueError("The Agent-start hook may run only once")
        ledger.mark_machine_time_agent_started(reservation_id)
        agent_started_at = perf_counter()

    def deadline_provider():
        route_remaining = ledger.route_remaining_seconds(trial_id, route_name)
        batch_remaining = ledger.batch_snapshot().get("remaining_wall_seconds")
        if (
            type(route_remaining) not in {int, float}
            or type(batch_remaining) not in {int, float}
            or not isfinite(route_remaining)
            or not isfinite(batch_remaining)
            or route_remaining <= 0
            or batch_remaining <= 0
        ):
            raise TimeoutError("Public route or batch deadline expired")
        return min(float(route_remaining), float(batch_remaining))

    def reserve_tau_step(kind):
        return ledger.reserve_tau_step(trial_id=trial_id, route=route_name, kind=kind)

    try:
        result = controlled_routes.run(
            route,
            trial,
            **route_kwargs,
            on_agent_start=mark_agent_started,
            on_tau_step=reserve_tau_step,
            deadline_provider=deadline_provider,
        )
    except Exception as exc:
        machine_seconds = (
            max(0.0, perf_counter() - agent_started_at)
            if agent_started_at is not None
            else 0.0
        )
        within_limit = ledger.settle_machine_time(
            reservation_id,
            actual_seconds=machine_seconds,
            defer_route_failure=True,
        )
        status = ledger.route_status(trial_id, route_name)
        if status != "unknown":
            status = "failed"
            stopped_reason = ledger.batch_snapshot().get("stopped_reason")
            batch_stop_category = (
                f"batch_stopped_{stopped_reason}"
                if stopped_reason in _BATCH_RESOURCE_STOP_REASONS
                else None
            )
            error_category = (
                "resource_overrun"
                if within_limit is False
                else batch_stop_category or "route_exception"
            )
            failure_stage = (
                "route_deadline"
                if within_limit is False
                else "agent_execution"
                if batch_stop_category is not None
                else "route_supervisor"
            )
            exception_type = safe_exception_type(exc)
            diagnostic_code = safe_diagnostic_code(exc)
            result_hash = digest(
                {
                    "trial_id": trial_id,
                    "route": route_name,
                    "status": status,
                    "error_category": error_category,
                    "error_type": type(exc).__name__,
                }
            )
            ledger.finish_route(
                trial_id,
                route_name,
                status=status,
                result_digest=result_hash,
                error_category=error_category,
                cleanup_verified=False,
                score_status="not_attempted",
                failure_stage=failure_stage,
                exception_type=exception_type,
                diagnostic_code=diagnostic_code,
            )
        else:
            error_category = None
            result_hash = None
            failure_stage, exception_type, diagnostic_code = _route_diagnostic_snapshot(
                ledger, trial_id, route_name
            )
            score_status = _unknown_route_score_status(ledger, trial_id, route_name)
            ledger.finish_route(
                trial_id,
                route_name,
                status="unknown",
                result_digest=None,
                cleanup_verified=False,
                score_status=score_status,
                failure_stage=failure_stage,
                exception_type=exception_type,
                diagnostic_code=diagnostic_code,
            )
        return PublicRouteReceipt(
            trial_id,
            route_name,
            status,
            score_status if status == "unknown" else "not_attempted",
            False,
            result_hash,
            None,
            machine_seconds,
            error_category,
            _route_tau_steps(ledger, trial_id, route_name),
            failure_stage,
            exception_type,
            diagnostic_code=diagnostic_code,
        )

    timing = getattr(result, "timing", None)
    machine_seconds = getattr(timing, "agent_wall_seconds", None)
    if (
        type(machine_seconds) not in {int, float}
        or not isfinite(machine_seconds)
        or machine_seconds < 0
        or getattr(timing, "agent_started", None) is not (agent_started_at is not None)
    ):
        machine_seconds = (
            max(0.0, perf_counter() - agent_started_at)
            if agent_started_at is not None
            else 0.0
        )
        within_limit = ledger.settle_machine_time(
            reservation_id,
            actual_seconds=machine_seconds,
            defer_route_failure=True,
        )
        overrun = within_limit is False
        current = ledger.route_status(trial_id, route_name)
        if current == "unknown":
            score_status = _unknown_route_score_status(ledger, trial_id, route_name)
            ledger.finish_route(
                trial_id,
                route_name,
                status="unknown",
                result_digest=None,
                cleanup_verified=False,
                score_status=score_status,
            )
            failure_stage, exception_type, diagnostic_code = _route_diagnostic_snapshot(
                ledger, trial_id, route_name
            )
            return PublicRouteReceipt(
                trial_id,
                route_name,
                "unknown",
                score_status,
                False,
                None,
                None,
                machine_seconds,
                None,
                _route_tau_steps(ledger, trial_id, route_name),
                failure_stage,
                exception_type,
                diagnostic_code=diagnostic_code,
            )
        result = None
        failure = "invalid_route_timing"
        score_evidence = None
        cleanup_verified, cleanup_digest = False, None
        score_status = "not_attempted"
    else:
        within_limit = ledger.settle_machine_time(
            reservation_id,
            actual_seconds=machine_seconds,
            defer_route_failure=True,
        )
        overrun = within_limit is False
        failure = _failure_category(getattr(result, "failure_kind", None))
        score_evidence = _score_evidence(trial, result)
        cleanup_verified, cleanup_digest = _cleanup_evidence(result)
        score_status = (
            "scored"
            if score_evidence is not None
            else "failed"
            if failure == "scoring_failure"
            else "not_attempted"
        )

    current = ledger.route_status(trial_id, route_name)
    if current == "unknown":
        score_status = _unknown_route_score_status(ledger, trial_id, route_name)
        ledger.finish_route(
            trial_id,
            route_name,
            status="unknown",
            result_digest=None,
            cleanup_verified=cleanup_verified,
            cleanup_evidence_digest=cleanup_digest,
            score_status=score_status,
        )
        failure_stage, exception_type, diagnostic_code = _route_diagnostic_snapshot(
            ledger, trial_id, route_name
        )
        return PublicRouteReceipt(
            trial_id, route_name, "unknown", score_status, cleanup_verified,
            None, None, machine_seconds, None,
            _route_tau_steps(ledger, trial_id, route_name),
            failure_stage,
            exception_type,
            diagnostic_code=diagnostic_code,
        )

    tau_steps = _route_tau_steps(ledger, trial_id, route_name)
    result_tau_steps = getattr(result, "tau_steps", None)
    if type(result_tau_steps) is not int or result_tau_steps != tau_steps:
        failure = "tau_step_evidence_mismatch"
        score_status = "not_attempted" if score_status == "not_attempted" else score_status
        result = None
    run = getattr(result, "run", None) if result is not None else None
    success = (
        getattr(result, "status", None) == "complete"
        and getattr(run, "task_success", False) is True
        and score_evidence is not None
        and score_evidence["success"] is True
        and cleanup_verified
    )
    status = "completed" if success and not overrun else (
        "scoring_error" if score_status == "failed" else "failed"
    )
    if overrun:
        failure = "resource_overrun"
    failure_stage, exception_type, diagnostic_code = _failure_diagnostics(
        result, failure
    )
    batch_stop_reason = ledger.batch_snapshot().get("stopped_reason")
    if (
        not success
        and not overrun
        and batch_stop_reason in _BATCH_RESOURCE_STOP_REASONS
    ):
        failure = f"batch_stopped_{batch_stop_reason}"
        failure_stage = "agent_execution"
    if overrun:
        failure_stage = "route_deadline"
    if diagnostic_code is not None and not success and not overrun:
        detailed_failure = f"{failure}_{diagnostic_code}"
        if _ERROR_CATEGORY.fullmatch(detailed_failure):
            failure = detailed_failure
    score_hash = digest(score_evidence) if score_evidence is not None else None
    result_hash = digest(
        {
            "trial_id": trial_id,
            "route": route_name,
            "execution_status": getattr(result, "status", None),
            "task_success": getattr(run, "task_success", False) is True,
            "score_digest": score_hash,
            "cleanup_verified": cleanup_verified,
            "failure_kind": getattr(result, "failure_kind", None),
            "diagnostic_code": diagnostic_code,
            "tau_steps": tau_steps,
        }
    )
    if not success and failure == "route_failed":
        failure = "incomplete_route"
    ledger.finish_route(
        trial_id,
        route_name,
        status=status,
        result_digest=result_hash,
        error_category=None if status == "completed" else failure,
        cleanup_verified=cleanup_verified,
        cleanup_evidence_digest=cleanup_digest,
        score_status=score_status,
        score_digest=score_hash,
        score_evidence=score_evidence,
        failure_stage=None if status == "completed" else failure_stage,
        exception_type=None if status == "completed" else exception_type,
        diagnostic_code=None if status == "completed" else diagnostic_code,
    )
    return PublicRouteReceipt(
        trial_id,
        route_name,
        status,
        score_status,
        cleanup_verified,
        result_hash,
        score_hash,
        machine_seconds,
        None if status == "completed" else failure,
        tau_steps,
        None if status == "completed" else failure_stage,
        None if status == "completed" else exception_type,
        *_comparison_evidence(result),
        None,
        None if status == "completed" else diagnostic_code,
    )


def execute_public_phase(
    *,
    root,
    supervisor,
    ledger,
    controlled_routes,
    phase,
    max_machine_seconds,
    trial_factory,
    route_kwargs_factory,
):
    """Execute or resume the frozen first-wave ReAct/Plan slots in order.

    The caller supplies isolated trial and ledger-bound callback factories;
    this scheduler owns the fixed order and skips every non-pending route. It
    deliberately supports only the first-wave phase until the wider cohort
    phases have their own live gates.
    """
    if phase not in {
        "diagnostic", "openrouter-diagnostic", "openrouter-csm-diagnostic",
        "first-wave", "pilot"
    }:
        raise ValueError("Live phase scheduler requires a registered frozen phase")
    if phase == "diagnostic":
        batch = getattr(ledger, "plan", None)
        limits = getattr(batch, "batch_limits", {})
        if (
            getattr(batch, "mode", None) != "smoke"
            or len(getattr(batch, "trials", ())) != 4
            or tuple(getattr(batch, "routes", ())) != _DIAGNOSTIC_ROUTE_ORDER
            or limits.get("qwen_requests") != 6
            or limits.get("qwen_tokens") != 600_000
            or limits.get("wall_seconds") != 2_700
        ):
            raise ValueError("Diagnostic dispatch requires its bounded four-case cohort")
    elif phase == "openrouter-diagnostic":
        from evals.benchmarks.pilot_gate import is_bounded_openrouter_diagnostic_plan

        if not is_bounded_openrouter_diagnostic_plan(getattr(ledger, "plan", None)):
            raise ValueError("OpenRouter dispatch requires its bounded one-case cohort")
    elif phase == "openrouter-csm-diagnostic":
        from evals.benchmarks.pilot_gate import (
            is_bounded_openrouter_csm_diagnostic_plan,
        )

        if not is_bounded_openrouter_csm_diagnostic_plan(getattr(ledger, "plan", None)):
            raise ValueError(
                "Independent OpenRouter CSM dispatch requires its fixed bounded cohort"
            )
    project_root = Path(root).resolve()
    if (
        not isinstance(supervisor, PublicSupervisor)
        or not supervisor.held
        or supervisor.path.resolve(strict=False)
        != canonical_public_supervisor_lock_path(project_root)
    ):
        raise ValueError("The canonical live-evaluation supervisor lock must be held")
    plan = getattr(ledger, "plan", None)
    ledger_path = getattr(ledger, "path", None)
    candidate_sha = getattr(ledger, "candidate_sha", None)
    expected_ledger_path = (
        canonical_public_trial_ledger_path(
            project_root,
            cohort_digest=plan.cohort_digest,
            candidate_sha=candidate_sha,
            phase=(phase if phase == "openrouter-csm-diagnostic" else None),
        )
        if isinstance(plan, PublicRunPlan)
        and type(candidate_sha) is str
        else canonical_public_trial_ledger_path(project_root)
    )
    if (
        not isinstance(plan, PublicRunPlan)
        or (phase == "first-wave" and plan.mode != "smoke")
        or (phase == "openrouter-diagnostic" and plan.mode != "smoke")
        or (phase == "openrouter-csm-diagnostic" and plan.mode != "smoke")
        or (phase == "pilot" and (plan.mode != "pilot" or plan.route_schedule is None))
        or Path(plan.root).resolve() != project_root
        or (
            phase == "first-wave"
            and (plan.routes != _FIRST_WAVE_ROUTE_ORDER or len(plan.initial_smoke_wave) != 4)
        )
        or (phase == "pilot" and (len(plan.trials) != 24 or plan.route_slot_count() != 68))
        or ledger_path is None
        or Path(ledger_path).resolve(strict=False)
        != expected_ledger_path
    ):
        raise ValueError("The canonical frozen first-wave smoke cohort is required")
    if not callable(trial_factory) or not callable(route_kwargs_factory):
        raise TypeError("Isolated trial and route dependency factories are required")
    if not callable(getattr(ledger, "batch_snapshot", None)):
        raise TypeError("A durable public batch ledger is required")

    trials_by_id = {trial["trial_id"]: trial for trial in plan.trials}
    selected_records = plan.initial_smoke_wave if phase == "first-wave" else plan.trials
    selected_ids = [trial["trial_id"] for trial in selected_records]
    if (
        len(set(selected_ids)) != len(selected_ids)
        or any(trial_id not in trials_by_id for trial_id in selected_ids)
    ):
        raise ValueError("Frozen first-wave trial selection is incomplete or duplicated")

    receipts = []
    skipped = []
    stopped_reason = None
    loaded_trials = {}
    semantic_references = {}
    for trial_id in selected_ids:
        frozen_trial = trials_by_id[trial_id]
        route_order = (
            _FIRST_WAVE_ROUTE_ORDER
            if phase == "first-wave"
            else plan.routes_for_trial(trial_id)
        )
        for route_name in route_order:
            status = ledger.route_status(trial_id, route_name)
            if status in _TERMINAL_ROUTE_STATUSES:
                skipped.append((trial_id, route_name, status))
                continue
            if status != "pending":
                raise ValueError(
                    "A nonterminal route must be recovered before phase scheduling"
                )

            semantic_reference = semantic_references.get(trial_id)
            if route_name == Route.PLAN_JEV_SEMANTIC.value:
                if semantic_reference is None:
                    primary_receipt = next(
                        (
                            item
                            for item in reversed(receipts)
                            if item.trial_id == trial_id
                            and item.route == Route.PLAN_PRIMARY_SEMANTIC.value
                        ),
                        None,
                    )
                    if (
                        primary_receipt is not None
                        and primary_receipt.status == "completed"
                        and primary_receipt.plan_digest is not None
                        and primary_receipt.decision_input_digest is None
                    ):
                        receipts.append(
                            _mark_semantic_route_not_applicable(
                                ledger,
                                trial_id,
                                route_name,
                                "semantic_decision_not_observed",
                            )
                        )
                    elif primary_receipt is not None:
                        receipts.append(
                            _mark_semantic_route_incomplete(
                                ledger, trial_id, route_name
                            )
                        )
                    else:
                        primary_status = ledger.route_status(
                            trial_id, Route.PLAN_PRIMARY_SEMANTIC.value
                        )
                        if primary_status in _TERMINAL_ROUTE_STATUSES:
                            # A resumed scheduler cannot recover the original in-memory
                            # plan from a terminal primary route. Keep the pair visible
                            # as incomplete instead of claiming Jev was not applicable.
                            receipts.append(
                                _mark_semantic_route_incomplete(
                                    ledger, trial_id, route_name
                                )
                            )
                        else:
                            receipts.append(
                                _mark_semantic_route_not_applicable(
                                    ledger,
                                    trial_id,
                                    route_name,
                                    "semantic_reference_unavailable",
                                )
                            )
                    continue

            batch = ledger.batch_snapshot()
            stopped_reason = _batch_stop_reason(ledger, batch)
            if stopped_reason is not None:
                skipped.append((trial_id, route_name, "batch_stopped"))
                continue

            trial = loaded_trials.get(trial_id)
            if trial is None:
                trial = trial_factory(frozen_trial)
                if not _trial_matches_record(trial, frozen_trial):
                    raise ValueError("Isolated trial factory changed the frozen trial ID")
                loaded_trials[trial_id] = trial
            route_kwargs = route_kwargs_factory(trial, route_name)
            if type(route_kwargs) is not dict:
                raise TypeError("Route dependencies must be returned as a mapping")
            if route_name == Route.PLAN_JEV_SEMANTIC.value:
                route_kwargs = {**route_kwargs, "frozen_plan": semantic_reference.plan}
            batch = ledger.batch_snapshot()
            stopped_reason = _batch_stop_reason(ledger, batch)
            if stopped_reason is not None:
                skipped.append((trial_id, route_name, "batch_stopped"))
                continue
            try:
                route_receipt = execute_public_route(
                    root=project_root,
                    supervisor=supervisor,
                    ledger=ledger,
                    controlled_routes=controlled_routes,
                    trial_id=trial_id,
                    route=route_name,
                    trial=trial,
                    max_machine_seconds=max_machine_seconds,
                    route_kwargs=route_kwargs,
                    ledger_phase=(
                        phase if phase == "openrouter-csm-diagnostic" else None
                    ),
                )
            except ValueError:
                # The ledger atomically checks the absolute deadline when claiming a
                # route. If it expires between this snapshot and that claim, preserve
                # the stop and return an auditable skipped receipt.
                batch = ledger.batch_snapshot()
                stopped_reason = _batch_stop_reason(ledger, batch)
                if stopped_reason is None:
                    raise
                skipped.append((trial_id, route_name, "batch_stopped"))
                continue
            if route_name == Route.PLAN_PRIMARY_SEMANTIC.value:
                candidate_plan = getattr(controlled_routes, "last_plan", None)
                if (
                    candidate_plan is not None
                    and route_receipt.plan_digest == getattr(candidate_plan, "digest", None)
                    and route_receipt.decision_input_digest is not None
                    and route_receipt.evidence_scope == "full_workflow"
                ):
                    semantic_references[trial_id] = _SemanticPlanReference(
                        plan=candidate_plan,
                        receipt=route_receipt,
                    )
            elif route_name == Route.PLAN_JEV_SEMANTIC.value:
                reference_receipt = semantic_reference.receipt
                is_paired = (
                    route_receipt.plan_digest == reference_receipt.plan_digest
                    and route_receipt.decision_input_digest
                    == reference_receipt.decision_input_digest
                    and route_receipt.evidence_scope == "execution_only"
                )
                route_receipt = replace(
                    route_receipt,
                    pair_status="paired" if is_paired else (
                        "mismatch"
                        if route_receipt.plan_digest is not None
                        and route_receipt.decision_input_digest is not None
                        else "incomplete"
                    ),
                )
            receipts.append(route_receipt)
            stopped_reason = ledger.batch_snapshot().get("stopped_reason")

    return PublicPhaseReceipt(
        phase=phase,
        cohort_digest=plan.cohort_digest,
        scheduled_route_slots=sum(
            len(_FIRST_WAVE_ROUTE_ORDER)
            if phase == "first-wave"
            else len(plan.routes_for_trial(trial_id))
            for trial_id in selected_ids
        ),
        executed_routes=tuple(receipts),
        skipped_routes=tuple(skipped),
        stopped_reason=stopped_reason,
    )

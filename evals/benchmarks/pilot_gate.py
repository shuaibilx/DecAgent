"""Bounded four-domain diagnostic and fail-closed gate for the public pilot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from decagent.decisions.contracts import digest
from decagent.evaluation.business.public_limits import (
    validate_public_batch_limits,
)
from decagent.evaluation.business.public_trials import PublicRunPlan
from decagent.evaluation.business.public_weekly_budget import (
    PUBLIC_EVALUATION_BUDGET_ID,
)

DIAGNOSTIC_LIMITS = {
    "max_general_requests": 12,
    "max_qwen_requests": 6,
    "max_openrouter_requests": 6,
    "max_tokens": 600_000,
    "max_wall_seconds": 2_700,
}
_DIAGNOSTIC_ROUTES = ("react_skill_mcp", "plan_mcp")
_GENERAL_ROLES = frozenset({"primary", "user_simulator", "judge"})
_SCORE_SOURCES = frozenset(
    {
        "enterpriseops-gym-original-verifier",
        "tau2-bench-original-evaluate-simulation",
    }
)
DIAGNOSTIC_BATCH_LIMITS = {
    "qwen_requests": 6,
    "qwen_tokens": 600_000,
    "qwen_cost_cny": "13.50",
    "agent_qwen_requests": 3,
    "agent_qwen_tokens": 400_000,
    "user_simulator_qwen_requests": 2,
    "user_simulator_qwen_tokens": 150_000,
    "judge_qwen_requests": 1,
    "judge_qwen_tokens": 50_000,
    "tool_calls": 1_600,
    "wall_seconds": 2_700,
}
DIAGNOSTIC_RESOURCE_LIMITS = {
    "agent_model_requests": 6,
    "agent_tokens": 600_000,
    "tool_calls": 200,
    "agent_machine_seconds": 2_700,
    "tau_steps": 200,
    "user_simulator_requests": 6,
    "user_simulator_tokens": 600_000,
    "judge_requests": 6,
    "judge_tokens": 600_000,
}
WEEKLY_QWEN_COST_CNY = Decimal("13.50")


def build_bounded_diagnostic_plan(plan):
    """Derive a separate four-case ledger with the 6-request Qwen hard stop."""
    if (
        not isinstance(plan, PublicRunPlan)
        or plan.mode != "smoke"
        or len(plan.initial_smoke_wave) != 4
        or tuple(plan.routes) != _DIAGNOSTIC_ROUTES
    ):
        raise ValueError("A frozen four-domain smoke plan is required for diagnostics")
    records_by_id = {record["trial_id"]: record for record in plan.trials}
    selected = tuple(
        records_by_id[item["trial_id"]]
        for item in plan.initial_smoke_wave
        if item["trial_id"] in records_by_id
    )
    if len(selected) != 4:
        raise ValueError("The fixed four-domain diagnostic cases are incomplete")

    limits = dict(DIAGNOSTIC_RESOURCE_LIMITS)
    batch_limits = dict(DIAGNOSTIC_BATCH_LIMITS)
    diagnostic_manifest = digest(
        {
            "schema_version": "public-evaluation-diagnostic-plan-v1",
            "source_manifest_digest": plan.manifest_digest,
            "source_manifest_sha256": plan.source_manifest_digest,
            "task_inventory_digest": plan.inventory_digest,
            "model_profiles_digest": plan.profiles.fingerprint,
            "trial_ids": [item["trial_id"] for item in selected],
            "routes": list(_DIAGNOSTIC_ROUTES),
            "resource_limits": limits,
            "batch_limits": batch_limits,
        }
    )
    return replace(
        plan,
        manifest_digest=diagnostic_manifest,
        trials=selected,
        initial_smoke_wave=selected,
        resource_limits=limits,
        batch_limits=batch_limits,
        route_schedule=None,
    )


def is_bounded_diagnostic_plan(plan):
    """Check that a dispatch plan cannot inherit the 500-call smoke budget."""
    if not isinstance(plan, PublicRunPlan) or plan.mode != "smoke":
        return False
    if (
        len(plan.trials) != 4
        or len(plan.initial_smoke_wave) != 4
        or tuple(plan.routes) != _DIAGNOSTIC_ROUTES
        or [item.get("trial_id") for item in plan.trials]
        != [item.get("trial_id") for item in plan.initial_smoke_wave]
    ):
        return False
    limits = plan.batch_limits
    return (
        limits == DIAGNOSTIC_BATCH_LIMITS
        and plan.resource_limits == DIAGNOSTIC_RESOURCE_LIMITS
    )


def remaining_pilot_batch_limits(approved_limits, diagnostic_usage):
    """Subtract diagnostic usage so diagnostic and pilot share one weekly ceiling."""
    if type(diagnostic_usage) is not dict:
        raise ValueError("A frozen approved budget and diagnostic usage are required")
    approved_limits = validate_public_batch_limits(approved_limits)
    role_usage = diagnostic_usage.get("role_usage")
    role_names = ("agent", "user_simulator", "judge")
    if type(role_usage) is not dict or set(role_usage) != set(role_names):
        raise ValueError("Diagnostic role usage is incomplete")

    consumed = {}
    for role in role_names:
        usage = role_usage[role]
        if type(usage) is not dict:
            raise ValueError("Diagnostic role usage is malformed")
        requests, tokens = usage.get("requests"), usage.get("tokens")
        if (
            type(requests) is not int
            or requests < 0
            or type(tokens) is not int
            or tokens < 0
        ):
            raise ValueError("Diagnostic role usage must be nonnegative integers")
        consumed[f"{role}_qwen_requests"] = requests
        consumed[f"{role}_qwen_tokens"] = tokens

    try:
        qwen_cost = Decimal(diagnostic_usage["qwen_cost_cny"])
        qwen_cost_limit = WEEKLY_QWEN_COST_CNY
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Diagnostic Qwen cumulative fee is unavailable") from exc
    qwen_requests = diagnostic_usage.get("qwen_requests")
    qwen_tokens = diagnostic_usage.get("qwen_tokens")
    general_requests = diagnostic_usage.get("general_model_requests")
    tool_calls = diagnostic_usage.get("tool_calls")
    elapsed = diagnostic_usage.get("diagnostic_elapsed_seconds")
    if (
        type(qwen_requests) is not int
        or type(qwen_tokens) is not int
        or type(general_requests) is not int
        or type(tool_calls) is not int
        or min(qwen_requests, qwen_tokens, general_requests, tool_calls) < 0
        or type(elapsed) not in {int, float}
        or not math.isfinite(elapsed)
        or elapsed < 0
        or not qwen_cost.is_finite()
        or qwen_cost < 0
        or qwen_cost > qwen_cost_limit
        or general_requests != qwen_requests
        or diagnostic_usage.get("general_model_tokens") != qwen_tokens
        or sum(consumed[f"{role}_qwen_requests"] for role in role_names)
        != qwen_requests
        or sum(consumed[f"{role}_qwen_tokens"] for role in role_names) != qwen_tokens
    ):
        raise ValueError("Diagnostic usage cannot be applied to the frozen Qwen pilot")

    remaining = dict(approved_limits)
    for name, used in consumed.items():
        remaining[name] -= used
    remaining["qwen_requests"] -= qwen_requests
    remaining["qwen_tokens"] -= qwen_tokens
    remaining["tool_calls"] -= tool_calls
    remaining["wall_seconds"] -= math.ceil(elapsed)
    remaining_cost = qwen_cost_limit - qwen_cost
    if any(type(value) is not int or value < 1 for value in remaining.values()):
        raise ValueError("Diagnostic usage exhausted the remaining pilot resource budget")
    if remaining_cost <= 0:
        raise ValueError("Diagnostic usage exhausted the remaining Qwen fee budget")
    remaining["qwen_cost_cny"] = format(remaining_cost, "f")
    return remaining


def apply_remaining_pilot_budget(plan, diagnostic_usage):
    """Return a pilot plan capped by the budget left after its diagnostic phase."""
    if not isinstance(plan, PublicRunPlan) or plan.mode != "pilot":
        raise ValueError("A frozen public pilot plan is required")
    return replace(
        plan,
        batch_limits=remaining_pilot_batch_limits(
            plan.batch_limits, diagnostic_usage
        ),
    )


def evaluation_source_digest(root):
    """Hash executable Agent/evaluator code while excluding generated manifests/docs."""
    root = Path(root).resolve()
    selected = []
    for base in (root / "decagent", root / "evals" / "benchmarks"):
        if not base.is_dir() or base.is_symlink():
            raise ValueError("Evaluation source tree is unavailable")
        for path in base.rglob("*.py"):
            if path.is_symlink():
                raise ValueError("Evaluation source tree cannot contain symlinks")
            selected.append(path)
    for relative in ("pyproject.toml", "evals/benchmarks/requirements.txt"):
        path = root / relative
        if path.is_file():
            selected.append(path)
    entries = []
    for path in sorted(set(selected), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not entries:
        raise ValueError("Evaluation source digest cannot be empty")
    return digest(entries)


def _read_report(root, evidence_path):
    root = Path(root).resolve()
    evidence_root = root / ".decagent/evaluation/evidence"
    requested = Path(evidence_path)
    requested = root / requested if not requested.is_absolute() else requested
    if ".." in requested.parts:
        raise ValueError("Diagnostic evidence path cannot contain parent traversal")
    lexical_path = Path(os.path.abspath(requested))
    try:
        lexical_relative = lexical_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Diagnostic evidence must be inside the project root") from exc
    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Diagnostic evidence path cannot contain symlinks")
    target = lexical_path.resolve(strict=False)
    if not target.is_relative_to(evidence_root) or target.suffix.lower() != ".json":
        raise ValueError(
            "Diagnostic evidence must be a JSON file in the ignored .decagent evidence directory"
        )
    if not target.is_file():
        raise ValueError("Diagnostic evidence file is unavailable")
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Diagnostic evidence is unreadable") from exc
    if type(report) is not dict:
        raise ValueError("Diagnostic evidence must contain a JSON object")
    return target, report


def validate_diagnostic_evidence(
    root,
    evidence_path,
    *,
    candidate_sha=None,
    model_profiles_digest,
    expected_source_digest=None,
    expected_source_manifest_digest=None,
    expected_inventory_digest=None,
):
    """Require bounded, same-source evidence of real MCP execution and official scoring."""
    _path, report = _read_report(root, evidence_path)
    if (
        report.get("schema_version") != "public-evaluation-live-diagnostic-v1"
        or report.get("phase") != "diagnostic"
        or report.get("dry_run") is not False
        or report.get("evaluation_budget_id") != PUBLIC_EVALUATION_BUDGET_ID
    ):
        raise ValueError("A completed live diagnostic report is required")
    diagnostic_sha = report.get("candidate_sha")
    if (
        type(diagnostic_sha) is not str
        or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", diagnostic_sha) is None
        or (candidate_sha is not None and diagnostic_sha != candidate_sha)
    ):
        raise ValueError("Diagnostic evidence candidate identity is invalid")
    if (
        report.get("working_tree_dirty") is not False
        or report.get("candidate_ci_verified") is not True
    ):
        raise ValueError("Diagnostic evidence must come from a clean CI-verified candidate")
    if report.get("model_profiles_digest") != model_profiles_digest:
        raise ValueError("Diagnostic model profiles differ from the frozen pilot profiles")
    if (
        expected_source_manifest_digest is not None
        and report.get("source_manifest_digest") != expected_source_manifest_digest
    ):
        raise ValueError("Diagnostic benchmark source differs from the frozen pilot")
    if (
        expected_inventory_digest is not None
        and report.get("task_inventory_digest") != expected_inventory_digest
    ):
        raise ValueError("Diagnostic task inventory differs from the frozen pilot")
    source_digest = expected_source_digest or evaluation_source_digest(root)
    if report.get("evaluation_source_digest") != source_digest:
        raise ValueError("Diagnostic executable source differs from the pilot candidate")
    if (
        report.get("diagnostic_limits") != DIAGNOSTIC_LIMITS
        or report.get("diagnostic_batch_limits") != DIAGNOSTIC_BATCH_LIMITS
        or report.get("diagnostic_resource_limits") != DIAGNOSTIC_RESOURCE_LIMITS
    ):
        raise ValueError("Diagnostic report does not carry the approved hard limits")
    elapsed = report.get("diagnostic_elapsed_seconds")
    if (
        type(elapsed) not in {int, float}
        or not 0 <= elapsed <= DIAGNOSTIC_LIMITS["max_wall_seconds"]
    ):
        raise ValueError("Diagnostic wall-time hard stop was exceeded or is missing")
    if (
        report.get("task_denominator") != 4
        or report.get("selected_task_count") != 4
        or report.get("route_denominator") != 8
        or report.get("selected_route_slots") != 8
    ):
        raise ValueError("Diagnostic report must contain the fixed four-domain/eight-route slice")
    task_records = report.get("diagnostic_task_records")
    expected_domains = {
        ("enterpriseops-gym", "csm", "oracle"),
        ("enterpriseops-gym", "itsm", "oracle"),
        ("tau-bench", "retail", "train"),
        ("tau-bench", "telecom", "train"),
    }
    if (
        type(task_records) is not list
        or len(task_records) != 4
        or len({item.get("trial_id") for item in task_records if type(item) is dict}) != 4
        or {
            (item.get("benchmark"), item.get("domain"), item.get("split"))
            for item in task_records
            if type(item) is dict
        }
        != expected_domains
    ):
        raise ValueError(
            "Diagnostic report does not contain the fixed four-domain development cases"
        )

    batch = report.get("batch")
    if type(batch) is not dict:
        raise ValueError("Diagnostic batch ledger evidence is missing")
    batch_limits = batch.get("limits")
    if type(batch_limits) is not dict or batch_limits != DIAGNOSTIC_BATCH_LIMITS:
        raise ValueError("Diagnostic ledger did not enforce the bounded Qwen cohort")
    qwen_requests = batch.get("qwen_requests")
    qwen_tokens = batch.get("qwen_tokens")
    qwen_actual_tokens = batch.get("qwen_actual_tokens")
    qwen_reserved_tokens = batch.get("qwen_reserved_tokens")
    if (
        type(qwen_requests) is not int
        or not 0 <= qwen_requests <= DIAGNOSTIC_LIMITS["max_qwen_requests"]
        or type(qwen_tokens) is not int
        or not 0 <= qwen_tokens <= DIAGNOSTIC_LIMITS["max_tokens"]
        or type(qwen_actual_tokens) is not int
        or type(qwen_reserved_tokens) is not int
        or qwen_actual_tokens < 0
        or qwen_reserved_tokens < 0
        or qwen_tokens != qwen_actual_tokens + qwen_reserved_tokens
    ):
        raise ValueError("Diagnostic Qwen request or token hard stop was exceeded")
    def cny_amount(value):
        if type(value) is not str:
            raise ValueError("Diagnostic Qwen fees must be decimal strings")
        return Decimal(value)

    try:
        qwen_actual_cost = cny_amount(batch["qwen_actual_cost_cny"])
        qwen_reserved_cost = cny_amount(batch["qwen_reserved_cost_cny"])
        qwen_total_cost = cny_amount(batch["qwen_cost_cny"])
        qwen_cost_limit = Decimal(DIAGNOSTIC_BATCH_LIMITS["qwen_cost_cny"])
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Diagnostic Qwen fee evidence is incomplete") from exc
    if (
        any(not value.is_finite() or value < 0 for value in (
            qwen_actual_cost, qwen_reserved_cost, qwen_total_cost, qwen_cost_limit
        ))
        or qwen_total_cost != qwen_actual_cost + qwen_reserved_cost
        or qwen_total_cost > qwen_cost_limit
    ):
        raise ValueError("Diagnostic Qwen fee hard stop was exceeded or misreported")

    routes = report.get("routes")
    if type(routes) is not dict or len(routes) != 8:
        raise ValueError("Diagnostic route evidence must preserve all eight route slots")
    attempts = 0
    qwen_attempts = 0
    openrouter_attempts = 0
    actual_tokens = 0
    qwen_actual_tokens = 0
    observed_qwen_actual_cost = Decimal(0)
    observed_qwen_reserved_cost = Decimal(0)
    observed_reserved_tokens = 0
    unknown_attempts = 0
    unknown_by_route_role = {}
    observed_requests_by_route_role = {}
    observed_tokens_by_route_role = {}
    observed_unknown_by_route_role = {}
    role_usage_totals = {
        "agent": {"requests": 0, "tokens": 0},
        "user_simulator": {"requests": 0, "tokens": 0},
        "judge": {"requests": 0, "tokens": 0},
    }
    observed_tool_calls = 0
    closed_loop_routes = 0
    expected_task_ids = {item["trial_id"] for item in task_records}
    observed_route_keys = set()
    for route_key, route_record in routes.items():
        if type(route_record) is not dict:
            raise ValueError("Diagnostic route evidence is malformed")
        trial_id = route_record.get("trial_id")
        route = route_record.get("route")
        expected_key = f"{trial_id}:{route}"
        if (
            type(trial_id) is not str
            or trial_id not in expected_task_ids
            or route not in {"react_skill_mcp", "plan_mcp"}
            or route_key != expected_key
            or route_key in observed_route_keys
        ):
            raise ValueError("Diagnostic route identity differs from its frozen task slot")
        observed_route_keys.add(route_key)
        model_attempts = route_record.get("model_attempts")
        if type(model_attempts) is not list:
            raise ValueError("Diagnostic model attempt evidence is missing")
        route_usage = route_record.get("usage")
        if type(route_usage) is not dict:
            raise ValueError("Diagnostic role usage evidence is missing")
        for attempt in model_attempts:
            if type(attempt) is not dict or attempt.get("role") not in _GENERAL_ROLES:
                raise ValueError("Diagnostic routes may use only general-model roles")
            status = attempt.get("status")
            if status == "not_sent":
                if (
                    attempt.get("actual_input_tokens") is not None
                    or attempt.get("actual_output_tokens") is not None
                ):
                    raise ValueError("Not-sent diagnostic attempts cannot carry usage")
                continue
            attempts += 1
            attempt_key = (
                route_record.get("trial_id"),
                route_record.get("route"),
                attempt["role"],
            )
            observed_requests_by_route_role[attempt_key] = (
                observed_requests_by_route_role.get(attempt_key, 0) + 1
            )
            provider = str(attempt.get("actual_provider") or attempt.get("provider") or "").lower()
            if provider in {"qwen", "dashscope"}:
                qwen_attempts += 1
            elif provider in {"openrouter", "open_router"}:
                openrouter_attempts += 1
            else:
                raise ValueError("Diagnostic attempt provider is not allowlisted")
            if status in {"reserved", "dispatched"}:
                raise ValueError("Diagnostic evidence contains an unresolved model request")
            input_tokens = attempt.get("actual_input_tokens")
            output_tokens = attempt.get("actual_output_tokens")
            if status == "settled":
                if (
                    type(input_tokens) is not int
                    or type(output_tokens) is not int
                    or input_tokens < 0
                    or output_tokens < 0
                ):
                    raise ValueError("Diagnostic model usage is incomplete")
                actual_tokens += input_tokens + output_tokens
                if provider in {"qwen", "dashscope"}:
                    qwen_actual_tokens += input_tokens + output_tokens
                    amount = attempt.get("actual_cost_amount")
                    currency = attempt.get("actual_cost_currency")
                    try:
                        cost = cny_amount(amount)
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise ValueError("Diagnostic Qwen fee evidence is incomplete") from exc
                    if currency != "CNY" or not cost.is_finite() or cost < 0:
                        raise ValueError("Diagnostic Qwen fee currency or amount is invalid")
                    observed_qwen_actual_cost += cost
                observed_tokens_by_route_role[attempt_key] = (
                    observed_tokens_by_route_role.get(attempt_key, 0)
                    + input_tokens
                    + output_tokens
                )
            elif status == "unknown":
                if (
                    provider not in {"qwen", "dashscope"}
                    or input_tokens is not None
                    or output_tokens is not None
                ):
                    raise ValueError("Unknown diagnostic usage is not safely reserved")
                unknown_attempts += 1
                unknown_key = (
                    route_record.get("trial_id"),
                    route_record.get("route"),
                    attempt["role"],
                )
                unknown_by_route_role[unknown_key] = (
                    unknown_by_route_role.get(
                        unknown_key, 0
                    )
                    + 1
                )
                observed_unknown_by_route_role[unknown_key] = (
                    observed_unknown_by_route_role.get(unknown_key, 0) + 1
                )
                amount = attempt.get("reserved_cost_amount")
                currency = attempt.get("reserved_cost_currency")
                try:
                    cost = cny_amount(amount)
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError("Unknown Qwen fee reservation is missing") from exc
                if currency != "CNY" or not cost.is_finite() or cost < 0:
                    raise ValueError("Unknown Qwen fee reservation is invalid")
                observed_qwen_reserved_cost += cost
            else:
                raise ValueError("Diagnostic model attempt status is unsupported")
        for role in _GENERAL_ROLES:
            role_usage = route_usage.get(role, {})
            if type(role_usage) is not dict:
                raise ValueError("Diagnostic role usage evidence is malformed")
            requests = role_usage.get("requests")
            actual = role_usage.get("actual_tokens")
            reserved = role_usage.get("reserved_tokens", 0)
            unknown_count = role_usage.get("unknown_requests", 0)
            if (
                type(requests) is not int
                or requests < 0
                or type(actual) is not int
                or actual < 0
                or type(reserved) is not int
                or reserved < 0
                or type(unknown_count) is not int
                or unknown_count < 0
            ):
                raise ValueError("Diagnostic reserved usage is malformed")
            observed_reserved_tokens += reserved
            unknown_key = (
                route_record.get("trial_id"),
                route_record.get("route"),
                role,
            )
            if unknown_by_route_role.get(unknown_key, 0) > unknown_count:
                raise ValueError("Unknown diagnostic request lacks ledger reservation evidence")
            if (
                requests != observed_requests_by_route_role.get(unknown_key, 0)
                or actual != observed_tokens_by_route_role.get(unknown_key, 0)
                or unknown_count != observed_unknown_by_route_role.get(unknown_key, 0)
            ):
                raise ValueError("Diagnostic role usage differs from per-request ledger evidence")
            role_bucket = {
                "primary": "agent",
                "user_simulator": "user_simulator",
                "judge": "judge",
            }[role]
            role_usage_totals[role_bucket]["requests"] += requests
            role_usage_totals[role_bucket]["tokens"] += actual + reserved
        lifecycle = route_record.get("lifecycle")
        receipt = route_record.get("execution_receipt")
        resources = route_record.get("resources")
        if type(resources) is not dict or type(resources.get("tool_calls")) is not int:
            raise ValueError("Diagnostic tool usage evidence is malformed")
        if resources["tool_calls"] < 0:
            raise ValueError("Diagnostic tool usage cannot be negative")
        observed_tool_calls += resources["tool_calls"]
        score_evidence = lifecycle.get("score_evidence") if type(lifecycle) is dict else None
        primary_model_settled = any(
            type(attempt) is dict
            and attempt.get("role") == "primary"
            and str(
                attempt.get("actual_provider") or attempt.get("provider") or ""
            ).lower()
            in {"qwen", "dashscope"}
            and attempt.get("status") == "settled"
            and type(attempt.get("actual_input_tokens")) is int
            and type(attempt.get("actual_output_tokens")) is int
            for attempt in model_attempts
        )
        if (
            type(lifecycle) is dict
            and lifecycle.get("status") == "completed"
            and lifecycle.get("score_status") == "scored"
            and type(lifecycle.get("score_digest")) is str
            and type(receipt) is dict
            and receipt.get("trial_id") == trial_id
            and receipt.get("route") == route
            and receipt.get("status") == "completed"
            and receipt.get("score_status") == "scored"
            and receipt.get("score_digest") == lifecycle.get("score_digest")
            and receipt.get("cleanup_verified") is True
            and lifecycle.get("cleanup_status") == "verified"
            and resources["tool_calls"] > 0
            and primary_model_settled
            and type(score_evidence) is dict
            and score_evidence.get("source") in _SCORE_SOURCES
        ):
            closed_loop_routes += 1

    if len(observed_route_keys) != 8:
        raise ValueError("Diagnostic report does not bind all eight task/route slots")

    if (
        attempts > DIAGNOSTIC_LIMITS["max_general_requests"]
        or qwen_attempts > DIAGNOSTIC_LIMITS["max_qwen_requests"]
        or openrouter_attempts > DIAGNOSTIC_LIMITS["max_openrouter_requests"]
        or actual_tokens > DIAGNOSTIC_LIMITS["max_tokens"]
        or attempts != qwen_requests + openrouter_attempts
        or qwen_attempts != qwen_requests
        or qwen_actual_tokens != batch["qwen_actual_tokens"]
        or attempts != sum(item["requests"] for item in role_usage_totals.values())
        or actual_tokens + observed_reserved_tokens != sum(
            item["tokens"] for item in role_usage_totals.values()
        )
        or qwen_attempts != sum(item["requests"] for item in role_usage_totals.values())
        or qwen_tokens != sum(item["tokens"] for item in role_usage_totals.values())
        or observed_qwen_actual_cost != qwen_actual_cost
        or observed_qwen_reserved_cost != qwen_reserved_cost
        or observed_reserved_tokens != qwen_reserved_tokens
        or actual_tokens + qwen_reserved_tokens > DIAGNOSTIC_LIMITS["max_tokens"]
    ):
        raise ValueError("Diagnostic general-model request or token hard stop was exceeded")
    if closed_loop_routes < 1:
        raise ValueError("Diagnostic evidence lacks a real MCP execution with official scoring")
    if observed_tool_calls != batch.get("tool_calls"):
        raise ValueError("Diagnostic tool usage differs from per-route ledger evidence")
    return {
        "candidate_sha": report["candidate_sha"],
        "model_profiles_digest": report["model_profiles_digest"],
        "evaluation_source_digest": source_digest,
        "source_manifest_digest": report["source_manifest_digest"],
        "task_inventory_digest": report["task_inventory_digest"],
        "general_model_requests": attempts,
        "general_model_tokens": actual_tokens + qwen_reserved_tokens,
        "qwen_requests": qwen_attempts,
        "qwen_tokens": qwen_tokens,
        "qwen_cost_cny": str(qwen_total_cost),
        "openrouter_requests": openrouter_attempts,
        "tool_calls": observed_tool_calls,
        "diagnostic_elapsed_seconds": elapsed,
        "role_usage": role_usage_totals,
        "unknown_requests": unknown_attempts,
        "closed_loop_route_count": closed_loop_routes,
        "report_sha256": hashlib.sha256(_path_read_bytes(root, evidence_path)).hexdigest(),
    }


def _path_read_bytes(root, evidence_path):
    path, _ = _read_report(root, evidence_path)
    return path.read_bytes()

"""Failures stay in the denominator and unknown/incomplete evidence cannot pass."""

import math
import random
from collections import Counter
from decimal import Decimal
from statistics import median

from .schedule import STRATEGIES


def wilson_interval(successes, total, *, z=1.959963984540054):
    """Return a two-sided Wilson interval for a binomial proportion."""
    if (
        type(total) is not int
        or total < 0
        or type(successes) is not int
        or not 0 <= successes <= total
        or isinstance(z, bool)
        or not isinstance(z, (int, float))
        or not math.isfinite(z)
        or z <= 0
    ):
        raise ValueError("Wilson interval requires valid counts and a positive finite z value")
    if total == 0:
        return None, None
    proportion = successes / total
    z_squared = z * z
    denominator = 1 + z_squared / total
    center = (proportion + z_squared / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z_squared / (4 * total * total))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def paired_cluster_bootstrap(rows, *, iterations=2000, seed=0):
    """Bootstrap paired differences after averaging repeated observations per cluster."""
    if type(iterations) is not int or not 100 <= iterations <= 100_000:
        raise ValueError("Bootstrap iterations must be between 100 and 100000")
    if type(seed) is not int:
        raise ValueError("Bootstrap seed must be an integer")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Paired bootstrap requires observations")
    grouped = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("cluster_id"), str):
            raise ValueError("Each pair requires a nonempty cluster ID")
        cluster = row["cluster_id"].strip()
        if not cluster:
            raise ValueError("Each pair requires a nonempty cluster ID")
        values = []
        for key in ("baseline", "treatment"):
            value = row.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Paired bootstrap values must be finite numbers")
            values.append(float(value))
        grouped.setdefault(cluster, []).append((values[0], values[1]))
    if len(grouped) < 2:
        raise ValueError("Paired cluster bootstrap requires at least two clusters")
    differences = []
    for cluster_rows in grouped.values():
        differences.append(
            sum(treatment - baseline for baseline, treatment in cluster_rows) / len(cluster_rows)
        )
    estimate = sum(differences) / len(differences)
    rng = random.Random(seed)
    replicates = [
        sum(rng.choices(differences, k=len(differences))) / len(differences)
        for _ in range(iterations)
    ]
    replicates.sort()

    def percentile(fraction):
        position = (len(replicates) - 1) * fraction
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return replicates[low]
        return replicates[low] + (replicates[high] - replicates[low]) * (position - low)

    return {
        "cluster_count": len(grouped),
        "paired_rows": len(rows),
        "estimate": estimate,
        "confidence_interval_95": [percentile(0.025), percentile(0.975)],
        "iterations": iterations,
        "seed": seed,
    }


def decision_intervention_counts(rows):
    """Count false/missed intervention and out-of-scope automatic execution separately."""
    if not isinstance(rows, list):
        raise ValueError("Intervention records must be a list")
    for row in rows:
        if not isinstance(row, dict) or any(
            type(row.get(key)) is not bool for key in ("needs_human", "auto_executed", "in_scope")
        ):
            raise ValueError("Each intervention record requires three boolean labels")
    return {
        "total": len(rows),
        "human_interventions": sum(not row["auto_executed"] for row in rows),
        "false_interventions": sum(
            not row["auto_executed"] and not row["needs_human"] for row in rows
        ),
        "missed_interventions": sum(row["auto_executed"] and row["needs_human"] for row in rows),
        "out_of_scope_misexecutions": sum(
            row["auto_executed"] and not row["in_scope"] for row in rows
        ),
    }


HUMAN_EVENT_TYPES = (
    "semantic_intervention",
    "parameter_clarification",
    "write_confirmation",
    "unknown_result_verification",
)


def human_interaction_metrics(tasks):
    """Summarize human event types, waits and unattended completion by task.

    Each row is one scheduled task and contains ``task_id``, a boolean
    ``completed`` and zero or more ``events``. Each event has a ``kind`` from
    ``HUMAN_EVENT_TYPES`` and a finite nonnegative ``wait_seconds``. The
    unattended rate uses all scheduled tasks as its denominator.
    """
    if not isinstance(tasks, list):
        raise ValueError("Human interaction input must be a task list")
    seen = set()
    grouped = {kind: {"waits": [], "tasks": set()} for kind in HUMAN_EVENT_TYPES}
    completed = unattended = tasks_with_events = 0
    for task in tasks:
        if (
            not isinstance(task, dict)
            or not isinstance(task.get("task_id"), str)
            or not task["task_id"].strip()
            or type(task.get("completed")) is not bool
            or not isinstance(task.get("events"), list)
            or task["task_id"] in seen
        ):
            raise ValueError(
                "Each human interaction task requires a unique ID, completion and events"
            )
        task_id = task["task_id"]
        seen.add(task_id)
        completed += task["completed"]
        if task["events"]:
            tasks_with_events += 1
        else:
            unattended += task["completed"]
        for event in task["events"]:
            if not isinstance(event, dict) or set(event) != {"kind", "wait_seconds"}:
                raise ValueError("Human events require only a kind and wait duration")
            kind, wait = event["kind"], event["wait_seconds"]
            if kind not in grouped or (
                isinstance(wait, bool)
                or not isinstance(wait, (int, float))
                or not math.isfinite(wait)
                or wait < 0
            ):
                raise ValueError("Human event kind or wait duration is invalid")
            grouped[kind]["waits"].append(float(wait))
            grouped[kind]["tasks"].add(task_id)

    summaries = {}
    for kind, values in grouped.items():
        waits = sorted(values["waits"])
        p95 = waits[math.ceil(0.95 * len(waits)) - 1] if waits else None
        summaries[kind] = {
            "events": len(waits),
            "tasks": len(values["tasks"]),
            "total_wait_seconds": sum(waits),
            "p50_wait_seconds": median(waits) if waits else None,
            "p95_wait_seconds": p95,
        }
    return {
        "total_tasks": len(tasks),
        "completed_tasks": completed,
        "tasks_with_human_events": tasks_with_events,
        "unattended_completed_tasks": unattended,
        "unattended_completion_rate": unattended / len(tasks) if tasks else None,
        "by_kind": summaries,
    }


def build_report(schedule, records, *, evidence_mode, metadata=None):
    metadata = metadata or {}
    expected = {t["id"]: t for t in schedule}
    rows = {r["id"]: r for r in records}
    if len(rows) != len(records) or any(key not in expected for key in rows):
        raise ValueError("Duplicate or unscheduled trial record")
    fingerprints = {row.get("fairness_digest") for row in records}
    if len(fingerprints) > 1:
        raise ValueError("Trial fairness information differs across strategies")
    for row in records:
        for key in ("machine_seconds", "human_wait_seconds"):
            value = row.get(key, 0)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError("Trial duration must be finite and nonnegative")
        for key in (
            "calls",
            "tokens",
            "forbidden_writes",
            "confirmations",
            "clarifications",
            "fallbacks",
        ):
            value = row.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError("Trial counters must be nonnegative integers")
    strategies = {}
    for strategy in STRATEGIES:
        planned = [t for t in schedule if t["strategy"] == strategy]
        observed = [rows[t["id"]] for t in planned if t["id"] in rows]
        for row in observed:
            if any(
                row.get(k) != expected[row["id"]][k]
                for k in ("case_id", "strategy", "round", "kind")
            ):
                raise ValueError("Trial identity differs from frozen schedule")
        common = [
            float(r["machine_seconds"])
            for r in observed
            if r["kind"] == "common" and r.get("attempted")
        ]
        count = len(planned)
        successes = sum(r.get("correct") is True for r in observed)
        strategies[strategy] = {
            "denominator": count,
            "correct": successes,
            "accuracy": successes / count if count else None,
            "attempted": sum(r.get("attempted") is True for r in observed),
            "failed_or_incorrect": count - successes,
            "states": dict(Counter(r.get("state", "unknown") for r in observed)),
            "confirmations": sum(r.get("confirmations", 0) for r in observed),
            "clarifications": sum(r.get("clarifications", 0) for r in observed),
            "fallbacks": sum(r.get("fallbacks", 0) for r in observed),
            "calls": sum(r.get("calls", 0) for r in observed),
            "cli_calls": sum(len(r.get("cli_calls", [])) for r in observed),
            "tokens": sum(r.get("tokens", 0) for r in observed),
            "known_cost_usd": str(
                sum((Decimal(str(r.get("cost_usd") or 0)) for r in observed), Decimal(0))
            ),
            "unknown_usage_trials": sum(r.get("usage_complete") is not True for r in observed),
            "forbidden_writes": sum(r.get("forbidden_writes", 0) for r in observed),
            "common_machine_p50_seconds": median(common) if common else None,
            "total_machine_seconds": sum(float(r.get("machine_seconds", 0)) for r in observed),
            "human_wait_seconds": sum(float(r.get("human_wait_seconds", 0)) for r in observed),
        }
    main, hybrid = strategies["main"], strategies["hybrid"]
    complete = (
        bool(schedule)
        and len(rows) == len(schedule)
        and all(
            r.get("attempted") is True
            and r.get("state")
            not in {"paused_budget", "not_run", "recovering", "running", "queued"}
            for r in rows.values()
        )
    )
    known = all(r.get("usage_complete") is True for r in rows.values())
    main_p50, hybrid_p50 = (s["common_machine_p50_seconds"] for s in (main, hybrid))
    checks = {
        "no_stop_reason": metadata.get("stop_reason") is None,
        "cumulative_jev_ledger_safe": not metadata.get("global_jev_ledger")
        or (
            metadata["global_jev_ledger"].get("blocked_unknown_or_pending") is False
            and metadata["global_jev_ledger"].get("within_cap") is True
        ),
        "live_evidence": evidence_mode == "live"
        and all(r.get("evidence_mode") == "live" for r in rows.values()),
        "complete": complete,
        "usage_known": known,
        "accuracy_not_lower": main["accuracy"] is not None
        and hybrid["accuracy"] is not None
        and hybrid["accuracy"] >= main["accuracy"],
        "forbidden_writes_zero": all(s["forbidden_writes"] == 0 for s in strategies.values()),
        "common_machine_p50_lower": main_p50 is not None
        and hybrid_p50 is not None
        and hybrid_p50 < main_p50,
        "mixed_total_machine_not_higher": hybrid["total_machine_seconds"]
        <= main["total_machine_seconds"],
    }
    return {
        "schema_version": "business-evaluation-report-1",
        "evidence_mode": evidence_mode,
        "metadata": metadata,
        "planned_trials": len(schedule),
        "missing_trials": len(schedule) - len(rows),
        "unattempted_trials": [
            {**trial, "state": "not_run"} for trial in schedule if trial["id"] not in rows
        ],
        "strategies": strategies,
        "acceptance": {
            "status": "passed" if all(checks.values()) else "not_passed",
            "checks": checks,
        },
        "records": records,
    }

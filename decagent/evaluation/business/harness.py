"""Historical v1 scoring and calibration analysis; live execution moved to MCP acceptance."""

import re
from decimal import Decimal
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from decagent.business.questions import build_business_request
from decagent.business.services import BusinessServices
from decagent.decisions import JevAdapter
from decagent.decisions.contracts import validate_answers
from decagent.runtime.model_calls import LedgerAttemptBudget
from decagent.storage.database import Database

from .calibration import EXPORT_V2, development_evaluation_kind
from .dataset import FROZEN_CHECKSUM, dataset_checksum, select_cases
from .ledger import GlobalAttemptBudget
from .metrics import build_report
from .schedule import make_schedule

DEFAULT_BUDGET = {
    "max_calls": 30,
    "max_tokens": 250000,
    "max_active_seconds": 1800,
    "max_cost": 0.99,
    "allow_unknown_price": False,
}

SCORER_VERSION = "business-outcome-2"


def create_benchmark_database(source_url):
    """Create once; keep evidence; never reset or drop the supplied database."""
    source = Database(source_url)
    name = "businessbench_" + uuid4().hex
    with source.connect(autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = conninfo_to_dict(source_url)
    settings["dbname"] = name
    database = Database(make_conninfo(**settings))
    database.migrate()
    return database


def benchmark_name(database):
    name = conninfo_to_dict(database.url).get("dbname", "")
    if not re.fullmatch(r"businessbench_[a-f0-9]{32}", name):
        raise ValueError("Benchmark mutation requires a newly created businessbench database")
    return name


def _substitute(value, ticket_id):
    if isinstance(value, str):
        return value.replace("{ticket_id}", ticket_id or "00000000-0000-0000-0000-000000000000")
    if isinstance(value, dict):
        return {key: _substitute(item, ticket_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, ticket_id) for item in value]
    return value


def _required_actions_match(observed, expected, spec):
    """Match ordered goals while permitting only supplementary read operations."""
    index = 0
    for action in observed:
        snapshot = action["snapshot"]
        command = spec.commands.get(snapshot["command"])
        if command is None:
            return False
        if index < len(expected):
            target = expected[index]
            if snapshot["command"] == target["command"] and all(
                snapshot["params"].get(key) == value for key, value in target["params"].items()
            ):
                index += 1
                continue
        if command.effect != "read":
            return False
    return index == len(expected)


class BenchmarkHarness:
    def __init__(self, database, settings, runtime_config, dataset, **kwargs):
        if (
            dataset.get("checksum") != FROZEN_CHECKSUM
            or dataset_checksum(dataset) != FROZEN_CHECKSUM
        ):
            raise ValueError("In-memory frozen dataset checksum drift")
        raise ValueError(
            "The v1 CLI benchmark is retired. Use MCPAcceptanceHarness for the authenticated "
            "MCP version; historical report and calibration analysis remain available."
        )

    @staticmethod
    def _directory(value):
        return {
            key: sorted(value[key], key=lambda row: row["id"]) for key in ("people", "categories")
        }

    def _seed(self, *args, **kwargs):
        raise ValueError("Legacy CLI execution is retired; use the fixed MCP acceptance suite")

    def prepare_case(self, *args, **kwargs):
        raise ValueError("Legacy CLI execution is retired; use the fixed MCP acceptance suite")

    def _setup_call(self, *args, **kwargs):
        raise ValueError("Legacy CLI execution is retired; use the fixed MCP acceptance suite")

    def _adapter(self, services, attribution):
        local = LedgerAttemptBudget(
            services.ledger,
            reservation_usd=self.config.jev_reservation,
            before_dispatch=services.guard,
        )
        budget = GlobalAttemptBudget(
            self.global_ledger,
            local,
            reservation=self.config.jev_reservation,
            attribution={
                **attribution,
                "price_version": self.jev_price_version,
                "cost_source": "owner-versioned-token-rates"
                if self.jev_cost_estimator
                else "provider-reported-or-unknown",
            },
        )
        return JevAdapter(
            api_key=self.settings.jev_api_key,
            model=self.settings.jev_model,
            provider=self.settings.jev_provider,
            budget=budget,
            cache_ttl=0,
            max_retries=0,
            cost_estimator=self.jev_cost_estimator,
        )

    def _approve(self, *args, **kwargs):
        raise ValueError("Legacy CLI execution is retired; use the fixed MCP acceptance suite")

    def run_trial(self, *args, **kwargs):
        raise ValueError("Legacy CLI execution is retired; use the fixed MCP acceptance suite")

    def _observe(self, task, reference, runner):
        with self.database.connect() as connection:
            actions = connection.execute(
                "SELECT * FROM app.actions WHERE task_id=%s ORDER BY created_at,id", (task["id"],)
            ).fetchall()
            approvals = connection.execute(
                "SELECT * FROM app.approvals WHERE task_id=%s", (task["id"],)
            ).fetchall()
            usage = connection.execute(
                "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
            ).fetchall()
            decisions = connection.execute(
                "SELECT * FROM app.decision_records WHERE task_id=%s", (task["id"],)
            ).fetchall()
            attempted_ids = {
                str(r["action_id"])
                for r in connection.execute(
                    "SELECT action_id FROM app.action_attempts WHERE action_id IN "
                    "(SELECT id FROM app.actions WHERE task_id=%s)",
                    (task["id"],),
                ).fetchall()
            }
        observed = [a for a in actions if a["status"] == "succeeded"]
        expected = reference.get("actions", [])
        params_match = _required_actions_match(observed, expected, self.spec)
        ticket = None
        resources = set()
        for action in observed:
            if action.get("receipt"):
                receipt = action["receipt"].get("data", {}).get("receipt") or {}
                if receipt.get("resource_id"):
                    resources.add(receipt["resource_id"])
        if len(resources) == 1:
            response = runner.run(
                self.spec,
                "get",
                {"ticket_id": next(iter(resources))},
                "benchmark-final-" + uuid4().hex,
            )
            if response["ok"]:
                ticket = response["data"]["ticket"]
            runner.calls[-1]["purpose"] = "scorer-verification-outside-machine-timing"
        ticket_match = not reference.get("ticket") or (
            ticket is not None
            and all(ticket.get(key) == value for key, value in reference["ticket"].items())
        )
        writes = [
            a
            for a in actions
            if str(a["id"]) in attempted_ids
            and self.spec.commands[a["snapshot"]["command"]].effect == "write"
        ]
        allowed = [a["command"] for a in expected]
        forbidden = sum(
            a["snapshot"]["command"] not in allowed
            or a["snapshot"]["command"] in reference.get("forbidden_commands", [])
            for a in writes
        )
        usage_known = all(
            r["actual_tokens"] is not None
            and (
                r["status"] == "settled"
                or (
                    r["provider"] == "primary"
                    and self.main_free_quota
                    and self.budget["allow_unknown_price"]
                    and r["status"] == "unknown"
                )
            )
            for r in usage
        )
        terminal = reference.get("terminal")
        correct = (
            task["state"] == "succeeded"
            and terminal == "succeeded"
            and params_match
            and ticket_match
            and len(resources) <= 1
        )
        if terminal == "clarification":
            correct = task["state"] == "awaiting_input" and not writes and params_match
        fallbacks = sum(
            d["final_route"] == "plan" and d["question_version"] != "primary-plan-1"
            for d in decisions
        )
        return {
            "task_id": str(task["id"]),
            "state": task["state"],
            "correct": bool(correct and forbidden == 0),
            "forbidden_writes": forbidden,
            "confirmations": sum(a["kind"] == "action" for a in approvals),
            "clarifications": sum(a["kind"] == "clarification" for a in approvals),
            "fallbacks": fallbacks,
            "calls": len(usage),
            "tokens": sum(r["actual_tokens"] or 0 for r in usage),
            "cost_usd": str(sum((r["actual_cost"] or Decimal(0) for r in usage), Decimal(0))),
            "usage_complete": usage_known,
            "main_unknown_price_owner_policy": self.main_free_quota,
            "actual_models": sorted(
                {d["resolved_model"] for d in decisions if d["resolved_model"]}
            ),
            "scorer_verified_ticket": ticket,
            "actions": [
                {
                    "id": str(a["id"]),
                    "status": a["status"],
                    "command": a["snapshot"]["command"],
                    "params": a["snapshot"]["params"],
                    "receipt": a.get("receipt"),
                }
                for a in actions
            ],
            "cli_calls": runner.calls,
            "active_seconds_ledger": float(task["active_seconds"]),
        }

    def run(self, *, split="holdout", rounds=3, max_trials=None, save=None):
        if self.evidence_mode == "live" and self.profile is None:
            raise ValueError("Live comparison requires a matching development calibration profile")
        if max_trials is not None and (type(max_trials) is not int or max_trials < 0):
            raise ValueError("Explicit trial limit must be a nonnegative integer")
        cases = select_cases(self.dataset, split)
        schedule, records = make_schedule(cases, rounds=rounds), []
        lookup = {c["id"]: c for c in cases}
        stop_reason = None
        for trial in schedule:
            if max_trials is not None and len(records) >= max_trials:
                stop_reason = "explicit_trial_limit"
                break
            if self.global_ledger:
                snapshot = self._ledger_snapshot()
                if (
                    snapshot["blocked_unknown_or_pending"]
                    or not snapshot["within_cap"]
                    or Decimal(snapshot["remaining_usd"]) < self.config.jev_reservation
                ):
                    stop_reason = "global_jev_budget_or_unknown_usage"
                    break
            record = self.run_trial(trial, lookup[trial["case_id"]])
            records.append(record)
            if self.global_ledger:
                snapshot = self._ledger_snapshot()
                if snapshot["blocked_unknown_or_pending"] or not snapshot["within_cap"]:
                    stop_reason = "global_jev_budget_or_unknown_usage"
            report = self.report(schedule, records, split, stop_reason)
            if save:
                save(report)
            if stop_reason or getattr(self, "_ledger_error", None):
                stop_reason = stop_reason or "global_jev_budget_or_unknown_usage"
                break
            if record["state"] in {"paused_budget", "recovering", "running", "queued"}:
                stop_reason = "task_budget_or_recovery_stop"
                break
        report = self.report(schedule, records, split, stop_reason)
        if save:
            save(report)
        return report

    def _ledger_snapshot(self):
        if getattr(self, "_ledger_error", None):
            return self._ledger_error
        try:
            return self.global_ledger.snapshot()
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            # Preserve the completed trial and deny further calls without leaking paths/DSNs.
            self._ledger_error = {
                "blocked_unknown_or_pending": True,
                "within_cap": False,
                "remaining_usd": "0",
                "error_type": type(error).__name__,
            }
            return self._ledger_error

    def report(self, schedule, records, split, stop_reason):
        report = build_report(
            schedule,
            records,
            evidence_mode=self.evidence_mode,
            metadata={
                "dataset_version": self.dataset["version"],
                "dataset_checksum": self.dataset["checksum"],
                "scorer_version": SCORER_VERSION,
                "label_source": "synthetic-reference",
                "split": split,
                "database_name": self.name,
                "fairness_digest": self.fairness_digest,
                "plugin_digest": self.spec.digest,
                "requested_jev_model": self.settings.jev_model,
                "requested_main_model": self.settings.main_model,
                "cache_ttl": 0,
                "jev_price_version": self.jev_price_version,
                "stop_reason": stop_reason,
                "deployment": "Windows-local; independent-Linux-pending",
                "human_wait": "scripted actor; no real human latency study",
                "global_jev_ledger": self._ledger_snapshot() if self.global_ledger else None,
            },
        )
        if split != "holdout" or len(schedule) != 720:
            report["acceptance"]["checks"]["full_frozen_holdout_three_rounds"] = False
            report["acceptance"]["status"] = "not_passed"
        return report

    def collect_development(self, *, save=None, max_cases=40):
        """Actual Jev labels on development cases only; never dispatch a business goal."""
        if self.evidence_mode != "live" or not self.global_ledger or not 1 <= max_cases <= 40:
            raise ValueError("Development collection requires explicit live mode and ledger")
        export = {
            "schema_version": EXPORT_V2,
            "dataset_checksum": self.dataset["checksum"],
            "dataset_version": self.dataset["version"],
            "label_source": "synthetic-reference",
            "records": [],
            "complete": False,
            "collection_complete": False,
            "collection_summary": {
                "valid_records": 0,
                "invalid_response_records": 0,
                "failed_or_unaccounted_records": 0,
                "disabled_family_count": 0,
                "disabled_families": [],
                "family_invalid_response_counts": {},
            },
            "stop_reason": None,
            "jev_price_version": self.jev_price_version,
        }
        resolved_model = None
        for case in select_cases(self.dataset, "development")[:max_cases]:
            snapshot = self._ledger_snapshot()
            if (
                snapshot["blocked_unknown_or_pending"]
                or not snapshot["within_cap"]
                or Decimal(snapshot["remaining_usd"]) < self.config.jev_reservation
            ):
                export["stop_reason"] = "global_jev_budget_or_unknown_usage"
                break
            instruction, _, _ = self.prepare_case(case)
            task = self.database.create_business_task(
                self.scope["id"],
                {
                    "instruction": instruction,
                    "strategy": "hybrid",
                    "plugin_digest": self.spec.digest,
                    "provenance": {"owner_id": "benchmark-owner", "authenticated": True},
                },
                self.budget,
            )
            owner = "development-collector-" + uuid4().hex
            claimed = self.database.claim(task["id"], owner, seconds=120)
            services = BusinessServices(
                self.database,
                self.settings,
                self.config,
                claimed,
                owner,
                claimed["lease_generation"],
            )
            adapter = self._adapter(
                services, {"stage": "development-calibration", "case_id": case["id"]}
            )
            try:
                prepared = services.run_phase("prepare", {"instruction": instruction, "steps": 0})
                request = build_business_request(
                    self.spec,
                    str(task["id"]),
                    str(self.scope["id"]),
                    instruction,
                    prepared["evidence"],
                    dataset_version=self.dataset["version"],
                )
                result = adapter.evaluate(request)
                result_document = result.to_dict()
                export["records"].append(
                    {
                        "case_id": case["id"],
                        "task_id": str(task["id"]),
                        "scope_id": str(self.scope["id"]),
                        "instruction": instruction,
                        "evidence": prepared["evidence"],
                        "context": vars(request.context),
                        "evaluation": result_document,
                    }
                )
                summary = export["collection_summary"]
                try:
                    kind = development_evaluation_kind(result_document, allow_invalid=True)
                    if kind == "valid":
                        validate_answers(result_document["answers"], request)
                        summary["valid_records"] += 1
                    else:
                        summary["invalid_response_records"] += 1
                        counts = summary["family_invalid_response_counts"]
                        for family in request.question_versions:
                            counts[family] = counts.get(family, 0) + 1
                        summary["disabled_families"] = sorted(counts)
                        summary["disabled_family_count"] = len(counts)
                    if resolved_model is None:
                        resolved_model = result.actual_model
                    if result.actual_model != resolved_model:
                        export["stop_reason"] = "development_model_or_binding_drift"
                except ValueError:
                    summary["failed_or_unaccounted_records"] += 1
                    export["stop_reason"] = "failed_or_unknown_development_evaluation"
            finally:
                adapter.close()
                services.close()
                # This task only collected judgments. It did not fulfil the business goal.
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE app.tasks SET state='cancelled',lease_owner=NULL,"
                        "lease_until=NULL WHERE id=%s",
                        (task["id"],),
                    )
                self.database.event(
                    task["id"],
                    "evaluation.development_collected",
                    {"case_id": case["id"], "business_goal_executed": False},
                )
            export["global_jev_ledger"] = self._ledger_snapshot()
            if (
                export["global_jev_ledger"]["blocked_unknown_or_pending"]
                or not export["global_jev_ledger"]["within_cap"]
            ):
                export["stop_reason"] = "global_jev_budget_or_unknown_usage"
            if save:
                save(export)
            if export["stop_reason"]:
                break
        export["complete"] = len(export["records"]) == 40 and not export["stop_reason"]
        export["collection_complete"] = export["complete"]
        if max_cases < 40 and not export["stop_reason"]:
            export["stop_reason"] = "explicit_probe_limit"
        if save:
            save(export)
        return export

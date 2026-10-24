"""Frozen first-wave adapters for the supervised public evaluation runner."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import (
    ComparisonTrial,
    PublicComparisonTrial,
    Route,
)
from decagent.evaluation.business.enterprise_environment import (
    EnterpriseEnvironment,
    spec_from_pinned_row,
)
from decagent.evaluation.business.enterprise_scoring import bounded_state_fingerprint
from decagent.evaluation.business.public_execution import execute_public_phase
from decagent.evaluation.business.public_resources import LedgeredToolGateway
from decagent.evaluation.business.public_session import (
    enterprise_session_factory,
    tau_session_factory,
)
from decagent.evaluation.business.public_supervisor import (
    PublicSupervisor,
    canonical_public_supervisor_lock_path,
)
from decagent.evaluation.business.public_trials import (
    TERMINAL_ROUTE_STATUSES,
    PublicRunPlan,
    canonical_public_trial_ledger_path,
    open_first_wave_public_trial_ledger,
)
from decagent.evaluation.business.public_weekly_budget import (
    PUBLIC_EVALUATION_BUDGET_ID,
    authorize_public_evaluation_phase,
)
from decagent.evaluation.business.routes import ControlledRoutes

_BOUNDARY_FIELDS = {
    "initial_state_digest",
    "goal_digest",
    "actor_profile_digest",
    "permission_digest",
    "tool_adapter_digest",
}
_PROFILE_FIELDS = {
    "model_profile_digest",
    "jev_profile_digest",
    "user_simulator_profile_digest",
    "judge_profile_digest",
}
_TAU_INSTRUCTION = "Handle the customer's request using the official domain policy and tools."


def require_verified_candidate(root):
    """Require a clean checkout whose exact HEAD has a successful GitHub CI run."""
    root = Path(root).resolve()

    def run(arguments):
        try:
            result = subprocess.run(
                arguments,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                "Verified candidate gate could not complete its read-only checks"
            ) from exc
        if result.returncode != 0:
            raise ValueError("Verified candidate gate could not verify Git or GitHub state")
        return result.stdout.strip()

    candidate = run(["git", "rev-parse", "HEAD"])
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", candidate) is None:
        raise ValueError("Git HEAD is not a valid immutable candidate SHA")
    status = run(["git", "status", "--porcelain", "--untracked-files=all"])
    if status:
        raise ValueError("Live evaluation requires a clean candidate worktree")

    repository = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("GitHub repository identity is not a safe owner/name pair")
    records = run(
        [
            "gh", "run", "list", "--repo", repository, "--commit", candidate,
            "--json", "headSha,workflowName,status,conclusion",
        ]
    )
    try:
        runs = json.loads(records)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub CI evidence response is malformed") from exc
    if type(runs) is not list or not any(
        type(item) is dict
        and item.get("headSha") == candidate
        and item.get("workflowName") == "CI"
        and item.get("status") == "completed"
        and item.get("conclusion") == "success"
        for item in runs
    ):
        raise ValueError("The exact candidate SHA has no completed successful GitHub CI run")
    return candidate


def build_public_comparison_trial(plan, record, *, dataset_checksum, boundary_facts):
    """Bind measured environment facts to one exact row of the frozen first wave."""
    if not isinstance(plan, PublicRunPlan) or plan.mode not in {"smoke", "pilot"}:
        raise ValueError("A frozen smoke or pilot public run plan is required")
    if type(record) is not dict or type(boundary_facts) is not dict:
        raise ValueError("Frozen public cohort record and measured boundary are required")
    if set(boundary_facts) != _BOUNDARY_FIELDS:
        raise ValueError("Measured public environment boundary is incomplete")
    if (
        type(dataset_checksum) is not str
        or len(dataset_checksum) != 64
        or any(character not in "0123456789abcdef" for character in dataset_checksum)
    ):
        raise ValueError("Pinned public dataset checksum must be a lowercase SHA-256")
    if any(
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in boundary_facts.values()
    ):
        raise ValueError("Measured public environment boundary must contain SHA-256 values")
    frozen = next(
        (item for item in plan.trials if item.get("trial_id") == record.get("trial_id")),
        None,
    )
    if frozen is None or frozen != record:
        raise ValueError("Trial record does not belong to the frozen public cohort")
    if plan.mode == "smoke":
        wave_ids = {item["trial_id"] for item in plan.initial_smoke_wave}
        if frozen["trial_id"] not in wave_ids:
            raise ValueError("Only a frozen first-wave trial can be dispatched")
    if not _PROFILE_FIELDS <= set(frozen) or any(
        frozen[name] != plan.profiles.digests[name] for name in _PROFILE_FIELDS
    ):
        raise ValueError("Trial model identity differs from the frozen public cohort")
    if frozen.get("resource_profile_digest") != digest(plan.resource_limits):
        raise ValueError("Trial resource profile differs from the frozen public cohort")

    trial = ComparisonTrial(
        dataset=frozen["benchmark"],
        dataset_version=frozen["version"],
        dataset_checksum=dataset_checksum,
        case_id=frozen["case_id"],
        branch_kind=(
            "semantic"
            if frozen["benchmark"] == "enterpriseops-gym" and frozen["domain"] == "csm"
            else "structured"
        ),
        memory_mode="disabled",
        workflow_mode="cold",
        exposure_group=frozen["exposure_group"],
        resource_profile_digest=frozen["resource_profile_digest"],
        response_policy_digest=frozen["response_policy_digest"],
        model_profile_digest=frozen["model_profile_digest"],
        jev_profile_digest=frozen["jev_profile_digest"],
        user_simulator_profile_digest=frozen["user_simulator_profile_digest"],
        judge_profile_digest=frozen["judge_profile_digest"],
        **deepcopy(boundary_facts),
    )
    return PublicComparisonTrial.from_trial(trial, frozen["trial_id"])


def _enterprise_task(root, record, source, inventory):
    if (
        record.get("benchmark") != "enterpriseops-gym"
        or record.get("domain") not in {"csm", "itsm"}
        or record.get("split") != "oracle"
    ):
        raise ValueError("Unsupported first-wave EnterpriseOps task")
    task_dataset = source["enterpriseops_gym"]["task_dataset"]
    parquet = next(
        (
            item
            for item in task_dataset["parquet_sha256"]
            if f"/{record['split']}/{record['domain']}-00000-of-00001.parquet"
            in item["path"].replace("\\", "/")
        ),
        None,
    )
    if parquet is None:
        raise ValueError("Pinned EnterpriseOps Parquet record is missing")
    parquet_path = (root / parquet["path"]).resolve()
    if not parquet_path.is_relative_to(root) or parquet_path.is_symlink():
        raise ValueError("Pinned EnterpriseOps Parquet path is unsafe")
    if hashlib.sha256(parquet_path.read_bytes()).hexdigest() != parquet["sha256"]:
        raise ValueError("Pinned EnterpriseOps Parquet checksum differs")
    selected_ids = inventory["enterpriseops_gym"]["primary_selection"]["task_ids"].get(
        record["domain"], []
    )
    if record["case_id"] not in selected_ids:
        raise ValueError("EnterpriseOps case is outside the pinned official selection")
    try:
        import pyarrow.parquet as parquet_reader
    except ImportError as exc:
        raise RuntimeError("Install evals/benchmarks/requirements.txt for live evaluation") from exc
    table = parquet_reader.read_table(parquet_path)
    rows = [row for row in table.to_pylist() if row.get("task_id") == record["case_id"]]
    if len(rows) != 1:
        raise ValueError("Pinned EnterpriseOps task row is missing or ambiguous")
    row = rows[0]
    row["domain"] = record["domain"]
    spec = spec_from_pinned_row(root, row, source)
    return spec, parquet["sha256"]


def _enterprise_boundary(spec, environment, record):
    from decagent.business.plans.evaluation_catalog import (
        ENTERPRISE_MCP_OBSERVATION_CONTRACT_VERSION,
        EvaluationCatalog,
        EvaluationScope,
    )

    public = environment.model_input()
    initial = bounded_state_fingerprint(environment.state_fingerprint())
    with EvaluationScope(
        "enterpriseops-gym", record["version"], record["case_id"]
    ) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            public["tools"],
            benchmark_version=spec.image,
            result_contract_version=ENTERPRISE_MCP_OBSERVATION_CONTRACT_VERSION,
        )
    boundary = {
        "initial_state_digest": initial["digest"],
        "goal_digest": digest(
            {"system_prompt": public["system_prompt"], "user_prompt": public["user_prompt"]}
        ),
        "actor_profile_digest": digest({"context_identity": spec.context}),
        "permission_digest": digest(
            {
                "selected_tools": sorted(spec.selected_tools),
                "restricted_tools": sorted(spec.restricted_tools),
                "effective_tools": sorted(tool["name"] for tool in public["tools"]),
            }
        ),
        "tool_adapter_digest": digest(
            {
                "image": spec.image,
                "platform": spec.platform,
                "mcp_protocol": "2024-11-05",
                "catalog_digest": catalog.digest,
            }
        ),
    }
    return public, boundary


def _tau_boundary(runtime, record, environment, tools, policy):
    process = environment.environment
    return {
        "initial_state_digest": digest(
            {"agent": process.get_db_hash(), "user": process.get_user_db_hash()}
        ),
        "goal_digest": digest(
            {"private_task_digest": process.task_digest, "policy_digest": digest(policy)}
        ),
        "actor_profile_digest": digest(
            {"domain": record["domain"], "case_id": record["case_id"]}
        ),
        "permission_digest": digest({"tools": tools, "policy": policy}),
        "tool_adapter_digest": digest(
            {
                "commit": runtime.source_commit,
                "package_version": runtime.package_version,
                "tools": tools,
                "mcp_protocol": "in-memory",
            }
        ),
    }


class FirstWaveRuntime:
    """Construct fresh, source-pinned benchmark sessions for each frozen task."""

    def __init__(self, plan, *, tau_python, journal_root):
        if not isinstance(plan, PublicRunPlan) or plan.mode not in {"smoke", "pilot"}:
            raise ValueError("A frozen smoke or pilot plan is required")
        if type(tau_python) is not str or not tau_python.strip():
            raise ValueError("A dedicated pinned τ Python 3.12 executable is required")
        self.plan = plan
        self.root = Path(plan.root).resolve()
        self.journal_root = Path(journal_root).resolve()
        self.source = json.loads(
            (self.root / "evals/benchmarks/manifests/source_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.inventory = json.loads(
            (self.root / "evals/benchmarks/manifests/task_inventory.json").read_text(
                encoding="utf-8"
            )
        )
        from decagent.evaluation.business.tau_process import PinnedTauRuntime

        self.tau_runtime = PinnedTauRuntime.from_manifest(self.root, Path(tau_python))
        self._sessions = {}
        self._trials = {}

    def trial_factory(self, record):
        if type(record) is not dict or record.get("trial_id") not in {
            item["trial_id"] for item in self.plan.trials
        }:
            raise ValueError("Trial record is outside the frozen public cohort")
        prepared = self._trials.get(record["trial_id"])
        if prepared is not None:
            if prepared[0] != record:
                raise ValueError("Prepared trial differs from its frozen record")
            return prepared[1]
        if record["benchmark"] == "enterpriseops-gym":
            trial = self._enterprise_trial(record)
        elif record["benchmark"] == "tau-bench":
            trial = self._tau_trial(record)
        else:
            raise ValueError("Unknown public benchmark")
        self._trials[record["trial_id"]] = (deepcopy(record), trial)
        return trial

    def _enterprise_trial(self, record):
        spec, dataset_checksum = _enterprise_task(
            self.root, record, self.source, self.inventory
        )
        with EnterpriseEnvironment(spec) as environment:
            _, boundary = _enterprise_boundary(spec, environment, record)
        if environment.cleanup_verified is not True:
            raise RuntimeError("EnterpriseOps read-only preflight cleanup was not verified")
        session_factory = enterprise_session_factory(
            spec,
            record["version"],
            self.journal_root,
        )
        self._sessions[record["trial_id"]] = session_factory
        return build_public_comparison_trial(
            self.plan,
            record,
            dataset_checksum=dataset_checksum,
            boundary_facts=boundary,
        )

    def _tau_trial(self, record):
        runtime = self.tau_runtime
        domain, case_id, split = record["domain"], record["case_id"], record["split"]
        expected_split = "test" if self.plan.mode == "pilot" else "train"
        if split != expected_split or case_id not in runtime.case_ids(domain, split=split):
            raise ValueError("τ case differs from the frozen benchmark split")
        task_input = {"instruction": _TAU_INSTRUCTION}
        from decagent.evaluation.business.tau_tools import TauMCPEnvironment

        def environment_factory(_trial):
            return runtime.open(domain, case_id, split=split)

        with TauMCPEnvironment(
            lambda: runtime.open(domain, case_id, split=split),
            system_prompt=None,
            user_prompt=task_input["instruction"],
        ) as preview:
            public = preview.model_input()
            policy = public["system_prompt"]
            if (
                type(policy) is not str
                or not policy.strip()
                or policy != preview.environment.get_policy()
                or public["user_prompt"] != task_input["instruction"]
            ):
                raise ValueError("τ prompt projection differs from the original domain policy")
            boundary = _tau_boundary(runtime, record, preview, public["tools"], policy)
        if preview.cleanup_verified is not True:
            raise RuntimeError("τ read-only preflight cleanup was not verified")
        domain_record = next(
            item
            for item in self.inventory["tau2_bench"]["official_domains"]
            if item["domain"] == domain
        )
        dataset_checksum = digest(
            {
                "commit": runtime.source_commit,
                "domain": domain,
                "split": split,
                "source_sha256": domain_record["source_sha256"],
            }
        )

        def boundary_factory(environment, _trial):
            current = environment.model_input()
            return _tau_boundary(runtime, record, environment, current["tools"], policy)

        session_factory = tau_session_factory(
            environment_factory,
            dataset_version=record["version"],
            tool_catalog_version=runtime.package_version,
            journal_root=self.journal_root,
            task_input_factory=lambda _trial: task_input,
            skill_factory=lambda _trial: policy,
            boundary_factory=boundary_factory,
            scorer_factory=lambda _environment, _trial: None,
        )
        self._sessions[record["trial_id"]] = session_factory
        return build_public_comparison_trial(
            self.plan,
            record,
            dataset_checksum=dataset_checksum,
            boundary_facts=boundary,
        )

    @contextmanager
    def environment_factory(self, trial, isolation_id):
        factory = self._sessions.get(getattr(trial, "trial_id", None))
        if factory is None:
            raise ValueError("Frozen trial must be preflighted before opening its route session")
        with factory(trial, isolation_id) as session:
            yield session

    def route_kwargs_factory(self, model_adapter_factory, trial, route):
        if getattr(trial, "trial_id", None) not in self._sessions:
            raise ValueError("Route trial was not prepared from the frozen public cohort")
        return {
            "model_adapter_factory": model_adapter_factory,
            "gateway_factory": lambda base, session, _trial, current_route: LedgeredToolGateway(
                base,
                model_adapter_factory.ledger,
                trial_id=trial.trial_id,
                route=current_route,
                tool_effects={
                    name: command.effect for name, command in session.spec.commands.items()
                },
            ),
        }


def live_first_wave_report(plan, phase, ledger, *, candidate_sha, working_tree_dirty):
    if (
        not isinstance(plan, PublicRunPlan)
        or phase.phase not in {"diagnostic", "first-wave", "pilot"}
        or (phase.phase == "pilot" and plan.mode != "pilot")
    ):
        raise ValueError("A frozen live phase receipt is required for reporting")
    if getattr(ledger, "candidate_sha", None) != candidate_sha:
        raise ValueError("Live report candidate SHA differs from the canonical ledger")
    receipts = {
        (receipt.trial_id, receipt.route): asdict(receipt)
        for receipt in phase.executed_routes
    }
    route_metrics = {}
    model_attempts = getattr(ledger, "model_attempts_snapshot", None)
    tool_attempts = getattr(ledger, "tool_attempts_snapshot", None)
    selected_trials = (
        plan.initial_smoke_wave if phase.phase == "first-wave" else plan.trials
    )
    for selected in selected_trials:
        trial_id = selected["trial_id"]
        for route in plan.routes_for_trial(trial_id):
            key = trial_id + ":" + route
            route_metrics[key] = {
                "trial_id": trial_id,
                "route": route,
                "execution_receipt": receipts.get((trial_id, route)),
                "lifecycle": ledger.route_lifecycle(trial_id, route),
                "usage": ledger.usage_snapshot(trial_id, route),
                "resources": ledger.resource_snapshot(trial_id, route),
                "billing": ledger.billing_snapshot(trial_id, route),
                "model_attempts": (
                    model_attempts(trial_id, route) if callable(model_attempts) else []
                ),
                "tool_attempts": (
                    tool_attempts(trial_id, route) if callable(tool_attempts) else []
                ),
            }
    statuses = [
        item["lifecycle"].get("status")
        for item in route_metrics.values()
    ]
    route_status_counts = dict(
        sorted(
            Counter(status for status in statuses if isinstance(status, str)).items()
        )
    )
    successful_route_count = route_status_counts.get("completed", 0)
    terminal_route_count = sum(
        route_status_counts.get(status, 0) for status in TERMINAL_ROUTE_STATUSES
    )
    not_applicable_route_count = route_status_counts.get("not_applicable", 0)
    evaluated_route_count = max(0, terminal_route_count - not_applicable_route_count)
    selected_route_slots = sum(
        len(plan.routes_for_trial(item["trial_id"])) for item in selected_trials
    )
    semantic_pairing = None
    if phase.phase == "pilot":
        semantic_pair_records = []
        for selected in selected_trials:
            if selected.get("domain") != "csm":
                continue
            trial_id = selected["trial_id"]
            jev_record = route_metrics.get(
                trial_id + ":" + Route.PLAN_JEV_SEMANTIC.value
            )
            if jev_record is None:
                continue
            execution_receipt = jev_record.get("execution_receipt") or {}
            lifecycle = jev_record.get("lifecycle") or {}
            primary_record = route_metrics.get(
                trial_id + ":" + Route.PLAN_PRIMARY_SEMANTIC.value, {}
            )
            primary_receipt = primary_record.get("execution_receipt") or {}
            pair_status = execution_receipt.get("pair_status") or (
                "not_applicable"
                if lifecycle.get("status") == "not_applicable"
                else "incomplete"
            )
            semantic_pair_records.append({
                "trial_id": trial_id,
                "case_id": selected.get("case_id"),
                "status": pair_status,
                "reason": execution_receipt.get("error_category")
                or lifecycle.get("error_category"),
                "primary_plan_digest": primary_receipt.get("plan_digest"),
                "jev_plan_digest": execution_receipt.get("plan_digest"),
                "primary_decision_input_digest": primary_receipt.get(
                    "decision_input_digest"
                ),
                "jev_decision_input_digest": execution_receipt.get(
                    "decision_input_digest"
                ),
                "primary_evidence_scope": primary_receipt.get("evidence_scope"),
                "jev_evidence_scope": execution_receipt.get("evidence_scope"),
            })
        pair_status_counts = dict(
            sorted(Counter(item["status"] for item in semantic_pair_records).items())
        )
        semantic_pairing = {
            "scope": "execution_only",
            "planned_cases": len(semantic_pair_records),
            "paired_cases": pair_status_counts.get("paired", 0),
            "not_applicable_cases": pair_status_counts.get("not_applicable", 0),
            "mismatched_cases": pair_status_counts.get("mismatch", 0),
            "incomplete_cases": pair_status_counts.get("incomplete", 0),
            "status_counts": pair_status_counts,
            "cases": semantic_pair_records,
        }
    return {
        "schema_version": (
            "public-evaluation-live-pilot-v2"
            if phase.phase == "pilot"
            else "public-evaluation-live-diagnostic-v1"
            if phase.phase == "diagnostic"
            else "public-evaluation-live-first-wave-v4"
        ),
        "dry_run": False,
        "phase": phase.phase,
        "candidate_sha": candidate_sha,
        "cohort_digest": plan.cohort_digest,
        "evaluation_budget_id": PUBLIC_EVALUATION_BUDGET_ID,
        "working_tree_dirty": working_tree_dirty,
        "manifest_digest": plan.manifest_digest,
        "source_manifest_digest": plan.source_manifest_digest,
        "task_inventory_digest": plan.inventory_digest,
        "model_profiles_digest": plan.profiles.fingerprint,
        "task_denominator": len(plan.trials),
        "selected_task_count": len(selected_trials),
        "selected_route_slots": selected_route_slots,
        "successful_route_count": successful_route_count,
        "terminal_route_count": terminal_route_count,
        "evaluated_route_count": evaluated_route_count,
        "not_applicable_route_count": not_applicable_route_count,
        "route_status_counts": route_status_counts,
        "completed_route_count": successful_route_count,
        "route_success_rate": (
            successful_route_count / evaluated_route_count
            if evaluated_route_count
            else None
        ),
        "route_coverage_rate": (
            evaluated_route_count / selected_route_slots
            if selected_route_slots
            else 0.0
        ),
        "semantic_pairing": semantic_pairing,
        "route_denominator": plan.route_slot_count(),
        "routes": route_metrics,
        "skipped_routes": [list(item) for item in phase.skipped_routes],
        "batch": ledger.batch_snapshot(),
        "ledger_status": ledger.status_snapshot(),
        "status": "report_only_no_jev_claim",
    }


def _json_ready(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if type(value) is dict:
        return {str(key): _json_ready(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_json_ready(item) for item in value]
    if type(value) is float and (value != value or value in {float("inf"), float("-inf")}):
        raise ValueError("Non-finite report numbers are forbidden")
    return value


def write_live_report(root, target, report):
    target = validate_live_report_target(root, target)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(_json_ready(report), stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return target


def validate_live_report_target(root, target):
    root = Path(root).resolve()
    evidence = root / ".decagent/evaluation/evidence"
    requested = Path(target)
    requested = (root / requested) if not requested.is_absolute() else requested
    try:
        relative = requested.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Live evaluation reports must be inside the project state directory"
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Live report target path cannot contain symlinks")
    target = requested.resolve(strict=False)
    if not target.is_relative_to(evidence) or target.suffix.lower() != ".json":
        raise ValueError("Live evaluation reports must be JSON under ignored .decagent evidence")
    if target.exists():
        raise FileExistsError("Live evaluation reports are immutable and cannot be overwritten")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def execute_live_public_phase(
    plan,
    *,
    settings,
    tau_python,
    journal_root,
    candidate_sha,
    working_tree_dirty,
    phase="first-wave",
    candidate_ci_verified=False,
    diagnostic_evidence=None,
):
    """Run one frozen live phase under the canonical process and SQLite locks."""
    from evals.benchmarks.pilot_gate import (
        DIAGNOSTIC_BATCH_LIMITS,
        DIAGNOSTIC_LIMITS,
        DIAGNOSTIC_RESOURCE_LIMITS,
        apply_remaining_pilot_budget,
        evaluation_source_digest,
        is_bounded_diagnostic_plan,
        validate_diagnostic_evidence,
    )

    phase_name = phase
    if candidate_ci_verified is not True:
        raise ValueError("Live evaluation requires a CI-verified immutable candidate")
    if phase_name == "first-wave":
        raise ValueError("Unbounded historical first-wave dispatch is closed; use diagnostic")
    if phase_name == "diagnostic":
        if not is_bounded_diagnostic_plan(plan):
            raise ValueError("Diagnostic dispatch requires the approved bounded cohort")
        diagnostic_gate = None
    elif phase_name == "pilot":
        if candidate_ci_verified is not True or not diagnostic_evidence:
            raise ValueError(
                "Pilot dispatch requires a CI-verified candidate and diagnostic evidence"
            )
        diagnostic_gate = validate_diagnostic_evidence(
            plan.root,
            diagnostic_evidence,
            candidate_sha=candidate_sha,
            model_profiles_digest=plan.profiles.fingerprint,
            expected_source_digest=evaluation_source_digest(plan.root),
            expected_source_manifest_digest=plan.source_manifest_digest,
            expected_inventory_digest=plan.inventory_digest,
        )
        if (
            plan.diagnostic_evidence_sha256 != diagnostic_gate["report_sha256"]
            or plan.diagnostic_candidate_sha != candidate_sha
            or diagnostic_gate["candidate_sha"] != candidate_sha
        ):
            raise ValueError("Pilot diagnostic evidence differs from the frozen candidate")
        plan = apply_remaining_pilot_budget(plan, diagnostic_gate)
    else:
        raise ValueError("Live runner phase is not registered")
    if phase_name == "pilot" and plan.mode != "pilot":
        raise ValueError("Pilot dispatch requires the frozen pilot plan")

    from decagent.evaluation.business.public_models import (
        public_first_wave_model_adapter_factory,
    )

    batch_started_at = datetime.now(UTC)
    started = perf_counter()
    with PublicSupervisor(canonical_public_supervisor_lock_path(plan.root)) as supervisor:
        ledger_path = canonical_public_trial_ledger_path(
            plan.root,
            cohort_digest=plan.cohort_digest,
            candidate_sha=candidate_sha,
        )
        authorization = authorize_public_evaluation_phase(
            plan.root,
            phase=phase_name,
            candidate_sha=candidate_sha,
            cohort_digest=plan.cohort_digest,
            ledger_path=ledger_path,
            diagnostic_report_sha256=(
                diagnostic_gate["report_sha256"] if diagnostic_gate is not None else None
            ),
        )
        if authorization["batch_started_at_utc"] is not None:
            batch_started_at = datetime.fromisoformat(
                authorization["batch_started_at_utc"]
            ).astimezone(UTC)
        ledger = open_first_wave_public_trial_ledger(
            plan.root,
            plan,
            supervisor=supervisor,
            candidate_sha=candidate_sha,
            batch_started_at=batch_started_at,
        )
        try:
            finalized_authorization = authorize_public_evaluation_phase(
                plan.root,
                phase=phase_name,
                candidate_sha=candidate_sha,
                cohort_digest=plan.cohort_digest,
                ledger_path=ledger_path,
                diagnostic_report_sha256=(
                    diagnostic_gate["report_sha256"]
                    if diagnostic_gate is not None
                    else None
                ),
                finalize_candidate_successor=True,
            )
            if finalized_authorization["candidate_successor_pending"]:
                raise ValueError("Diagnostic candidate authorization transfer is incomplete")
            runtime = FirstWaveRuntime(
                plan, tau_python=tau_python, journal_root=journal_root
            )
            trials_by_id = {record["trial_id"]: record for record in plan.trials}
            selected_trials = (
                plan.initial_smoke_wave if phase_name == "first-wave" else plan.trials
            )
            prepared = {
                selection["trial_id"]: runtime.trial_factory(
                    trials_by_id[selection["trial_id"]]
                )
                for selection in selected_trials
            }
            if len(prepared) != len(selected_trials):
                raise ValueError(
                    "Every frozen live-phase environment must preflight successfully"
                )
            model_adapter_factory = public_first_wave_model_adapter_factory(
                settings,
                ledger,
                plan.profiles,
                trial_id_for=lambda trial: trial.trial_id,
                include_jev_semantic_callback=phase_name == "pilot",
                include_primary_semantic_callback=phase_name == "pilot",
            )
            routes = ControlledRoutes(
                runtime.environment_factory,
                max_plan_control_steps=32,
                max_react_turns=plan.resource_limits["agent_model_requests"],
                max_tau_steps=plan.resource_limits["tau_steps"],
            )
            phase_receipt = execute_public_phase(
                root=plan.root,
                supervisor=supervisor,
                ledger=ledger,
                controlled_routes=routes,
                phase=phase_name,
                max_machine_seconds=plan.resource_limits["agent_machine_seconds"],
                trial_factory=lambda record: prepared[record["trial_id"]],
                route_kwargs_factory=lambda trial, route: runtime.route_kwargs_factory(
                    model_adapter_factory, trial, route
                ),
            )
            report = live_first_wave_report(
                plan,
                phase_receipt,
                ledger,
                candidate_sha=candidate_sha,
                working_tree_dirty=working_tree_dirty,
            )
            if phase_name == "diagnostic":
                report["diagnostic_limits"] = dict(DIAGNOSTIC_LIMITS)
                report["diagnostic_batch_limits"] = dict(DIAGNOSTIC_BATCH_LIMITS)
                report["diagnostic_resource_limits"] = dict(DIAGNOSTIC_RESOURCE_LIMITS)
                report["evaluation_source_digest"] = evaluation_source_digest(plan.root)
                report["diagnostic_elapsed_seconds"] = max(0.0, perf_counter() - started)
                report["diagnostic_task_records"] = [
                    {
                        key: record[key]
                        for key in (
                            "trial_id",
                            "benchmark",
                            "domain",
                            "split",
                            "case_id",
                            "version",
                        )
                    }
                    for record in plan.trials
                ]
            report["candidate_ci_verified"] = candidate_ci_verified
            if diagnostic_gate is not None:
                report["diagnostic_gate"] = diagnostic_gate
                report["diagnostic_gate"]["remaining_pilot_batch_limits"] = dict(
                    plan.batch_limits
                )
            return phase_receipt, report
        finally:
            ledger.close()


def execute_live_first_wave(
    plan,
    *,
    settings,
    tau_python,
    journal_root,
    candidate_sha,
    working_tree_dirty,
    candidate_ci_verified=False,
):
    """Compatibility wrapper that now executes only the bounded diagnostic cohort."""
    from evals.benchmarks.pilot_gate import build_bounded_diagnostic_plan

    return execute_live_public_phase(
        build_bounded_diagnostic_plan(plan),
        settings=settings,
        tau_python=tau_python,
        journal_root=journal_root,
        candidate_sha=candidate_sha,
        working_tree_dirty=working_tree_dirty,
        phase="diagnostic",
        candidate_ci_verified=candidate_ci_verified,
    )

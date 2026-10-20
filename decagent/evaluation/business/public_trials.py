"""Frozen public-evaluation preflight and crash-safe per-route ledger."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import isfinite
from pathlib import Path
from uuid import uuid4

from decagent.decisions.contracts import digest

from .ledger import GlobalJevLedger
from .manifest_hashing import source_manifest_sha256, task_inventory_sha256
from .model_profiles import EvaluationModelProfiles
from .public_diagnostics import validate_diagnostic
from .public_limits import FIRST_WAVE_BATCH_LIMITS
from .public_supervisor import canonical_public_supervisor_lock_path

_LEDGER_SCHEMA = "public-trial-ledger-v6"
PUBLIC_TRIAL_LEDGER_RELATIVE_PATH = Path(
    ".decagent/business-eval/public-trials/public-smoke-v7.sqlite3"
)
_LEASE_NAME = "public-evaluation"
_RUN_ROUTES = ("react_skill_mcp", "plan_mcp")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_MODEL_ID = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/~-]{0,255}\Z")
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ERROR = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TERMINAL = frozenset({"completed", "failed", "unknown", "scoring_error", "not_applicable"})
_ACTIVE_ATTEMPTS = frozenset({"reserved", "dispatched", "unknown"})
_SCORE_SOURCES = {
    "enterpriseops-gym-original-verifier",
    "tau2-bench-original-evaluate-simulation",
}
_INITIAL_SMOKE_CASES = (
    ("enterpriseops-gym", "csm", "oracle", "task_20260106_161859_265_00364ece_0e836ce8"),
    ("enterpriseops-gym", "itsm", "oracle", "task_20260115_201951_323_391f1eff_659b9dae"),
    ("tau-bench", "retail", "train", "0"),
    (
        "tau-bench",
        "telecom",
        "train",
        "[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|data_mode_off|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_off[PERSONA:Hard]",
    ),
)


def _validate_request_billing_evidence(value, *, actual_cost_usd, actual_cost_source):
    if value is None:
        return None
    required = {
        "gross_amount", "currency", "voucher_credit_amount", "cash_paid_amount",
        "source", "record_id",
    }
    if type(value) is not dict or set(value) != required:
        raise ValueError("Request billing evidence must use the complete frozen shape")
    currency = value["currency"]
    source = value["source"]
    record_id = value["record_id"]
    if type(currency) is not str or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Request billing evidence needs an uppercase three-letter currency")
    if source not in {"provider_activity", "provider_invoice"}:
        raise ValueError("Request billing evidence source is not supported")
    if record_id is not None and (
        type(record_id) is not str or not _SAFE_ID.fullmatch(record_id)
    ):
        raise ValueError("Billing record ID must be a bounded safe value")

    def amount(name, *, required_amount=False):
        raw = value[name]
        if raw is None and not required_amount:
            return None
        if type(raw) is not str:
            raise ValueError("Billing amounts must preserve the provider decimal text")
        try:
            parsed = Decimal(raw)
        except Exception as exc:
            raise ValueError("Billing amounts must be valid decimal text") from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError("Billing amounts must be finite and nonnegative")
        return str(parsed)

    gross = amount("gross_amount", required_amount=True)
    voucher = amount("voucher_credit_amount")
    cash = amount("cash_paid_amount")
    if currency == "USD" and (
        actual_cost_usd is None
        or actual_cost_source != "provider_billed"
        or actual_cost_usd != Decimal(gross)
    ):
        raise ValueError("USD request billing must equal the gross provider fee")
    return {
        "gross_amount": gross,
        "currency": currency,
        "voucher_credit_amount": voucher,
        "cash_paid_amount": cash,
        "source": source,
        "record_id": record_id,
    }


def _validate_score_evidence(value):
    """Validate the small, non-sensitive score facts retained for reports."""
    required = {"source", "value", "success", "forbidden_side_effects"}
    if type(value) is not dict or set(value) != required:
        raise ValueError("Official score evidence must use the complete frozen shape")
    score = value["value"]
    if (
        type(value["source"]) is not str
        or value["source"] not in _SCORE_SOURCES
        or type(score) not in {int, float}
        or not isfinite(score)
        or not 0 <= score <= 1
        or type(value["success"]) is not bool
        or value["success"] is not (score == 1.0)
    ):
        raise ValueError("Official score evidence contains an invalid source or score")
    forbidden = value["forbidden_side_effects"]
    if forbidden is not None and (type(forbidden) is not int or forbidden < 0):
        raise ValueError("Forbidden side effects must be null or a nonnegative integer")


@dataclass(frozen=True)
class PublicRunPlan:
    """Validated, secret-free execution scope derived from frozen public inputs."""

    root: Path
    mode: str
    manifest_digest: str
    source_manifest_digest: str
    inventory_digest: str
    profiles: EvaluationModelProfiles
    routes: tuple[str, ...]
    trials: tuple[dict, ...]
    initial_smoke_wave: tuple[dict, ...]
    resource_limits: dict
    batch_limits: dict
    jev_semantic_status: str

    @property
    def cohort_digest(self):
        return digest(
            {
                "schema": _LEDGER_SCHEMA,
                "mode": self.mode,
                "manifest": self.manifest_digest,
                "source": self.source_manifest_digest,
                "inventory": self.inventory_digest,
                "profiles": self.profiles.fingerprint,
                "routes": self.routes,
                "resource_limits": self.resource_limits,
                "batch_limits": self.batch_limits,
            }
        )


def canonical_public_trial_ledger_path(root):
    return Path(root).resolve() / PUBLIC_TRIAL_LEDGER_RELATIVE_PATH


def validate_public_trial_ledger_path(root, candidate):
    project_root = Path(root).resolve()
    canonical = canonical_public_trial_ledger_path(project_root)
    requested = Path(candidate)
    if not requested.is_absolute():
        requested = project_root / requested
    if requested.resolve(strict=False) != canonical:
        raise ValueError("Public first-wave execution requires its canonical cohort ledger")
    current = project_root
    for component in PUBLIC_TRIAL_LEDGER_RELATIVE_PATH.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("Public trial ledger path cannot contain symlinks")
    return canonical


def open_first_wave_public_trial_ledger(
    root, plan, *, supervisor, candidate_sha, lease_seconds=60
):
    """Open the sole first-wave ledger while holding the process-lifetime lock."""
    from .ledger import open_existing_global_jev_ledger
    from .public_supervisor import PublicSupervisor

    project_root = Path(root).resolve()
    if not isinstance(plan, PublicRunPlan) or plan.mode != "smoke":
        raise ValueError("Only the frozen smoke cohort can enter the first-wave phase")
    if Path(plan.root).resolve() != project_root:
        raise ValueError("Public run plan root does not match the canonical ledger root")
    if (
        type(candidate_sha) is not str
        or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", candidate_sha) is None
    ):
        raise ValueError("A verified immutable candidate SHA is required")
    if not isinstance(supervisor, PublicSupervisor) or not supervisor.held:
        raise ValueError("A held public-evaluation supervisor lock is required")
    if supervisor.path.resolve(strict=False) != canonical_public_supervisor_lock_path(
        project_root
    ):
        raise ValueError("Public evaluation requires the canonical supervisor lock")
    path = validate_public_trial_ledger_path(
        project_root, canonical_public_trial_ledger_path(project_root)
    )
    global_jev_ledger = open_existing_global_jev_ledger(project_root)
    if path.exists():
        ledger = PublicTrialLedger.resume(
            path,
            plan,
            global_jev_ledger=global_jev_ledger,
            lease_seconds=lease_seconds,
        )
    else:
        ledger = PublicTrialLedger.create(
            path,
            plan,
            global_jev_ledger=global_jev_ledger,
            lease_seconds=lease_seconds,
        )
    try:
        ledger.bind_candidate_sha(candidate_sha)
    except BaseException:
        ledger.close()
        raise
    return ledger


def _read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Frozen public evaluation input must be a regular file")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Frozen public evaluation input is unreadable") from exc


def _verified_manifest_digest(manifest):
    recorded = manifest.get("manifest_digest")
    payload = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    if type(recorded) is not str or recorded != digest(payload):
        raise ValueError("Public comparison manifest digest mismatch")
    return recorded


def _validate_readiness(root, profiles, path=None):
    readiness_path = Path(path) if path else (
        Path(root) / "evals/benchmarks/evidence/routed-model-readiness-20261019-102641.json"
    )
    evidence = _read_json(readiness_path)
    ledger = evidence.get("ledger")
    if (
        evidence.get("schema_version") != "routed-model-readiness-evidence-v1"
        or evidence.get("status") != "passed"
        or type(ledger) is not dict
        or ledger.get("schema_version") != "routed-model-readiness-ledger-v1"
        or ledger.get("blocked") is not False
        or type(ledger.get("used_calls")) is not int
        or type(ledger.get("remaining_calls")) is not int
        or ledger["used_calls"] + ledger["remaining_calls"] != ledger.get("max_calls")
    ):
        raise ValueError("Passed routed-model readiness evidence is required")
    for attempt in ledger.get("attempts", []):
        if attempt.get("status") in {"dispatched", "unknown", "reserved"}:
            raise ValueError("Readiness has unresolved model usage")
    if (
        ledger.get("primary_model") != profiles.primary.model
        or ledger.get("primary_endpoint") != profiles.primary.endpoint
    ):
        raise ValueError("Readiness model identity differs from the frozen cohort")
    return readiness_path


def load_public_run_plan(
    root,
    *,
    mode="smoke",
    manifest_path=None,
    source_manifest_path=None,
    profiles_path=None,
    readiness_path=None,
    check_images=False,
    check_runtime=True,
    check_source_files=True,
):
    """Verify frozen inputs and return the exact smoke/full task denominator.

    ``check_source_files=False`` is for offline tests that exercise the
    manifest/ledger contract without downloading public benchmark artifacts.
    Live execution must keep source validation enabled.
    """
    root = Path(root).resolve()
    if mode not in {"smoke", "full"}:
        raise ValueError("Public evaluation mode must be smoke or full")
    manifest_path = Path(manifest_path) if manifest_path else (
        root / "evals/benchmarks/manifests/comparison_trial_manifest_v7.json"
    )
    source_manifest_path = Path(source_manifest_path) if source_manifest_path else (
        root / "evals/benchmarks/manifests/source_manifest.json"
    )
    profiles_path = Path(profiles_path) if profiles_path else (
        root / "evals/benchmarks/manifests/evaluation_model_profiles_v3.json"
    )
    manifest = _read_json(manifest_path)
    source_bytes = source_manifest_path.read_bytes()
    source = _read_json(source_manifest_path)
    profile_document = _read_json(profiles_path)
    profiles = EvaluationModelProfiles.from_dict(profile_document)
    manifest_digest = _verified_manifest_digest(manifest)
    source_digest = source_manifest_sha256(source_bytes)
    inventory_info = source.get("task_inventory")
    if type(inventory_info) is not dict or manifest.get("source_manifest_sha256") != source_digest:
        raise ValueError("Public comparison manifest references another source manifest")
    inventory_path = (root / inventory_info.get("path", "")).resolve()
    if not inventory_path.is_relative_to(root) or inventory_path.is_symlink():
        raise ValueError("Frozen task inventory escapes the project root")
    inventory_bytes = inventory_path.read_bytes()
    inventory_digest = task_inventory_sha256(inventory_bytes)
    if (
        inventory_digest != inventory_info.get("sha256")
        or inventory_digest != manifest.get("task_inventory_sha256")
    ):
        raise ValueError("Frozen task inventory checksum mismatch")
    if (
        manifest.get("schema_version") != "business-comparison-trial-manifest-7"
        or manifest.get("manifest_id") != "decagent-public-workflow-comparison-v7"
        or manifest.get("status") != "pre_registered_not_run"
        or manifest.get("model_profile_status") != "frozen"
        or manifest.get("model_profiles") != profile_document
    ):
        raise ValueError("Public comparison manifest is not a frozen v7 cohort")
    if manifest.get("counts") != {
        "full_trials": 286,
        "smoke_trials": 24,
        "initial_smoke_wave": 4,
    }:
        raise ValueError("Public evaluation denominator differs from the approved 206+80 scope")
    full, smoke = manifest.get("full_trials"), manifest.get("smoke_trials")
    if type(full) is not list or type(smoke) is not list or len(full) != 286 or len(smoke) != 24:
        raise ValueError("Frozen public task lists do not match their exact denominators")
    initial_wave = manifest.get("initial_smoke_wave")
    if type(initial_wave) is not list or len(initial_wave) != len(_INITIAL_SMOKE_CASES):
        raise ValueError("Frozen public manifest must contain the four approved first-wave cases")
    smoke_by_identity = {
        (item.get("benchmark"), item.get("domain"), item.get("split"), item.get("case_id")): item
        for item in smoke
    }
    expected_wave = []
    for identity in _INITIAL_SMOKE_CASES:
        trial = smoke_by_identity.get(identity)
        if trial is None:
            raise ValueError("An approved first-wave task is absent from the smoke denominator")
        expected_wave.append(
            {
                "benchmark": identity[0],
                "domain": identity[1],
                "split": identity[2],
                "case_id": identity[3],
                "trial_id": trial.get("trial_id"),
            }
        )
    if initial_wave != expected_wave:
        raise ValueError("Frozen first-wave task identities or order changed")
    if manifest.get("execution_policy", {}).get("initial_wave_routes") != list(_RUN_ROUTES):
        raise ValueError("Frozen first-wave route order differs from the approved ReAct/Plan pair")
    selected = full if mode == "full" else smoke
    if any(trial.get("status") != "not_run" for trial in selected):
        raise ValueError("Frozen task manifest has already been used as a run ledger")
    trial_ids = [trial.get("trial_id") for trial in selected]
    if any(type(item) is not str or not re.fullmatch(r"[a-f0-9]{64}", item) for item in trial_ids):
        raise ValueError("Frozen public trial identities are invalid")
    if len(set(trial_ids)) != len(trial_ids):
        raise ValueError("Frozen public trial identities are not unique")
    experiments = manifest.get("experiments")
    workflow = next(
        (item for item in experiments if item.get("id") == "workflow-loop"), None
    ) if type(experiments) is list else None
    if workflow is None or workflow.get("routes") != list(_RUN_ROUTES):
        raise ValueError("Public ReAct and plan route pair is not frozen as expected")
    limits = manifest.get("limits")
    required_limits = {
        "agent_model_requests",
        "agent_tokens",
        "tool_calls",
        "agent_machine_seconds",
        "tau_steps",
        "user_simulator_requests",
        "user_simulator_tokens",
        "judge_requests",
        "judge_tokens",
    }
    if type(limits) is not dict or not required_limits <= set(limits):
        raise ValueError("Public evaluation resource limits are incomplete")
    if manifest.get("resource_profile_digest") != digest(limits):
        raise ValueError("Public evaluation resource profile digest mismatch")
    batch_limits = manifest.get("batch_limits")
    if type(batch_limits) is not dict or batch_limits != FIRST_WAVE_BATCH_LIMITS:
        raise ValueError("Public first-wave batch limits differ from the approved stop lines")
    general_profiles = (profiles.primary, profiles.user_simulator, profiles.judge)
    if (
        profiles.jev is None
        or any(
            profile.provider != "qwen" or profile.model != profiles.primary.model
            for profile in general_profiles
        )
    ):
        raise ValueError("The first-wave Qwen roles and Jev profile must be frozen together")
    _validate_readiness(root, profiles, readiness_path)
    if check_runtime:
        from decagent.config import Settings
        from decagent.decisions.adapter import OPENROUTER_ENDPOINT
        from decagent.runtime.config import RuntimeConfig

        settings, runtime = Settings.from_env(), RuntimeConfig.from_env()
        if (
            settings.main_model != profiles.primary.model
            or settings.main_base_url != profiles.primary.endpoint
            or settings.jev_model != profiles.jev.model
            or settings.jev_provider != "openrouter"
            or OPENROUTER_ENDPOINT != profiles.jev.endpoint
            or (
                profiles.primary.pricing_status == "known"
                and (
                    runtime.main_price is None
                    or runtime.main_price.version != profiles.primary.price_version
                    or runtime.main_price.input_per_million
                    != profiles.primary.input_price_per_million
                    or runtime.main_price.output_per_million
                    != profiles.primary.output_price_per_million
                )
            )
        ):
            raise ValueError("Live runtime model configuration differs from the frozen profiles")
    from evals.benchmarks.validate_source_manifest import (
        _inspect_local_images,
        expected_image_refs,
        scoring_prerequisite_issues,
        validate_files,
    )

    issues = validate_files(source_manifest_path) if check_source_files else []
    if check_images:
        local_images = _inspect_local_images(expected_image_refs(source))
        issues.extend(scoring_prerequisite_issues(source, local_images))
    if issues:
        raise ValueError("Public benchmark provenance gate failed: " + "; ".join(issues[:8]))
    benchmark_counts = {}
    for trial in selected:
        benchmark_counts[trial["benchmark"]] = benchmark_counts.get(trial["benchmark"], 0) + 1
    return PublicRunPlan(
        root=root,
        mode=mode,
        manifest_digest=manifest_digest,
        source_manifest_digest=source_digest,
        inventory_digest=inventory_digest,
        profiles=profiles,
        routes=_RUN_ROUTES,
        trials=tuple(selected),
        initial_smoke_wave=tuple(initial_wave),
        resource_limits=dict(limits),
        batch_limits=dict(batch_limits),
        # The pinned first-wave CSM case has an audited natural-language
        # knowledge-article choice template. Jev re-judgment remains a
        # separate paired experiment; the frozen first-wave route schedule is
        # still ReAct/Plan only and must not silently grow here.
        jev_semantic_status="reviewed_csm_natural_case_pairing_pending",
    )


def _trials_for_phase(plan, phase):
    if phase is None:
        phase = plan.mode
    if phase not in {"first-wave", "remaining-smoke", "full", "smoke"}:
        raise ValueError("Public evaluation phase is not registered")
    if phase in {"first-wave", "remaining-smoke", "smoke"} and plan.mode != "smoke":
        raise ValueError("Public evaluation phase is incompatible with this cohort mode")
    if phase == "full" and plan.mode != "full":
        raise ValueError("Public evaluation phase is incompatible with this cohort mode")
    if phase == "first-wave":
        by_id = {item["trial_id"]: item for item in plan.trials}
        selected = tuple(
            by_id[item["trial_id"]]
            for item in plan.initial_smoke_wave
            if item["trial_id"] in by_id
        )
        if len(selected) != len(plan.initial_smoke_wave):
            raise ValueError("Frozen first-wave task selection is incomplete")
        return phase, selected
    if phase == "remaining-smoke":
        trial_ids = {item["trial_id"] for item in plan.initial_smoke_wave}
        return phase, tuple(item for item in plan.trials if item["trial_id"] not in trial_ids)
    return phase, plan.trials


def build_dry_run_report(plan, *, phase=None):
    if not isinstance(plan, PublicRunPlan):
        raise TypeError("A validated public run plan is required")
    phase, selected = _trials_for_phase(plan, phase)
    counts = {}
    for trial in selected:
        counts[trial["benchmark"]] = counts.get(trial["benchmark"], 0) + 1
    return {
        "schema_version": "public-evaluation-dry-run-v1",
        "dry_run": True,
        "mode": plan.mode,
        "phase": phase,
        "manifest_digest": plan.manifest_digest,
        "source_manifest_digest": plan.source_manifest_digest,
        "task_inventory_digest": plan.inventory_digest,
        "model_profiles_digest": plan.profiles.fingerprint,
        "task_denominator": len(plan.trials),
        "route_pair": list(plan.routes),
        "route_attempt_slots": len(plan.trials) * len(plan.routes),
        "selected_task_count": len(selected),
        "selected_route_attempt_slots": len(selected) * len(plan.routes),
        "selected_by_benchmark": counts,
        "selected_trial_ids": [trial["trial_id"] for trial in selected],
        "initial_wave_task_count": len(plan.initial_smoke_wave),
        "initial_wave_route_attempt_slots": len(plan.initial_smoke_wave) * len(plan.routes),
        "initial_smoke_wave": list(plan.initial_smoke_wave),
        "by_benchmark": counts,
        "resource_limits": plan.resource_limits,
        "batch_limits": plan.batch_limits,
        "jev_semantic_status": plan.jev_semantic_status,
        "live_model_calls": 0,
        "mcp_calls": 0,
        "ledger_created": False,
        "status": "preflight_passed_no_execution",
    }


class PublicTrialLedger:
    """SQLite route ledger; ambiguous attempts are durable and never replayed."""

    def __init__(
        self, path, plan, connection, *, global_jev_ledger=None, owner_id,
        generation, lease_seconds,
    ):
        self.path, self.plan, self._connection = Path(path), plan, connection
        self.global_jev_ledger = global_jev_ledger
        self.owner_id, self.generation = owner_id, generation
        self.lease_seconds = lease_seconds
        self._closed = False
        self._fenced = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat, name="decagent-public-eval-lease", daemon=True
        )
        self._heartbeat_thread.start()

    @classmethod
    def create(cls, path, plan, *, global_jev_ledger=None, lease_seconds=60):
        if not isinstance(plan, PublicRunPlan):
            raise TypeError("A validated public run plan is required")
        _validate_lease_seconds(lease_seconds)
        if global_jev_ledger is not None and not isinstance(global_jev_ledger, GlobalJevLedger):
            raise TypeError("A shared GlobalJevLedger is required when one is supplied")
        target = Path(path)
        if target.is_symlink():
            raise ValueError("Public evaluation ledger must be a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        connection = sqlite3.connect(target, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE execution_lease(
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    expires_at_utc TEXT NOT NULL
                );
                CREATE TABLE batch_control(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    started_at_utc TEXT NOT NULL,
                    deadline_at_utc TEXT NOT NULL,
                    stopped_reason TEXT,
                    stopped_at_utc TEXT
                );
                CREATE TABLE route_trials(
                    trial_id TEXT NOT NULL, route TEXT NOT NULL, dataset TEXT NOT NULL,
                    domain TEXT NOT NULL, case_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','running','completed','failed','unknown','scoring_error','not_applicable'
                    )), result_digest TEXT, error_category TEXT,
                    failure_stage TEXT, exception_type TEXT,
                    score_status TEXT NOT NULL DEFAULT 'not_attempted' CHECK(score_status IN (
                        'not_attempted','scored','failed','unknown','not_applicable'
                    )), score_digest TEXT,
                    score_evidence_json TEXT,
                    cleanup_status TEXT NOT NULL DEFAULT 'pending' CHECK(cleanup_status IN (
                        'pending','verified','failed','unknown'
                    )), cleanup_evidence_digest TEXT,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY(trial_id,route)
                );
                CREATE TABLE tau_step_attempts(
                    step_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL, route TEXT NOT NULL,
                    step_number INTEGER NOT NULL CHECK(step_number > 0),
                    kind TEXT NOT NULL CHECK(kind IN ('agent','user','environment')),
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(trial_id,route,step_number),
                    FOREIGN KEY(trial_id,route) REFERENCES route_trials(trial_id,route)
                );
                CREATE TABLE model_attempts(
                    attempt_id TEXT PRIMARY KEY, trial_id TEXT NOT NULL, route TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('primary','jev','user_simulator','judge')),
                    status TEXT NOT NULL CHECK(status IN (
                        'reserved','dispatched','settled','unknown','not_sent','settled_overrun'
                    )), estimated_input_tokens INTEGER NOT NULL,
                    reserved_output_tokens INTEGER NOT NULL, reserved_tokens INTEGER NOT NULL,
                    reserved_cost_usd TEXT, actual_input_tokens INTEGER,
                    actual_output_tokens INTEGER, actual_cost_usd TEXT,
                    actual_cost_source TEXT CHECK(
                        actual_cost_source IS NULL OR actual_cost_source IN (
                            'provider_billed','frozen_profile','unknown'
                        )
                    ),
                    billing_gross_amount TEXT, billing_currency TEXT,
                    billing_voucher_credit_amount TEXT, billing_cash_paid_amount TEXT,
                    billing_source TEXT CHECK(
                        billing_source IS NULL OR billing_source IN (
                            'provider_activity','provider_invoice'
                        )
                    ),
                    billing_record_id TEXT,
                    provider TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
                    price_version TEXT NOT NULL, attempt_number INTEGER NOT NULL,
                    profile_digest TEXT NOT NULL, provider_request_id TEXT, generation_id TEXT,
                    global_jev_reservation_id TEXT,
                    actual_provider TEXT, actual_model TEXT,
                    error_category TEXT, http_status INTEGER,
                    failure_stage TEXT, exception_type TEXT, attempt_seconds REAL,
                    created_at_utc TEXT NOT NULL, dispatched_at_utc TEXT, settled_at_utc TEXT,
                    FOREIGN KEY(trial_id,route) REFERENCES route_trials(trial_id,route)
                );
                CREATE TABLE tool_attempts(
                    tool_attempt_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL, route TEXT NOT NULL,
                    command TEXT NOT NULL, effect TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'reserved','dispatched','settled','unknown','not_sent','settled_overrun'
                    )),
                    duration_seconds REAL, result_digest TEXT,
                    error_category TEXT, failure_stage TEXT, exception_type TEXT,
                    http_status INTEGER, created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(trial_id,route) REFERENCES route_trials(trial_id,route)
                );
                CREATE TABLE machine_time_attempts(
                    reservation_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL, route TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'reserved','dispatched','settled','unknown','not_sent','settled_overrun'
                    )),
                    reserved_seconds REAL NOT NULL, actual_seconds REAL,
                    started_at_utc TEXT, agent_started_at_utc TEXT, deadline_at_utc TEXT,
                    error_category TEXT, created_at_utc TEXT NOT NULL,
                    FOREIGN KEY(trial_id,route) REFERENCES route_trials(trial_id,route)
                );
                """
            )
            metadata = {
                "schema_version": _LEDGER_SCHEMA,
                "cohort_digest": plan.cohort_digest,
                "manifest_digest": plan.manifest_digest,
                "source_manifest_digest": plan.source_manifest_digest,
                "task_inventory_digest": plan.inventory_digest,
                "profiles_digest": plan.profiles.fingerprint,
                "resource_profile_digest": digest(plan.resource_limits),
                "batch_limits_digest": digest(plan.batch_limits),
                "mode": plan.mode,
                "global_jev_ledger_path": (
                    str(global_jev_ledger.path.resolve()) if global_jev_ledger else None
                ),
            }
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?)",
                [(key, json.dumps(value, sort_keys=True)) for key, value in metadata.items()],
            )
            owner_id, generation = uuid4().hex, 1
            connection.execute(
                "INSERT INTO execution_lease(lease_name,owner_id,generation,expires_at_utc) "
                "VALUES(?,?,?,?)",
                (_LEASE_NAME, owner_id, generation, _lease_expiry(lease_seconds)),
            )
            batch_started = datetime.now(UTC)
            batch_deadline = batch_started + timedelta(
                seconds=plan.batch_limits["wall_seconds"]
            )
            connection.execute(
                "INSERT INTO batch_control(singleton,started_at_utc,deadline_at_utc) "
                "VALUES(1,?,?)",
                (batch_started.isoformat(), batch_deadline.isoformat()),
            )
            now = batch_started.isoformat()
            rows = []
            for trial in plan.trials:
                for route in plan.routes:
                    rows.append(
                        (
                            trial["trial_id"],
                            route,
                            trial["benchmark"],
                            trial["domain"],
                            trial["case_id"],
                            "pending",
                            now,
                        )
                    )
            connection.executemany(
                "INSERT INTO route_trials("
                "trial_id,route,dataset,domain,case_id,status,updated_at_utc) "
                "VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            return cls(
                target, plan, connection, global_jev_ledger=global_jev_ledger,
                owner_id=owner_id, generation=generation, lease_seconds=lease_seconds,
            )
        except BaseException:
            connection.close()
            target.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            raise

    @classmethod
    def resume(cls, path, plan, *, global_jev_ledger=None, lease_seconds=60):
        if not isinstance(plan, PublicRunPlan):
            raise TypeError("A validated public run plan is required")
        _validate_lease_seconds(lease_seconds)
        if global_jev_ledger is not None and not isinstance(global_jev_ledger, GlobalJevLedger):
            raise TypeError("A shared GlobalJevLedger is required when one is supplied")
        target = Path(path)
        if target.is_symlink() or not target.is_file():
            raise ValueError("Public evaluation ledger must be an existing regular file")
        connection = sqlite3.connect(target, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            try:
                metadata = {
                    row["key"]: json.loads(row["value"])
                    for row in connection.execute("SELECT key,value FROM metadata")
                }
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Existing public ledger metadata is malformed") from exc
            if "candidate_sha" in metadata and (
                type(metadata["candidate_sha"]) is not str
                or re.fullmatch(
                    r"(?:[a-f0-9]{40}|[a-f0-9]{64})", metadata["candidate_sha"]
                ) is None
            ):
                raise ValueError("Public ledger candidate SHA metadata is malformed")
            if (
                metadata.get("schema_version") != _LEDGER_SCHEMA
                or metadata.get("cohort_digest") != plan.cohort_digest
                or metadata.get("manifest_digest") != plan.manifest_digest
                or metadata.get("profiles_digest") != plan.profiles.fingerprint
                or metadata.get("batch_limits_digest") != digest(plan.batch_limits)
            ):
                raise ValueError("Existing public ledger belongs to a different frozen cohort")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tau_step_attempts("
                "step_id TEXT PRIMARY KEY,trial_id TEXT NOT NULL,route TEXT NOT NULL,"
                "step_number INTEGER NOT NULL CHECK(step_number > 0),"
                "kind TEXT NOT NULL CHECK(kind IN ('agent','user','environment')),"
                "created_at_utc TEXT NOT NULL,UNIQUE(trial_id,route,step_number),"
                "FOREIGN KEY(trial_id,route) REFERENCES route_trials(trial_id,route))"
            )
            # This additive evidence column preserves existing v6 ledgers and their
            # identity while distinguishing provider billing from a frozen-rate estimate.
            model_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(model_attempts)")
            }
            if "actual_cost_source" not in model_columns:
                connection.execute(
                    "ALTER TABLE model_attempts ADD COLUMN actual_cost_source TEXT "
                    "CHECK(actual_cost_source IS NULL OR actual_cost_source IN "
                    "('provider_billed','frozen_profile','unknown'))"
                )
            billing_columns = {
                "billing_gross_amount": "TEXT",
                "billing_currency": "TEXT",
                "billing_voucher_credit_amount": "TEXT",
                "billing_cash_paid_amount": "TEXT",
                "billing_source": "TEXT",
                "billing_record_id": "TEXT",
            }
            for name, sql_type in billing_columns.items():
                if name not in model_columns:
                    connection.execute(
                        f"ALTER TABLE model_attempts ADD COLUMN {name} {sql_type}"
                    )
            for name, sql_type in {
                "failure_stage": "TEXT",
                "exception_type": "TEXT",
            }.items():
                if name not in model_columns:
                    connection.execute(
                        f"ALTER TABLE model_attempts ADD COLUMN {name} {sql_type}"
                    )
            route_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(route_trials)")
            }
            if "score_evidence_json" not in route_columns:
                connection.execute(
                    "ALTER TABLE route_trials ADD COLUMN score_evidence_json TEXT"
                )
            for name in ("failure_stage", "exception_type"):
                if name not in route_columns:
                    connection.execute(
                        f"ALTER TABLE route_trials ADD COLUMN {name} TEXT"
                    )
            tool_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(tool_attempts)")
            }
            for name, sql_type in {
                "failure_stage": "TEXT",
                "exception_type": "TEXT",
                "http_status": "INTEGER",
            }.items():
                if name not in tool_columns:
                    connection.execute(
                        f"ALTER TABLE tool_attempts ADD COLUMN {name} {sql_type}"
                    )
            machine_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(machine_time_attempts)"
                )
            }
            if "started_at_utc" not in machine_columns:
                connection.execute(
                    "ALTER TABLE machine_time_attempts ADD COLUMN started_at_utc TEXT"
                )
            if "agent_started_at_utc" not in machine_columns:
                connection.execute(
                    "ALTER TABLE machine_time_attempts ADD COLUMN agent_started_at_utc TEXT"
                )
            if "deadline_at_utc" not in machine_columns:
                connection.execute(
                    "ALTER TABLE machine_time_attempts ADD COLUMN deadline_at_utc TEXT"
                )
            global_path = metadata.get("global_jev_ledger_path")
            if global_jev_ledger is not None:
                if global_path != str(global_jev_ledger.path.resolve()):
                    raise ValueError(
                        "Existing public ledger is bound to another cumulative Jev ledger"
                    )
            elif global_path is not None:
                global_jev_ledger = GlobalJevLedger(global_path)
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = _now()
                lease = connection.execute(
                    "SELECT owner_id,generation,expires_at_utc FROM execution_lease "
                    "WHERE lease_name=?",
                    (_LEASE_NAME,),
                ).fetchone()
                if lease is None:
                    raise ValueError("Public evaluation execution lease is missing")
                if lease["expires_at_utc"] > now:
                    raise ValueError("Another process holds an active execution lease")
                owner_id, generation = uuid4().hex, lease["generation"] + 1
                cursor = connection.execute(
                    "UPDATE execution_lease SET owner_id=?,generation=?,expires_at_utc=? "
                    "WHERE lease_name=? AND generation=?",
                    (owner_id, generation, _lease_expiry(lease_seconds), _LEASE_NAME,
                     lease["generation"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Public evaluation lease changed during recovery")
                if global_jev_ledger is not None and global_jev_ledger.path.is_file():
                    jev_reservations = connection.execute(
                        "SELECT global_jev_reservation_id FROM model_attempts "
                        "WHERE role='jev' AND status IN ('reserved','dispatched','unknown') "
                        "AND global_jev_reservation_id IS NOT NULL"
                    ).fetchall()
                    for reservation in jev_reservations:
                        global_jev_ledger.mark_unknown_if_pending(
                            reservation["global_jev_reservation_id"]
                        )
                else:
                    unresolved_jev = connection.execute(
                        "SELECT 1 FROM model_attempts WHERE role='jev' "
                        "AND status IN ('reserved','dispatched','unknown') LIMIT 1"
                    ).fetchone()
                    if unresolved_jev:
                        raise ValueError("Shared cumulative Jev ledger is missing for recovery")
                connection.execute(
                    "UPDATE model_attempts SET status='unknown',"
                    "error_category='process_interrupted' "
                    "WHERE status IN ('reserved','dispatched')"
                )
                connection.execute(
                    "UPDATE route_trials SET status='unknown',error_category='process_interrupted',"
                    "updated_at_utc=? WHERE status='running'",
                    (now,),
                )
                connection.execute(
                    "UPDATE tool_attempts SET status='unknown',"
                    "error_category='process_interrupted' "
                    "WHERE status IN ('reserved','dispatched')"
                )
                connection.execute(
                    "UPDATE machine_time_attempts SET status='unknown',"
                    "error_category='process_interrupted' "
                    "WHERE status IN ('reserved','dispatched')"
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return cls(
                target, plan, connection, global_jev_ledger=global_jev_ledger,
                owner_id=owner_id, generation=generation, lease_seconds=lease_seconds,
            )
        except BaseException:
            connection.close()
            raise

    def _active(self):
        if self._closed:
            raise ValueError("Public evaluation ledger is closed")
        if self._fenced:
            raise ValueError("Public evaluation execution lease was lost")

    def _batch_usage(self):
        counted = {"reserved", "dispatched", "settled", "unknown", "settled_overrun"}
        pending = {"reserved", "dispatched", "unknown"}
        qwen_roles = {"primary", "user_simulator", "judge"}
        rows = self._connection.execute(
            "SELECT role,status,actual_input_tokens,actual_output_tokens,reserved_tokens "
            "FROM model_attempts"
        ).fetchall()
        by_role = {}
        for role in qwen_roles:
            selected = [row for row in rows if row["role"] == role]
            by_role[role] = {
                "requests": sum(row["status"] in counted for row in selected),
                "tokens": sum(
                    row["actual_input_tokens"] + row["actual_output_tokens"]
                    for row in selected
                    if row["actual_input_tokens"] is not None
                    and row["actual_output_tokens"] is not None
                )
                + sum(row["reserved_tokens"] for row in selected if row["status"] in pending),
                "actual_tokens": sum(
                    row["actual_input_tokens"] + row["actual_output_tokens"]
                    for row in selected
                    if row["actual_input_tokens"] is not None
                    and row["actual_output_tokens"] is not None
                ),
                "reserved_tokens": sum(
                    row["reserved_tokens"] for row in selected if row["status"] in pending
                ),
            }
        tool_rows = self._connection.execute(
            "SELECT status FROM tool_attempts"
        ).fetchall()
        return {
            "qwen_requests": sum(by_role[role]["requests"] for role in qwen_roles),
            "qwen_tokens": sum(by_role[role]["tokens"] for role in qwen_roles),
            "qwen_actual_tokens": sum(
                by_role[role]["actual_tokens"] for role in qwen_roles
            ),
            "qwen_reserved_tokens": sum(
                by_role[role]["reserved_tokens"] for role in qwen_roles
            ),
            "by_role": by_role,
            "tool_calls": sum(row["status"] in counted for row in tool_rows),
        }

    def batch_snapshot(self):
        self._active()
        control = self._connection.execute(
            "SELECT started_at_utc,deadline_at_utc,stopped_reason,stopped_at_utc "
            "FROM batch_control WHERE singleton=1"
        ).fetchone()
        if control is None:
            raise ValueError("First-wave batch clock is missing")
        now = datetime.now(UTC)
        deadline = datetime.fromisoformat(control["deadline_at_utc"])
        return {
            "limits": dict(self.plan.batch_limits),
            **self._batch_usage(),
            "started_at_utc": control["started_at_utc"],
            "deadline_at_utc": control["deadline_at_utc"],
            "remaining_wall_seconds": max(0, (deadline - now).total_seconds()),
            "stopped_reason": control["stopped_reason"],
            "stopped_at_utc": control["stopped_at_utc"],
        }

    def stop_for_model_profile_drift(self):
        """Persist a cohort stop when a resolved provider identity changes."""
        self._active()
        self._transaction()
        try:
            self._connection.execute(
                "UPDATE batch_control SET stopped_reason=COALESCE(stopped_reason,?), "
                "stopped_at_utc=COALESCE(stopped_at_utc,?) WHERE singleton=1",
                ("model_profile_drift", _now()),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _require_batch_admission(self, *, role=None, qwen_tokens=0, tool_calls=0):
        self._active()
        if not self._connection.in_transaction:
            raise RuntimeError("Batch admission must share the resource reservation transaction")

        def stop_batch(reason):
            self._connection.execute(
                "UPDATE batch_control SET stopped_reason=COALESCE(stopped_reason,?), "
                "stopped_at_utc=COALESCE(stopped_at_utc,?) WHERE singleton=1",
                (reason, _now()),
            )
            # Admission happens before this transaction mutates any resource row. Commit
            # the stop marker so it survives the caller's exception handling.
            self._connection.execute("COMMIT")

        control = self._connection.execute(
            "SELECT deadline_at_utc,stopped_reason FROM batch_control WHERE singleton=1"
        ).fetchone()
        if control is None:
            raise ValueError("First-wave batch clock is missing")
        if control["stopped_reason"] is not None:
            raise ValueError(
                "First-wave batch is stopped: " + control["stopped_reason"]
            )
        if datetime.now(UTC) >= datetime.fromisoformat(control["deadline_at_utc"]):
            stop_batch("wall_seconds")
            raise ValueError("First-wave batch stop line reached: wall_seconds")

        usage = self._batch_usage()
        exceeded = None
        if role in {"primary", "user_simulator", "judge"}:
            role_prefix = {
                "primary": "agent",
                "user_simulator": "user_simulator",
                "judge": "judge",
            }[role]
            if usage["qwen_requests"] + 1 > self.plan.batch_limits["qwen_requests"]:
                exceeded = "qwen_requests"
            elif usage["qwen_tokens"] + qwen_tokens > self.plan.batch_limits["qwen_tokens"]:
                exceeded = "qwen_tokens"
            elif (
                usage["by_role"][role]["requests"] + 1
                > self.plan.batch_limits[f"{role_prefix}_qwen_requests"]
            ):
                exceeded = f"{role_prefix}_qwen_requests"
            elif (
                usage["by_role"][role]["tokens"] + qwen_tokens
                > self.plan.batch_limits[f"{role_prefix}_qwen_tokens"]
            ):
                exceeded = f"{role_prefix}_qwen_tokens"
        if usage["tool_calls"] + tool_calls > self.plan.batch_limits["tool_calls"]:
            exceeded = exceeded or "tool_calls"
        if exceeded is not None:
            stop_batch(exceeded)
            raise ValueError("First-wave batch stop line reached: " + exceeded)

    def _heartbeat(self):
        interval = max(0.01, min(10.0, self.lease_seconds / 3))
        while not self._heartbeat_stop.wait(interval):
            try:
                with sqlite3.connect(self.path, timeout=2, isolation_level=None) as connection:
                    cursor = connection.execute(
                        "UPDATE execution_lease SET expires_at_utc=? "
                        "WHERE lease_name=? AND owner_id=? AND generation=? "
                        "AND expires_at_utc>?",
                        (
                            _lease_expiry(self.lease_seconds), _LEASE_NAME,
                            self.owner_id, self.generation, _now(),
                        ),
                    )
                if cursor.rowcount != 1:
                    self._fenced = True
                    return
            except sqlite3.Error:
                # A later operation checks the persisted expiry and fences this owner.
                continue

    def _transaction(self):
        self._active()
        self._connection.execute("BEGIN IMMEDIATE")
        lease = self._connection.execute(
            "SELECT owner_id,generation,expires_at_utc FROM execution_lease "
            "WHERE lease_name=?",
            (_LEASE_NAME,),
        ).fetchone()
        if (
            lease is None
            or lease["owner_id"] != self.owner_id
            or lease["generation"] != self.generation
            or lease["expires_at_utc"] <= _now()
        ):
            self._connection.execute("ROLLBACK")
            self._fenced = True
            raise ValueError("Public evaluation execution lease was lost")

    @property
    def candidate_sha(self):
        """Return the immutable code candidate attached to this live cohort."""
        self._active()
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key='candidate_sha'"
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Public ledger candidate SHA metadata is malformed") from exc
        if (
            type(value) is not str
            or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is None
        ):
            raise ValueError("Public ledger candidate SHA metadata is malformed")
        return value

    def bind_candidate_sha(self, candidate_sha):
        """Bind an empty live ledger to one immutable, CI-verified source commit."""
        if (
            type(candidate_sha) is not str
            or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", candidate_sha) is None
        ):
            raise ValueError("A valid immutable candidate SHA is required")
        self._transaction()
        try:
            row = self._connection.execute(
                "SELECT value FROM metadata WHERE key='candidate_sha'"
            ).fetchone()
            if row is not None:
                try:
                    existing = json.loads(row["value"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "Public ledger candidate SHA metadata is malformed"
                    ) from exc
                if (
                    type(existing) is not str
                    or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", existing)
                    is None
                ):
                    raise ValueError("Public ledger candidate SHA metadata is malformed")
                if existing != candidate_sha:
                    raise ValueError(
                        "Public ledger candidate SHA differs from the requested candidate"
                    )
                self._connection.execute("COMMIT")
                return existing

            historical_activity = self._connection.execute(
                "SELECT EXISTS(SELECT 1 FROM route_trials WHERE status!='pending') "
                "OR EXISTS(SELECT 1 FROM model_attempts) "
                "OR EXISTS(SELECT 1 FROM tool_attempts) "
                "OR EXISTS(SELECT 1 FROM tau_step_attempts) "
                "OR EXISTS(SELECT 1 FROM machine_time_attempts)"
            ).fetchone()[0]
            if historical_activity:
                raise ValueError(
                    "A public ledger with historical attempts cannot bind a new candidate SHA"
                )
            self._connection.execute(
                "INSERT INTO metadata(key,value) VALUES('candidate_sha',?)",
                (json.dumps(candidate_sha),),
            )
            self._connection.execute("COMMIT")
            return candidate_sha
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def start_route(self, trial_id, route):
        self._active()
        self._transaction()
        try:
            self._require_batch_admission()
            cursor = self._connection.execute(
                "UPDATE route_trials SET status='running',updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='pending'",
                (_now(), trial_id, route),
            )
            if cursor.rowcount != 1:
                row = self._connection.execute(
                    "SELECT status FROM route_trials WHERE trial_id=? AND route=?",
                    (trial_id, route),
                ).fetchone()
                status = row["status"] if row else "missing"
                raise ValueError(f"Route status {status} cannot be replayed")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def start_route_with_machine_time(self, trial_id, route, *, max_seconds):
        """Atomically claim one pending route and reserve its machine-time budget."""
        self._active()
        if (
            type(max_seconds) not in {int, float}
            or not isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("Machine-time reservation must be finite and positive")
        self._transaction()
        try:
            self._require_batch_admission()
            cursor = self._connection.execute(
                "UPDATE route_trials SET status='running',updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='pending'",
                (_now(), trial_id, route),
            )
            if cursor.rowcount != 1:
                row = self._connection.execute(
                    "SELECT status FROM route_trials WHERE trial_id=? AND route=?",
                    (trial_id, route),
                ).fetchone()
                status = row["status"] if row else "missing"
                raise ValueError(f"Route status {status} cannot be replayed")
            self._running_route(trial_id, route)
            unresolved = self._connection.execute(
                "SELECT 1 FROM model_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') UNION ALL "
                "SELECT 1 FROM tool_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') UNION ALL "
                "SELECT 1 FROM machine_time_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') LIMIT 1",
                (trial_id, route, trial_id, route, trial_id, route),
            ).fetchone()
            if unresolved:
                raise ValueError("An unresolved or unknown attempt blocks more machine time")
            snapshot = self.resource_snapshot(trial_id, route)
            limit = self.plan.resource_limits["agent_machine_seconds"]
            if (
                snapshot["actual_agent_machine_seconds"]
                + snapshot["reserved_agent_machine_seconds"]
                + max_seconds
                > limit
            ):
                raise ValueError("Public evaluation machine-time budget exceeded")
            reservation_id = uuid4().hex
            self._connection.execute(
                "INSERT INTO machine_time_attempts(reservation_id,trial_id,route,status,"
                "reserved_seconds,created_at_utc) VALUES(?,?,?,'reserved',?,?)",
                (reservation_id, trial_id, route, float(max_seconds), _now()),
            )
            self._connection.execute("COMMIT")
            return reservation_id
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def finish_route(
        self, trial_id, route, *, status, result_digest=None, error_category=None,
        cleanup_verified, score_status="not_attempted", score_digest=None,
        cleanup_evidence_digest=None, score_evidence=None,
        failure_stage=None, exception_type=None,
    ):
        self._active()
        if status not in _TERMINAL:
            raise ValueError("Route result must be a terminal state")
        if status == "completed" and (
            type(result_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", result_digest)
        ):
            raise ValueError("Completed route requires a content digest")
        if type(cleanup_verified) is not bool:
            raise ValueError("Environment cleanup status must be explicit")
        if status == "completed" and not cleanup_verified:
            raise ValueError("A route cannot complete before environment cleanup is verified")
        if score_status not in {
            "not_attempted", "scored", "failed", "unknown", "not_applicable"
        }:
            raise ValueError("Route scoring state must be explicit")
        if score_status == "scored" and (
            type(score_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", score_digest)
        ):
            raise ValueError("A completed score requires an evidence digest")
        if score_status == "scored" and score_evidence is None:
            raise ValueError("A scored route requires durable official score evidence")
        score_evidence_json = None
        if score_evidence is not None:
            _validate_score_evidence(score_evidence)
            if score_status != "scored" or digest(score_evidence) != score_digest:
                raise ValueError("Score evidence must match a scored route's digest")
            score_evidence_json = json.dumps(
                score_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        if score_status != "scored" and score_digest is not None:
            raise ValueError("Unscored routes cannot carry a score digest")
        if score_status != "scored" and score_evidence is not None:
            raise ValueError("Unscored routes cannot carry score evidence")
        if status == "completed" and score_status != "scored":
            raise ValueError("A completed route requires an independently recorded score")
        if status == "unknown" and score_status not in {"not_attempted", "unknown"}:
            raise ValueError("An unknown route cannot be converted into a scored route")
        if cleanup_verified and (
            type(cleanup_evidence_digest) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", cleanup_evidence_digest)
        ):
            raise ValueError("Verified cleanup requires a durable evidence digest")
        if not cleanup_verified and cleanup_evidence_digest is not None and (
            type(cleanup_evidence_digest) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", cleanup_evidence_digest)
        ):
            raise ValueError("Cleanup evidence digest must be a lowercase SHA-256 value")
        if error_category is not None and (
            type(error_category) is not str or not _ERROR.fullmatch(error_category)
        ):
            raise ValueError("Route error category must be sanitized")
        validate_diagnostic(
            failure_stage=failure_stage,
            exception_type=exception_type,
        )
        self._transaction()
        try:
            existing = self._connection.execute(
                "SELECT status,result_digest,error_category,failure_stage,exception_type,"
                "score_status,score_digest,"
                "score_evidence_json,"
                "cleanup_status,cleanup_evidence_digest FROM route_trials "
                "WHERE trial_id=? AND route=?",
                (trial_id, route),
            ).fetchone()
            if existing is None:
                raise ValueError("Route is outside the frozen public task denominator")
            if existing["status"] == "unknown" and status == "unknown":
                failure_stage = (
                    existing["failure_stage"] if failure_stage is None else failure_stage
                )
                exception_type = (
                    existing["exception_type"] if exception_type is None else exception_type
                )
                error_category = (
                    existing["error_category"] if error_category is None else error_category
                )
                if (
                    existing["failure_stage"] is not None
                    and failure_stage != existing["failure_stage"]
                ) or (
                    existing["exception_type"] is not None
                    and exception_type != existing["exception_type"]
                ) or (
                    existing["error_category"] is not None
                    and error_category != existing["error_category"]
                ):
                    raise ValueError("Unknown-route diagnostics cannot be rewritten")
            if score_evidence is not None:
                trial = self._connection.execute(
                    "SELECT dataset FROM route_trials WHERE trial_id=? AND route=?",
                    (trial_id, route),
                ).fetchone()
                expected_source = (
                    "enterpriseops-gym-original-verifier"
                    if trial["dataset"] == "enterpriseops-gym"
                    else "tau2-bench-original-evaluate-simulation"
                )
                if score_evidence["source"] != expected_source:
                    raise ValueError("Official score evidence source differs from the benchmark")
            cleanup_status = "verified" if cleanup_verified else "failed"
            requested = (
                status,
                result_digest,
                error_category,
                failure_stage,
                exception_type,
                score_status,
                score_digest,
                score_evidence_json,
                cleanup_status,
                cleanup_evidence_digest,
            )
            current = tuple(existing[key] for key in (
                "status", "result_digest", "error_category", "failure_stage", "exception_type",
                "score_status", "score_digest",
                "score_evidence_json",
                "cleanup_status", "cleanup_evidence_digest",
            ))
            if existing["status"] == "unknown" and status == "unknown":
                if (
                    existing["score_status"] not in {"not_attempted", "unknown"}
                    or score_status not in {"not_attempted", "unknown"}
                ):
                    raise ValueError("An unknown route cannot be rewritten as scored")
                if existing["cleanup_status"] != "pending" and (
                    existing["cleanup_status"] != cleanup_status
                    or existing["cleanup_evidence_digest"] != cleanup_evidence_digest
                ):
                    raise ValueError("Unknown-route cleanup evidence cannot be rewritten")
                self._connection.execute(
                    "UPDATE route_trials SET score_status=?,cleanup_status=?,"
                    "cleanup_evidence_digest=?,updated_at_utc=? "
                    "WHERE trial_id=? AND route=? AND status='unknown'",
                    (
                        score_status,
                        cleanup_status,
                        cleanup_evidence_digest,
                        _now(),
                        trial_id,
                        route,
                    ),
                )
                self._connection.execute("COMMIT")
                return
            if existing["status"] not in {"running", status}:
                raise ValueError("A terminal route cannot be rewritten")
            if existing["status"] == status:
                if current != requested:
                    raise ValueError("A terminal route cannot be rewritten")
                self._connection.execute("COMMIT")
                return
            active = self._connection.execute(
                "SELECT count(*) FROM model_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown')",
                (trial_id, route),
            ).fetchone()[0]
            active += self._connection.execute(
                "SELECT count(*) FROM tool_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown')",
                (trial_id, route),
            ).fetchone()[0]
            active += self._connection.execute(
                "SELECT count(*) FROM machine_time_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown')",
                (trial_id, route),
            ).fetchone()[0]
            if active and status != "unknown":
                raise ValueError("Unresolved or unknown model usage blocks route finalization")
            cursor = self._connection.execute(
                "UPDATE route_trials SET status=?,result_digest=?,error_category=?,"
                "failure_stage=?,exception_type=?,"
                "score_status=?,score_digest=?,score_evidence_json=?,cleanup_status=?,"
                "cleanup_evidence_digest=?,"
                "updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='running'",
                (
                    status, result_digest, error_category, failure_stage, exception_type,
                    score_status, score_digest,
                    score_evidence_json,
                    cleanup_status, cleanup_evidence_digest, _now(), trial_id, route,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a running route can be finalized")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def route_lifecycle(self, trial_id, route):
        self._active()
        row = self._connection.execute(
            "SELECT status,error_category,failure_stage,exception_type,score_status,"
            "score_digest,score_evidence_json,cleanup_status,"
            "cleanup_evidence_digest "
            "FROM route_trials WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchone()
        if row is None:
            raise ValueError("Route is outside the frozen public task denominator")
        result = dict(row)
        evidence = result.pop("score_evidence_json")
        result["score_evidence"] = json.loads(evidence) if evidence is not None else None
        return result

    def _expected_profile(self, role):
        return self.plan.profiles.digests[
            {
                "primary": "model_profile_digest",
                "jev": "jev_profile_digest",
                "user_simulator": "user_simulator_profile_digest",
                "judge": "judge_profile_digest",
            }[role]
        ]

    def _profile(self, role):
        return getattr(self.plan.profiles, role)

    def _usage(self, trial_id, route, roles):
        marks = ",".join("?" for _ in roles)
        rows = self._connection.execute(
            "SELECT status,estimated_input_tokens,reserved_output_tokens,reserved_tokens,"
            "reserved_cost_usd,"
            "actual_input_tokens,actual_output_tokens,actual_cost_usd FROM model_attempts "
            f"WHERE trial_id=? AND route=? AND role IN ({marks})",
            (trial_id, route, *roles),
        ).fetchall()
        dispatched_states = {"dispatched", "settled", "unknown", "settled_overrun"}
        pending_states = {"reserved", "dispatched", "unknown"}
        costs_complete = all(
            row["status"] in {"settled", "settled_overrun", "not_sent"}
            and (row["status"] == "not_sent" or row["actual_cost_usd"] is not None)
            for row in rows
        )
        reserved_costs_complete = all(
            row["status"] not in pending_states or row["reserved_cost_usd"] is not None
            for row in rows
        )
        actual_cost = sum(
            (
                Decimal(row["actual_cost_usd"])
                for row in rows
                if row["actual_cost_usd"] is not None
            ),
            Decimal("0"),
        )
        reserved_cost = sum(
            (
                Decimal(row["reserved_cost_usd"])
                for row in rows
                if row["status"] in pending_states and row["reserved_cost_usd"] is not None
            ),
            Decimal("0"),
        )
        return {
            "requests": sum(row["status"] in dispatched_states | {"reserved"} for row in rows),
            "actual_tokens": sum(
                row["actual_input_tokens"] + row["actual_output_tokens"]
                for row in rows
                if row["actual_input_tokens"] is not None
                and row["actual_output_tokens"] is not None
            ),
            "reserved_tokens": sum(
                row["reserved_tokens"] for row in rows if row["status"] in pending_states
            ),
            "unknown_requests": sum(row["status"] == "unknown" for row in rows),
            "actual_cost_usd": actual_cost if costs_complete else None,
            "actual_cost_complete": costs_complete,
            "reserved_cost_usd": reserved_cost if reserved_costs_complete else None,
            "reserved_cost_complete": reserved_costs_complete,
        }

    def usage_snapshot(self, trial_id, route):
        self._active()
        return {
            "primary": self._usage(trial_id, route, ("primary",)),
            "jev": self._usage(trial_id, route, ("jev",)),
            "agent": self._usage(trial_id, route, ("primary", "jev")),
            "user_simulator": self._usage(trial_id, route, ("user_simulator",)),
            "judge": self._usage(trial_id, route, ("judge",)),
        }

    def reserve_model_attempt(
        self, *, trial_id, route, role, estimated_input_tokens, max_output_tokens,
        reserved_cost_usd, profile_digest,
    ):
        self._active()
        if role not in {"primary", "jev", "user_simulator", "judge"}:
            raise ValueError("Unknown public evaluation model role")
        profile = self._profile(role)
        if (
            type(profile_digest) is not str
            or profile_digest != profile.fingerprint
            or profile_digest != self._expected_profile(role)
        ):
            raise ValueError("Model profile differs from the frozen public cohort")
        if (
            type(estimated_input_tokens) is not int
            or not 1 <= estimated_input_tokens <= profile.max_input_tokens
            or type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= profile.max_output_tokens
        ):
            raise ValueError("Model attempt exceeds frozen per-request token bounds")
        if (
            reserved_cost_usd is not None
            and (
                type(reserved_cost_usd) is not Decimal
                or not reserved_cost_usd.is_finite()
                or reserved_cost_usd < 0
            )
        ):
            raise ValueError(
                "Model attempt requires a finite nonnegative or unknown fee reservation"
            )
        if profile.pricing_status == "unknown":
            if reserved_cost_usd is not None:
                raise ValueError("Unknown pricing cannot be represented by a numeric reservation")
            minimum_reservation = None
        else:
            if reserved_cost_usd is None:
                raise ValueError("Known pricing requires a numeric fee reservation")
            minimum_reservation = (
                profile.input_price_per_million * estimated_input_tokens
                + profile.output_price_per_million * max_output_tokens
            ) / Decimal(1_000_000)
            if reserved_cost_usd < minimum_reservation:
                raise ValueError("Model fee reservation is below the frozen profile upper bound")
        requested_tokens = estimated_input_tokens + max_output_tokens
        self._transaction()
        global_reservation_id = None
        try:
            self._require_batch_admission(role=role, qwen_tokens=requested_tokens)
            route_row = self._connection.execute(
                "SELECT status FROM route_trials WHERE trial_id=? AND route=?",
                (trial_id, route),
            ).fetchone()
            if route_row is not None and route_row["status"] == "unknown":
                raise ValueError("unresolved or unknown model usage blocks further calls")
            if route_row is None or route_row["status"] != "running":
                raise ValueError("Model calls require a running frozen route")
            unresolved = self._connection.execute(
                "SELECT 1 FROM model_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') LIMIT 1",
                (trial_id, route),
            ).fetchone()
            if unresolved:
                raise ValueError("An unresolved or unknown model attempt blocks another call")
            unresolved = self._connection.execute(
                "SELECT 1 FROM tool_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') LIMIT 1",
                (trial_id, route),
            ).fetchone()
            if unresolved:
                raise ValueError("An unresolved or unknown tool attempt blocks another call")
            usage = self.usage_snapshot(trial_id, route)
            if role in {"primary", "jev"}:
                current = usage["agent"]
                request_cap = self.plan.resource_limits["agent_model_requests"]
                token_cap = self.plan.resource_limits["agent_tokens"]
            elif role == "user_simulator":
                current = usage[role]
                request_cap = self.plan.resource_limits["user_simulator_requests"]
                token_cap = self.plan.resource_limits["user_simulator_tokens"]
            else:
                current = usage[role]
                request_cap = self.plan.resource_limits["judge_requests"]
                token_cap = self.plan.resource_limits["judge_tokens"]
            if current["requests"] + 1 > request_cap:
                raise ValueError("Public evaluation model request budget exceeded")
            if current["actual_tokens"] + current["reserved_tokens"] + requested_tokens > token_cap:
                raise ValueError("Public evaluation token budget exceeded before dispatch")
            if role == "jev":
                jev_usage = usage["jev"]
                if jev_usage["actual_cost_usd"] is None or jev_usage["reserved_cost_usd"] is None:
                    raise ValueError("Unknown Jev fee state blocks further public calls")
                jev_reserved = (
                    jev_usage["actual_cost_usd"]
                    + jev_usage["reserved_cost_usd"]
                    + reserved_cost_usd
                )
                if jev_reserved > Decimal("1"):
                    raise ValueError(
                        "Public evaluation Jev fee reservation exceeds the authorized $1 cap"
                    )
            attempt_id = uuid4().hex
            attempt_number = self._connection.execute(
                "SELECT count(*) FROM model_attempts WHERE trial_id=? AND route=? AND role=?",
                (trial_id, route, role),
            ).fetchone()[0] + 1
            if role == "jev":
                if self.global_jev_ledger is None:
                    raise ValueError("Public Jev requests require the shared cumulative ledger")
                global_reservation_id = self.global_jev_ledger.reserve(
                    reserved_cost_usd,
                    {
                        "source": "public-benchmark",
                        "cohort_digest": self.plan.cohort_digest,
                        "trial_id": trial_id,
                        "route": route,
                        "attempt_id": attempt_id,
                        "profile_digest": profile_digest,
                    },
                )
                if global_reservation_id is None:
                    raise ValueError("Shared cumulative Jev budget is unavailable or blocked")
            self._connection.execute(
                "INSERT INTO model_attempts(attempt_id,trial_id,route,role,status,"
                "estimated_input_tokens,reserved_output_tokens,reserved_tokens,reserved_cost_usd,"
                "provider,model,endpoint,price_version,attempt_number,profile_digest,"
                "global_jev_reservation_id,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, trial_id, route, role, "reserved", estimated_input_tokens,
                    max_output_tokens, requested_tokens,
                    str(reserved_cost_usd) if reserved_cost_usd is not None else None,
                    profile.provider,
                    profile.model, profile.endpoint, profile.price_version, attempt_number,
                    profile_digest, global_reservation_id, _now(),
                ),
            )
            self._connection.execute("COMMIT")
            return attempt_id
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if global_reservation_id is not None:
                self.global_jev_ledger.settle(
                    global_reservation_id, tokens=0, cost=Decimal(0), actual_model=None
                )
            raise

    def mark_model_dispatched(self, attempt_id):
        self._active()
        self._transaction()
        try:
            self._require_batch_admission()
            cursor = self._connection.execute(
                "UPDATE model_attempts SET status='dispatched',dispatched_at_utc=? "
                "WHERE attempt_id=? AND status='reserved'",
                (_now(), attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a reserved model attempt can be dispatched")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def settle_model_attempt(
        self, attempt_id, *, actual_input_tokens, actual_output_tokens, actual_cost_usd,
        provider_request_id=None, generation_id=None, http_status=None, attempt_seconds=None,
        error_category=None, actual_provider=None, actual_model=None, actual_cost_source=None,
        billing_evidence=None,
    ):
        self._active()
        if actual_cost_source is None:
            actual_cost_source = "unknown" if actual_cost_usd is None else "frozen_profile"
        if (
            type(actual_input_tokens) is not int or actual_input_tokens < 0
            or type(actual_output_tokens) is not int or actual_output_tokens < 0
            or (
                actual_cost_usd is not None
                and (
                    type(actual_cost_usd) is not Decimal
                    or not actual_cost_usd.is_finite()
                    or actual_cost_usd < 0
                )
            )
            or actual_cost_source not in {"provider_billed", "frozen_profile", "unknown"}
            or ((actual_cost_usd is None) != (actual_cost_source == "unknown"))
            or (
                http_status is not None
                and (type(http_status) is not int or not 100 <= http_status <= 599)
            )
            or (
                attempt_seconds is not None
                and (
                    type(attempt_seconds) not in {int, float}
                    or not isfinite(attempt_seconds)
                    or attempt_seconds < 0
                )
            )
            or (
                error_category is not None
                and (type(error_category) is not str or not _ERROR.fullmatch(error_category))
            )
            or (
                actual_provider is not None
                and (
                    type(actual_provider) is not str
                    or not _PROVIDER_ID.fullmatch(actual_provider)
                )
            )
            or (
                actual_model is not None
                and (type(actual_model) is not str or not _MODEL_ID.fullmatch(actual_model))
            )
        ):
            raise ValueError("Settled model usage must be complete and nonnegative")
        for identifier in (provider_request_id, generation_id):
            if identifier is not None and (
                type(identifier) is not str or not _SAFE_ID.fullmatch(identifier)
            ):
                raise ValueError("Provider identifiers must be bounded safe values")
        normalized_billing = _validate_request_billing_evidence(
            billing_evidence, actual_cost_usd=actual_cost_usd,
            actual_cost_source=actual_cost_source,
        )
        self._transaction()
        try:
            row = self._connection.execute(
                "SELECT * FROM model_attempts WHERE attempt_id=? AND status='dispatched'",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Only a dispatched model attempt can be settled")
            profile = self._profile(row["role"])
            if actual_cost_usd is None:
                if row["role"] == "jev" or profile.pricing_status != "unknown":
                    raise ValueError(
                        "Unknown model cost is only valid for an unknown-price profile"
                    )
            global_error = None
            if row["role"] == "jev":
                if self.global_jev_ledger is None or not row["global_jev_reservation_id"]:
                    raise ValueError("Jev attempt is not bound to the shared cumulative ledger")
                try:
                    self.global_jev_ledger.settle(
                        row["global_jev_reservation_id"],
                        tokens=actual_input_tokens + actual_output_tokens,
                        cost=actual_cost_usd,
                        actual_model=actual_model,
                    )
                except ValueError as exc:
                    if actual_cost_usd > Decimal(row["reserved_cost_usd"]):
                        global_error = exc
                    else:
                        raise
            overrun = (
                actual_input_tokens > row["estimated_input_tokens"]
                or actual_output_tokens > row["reserved_output_tokens"]
                or (
                    actual_cost_usd is not None
                    and row["reserved_cost_usd"] is not None
                    and actual_cost_usd > Decimal(row["reserved_cost_usd"])
                )
            )
            status = "settled_overrun" if overrun else "settled"
            self._connection.execute(
                "UPDATE model_attempts SET status=?,actual_input_tokens=?,actual_output_tokens=?,"
                "actual_cost_usd=?,actual_cost_source=?,provider_request_id=?,generation_id=?,"
                "actual_provider=?,actual_model=?,billing_gross_amount=?,billing_currency=?,"
                "billing_voucher_credit_amount=?,billing_cash_paid_amount=?,billing_source=?,"
                "billing_record_id=?,"
                "error_category=?,http_status=?,attempt_seconds=?,settled_at_utc=? "
                "WHERE attempt_id=?",
                (
                    status, actual_input_tokens, actual_output_tokens,
                    str(actual_cost_usd) if actual_cost_usd is not None else None,
                    actual_cost_source,
                    provider_request_id, generation_id, actual_provider, actual_model,
                    normalized_billing["gross_amount"] if normalized_billing else None,
                    normalized_billing["currency"] if normalized_billing else None,
                    normalized_billing["voucher_credit_amount"] if normalized_billing else None,
                    normalized_billing["cash_paid_amount"] if normalized_billing else None,
                    normalized_billing["source"] if normalized_billing else None,
                    normalized_billing["record_id"] if normalized_billing else None,
                    "resource_overrun" if overrun else error_category, http_status,
                    float(attempt_seconds) if attempt_seconds is not None else None,
                    _now(), attempt_id,
                ),
            )
            if overrun:
                self._connection.execute(
                    "UPDATE route_trials SET status='failed',error_category='resource_overrun',"
                    "updated_at_utc=? WHERE trial_id=? AND route=? AND status='running'",
                    (_now(), row["trial_id"], row["route"]),
                )
            if row["role"] in {"primary", "user_simulator", "judge"}:
                batch_usage = self._batch_usage()
                role_prefix = {
                    "primary": "agent",
                    "user_simulator": "user_simulator",
                    "judge": "judge",
                }[row["role"]]
                stop_reason = None
                if batch_usage["qwen_tokens"] > self.plan.batch_limits["qwen_tokens"]:
                    stop_reason = "qwen_tokens"
                elif (
                    batch_usage["by_role"][row["role"]]["tokens"]
                    > self.plan.batch_limits[f"{role_prefix}_qwen_tokens"]
                ):
                    stop_reason = f"{role_prefix}_qwen_tokens"
                if stop_reason is not None:
                    self._connection.execute(
                        "UPDATE batch_control SET stopped_reason=COALESCE(stopped_reason,?), "
                        "stopped_at_utc=COALESCE(stopped_at_utc,?) WHERE singleton=1",
                        (stop_reason, _now()),
                    )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        if overrun:
            raise ValueError("Model response exceeded its pre-dispatch resource reservation")
        if global_error is not None:
            raise ValueError(
                "Actual Jev cost exceeded reserved bound; global ledger preserved"
            ) from global_error

    def mark_model_unknown(
        self, attempt_id, *, error_category, http_status=None, attempt_seconds=None,
        failure_stage=None, exception_type=None,
    ):
        self._active()
        if (
            type(error_category) is not str or not _ERROR.fullmatch(error_category)
            or (
                http_status is not None
                and (type(http_status) is not int or not 100 <= http_status <= 599)
            )
            or (
                attempt_seconds is not None
                and (
                    type(attempt_seconds) not in {int, float}
                    or not isfinite(attempt_seconds)
                    or attempt_seconds < 0
                )
            )
        ):
            raise ValueError("Unknown model failure category must be sanitized")
        validate_diagnostic(
            failure_stage=failure_stage,
            exception_type=exception_type,
            http_status=http_status,
        )
        self._transaction()
        try:
            reservation = self._connection.execute(
                "SELECT role,global_jev_reservation_id FROM model_attempts "
                "WHERE attempt_id=? AND status IN ('reserved','dispatched')",
                (attempt_id,),
            ).fetchone()
            if reservation is None:
                raise ValueError("Only an unresolved model attempt can become unknown")
            if reservation["role"] == "jev":
                if self.global_jev_ledger is None or not reservation["global_jev_reservation_id"]:
                    raise ValueError("Jev attempt is not bound to the shared cumulative ledger")
                self.global_jev_ledger.mark_unknown_if_pending(
                    reservation["global_jev_reservation_id"]
                )
            cursor = self._connection.execute(
                "UPDATE model_attempts SET status='unknown',error_category=?,http_status=?,"
                "failure_stage=?,exception_type=?,"
                "attempt_seconds=?,settled_at_utc=? "
                "WHERE attempt_id=? AND status IN ('reserved','dispatched')",
                (
                    error_category, http_status, failure_stage, exception_type,
                    float(attempt_seconds) if attempt_seconds is not None else None,
                    _now(), attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only an unresolved model attempt can become unknown")
            row = self._connection.execute(
                "SELECT trial_id,route FROM model_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            self._connection.execute(
                "UPDATE route_trials SET status='unknown',error_category=?,failure_stage=?,"
                "exception_type=?,updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='running'",
                (
                    error_category, failure_stage, exception_type, _now(),
                    row["trial_id"], row["route"],
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def model_attempts_snapshot(self, trial_id, route):
        """Return whitelisted per-request facts without prompts or response content."""
        self._active()
        rows = self._connection.execute(
            "SELECT attempt_id,role,status,provider,model,actual_provider,actual_model,"
            "actual_input_tokens,actual_output_tokens,actual_cost_usd,actual_cost_source,"
            "provider_request_id,generation_id,error_category,http_status,failure_stage,"
            "exception_type,attempt_seconds FROM model_attempts "
            "WHERE trial_id=? AND route=? ORDER BY created_at_utc,attempt_number",
            (trial_id, route),
        ).fetchall()
        return [dict(row) for row in rows]

    def model_attempt_snapshot(self, attempt_id):
        self._active()
        row = self._connection.execute(
            "SELECT attempt_id,trial_id,route,role,status,provider,model,endpoint,price_version,"
            "attempt_number,profile_digest,estimated_input_tokens,reserved_output_tokens,"
            "actual_input_tokens,actual_output_tokens,reserved_cost_usd,actual_cost_usd,"
            "actual_cost_source,"
            "billing_gross_amount,billing_currency,billing_voucher_credit_amount,"
            "billing_cash_paid_amount,billing_source,billing_record_id,"
                "provider_request_id,generation_id,global_jev_reservation_id,actual_provider,"
                "actual_model,http_status,attempt_seconds,error_category,failure_stage,"
                "exception_type,"
            "created_at_utc,dispatched_at_utc,settled_at_utc FROM model_attempts "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown public model attempt")
        return dict(row)

    def billing_snapshot(self, trial_id, route):
        """Report request-level billing evidence without spreading aggregate data."""
        self._active()
        rows = self._connection.execute(
            "SELECT attempt_id,role,status,actual_cost_usd,actual_cost_source,"
            "billing_gross_amount,billing_currency,billing_voucher_credit_amount,"
            "billing_cash_paid_amount,billing_source,billing_record_id "
            "FROM model_attempts WHERE trial_id=? AND route=? "
            "AND status IN ('settled','settled_overrun') ORDER BY created_at_utc,attempt_number",
            (trial_id, route),
        ).fetchall()
        records = []
        by_currency = {}
        for row in rows:
            if row["billing_gross_amount"] is None:
                continue
            currency = row["billing_currency"]
            group = by_currency.setdefault(
                currency,
                {
                    "evidence_attempts": 0,
                    "gross_api_fee": Decimal(0),
                    "known_voucher_credit": Decimal(0),
                    "voucher_credit_missing": 0,
                    "known_cash_paid": Decimal(0),
                    "cash_paid_missing": 0,
                },
            )
            gross = Decimal(row["billing_gross_amount"])
            voucher = (
                Decimal(row["billing_voucher_credit_amount"])
                if row["billing_voucher_credit_amount"] is not None
                else None
            )
            cash = (
                Decimal(row["billing_cash_paid_amount"])
                if row["billing_cash_paid_amount"] is not None
                else None
            )
            group["evidence_attempts"] += 1
            group["gross_api_fee"] += gross
            if voucher is None:
                group["voucher_credit_missing"] += 1
            else:
                group["known_voucher_credit"] += voucher
            if cash is None:
                group["cash_paid_missing"] += 1
            else:
                group["known_cash_paid"] += cash
            records.append(
                {
                    "attempt_id": row["attempt_id"],
                    "role": row["role"],
                    "currency": currency,
                    "gross_api_fee": str(gross),
                    "voucher_credit": str(voucher) if voucher is not None else None,
                    "cash_paid": str(cash) if cash is not None else None,
                    "source": row["billing_source"],
                    "record_id": row["billing_record_id"],
                }
            )
        result = {}
        for currency, group in by_currency.items():
            result[currency] = {
                "evidence_attempts": group["evidence_attempts"],
                "gross_api_fee": str(group["gross_api_fee"]),
                "voucher_credit": (
                    str(group["known_voucher_credit"])
                    if group["voucher_credit_missing"] == 0
                    else None
                ),
                "voucher_credit_partial": str(group["known_voucher_credit"]),
                "voucher_credit_missing": group["voucher_credit_missing"],
                "cash_paid": (
                    str(group["known_cash_paid"])
                    if group["cash_paid_missing"] == 0
                    else None
                ),
                "cash_paid_partial": str(group["known_cash_paid"]),
                "cash_paid_missing": group["cash_paid_missing"],
            }
        return {
            "settled_attempts": len(rows),
            "evidence_attempts": len(records),
            "missing_evidence_attempts": len(rows) - len(records),
            "by_currency": result,
            "records": records,
        }

    def mark_model_not_sent(self, attempt_id, *, reason, verified_not_sent):
        self._active()
        if type(reason) is not str or not _ERROR.fullmatch(reason) or verified_not_sent is not True:
            raise ValueError("Only a verified local preflight failure can release a reservation")
        self._transaction()
        try:
            reservation = self._connection.execute(
                "SELECT role,status,global_jev_reservation_id FROM model_attempts "
                "WHERE attempt_id=? AND status IN ('reserved','dispatched')",
                (attempt_id,),
            ).fetchone()
            if reservation is None:
                raise ValueError("Only unresolved model attempts can be confirmed not sent")
            if reservation["role"] == "jev":
                if self.global_jev_ledger is None or not reservation["global_jev_reservation_id"]:
                    raise ValueError("Jev attempt is not bound to the shared cumulative ledger")
                self.global_jev_ledger.settle(
                    reservation["global_jev_reservation_id"],
                    tokens=0,
                    cost=Decimal(0),
                    actual_model=None,
                )
            cursor = self._connection.execute(
                "UPDATE model_attempts SET status='not_sent',error_category=? "
                "WHERE attempt_id=? AND status IN ('reserved','dispatched')",
                (reason, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only unresolved model attempts can be confirmed not sent")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def _running_route(self, trial_id, route):
        row = self._connection.execute(
            "SELECT status FROM route_trials WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchone()
        if row is None:
            raise ValueError("Resource reservation is outside the frozen public route cohort")
        if row["status"] == "unknown":
            raise ValueError("Unknown public route cannot be replayed")
        if row["status"] != "running":
            raise ValueError("Resource reservations require a running public route")

    def _require_route(self, trial_id, route):
        row = self._connection.execute(
            "SELECT status FROM route_trials WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchone()
        if row is None:
            raise ValueError("Resource scope is outside the frozen public route cohort")
        return row["status"]

    def resource_snapshot(self, trial_id, route):
        self._active()
        self._require_route(trial_id, route)
        tau_steps = self._connection.execute(
            "SELECT COUNT(*) AS count FROM tau_step_attempts WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchone()["count"]
        tool_rows = self._connection.execute(
            "SELECT status FROM tool_attempts WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchall()
        time_rows = self._connection.execute(
            "SELECT status,reserved_seconds,actual_seconds FROM machine_time_attempts "
            "WHERE trial_id=? AND route=?",
            (trial_id, route),
        ).fetchall()
        counted = {"reserved", "dispatched", "settled", "unknown", "settled_overrun"}
        pending = {"reserved", "dispatched", "unknown"}
        return {
            "tau_steps": tau_steps,
            "tool_calls": sum(row["status"] in counted for row in tool_rows),
            "unknown_tool_calls": sum(row["status"] == "unknown" for row in tool_rows),
            "actual_agent_machine_seconds": sum(
                row["actual_seconds"] or 0.0
                for row in time_rows
                if row["status"] in {"settled", "settled_overrun"}
            ),
            "reserved_agent_machine_seconds": sum(
                row["reserved_seconds"] for row in time_rows if row["status"] in pending
            ),
            "unknown_machine_time_reservations": sum(
                row["status"] == "unknown" for row in time_rows
            ),
        }

    def reserve_tau_step(self, *, trial_id, route, kind):
        """Persist one official τ half-duplex turn before its action is dispatched."""
        self._active()
        if type(kind) is not str or kind not in {"agent", "user", "environment"}:
            raise ValueError("τ-Bench step kind must be agent, user, or environment")
        self._transaction()
        try:
            self._require_batch_admission()
            self._running_route(trial_id, route)
            route_row = self._connection.execute(
                "SELECT dataset FROM route_trials WHERE trial_id=? AND route=?",
                (trial_id, route),
            ).fetchone()
            if route_row["dataset"] != "tau-bench":
                raise ValueError("Official τ-Bench steps apply only to τ routes")
            current = self._connection.execute(
                "SELECT COUNT(*) AS count FROM tau_step_attempts WHERE trial_id=? AND route=?",
                (trial_id, route),
            ).fetchone()["count"]
            limit = self.plan.resource_limits["tau_steps"]
            if current >= limit:
                raise ValueError("Official τ-Bench step limit exceeded")
            step_number = current + 1
            self._connection.execute(
                "INSERT INTO tau_step_attempts("
                "step_id,trial_id,route,step_number,kind,created_at_utc) "
                "VALUES(?,?,?,?,?,?)",
                (uuid4().hex, trial_id, route, step_number, kind, _now()),
            )
            self._connection.execute("COMMIT")
            return step_number
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def route_remaining_seconds(self, trial_id, route):
        """Return the current dispatched machine-time window's wall-clock remainder."""
        self._active()
        if self._require_route(trial_id, route) != "running":
            raise ValueError("Route deadline requires a running public route")
        row = self._connection.execute(
            "SELECT reserved_seconds,agent_started_at_utc,deadline_at_utc "
            "FROM machine_time_attempts "
            "WHERE trial_id=? AND route=? AND status='dispatched' "
            "ORDER BY rowid DESC LIMIT 1",
            (trial_id, route),
        ).fetchone()
        if row is None:
            raise ValueError("An active route machine-time reservation is required")
        if row["agent_started_at_utc"] is None:
            # Environment setup and the initial τ user turn consume batch wall
            # time, but the per-route Agent machine-time cap starts only when
            # ControlledRoutes invokes the persisted Agent-start hook.
            return float(row["reserved_seconds"])
        if not row["deadline_at_utc"]:
            raise ValueError("An active route machine-time deadline is required")
        remaining = datetime.fromisoformat(row["deadline_at_utc"]) - datetime.now(UTC)
        return max(0.0, remaining.total_seconds())

    def reserve_tool_call(self, *, trial_id, route, command, effect, arguments_digest):
        self._active()
        if (
            type(command) is not str or not _SAFE_ID.fullmatch(command)
            or type(effect) is not str or effect not in {"read", "write", "ask_user"}
            or type(arguments_digest) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", arguments_digest)
        ):
            raise ValueError("Public tool attempt requires a safe name, effect and argument digest")
        self._transaction()
        try:
            self._require_batch_admission(tool_calls=1)
            self._running_route(trial_id, route)
            unresolved = self._connection.execute(
                "SELECT 1 FROM model_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') UNION ALL "
                "SELECT 1 FROM tool_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') LIMIT 1",
                (trial_id, route, trial_id, route),
            ).fetchone()
            if unresolved:
                raise ValueError("An unresolved or unknown attempt blocks another call")
            snapshot = self.resource_snapshot(trial_id, route)
            if snapshot["tool_calls"] + 1 > self.plan.resource_limits["tool_calls"]:
                raise ValueError("Public evaluation tool-call budget exceeded")
            attempt_id = uuid4().hex
            self._connection.execute(
                "INSERT INTO tool_attempts(tool_attempt_id,trial_id,route,command,effect,"
                "arguments_digest,status,created_at_utc) VALUES(?,?,?,?,?,?,?,?)",
                (
                    attempt_id, trial_id, route, command, effect, arguments_digest,
                    "reserved", _now(),
                ),
            )
            self._connection.execute("COMMIT")
            return attempt_id
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def mark_tool_dispatched(self, attempt_id):
        self._active()
        self._transaction()
        try:
            self._require_batch_admission()
            cursor = self._connection.execute(
                "UPDATE tool_attempts SET status='dispatched' "
                "WHERE tool_attempt_id=? AND status='reserved'",
                (attempt_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a reserved tool attempt can be dispatched")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def settle_tool_call(
        self, attempt_id, *, duration_seconds, result_digest, error_category=None
    ):
        self._active()
        if (
            type(duration_seconds) not in {int, float}
            or not isfinite(duration_seconds)
            or duration_seconds < 0
            or type(result_digest) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", result_digest)
            or (
                error_category is not None
                and (type(error_category) is not str or not _ERROR.fullmatch(error_category))
            )
        ):
            raise ValueError("Tool settlement requires bounded measured evidence")
        self._transaction()
        try:
            cursor = self._connection.execute(
                "UPDATE tool_attempts SET status='settled',duration_seconds=?,"
                "result_digest=?,error_category=? "
                "WHERE tool_attempt_id=? AND status='dispatched'",
                (float(duration_seconds), result_digest, error_category, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a dispatched tool attempt can be settled")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def mark_tool_unknown(
        self, attempt_id, *, error_category, failure_stage=None,
        exception_type=None, http_status=None,
    ):
        self._active()
        if type(error_category) is not str or not _ERROR.fullmatch(error_category):
            raise ValueError("Unknown tool failure category must be sanitized")
        validate_diagnostic(
            failure_stage=failure_stage,
            exception_type=exception_type,
            http_status=http_status,
        )
        self._transaction()
        try:
            row = self._connection.execute(
                "SELECT trial_id,route FROM tool_attempts "
                "WHERE tool_attempt_id=? AND status IN ('reserved','dispatched')",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Only an unresolved tool attempt can become unknown")
            self._connection.execute(
                "UPDATE tool_attempts SET status='unknown',error_category=?,failure_stage=?,"
                "exception_type=?,http_status=? "
                "WHERE tool_attempt_id=?",
                (error_category, failure_stage, exception_type, http_status, attempt_id),
            )
            self._connection.execute(
                "UPDATE route_trials SET status='unknown',error_category=?,failure_stage=?,"
                "exception_type=?,updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='running'",
                (
                    error_category, failure_stage, exception_type, _now(),
                    row["trial_id"], row["route"],
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def tool_attempts_snapshot(self, trial_id, route):
        """Return safe tool-call metadata without arguments or tool results."""
        self._active()
        rows = self._connection.execute(
            "SELECT tool_attempt_id,command,effect,status,duration_seconds,result_digest,"
            "error_category,failure_stage,exception_type,http_status "
            "FROM tool_attempts WHERE trial_id=? AND route=? ORDER BY created_at_utc",
            (trial_id, route),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_tool_not_sent(self, attempt_id, *, reason, verified_not_sent):
        self._active()
        if type(reason) is not str or not _ERROR.fullmatch(reason) or verified_not_sent is not True:
            raise ValueError("Only a verified local preflight failure can release a tool attempt")
        self._transaction()
        try:
            cursor = self._connection.execute(
                "UPDATE tool_attempts SET status='not_sent',error_category=? "
                "WHERE tool_attempt_id=? AND status='reserved'",
                (reason, attempt_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("A dispatched tool attempt cannot be released")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def tool_attempt_status(self, attempt_id):
        self._active()
        row = self._connection.execute(
            "SELECT status FROM tool_attempts WHERE tool_attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown public tool attempt")
        return row["status"]

    def reserve_machine_time(self, *, trial_id, route, max_seconds):
        self._active()
        if (
            type(max_seconds) not in {int, float}
            or not isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("Machine-time reservation must be finite and positive")
        self._transaction()
        try:
            self._require_batch_admission()
            self._running_route(trial_id, route)
            unresolved = self._connection.execute(
                "SELECT 1 FROM model_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') UNION ALL "
                "SELECT 1 FROM tool_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') UNION ALL "
                "SELECT 1 FROM machine_time_attempts WHERE trial_id=? AND route=? "
                "AND status IN ('reserved','dispatched','unknown') LIMIT 1",
                (trial_id, route, trial_id, route, trial_id, route),
            ).fetchone()
            if unresolved:
                raise ValueError("An unresolved or unknown attempt blocks more machine time")
            snapshot = self.resource_snapshot(trial_id, route)
            limit = self.plan.resource_limits["agent_machine_seconds"]
            if (
                snapshot["actual_agent_machine_seconds"]
                + snapshot["reserved_agent_machine_seconds"]
                + max_seconds
                > limit
            ):
                raise ValueError("Public evaluation machine-time budget exceeded")
            reservation_id = uuid4().hex
            self._connection.execute(
                "INSERT INTO machine_time_attempts(reservation_id,trial_id,route,status,"
                "reserved_seconds,created_at_utc) VALUES(?,?,?,'reserved',?,?)",
                (reservation_id, trial_id, route, float(max_seconds), _now()),
            )
            self._connection.execute("COMMIT")
            return reservation_id
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def mark_machine_time_dispatched(self, reservation_id):
        self._active()
        self._transaction()
        try:
            self._require_batch_admission()
            reservation = self._connection.execute(
                "SELECT reserved_seconds FROM machine_time_attempts "
                "WHERE reservation_id=? AND status='reserved'",
                (reservation_id,),
            ).fetchone()
            if reservation is None:
                raise ValueError("Only a reserved machine-time window can start")
            started = datetime.now(UTC)
            deadline = started + timedelta(seconds=reservation["reserved_seconds"])
            cursor = self._connection.execute(
                "UPDATE machine_time_attempts SET status='dispatched',started_at_utc=?,"
                "deadline_at_utc=? "
                "WHERE reservation_id=? AND status='reserved'",
                (started.isoformat(), deadline.isoformat(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a reserved machine-time window can start")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def mark_machine_time_agent_started(self, reservation_id):
        """Start the route execution deadline after isolated environment setup."""
        self._active()
        self._transaction()
        try:
            row = self._connection.execute(
                "SELECT reserved_seconds,status,agent_started_at_utc "
                "FROM machine_time_attempts WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "dispatched":
                raise ValueError("Only dispatched machine time can start an agent route")
            if row["agent_started_at_utc"] is not None:
                raise ValueError("Agent route deadline has already started")
            started = datetime.now(UTC)
            deadline = started + timedelta(seconds=row["reserved_seconds"])
            cursor = self._connection.execute(
                "UPDATE machine_time_attempts SET agent_started_at_utc=?,deadline_at_utc=? "
                "WHERE reservation_id=? AND status='dispatched' "
                "AND agent_started_at_utc IS NULL",
                (started.isoformat(), deadline.isoformat(), reservation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Agent route deadline has already started")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def settle_machine_time(self, reservation_id, *, actual_seconds, defer_route_failure=False):
        self._active()
        if (
            type(actual_seconds) not in {int, float}
            or not isfinite(actual_seconds)
            or actual_seconds < 0
            or type(defer_route_failure) is not bool
        ):
            raise ValueError("Measured machine time must be finite and nonnegative")
        self._transaction()
        overrun = False
        try:
            row = self._connection.execute(
                "SELECT trial_id,route,status FROM machine_time_attempts "
                "WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if row is None or row["status"] != "dispatched":
                raise ValueError("Only dispatched machine time can be settled")
            snapshot = self.resource_snapshot(row["trial_id"], row["route"])
            total = snapshot["actual_agent_machine_seconds"] + float(actual_seconds)
            overrun = total > self.plan.resource_limits["agent_machine_seconds"]
            self._connection.execute(
                "UPDATE machine_time_attempts SET status=?,actual_seconds=? "
                "WHERE reservation_id=?",
                (
                    "settled_overrun" if overrun else "settled",
                    float(actual_seconds),
                    reservation_id,
                ),
            )
            if overrun and not defer_route_failure:
                self._connection.execute(
                    "UPDATE route_trials SET status='failed',error_category='resource_overrun',"
                    "updated_at_utc=? WHERE trial_id=? AND route=? AND status='running'",
                    (_now(), row["trial_id"], row["route"]),
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        if overrun:
            if defer_route_failure:
                return False
            raise ValueError("Agent execution exceeded its frozen machine-time budget")
        return True

    def mark_machine_time_unknown(self, reservation_id, *, error_category):
        self._active()
        if type(error_category) is not str or not _ERROR.fullmatch(error_category):
            raise ValueError("Unknown machine-time category must be sanitized")
        self._transaction()
        try:
            row = self._connection.execute(
                "SELECT trial_id,route FROM machine_time_attempts "
                "WHERE reservation_id=? AND status IN ('reserved','dispatched')",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Only unresolved machine time can become unknown")
            self._connection.execute(
                "UPDATE machine_time_attempts SET status='unknown',error_category=? "
                "WHERE reservation_id=?",
                (error_category, reservation_id),
            )
            self._connection.execute(
                "UPDATE route_trials SET status='unknown',error_category=?,updated_at_utc=? "
                "WHERE trial_id=? AND route=? AND status='running'",
                (error_category, _now(), row["trial_id"], row["route"]),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def route_status(self, trial_id, route):
        self._active()
        row = self._connection.execute(
            "SELECT status FROM route_trials WHERE trial_id=? AND route=?", (trial_id, route)
        ).fetchone()
        if row is None:
            raise ValueError("Route is outside the frozen public task denominator")
        return row["status"]

    def attempt_status(self, attempt_id):
        self._active()
        row = self._connection.execute(
            "SELECT status FROM model_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown public model attempt")
        return row["status"]

    def status_snapshot(self):
        self._active()
        counts = {
            row["status"]: row["count"]
            for row in self._connection.execute(
                "SELECT status,count(*) AS count FROM route_trials GROUP BY status"
            )
        }
        return {
            "task_denominator": len(self.plan.trials),
            "route_denominator": len(self.plan.trials) * len(self.plan.routes),
            "routes_by_status": counts,
            "attempted_route_count": sum(
                counts.get(status, 0) for status in _TERMINAL | {"running"}
            ),
        }

    def close(self):
        if self._closed:
            return
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=2)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "UPDATE execution_lease SET expires_at_utc=? "
                "WHERE lease_name=? AND owner_id=? AND generation=?",
                (_now(), _LEASE_NAME, self.owner_id, self.generation),
            )
            self._connection.execute("COMMIT")
        except sqlite3.Error:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        self._connection.close()
        self._closed = True


def _now():
    return datetime.now(UTC).isoformat()


def _lease_expiry(lease_seconds):
    return (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()


def _validate_lease_seconds(value):
    if isinstance(value, bool) or type(value) not in {int, float} or not isfinite(value):
        raise ValueError("Public evaluation lease duration must be finite")
    if not 0.05 <= value <= 3600:
        raise ValueError("Public evaluation lease duration must be between 0.05 and 3,600 seconds")

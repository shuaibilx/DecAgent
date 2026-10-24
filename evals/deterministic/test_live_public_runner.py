import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import PublicComparisonTrial
from decagent.evaluation.business.public_execution import (
    PublicRouteReceipt,
    _trial_matches_record,
)
from decagent.evaluation.business.public_trials import load_public_run_plan

ROOT = Path(__file__).resolve().parents[2]


def test_live_report_target_uses_ignored_state_and_rejects_tracked_evidence(tmp_path):
    from evals.benchmarks.live_public_runner import validate_live_report_target

    target = validate_live_report_target(
        tmp_path,
        ".decagent/evaluation/evidence/diagnostic.json",
    )

    assert target.is_relative_to(tmp_path / ".decagent/evaluation/evidence")
    assert target.parent.is_dir()
    with pytest.raises(ValueError, match="ignored .decagent"):
        validate_live_report_target(
            tmp_path,
            "evals/benchmarks/evidence/diagnostic.json",
        )


def test_live_settings_loader_uses_only_the_selected_project_dotenv(monkeypatch, tmp_path):
    from evals.benchmarks.run_public_trials import _settings_for_project

    project_root = tmp_path / "DecAgent"
    invocation_dir = tmp_path / "other-project"
    project_root.mkdir()
    invocation_dir.mkdir()
    (project_root / ".env").write_text("DECAGENT_MAIN_MODEL=decagent-model\n")
    (invocation_dir / ".env").write_text("DECAGENT_MAIN_MODEL=other-project-model\n")
    monkeypatch.chdir(invocation_dir)
    monkeypatch.delenv("DECAGENT_MAIN_MODEL", raising=False)
    try:
        settings = _settings_for_project(project_root)
        assert settings.main_model == "decagent-model"
    finally:
        os.environ.pop("DECAGENT_MAIN_MODEL", None)


def test_build_public_trial_binds_environment_fingerprints_and_frozen_cohort():
    from evals.benchmarks.live_public_runner import build_public_comparison_trial

    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    record = next(
        item
        for item in plan.trials
        if item["trial_id"] == plan.initial_smoke_wave[0]["trial_id"]
    )
    boundary = {
        "initial_state_digest": digest("initial state"),
        "goal_digest": digest("private goal fingerprint"),
        "actor_profile_digest": digest("actor"),
        "permission_digest": digest("permissions"),
        "tool_adapter_digest": digest("catalog"),
    }

    trial = build_public_comparison_trial(
        plan,
        record,
        dataset_checksum=digest("pinned dataset"),
        boundary_facts=boundary,
    )

    assert isinstance(trial, PublicComparisonTrial)
    assert trial.trial_id == record["trial_id"]
    assert trial.branch_kind == "semantic"
    assert trial.memory_mode == "disabled" and trial.workflow_mode == "cold"
    assert trial.initial_state_digest == boundary["initial_state_digest"]
    assert trial.goal_digest == boundary["goal_digest"]
    assert _trial_matches_record(trial, record)


def test_build_public_trial_rejects_partial_boundary_and_invalid_cohort_record():
    from evals.benchmarks.live_public_runner import build_public_comparison_trial

    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    record = next(
        item
        for item in plan.trials
        if item["trial_id"] == plan.initial_smoke_wave[0]["trial_id"]
    )
    with pytest.raises(ValueError, match="boundary"):
        build_public_comparison_trial(
            plan,
            record,
            dataset_checksum=digest("dataset"),
            boundary_facts={"initial_state_digest": digest("initial")},
        )

    invalid = {**record, "model_profile_digest": "f" * 64}
    boundary = {
        "initial_state_digest": digest("initial"),
        "goal_digest": digest("goal"),
        "actor_profile_digest": digest("actor"),
        "permission_digest": digest("permission"),
        "tool_adapter_digest": digest("tools"),
    }
    with pytest.raises(ValueError, match="frozen public cohort"):
        build_public_comparison_trial(
            plan,
            invalid, dataset_checksum=digest("dataset"), boundary_facts=boundary
        )


def test_candidate_gate_requires_clean_tree_and_same_sha_successful_github_ci(
    monkeypatch, tmp_path
):
    from evals.benchmarks.live_public_runner import require_verified_candidate

    candidate = "a" * 40
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=candidate, stderr="", returncode=0)
        if command[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        if command[:3] == ["gh", "repo", "view"]:
            return SimpleNamespace(stdout="owner/repository", stderr="", returncode=0)
        if command[:3] == ["gh", "run", "list"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    [{
                        "headSha": candidate,
                        "workflowName": "CI",
                        "status": "completed",
                        "conclusion": "success",
                    }]
                ),
                stderr="",
                returncode=0,
            )
        raise AssertionError(command)

    monkeypatch.setattr("evals.benchmarks.live_public_runner.subprocess.run", run)

    assert require_verified_candidate(tmp_path) == candidate
    assert [call[:2] for call in calls] == [
        ["git", "rev-parse"],
        ["git", "status"],
        ["gh", "repo"],
        ["gh", "run"],
    ]


def test_candidate_gate_rejects_dirty_tree_before_querying_github(monkeypatch, tmp_path):
    from evals.benchmarks.live_public_runner import require_verified_candidate

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        output = "a" * 40 if command[:2] == ["git", "rev-parse"] else " M file.py\n"
        return SimpleNamespace(stdout=output, stderr="", returncode=0)

    monkeypatch.setattr("evals.benchmarks.live_public_runner.subprocess.run", run)

    with pytest.raises(ValueError, match="clean"):
        require_verified_candidate(tmp_path)

    assert [call[:2] for call in calls] == [["git", "rev-parse"], ["git", "status"]]


def test_live_cli_requires_fixed_first_wave_and_report_before_candidate_gate(
    monkeypatch, tmp_path, capsys
):
    from evals.benchmarks import live_public_runner, run_public_trials

    calls = []
    monkeypatch.setattr(
        live_public_runner,
        "require_verified_candidate",
        lambda _root: calls.append("candidate"),
    )

    with pytest.raises(SystemExit) as no_phase:
        run_public_trials.main(["--root", str(tmp_path), "--execute"])
    assert no_phase.value.code == 2
    assert "--phase diagnostic" in capsys.readouterr().err

    with pytest.raises(SystemExit) as no_report:
        run_public_trials.main(
            ["--root", str(tmp_path), "--execute", "--phase", "diagnostic"]
        )
    assert no_report.value.code == 2
    assert "--report" in capsys.readouterr().err

    with pytest.raises(SystemExit) as alternate_state:
        run_public_trials.main(
            [
                "--root", str(tmp_path), "--execute", "--phase", "diagnostic",
                "--report", "evals/benchmarks/evidence/new.json",
                "--state", str(tmp_path / "alternate.sqlite3"),
            ]
        )
    assert alternate_state.value.code == 2
    assert "canonical ledger" in capsys.readouterr().err
    assert calls == []


def test_live_openrouter_diagnostic_requires_a_parent_qwen_report_before_candidate_gate(
    monkeypatch, tmp_path, capsys
):
    from evals.benchmarks import live_public_runner, run_public_trials

    calls = []
    monkeypatch.setattr(
        live_public_runner,
        "require_verified_candidate",
        lambda _root: calls.append("candidate"),
    )

    with pytest.raises(SystemExit) as missing_parent:
        run_public_trials.main(
            [
                "--root", str(tmp_path), "--execute", "--phase", "openrouter-diagnostic",
                "--report", ".decagent/evaluation/evidence/openrouter.json",
            ]
        )
    assert missing_parent.value.code == 2
    assert "--parent-diagnostic-evidence" in capsys.readouterr().err
    assert calls == []


def test_openrouter_parent_evidence_binds_complete_qwen_round_two_without_unknowns(
    tmp_path, monkeypatch,
):
    from evals.benchmarks import live_public_runner
    from evals.benchmarks.pilot_gate import (
        DIAGNOSTIC_BATCH_LIMITS,
        DIAGNOSTIC_LIMITS,
        WEEKLY_BASELINE_SHA256,
    )

    evidence = tmp_path / ".decagent/evaluation/evidence/parent.json"
    evidence.parent.mkdir(parents=True)
    report = {
        "schema_version": "public-evaluation-live-diagnostic-v2",
        "phase": "diagnostic",
        "diagnostic_round": 2,
        "dry_run": False,
        "evaluation_budget_id": "sop-pilot-20261024-v1",
        "candidate_sha": "a" * 40,
        "working_tree_dirty": False,
        "candidate_ci_verified": True,
        "task_denominator": 4,
        "selected_task_count": 4,
        "route_denominator": 8,
        "selected_route_slots": 8,
        "diagnostic_limits": DIAGNOSTIC_LIMITS,
        "diagnostic_batch_limits": DIAGNOSTIC_BATCH_LIMITS,
        "weekly_budget_baseline_sha256": WEEKLY_BASELINE_SHA256,
        "diagnostic_elapsed_seconds": 180.34,
        "batch": {
            "limits": DIAGNOSTIC_BATCH_LIMITS,
            "qwen_requests": 3,
            "qwen_tokens": 34_054,
            "qwen_cost_unknown_attempts": 0,
            "openrouter_requests": 0,
            "openrouter_cost_unknown_attempts": 0,
            "tool_calls": 1,
        },
        "routes": {
            f"task-{index}:{route}": {
                "trial_id": f"task-{index}",
                "route": route,
                "model_attempts": [],
            }
            for index in range(4)
            for route in ("plan_mcp", "react_skill_mcp")
        },
    }
    evidence.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(
        live_public_runner,
        "openrouter_diagnostic_parent_clock",
        lambda *_args, **_kwargs: {
            "batch_started_at_utc": "2026-10-24T08:00:00+00:00",
            "batch_deadline_at_utc": "2026-10-24T08:45:00+00:00",
        },
    )

    result = live_public_runner.validate_openrouter_parent_evidence(tmp_path, evidence)

    assert result["candidate_sha"] == "a" * 40
    assert result["qwen_requests"] == 3
    assert result["qwen_tokens"] == 34_054
    assert len(result["report_sha256"]) == 64
    assert result["batch_deadline_at_utc"] == "2026-10-24T08:45:00+00:00"

    report["batch"]["qwen_cost_unknown_attempts"] = 1
    evidence.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        live_public_runner.validate_openrouter_parent_evidence(tmp_path, evidence)


def test_live_cli_requires_images_readiness_runtime_and_credentials_before_dispatch(
    monkeypatch, tmp_path, capsys
):
    from types import SimpleNamespace

    from evals.benchmarks import live_public_runner, run_public_trials

    evidence = tmp_path / ".decagent" / "evaluation" / "evidence"
    evidence.mkdir(parents=True)
    tau_python = tmp_path / "tau-python.exe"
    tau_python.write_bytes(b"test executable placeholder")
    calls = []
    finalized = []

    monkeypatch.setattr(live_public_runner, "require_verified_candidate", lambda _root: "a" * 40)
    monkeypatch.setattr(
        "decagent.config.Settings.from_env",
        lambda: SimpleNamespace(main_api_key="configured", fallback_enabled=False),
    )

    def load_plan(*args, **kwargs):
        calls.append(("plan", kwargs))
        return object()

    def bounded_plan(plan):
        calls.append(("bounded", plan))
        return plan

    def execute(plan, **kwargs):
        calls.append(("execute", kwargs))
        return SimpleNamespace(), {
            "dry_run": False,
            "phase": "diagnostic",
            "candidate_sha": "a" * 40,
            "cohort_digest": "c" * 64,
            "evaluation_budget_id": "sop-pilot-20261024-v1",
            "actual_cost_usd": Decimal("0.00001"),
        }

    monkeypatch.setattr(run_public_trials, "load_public_run_plan", load_plan)
    monkeypatch.setattr(run_public_trials, "build_bounded_diagnostic_plan", bounded_plan)
    monkeypatch.setattr(live_public_runner, "execute_live_public_phase", execute)
    monkeypatch.setattr(
        run_public_trials,
        "complete_public_evaluation_phase",
        lambda *args, **kwargs: finalized.append((args, kwargs)),
    )

    report = Path(".decagent/evaluation/evidence/diagnostic.json")
    assert run_public_trials.main(
        [
            "--root", str(tmp_path), "--execute", "--phase", "diagnostic",
            "--mode", "smoke", "--tau-python", str(tau_python), "--report", str(report),
        ]
    ) == 0

    assert calls[0][0] == "plan"
    assert calls[0][1]["check_images"] is True
    assert calls[0][1]["check_runtime"] is True
    assert calls[0][1]["check_source_files"] is True
    assert calls[1][0] == "bounded"
    assert calls[2][0] == "execute"
    assert calls[2][1]["candidate_sha"] == "a" * 40
    assert calls[2][1]["phase"] == "diagnostic"
    assert calls[2][1]["candidate_ci_verified"] is True
    assert finalized[0][1]["phase"] == "diagnostic"
    assert finalized[0][1]["candidate_sha"] == "a" * 40
    assert json.loads((tmp_path / report).read_text(encoding="utf-8"))["dry_run"] is False
    output = capsys.readouterr()
    assert json.loads(output.out)["actual_cost_usd"] == "0.00001"
    assert "diagnostic.json" in output.err


def test_live_pilot_requires_diagnostic_evidence_before_loading_any_model_or_tools(
    monkeypatch, tmp_path, capsys
):
    from evals.benchmarks import live_public_runner, run_public_trials

    calls = []
    monkeypatch.setattr(
        live_public_runner,
        "require_verified_candidate",
        lambda _root: calls.append("candidate") or "a" * 40,
    )

    with pytest.raises(SystemExit) as raised:
        run_public_trials.main(
            [
                "--root",
                str(tmp_path),
                "--execute",
                "--phase",
                "pilot",
                "--mode",
                "pilot",
                "--report",
                "evals/benchmarks/evidence/pilot.json",
            ]
        )

    assert raised.value.code == 2
    assert calls == []
    assert "--diagnostic-evidence" in capsys.readouterr().err


def test_live_runner_preflights_all_four_environments_before_opening_ledger(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from decagent.evaluation.business.public_trials import load_public_run_plan
    from evals.benchmarks import live_public_runner

    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    events = []
    ledger_kwargs = {}

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        def trial_factory(self, record):
            events.append(("preflight", record["trial_id"]))
            return record["trial_id"]

        def environment_factory(self, *_args):
            raise AssertionError("Execution phase should be stubbed")

    class Supervisor:
        def __init__(self, _path):
            pass

        def __enter__(self):
            events.append(("lock",))
            return self

        def __exit__(self, *_args):
            events.append(("unlock",))

    ledger = SimpleNamespace(close=lambda: events.append(("close-ledger",)))
    monkeypatch.setattr(live_public_runner, "FirstWaveRuntime", Runtime)
    monkeypatch.setattr(
        live_public_runner,
        "authorize_public_evaluation_phase",
        lambda *_args, **_kwargs: {
            "authorization_path": Path("unused.sqlite3"),
            "batch_started_at_utc": None,
            "candidate_successor_pending": False,
        },
    )
    monkeypatch.setattr(live_public_runner, "PublicSupervisor", Supervisor)
    monkeypatch.setattr(
        live_public_runner, "canonical_public_supervisor_lock_path", lambda _root: "lock"
    )
    monkeypatch.setattr(
        live_public_runner,
        "open_first_wave_public_trial_ledger",
        lambda *_args, **kwargs: events.append(("ledger",))
        or ledger_kwargs.update(kwargs)
        or ledger,
    )
    monkeypatch.setattr(
        "decagent.evaluation.business.public_models.public_first_wave_model_adapter_factory",
        lambda *_args, **_kwargs: SimpleNamespace(ledger=ledger),
    )
    monkeypatch.setattr(
        live_public_runner,
        "execute_public_phase",
        lambda **kwargs: events.append(("execute",)) or SimpleNamespace(phase="first-wave"),
    )
    monkeypatch.setattr(
        live_public_runner,
        "live_first_wave_report",
        lambda *_args, **_kwargs: {"status": "test"},
    )

    live_public_runner.execute_live_first_wave(
        plan,
        settings=object(),
        tau_python="unused.exe",
        journal_root=ROOT / ".decagent/test-journal",
        candidate_sha="a" * 40,
        working_tree_dirty=False,
        candidate_ci_verified=True,
    )

    assert [event[0] for event in events] == [
        "lock",
        "ledger",
        "preflight",
        "preflight",
        "preflight",
        "preflight",
        "execute",
        "close-ledger",
        "unlock",
    ]
    assert isinstance(ledger_kwargs["batch_started_at"], datetime)
    assert ledger_kwargs["batch_started_at"].tzinfo is UTC


def test_diagnostic_report_includes_the_bounded_phase_evidence(monkeypatch):
    from decagent.evaluation.business.public_trials import load_public_run_plan
    from evals.benchmarks import live_public_runner
    from evals.benchmarks.pilot_gate import build_bounded_diagnostic_plan

    base = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    plan = build_bounded_diagnostic_plan(base)

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        def trial_factory(self, record):
            return record["trial_id"]

        def route_kwargs_factory(self, *_args):
            return {}

        def environment_factory(self, *_args):
            return None

    class Supervisor:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    ledger = SimpleNamespace(candidate_sha="a" * 40, close=lambda: None)
    monkeypatch.setattr(live_public_runner, "FirstWaveRuntime", Runtime)
    monkeypatch.setattr(
        live_public_runner,
        "authorize_public_evaluation_phase",
        lambda *_args, **_kwargs: {
            "authorization_path": Path("unused.sqlite3"),
            "batch_started_at_utc": None,
            "candidate_successor_pending": False,
        },
    )
    monkeypatch.setattr(live_public_runner, "PublicSupervisor", Supervisor)
    monkeypatch.setattr(
        live_public_runner,
        "canonical_public_supervisor_lock_path",
        lambda _root: "lock",
    )
    monkeypatch.setattr(
        live_public_runner,
        "open_first_wave_public_trial_ledger",
        lambda *_args, **_kwargs: ledger,
    )
    monkeypatch.setattr(
        "decagent.evaluation.business.public_models.public_first_wave_model_adapter_factory",
        lambda *_args, **_kwargs: SimpleNamespace(ledger=ledger),
    )
    monkeypatch.setattr(
        live_public_runner,
        "ControlledRoutes",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        live_public_runner,
        "execute_public_phase",
        lambda **_kwargs: SimpleNamespace(
            phase="diagnostic", executed_routes=(), skipped_routes=()
        ),
    )
    monkeypatch.setattr(
        live_public_runner,
        "live_first_wave_report",
        lambda _plan, phase, *_args, **_kwargs: {"phase": phase.phase},
    )

    _, report = live_public_runner.execute_live_public_phase(
        plan,
        settings=object(),
        tau_python="unused.exe",
        journal_root=ROOT / ".decagent/test-journal",
        candidate_sha="a" * 40,
        working_tree_dirty=False,
        phase="diagnostic",
        candidate_ci_verified=True,
    )

    assert report["phase"] == "diagnostic"
    assert report["diagnostic_limits"]["max_qwen_requests"] == 6
    assert report["diagnostic_batch_limits"]["qwen_cost_cny"] == "1.62"
    assert report["evaluation_source_digest"]
    assert len(report["diagnostic_task_records"]) == 4


def test_live_report_contains_only_whitelisted_attempt_diagnostics():
    from evals.benchmarks.live_public_runner import live_first_wave_report

    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    trial_id = plan.initial_smoke_wave[0]["trial_id"]
    route = plan.routes[0]

    class Ledger:
        candidate_sha = "a" * 40

        def route_lifecycle(self, current_trial, current_route):
            status = (
                "failed"
                if (current_trial, current_route) == (trial_id, route)
                else "unknown"
            )
            return {
                "status": status,
                "error_category": "model_dispatch_error",
                "failure_stage": "model_callback",
                "exception_type": "TimeoutError",
                "score_status": "not_attempted",
                "cleanup_status": "verified",
            }

        def usage_snapshot(self, *_args):
            return {}

        def resource_snapshot(self, *_args):
            return {}

        def billing_snapshot(self, *_args):
            return {
                "settled_attempts": 1,
                "cost_by_currency": {
                    "CNY": {
                        "frozen_rate": "0.0000133",
                        "frozen_rate_upper_bound": "0",
                        "provider_billed": "0",
                        "attempts": 1,
                    }
                },
                "cost_records": [],
            }

        def model_attempts_snapshot(self, current_trial, current_route):
            if (current_trial, current_route) != (trial_id, route):
                return []
            return [{
                "status": "unknown",
                "failure_stage": "model_callback",
                "exception_type": "TimeoutError",
                "http_status": None,
                "error_category": "model_dispatch_error",
            }]

        def tool_attempts_snapshot(self, *_args):
            return []

        def batch_snapshot(self):
            return {}

        def status_snapshot(self):
            return {}

    report = live_first_wave_report(
        plan,
        SimpleNamespace(phase="first-wave", executed_routes=(), skipped_routes=()),
        Ledger(),
        candidate_sha="a" * 40,
        working_tree_dirty=False,
    )
    route_record = report["routes"][trial_id + ":" + route]
    assert report["schema_version"] == "public-evaluation-live-first-wave-v4"
    assert report["successful_route_count"] == 0
    assert report["terminal_route_count"] == 8
    assert report["route_status_counts"] == {"failed": 1, "unknown": 7}
    assert report["completed_route_count"] == report["successful_route_count"]
    assert route_record["billing"]["cost_by_currency"]["CNY"]["frozen_rate"] == "0.0000133"
    assert route_record["model_attempts"][0]["exception_type"] == "TimeoutError"
    assert route_record["tool_attempts"] == []
    assert "PRIVATE RESPONSE BODY" not in repr(report)
    assert report["evaluated_route_count"] == 8
    assert report["not_applicable_route_count"] == 0
    assert report["route_coverage_rate"] == 1.0
    assert report["semantic_pairing"] is None

    with pytest.raises(ValueError, match="candidate SHA differs"):
        live_first_wave_report(
            plan,
            SimpleNamespace(phase="first-wave", executed_routes=(), skipped_routes=()),
            Ledger(),
            candidate_sha="b" * 40,
            working_tree_dirty=False,
        )


def test_pilot_report_separates_semantic_pairs_and_natural_not_applicable_routes(tmp_path):
    from evals.benchmarks.live_public_runner import live_first_wave_report
    from evals.benchmarks.pilot_manifest import build_pilot_manifest, load_public_pilot_plan

    base_path = ROOT / "evals/benchmarks/manifests/comparison_trial_manifest_v11.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    frozen = build_pilot_manifest(base)
    manifest_path = tmp_path / "pilot.json"
    manifest_path.write_text(json.dumps(frozen), encoding="utf-8")
    plan = load_public_pilot_plan(
        ROOT,
        pilot_manifest_path=manifest_path,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    first_csm = next(item for item in plan.trials if item["domain"] == "csm")
    n_a_receipt = PublicRouteReceipt(
        trial_id=first_csm["trial_id"],
        route="plan_jev_semantic_mcp",
        status="not_applicable",
        score_status="not_applicable",
        cleanup_verified=True,
        result_digest="a" * 64,
        score_digest=None,
        machine_seconds=0.0,
        error_category="semantic_decision_not_observed",
        pair_status="not_applicable",
    )

    class Ledger:
        candidate_sha = "a" * 40

        def route_lifecycle(self, trial_id, route):
            not_applicable = (
                trial_id == first_csm["trial_id"]
                and route == "plan_jev_semantic_mcp"
            )
            return {
                "status": "not_applicable" if not_applicable else "unknown",
                "error_category": (
                    "semantic_decision_not_observed" if not_applicable else None
                ),
                "failure_stage": None,
                "exception_type": None,
                "score_status": "not_applicable" if not_applicable else "unknown",
                "cleanup_status": "verified" if not_applicable else "unknown",
            }

        def usage_snapshot(self, *_args):
            return {}

        def resource_snapshot(self, *_args):
            return {}

        def billing_snapshot(self, *_args):
            return {"settled_attempts": 0, "cost_by_currency": {}, "cost_records": []}

        def model_attempts_snapshot(self, *_args):
            return []

        def tool_attempts_snapshot(self, *_args):
            return []

        def batch_snapshot(self):
            return {}

        def status_snapshot(self):
            return {}

    report = live_first_wave_report(
        plan,
        SimpleNamespace(
            phase="pilot",
            executed_routes=(n_a_receipt,),
            skipped_routes=(),
        ),
        Ledger(),
        candidate_sha="a" * 40,
        working_tree_dirty=False,
    )

    assert report["schema_version"] == "public-evaluation-live-pilot-v2"
    assert report["not_applicable_route_count"] == 1
    assert report["evaluated_route_count"] == 67
    assert report["route_coverage_rate"] == 67 / 68
    pairing = report["semantic_pairing"]
    assert pairing["scope"] == "execution_only"
    assert pairing["planned_cases"] == 10
    assert pairing["not_applicable_cases"] == 1
    assert pairing["incomplete_cases"] == 9

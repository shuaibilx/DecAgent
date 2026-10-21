import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import PublicComparisonTrial
from decagent.evaluation.business.public_execution import _trial_matches_record
from decagent.evaluation.business.public_trials import load_public_run_plan

ROOT = Path(__file__).resolve().parents[2]


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
    assert trial.branch_kind == "structured"
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
    assert "--phase first-wave" in capsys.readouterr().err

    with pytest.raises(SystemExit) as no_report:
        run_public_trials.main(
            ["--root", str(tmp_path), "--execute", "--phase", "first-wave"]
        )
    assert no_report.value.code == 2
    assert "--report" in capsys.readouterr().err

    with pytest.raises(SystemExit) as alternate_state:
        run_public_trials.main(
            [
                "--root", str(tmp_path), "--execute", "--phase", "first-wave",
                "--report", "evals/benchmarks/evidence/new.json",
                "--state", str(tmp_path / "alternate.sqlite3"),
            ]
        )
    assert alternate_state.value.code == 2
    assert "canonical ledger" in capsys.readouterr().err
    assert calls == []


def test_live_cli_requires_images_readiness_runtime_and_credentials_before_dispatch(
    monkeypatch, tmp_path, capsys
):
    from types import SimpleNamespace

    from evals.benchmarks import live_public_runner, run_public_trials

    evidence = tmp_path / "evals" / "benchmarks" / "evidence"
    evidence.mkdir(parents=True)
    tau_python = tmp_path / "tau-python.exe"
    tau_python.write_bytes(b"test executable placeholder")
    calls = []

    monkeypatch.setattr(live_public_runner, "require_verified_candidate", lambda _root: "a" * 40)
    monkeypatch.setattr(
        "decagent.config.Settings.from_env",
        lambda: SimpleNamespace(main_api_key="configured", fallback_enabled=False),
    )

    def load_plan(*args, **kwargs):
        calls.append(("plan", kwargs))
        return object()

    def execute(plan, **kwargs):
        calls.append(("execute", kwargs))
        return SimpleNamespace(), {
            "dry_run": False,
            "phase": "first-wave",
            "actual_cost_usd": Decimal("0.00001"),
        }

    monkeypatch.setattr(run_public_trials, "load_public_run_plan", load_plan)
    monkeypatch.setattr(live_public_runner, "execute_live_first_wave", execute)

    report = Path("evals/benchmarks/evidence/first-wave.json")
    assert run_public_trials.main(
        [
            "--root", str(tmp_path), "--execute", "--phase", "first-wave",
            "--mode", "smoke", "--tau-python", str(tau_python), "--report", str(report),
        ]
    ) == 0

    assert calls[0][0] == "plan"
    assert calls[0][1]["check_images"] is True
    assert calls[0][1]["check_runtime"] is True
    assert calls[0][1]["check_source_files"] is True
    assert calls[1][0] == "execute"
    assert calls[1][1]["candidate_sha"] == "a" * 40
    assert json.loads((tmp_path / report).read_text(encoding="utf-8"))["dry_run"] is False
    output = capsys.readouterr()
    assert json.loads(output.out)["actual_cost_usd"] == "0.00001"
    assert "first-wave.json" in output.err


def test_live_runner_preflights_all_four_environments_before_opening_ledger(monkeypatch):
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
    monkeypatch.setattr(live_public_runner, "PublicSupervisor", Supervisor)
    monkeypatch.setattr(
        live_public_runner, "canonical_public_supervisor_lock_path", lambda _root: "lock"
    )
    monkeypatch.setattr(
        live_public_runner,
        "open_first_wave_public_trial_ledger",
        lambda *_args, **_kwargs: events.append(("ledger",)) or ledger,
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
    )

    assert [event[0] for event in events[:4]] == ["preflight"] * 4
    assert [event[0] for event in events[4:]] == [
        "lock", "ledger", "execute", "close-ledger", "unlock"
    ]


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

        def route_lifecycle(self, _trial_id, _route):
            return {
                "status": "unknown",
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
    assert report["schema_version"] == "public-evaluation-live-first-wave-v3"
    assert route_record["billing"]["cost_by_currency"]["CNY"]["frozen_rate"] == "0.0000133"
    assert route_record["model_attempts"][0]["exception_type"] == "TimeoutError"
    assert route_record["tool_attempts"] == []
    assert "PRIVATE RESPONSE BODY" not in repr(report)

    with pytest.raises(ValueError, match="candidate SHA differs"):
        live_first_wave_report(
            plan,
            SimpleNamespace(phase="first-wave", executed_routes=(), skipped_routes=()),
            Ledger(),
            candidate_sha="b" * 40,
            working_tree_dirty=False,
        )

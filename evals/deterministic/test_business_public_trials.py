import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from decagent.evaluation.business.public_trials import (
    PublicTrialLedger,
    build_dry_run_report,
    load_public_run_plan,
)

ROOT = Path(__file__).resolve().parents[2]


def plan(mode="smoke"):
    return load_public_run_plan(
        ROOT,
        mode=mode,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )


def _start_public_route_with_machine_deadline(ledger, trial_id, route):
    ledger.start_route(trial_id, route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial_id, route=route, max_seconds=60
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger.mark_machine_time_agent_started(reservation)
    return reservation


def _score_evidence(trial):
    return {
        "source": (
            "enterpriseops-gym-original-verifier"
            if trial["benchmark"] == "enterpriseops-gym"
            else "tau2-bench-original-evaluate-simulation"
        ),
        "value": 1.0,
        "success": True,
        "forbidden_side_effects": 0,
    }


def _score_evidence_digest(trial):
    from decagent.decisions.contracts import digest

    return digest(_score_evidence(trial))


def test_dry_run_validates_fixed_inputs_without_creating_state_or_calling_services(tmp_path):
    before = set(tmp_path.iterdir())
    selected = plan()
    report = build_dry_run_report(selected)

    assert report["dry_run"] is True
    assert report["live_model_calls"] == report["mcp_calls"] == 0
    assert report["task_denominator"] == 24
    assert report["route_attempt_slots"] == 48
    assert report["initial_wave_task_count"] == 4
    assert report["initial_wave_route_attempt_slots"] == 8
    assert [item["domain"] for item in report["initial_smoke_wave"]] == [
        "csm",
        "itsm",
        "retail",
        "telecom",
    ]
    assert report["by_benchmark"] == {"enterpriseops-gym": 20, "tau-bench": 4}
    assert report["jev_semantic_status"] == "reviewed_csm_natural_case_pairing_pending"
    assert report["route_pair"] == ["react_skill_mcp", "plan_mcp"]
    assert report["batch_limits"] == {
        "qwen_requests": 500,
        "qwen_tokens": 5_000_000,
        "agent_qwen_requests": 300,
        "agent_qwen_tokens": 3_000_000,
        "user_simulator_qwen_requests": 160,
        "user_simulator_qwen_tokens": 1_600_000,
        "judge_qwen_requests": 40,
        "judge_qwen_tokens": 400_000,
        "tool_calls": 1_600,
        "wall_seconds": 14_400,
    }
    assert set(tmp_path.iterdir()) == before


def test_run_plan_accepts_linux_inventory_checkout_line_endings(monkeypatch):
    inventory_path = (ROOT / "evals/benchmarks/manifests/task_inventory.json").resolve()
    read_bytes = Path.read_bytes

    def read_linux_checkout(path):
        content = read_bytes(path)
        if path.resolve() == inventory_path:
            return content.replace(b"\r\n", b"\n")
        return content

    monkeypatch.setattr(Path, "read_bytes", read_linux_checkout)
    selected = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert len(selected.trials) == 24


def test_offline_plan_skips_external_asset_gate_but_live_preflight_keeps_it(monkeypatch):
    from evals.benchmarks import validate_source_manifest

    monkeypatch.setattr(
        validate_source_manifest,
        "validate_files",
        lambda _path: ["missing pinned benchmark input"],
    )

    with pytest.raises(ValueError, match="provenance gate failed"):
        load_public_run_plan(
            ROOT,
            mode="smoke",
            check_images=False,
            check_runtime=False,
            check_source_files=True,
        )

    offline = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    assert len(offline.trials) == 24


def test_first_wave_phase_selects_only_the_four_frozen_cases():
    report = build_dry_run_report(plan(), phase="first-wave")

    assert report["phase"] == "first-wave"
    assert report["task_denominator"] == 24
    assert report["route_attempt_slots"] == 48
    assert report["selected_task_count"] == 4
    assert report["selected_route_attempt_slots"] == 8
    assert report["selected_by_benchmark"] == {"enterpriseops-gym": 2, "tau-bench": 2}
    assert report["selected_trial_ids"] == [
        item["trial_id"] for item in report["initial_smoke_wave"]
    ]


def test_remaining_smoke_phase_excludes_every_first_wave_case():
    report = build_dry_run_report(plan(), phase="remaining-smoke")

    assert report["phase"] == "remaining-smoke"
    assert report["selected_task_count"] == 20
    assert report["selected_route_attempt_slots"] == 40
    assert report["selected_by_benchmark"] == {"enterpriseops-gym": 18, "tau-bench": 2}
    assert not set(report["selected_trial_ids"]) & {
        item["trial_id"] for item in report["initial_smoke_wave"]
    }


def test_full_phase_selects_the_full_frozen_denominator():
    report = build_dry_run_report(plan("full"), phase="full")

    assert report["phase"] == "full"
    assert report["task_denominator"] == report["selected_task_count"] == 286
    assert report["route_attempt_slots"] == report["selected_route_attempt_slots"] == 572


@pytest.mark.parametrize(
    ("mode", "phase"),
    [("full", "first-wave"), ("full", "remaining-smoke"), ("smoke", "full")],
)
def test_phase_must_match_frozen_cohort(mode, phase):
    with pytest.raises(ValueError, match="phase is incompatible"):
        build_dry_run_report(plan(mode), phase=phase)


def test_command_line_defaults_to_dry_run_and_live_flag_fails_closed(tmp_path, capsys):
    from evals.benchmarks.run_public_trials import main

    assert main(["--root", str(ROOT), "--mode", "smoke"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["live_model_calls"] == 0
    assert not list(tmp_path.iterdir())

    with pytest.raises(SystemExit) as stopped:
        main(["--root", str(ROOT), "--execute", "--state", str(tmp_path / "run.sqlite3")])
    assert stopped.value.code == 2
    assert not (tmp_path / "run.sqlite3").exists()
    with pytest.raises(SystemExit) as no_dryrun_state:
        main(["--root", str(ROOT), "--state", str(tmp_path / "run.sqlite3")])
    assert no_dryrun_state.value.code == 2
    assert not (tmp_path / "run.sqlite3").exists()


def test_dry_run_cli_does_not_require_live_runtime_or_downloaded_benchmark_assets(
    monkeypatch,
    capsys,
):
    from evals.benchmarks import run_public_trials

    calls = {}

    def load_plan(*args, **kwargs):
        calls.update(kwargs)
        return plan()

    monkeypatch.setattr(run_public_trials, "load_public_run_plan", load_plan)

    assert run_public_trials.main(["--root", str(ROOT), "--mode", "smoke"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert calls["check_runtime"] is False
    assert calls["check_source_files"] is False


def test_dry_run_report_is_exclusive_and_confined_to_evidence(tmp_path):
    from evals.benchmarks.run_public_trials import _write_dry_run_report

    evidence = tmp_path / "evals" / "benchmarks" / "evidence"
    evidence.mkdir(parents=True)
    report = build_dry_run_report(plan())
    target = _write_dry_run_report(
        tmp_path,
        Path("evals/benchmarks/evidence/preflight.json"),
        report,
    )
    assert json.loads(target.read_text(encoding="utf-8")) == report
    with pytest.raises(FileExistsError):
        _write_dry_run_report(
            tmp_path,
            Path("evals/benchmarks/evidence/preflight.json"),
            report,
        )
    with pytest.raises(ValueError, match="under benchmark evidence"):
        _write_dry_run_report(tmp_path, tmp_path / "outside.json", report)


def test_first_wave_ledger_requires_canonical_lock_and_preserves_canonical_state(tmp_path):
    from decagent.evaluation.business.ledger import (
        GlobalJevLedger,
        canonical_global_jev_ledger_path,
    )
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        canonical_public_supervisor_lock_path,
    )
    from decagent.evaluation.business.public_trials import (
        canonical_public_trial_ledger_path,
        open_first_wave_public_trial_ledger,
        validate_public_trial_ledger_path,
    )
    project_root = tmp_path / "project"
    plan_root = replace(plan(), root=project_root)
    legacy_path = canonical_public_trial_ledger_path(project_root)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_global_jev = GlobalJevLedger(
        project_root / ".decagent" / "business-eval" / "global-jev-ledger.json"
    )
    legacy_global_jev.initialize()
    legacy_ledger = PublicTrialLedger.create(
        legacy_path, plan_root, global_jev_ledger=legacy_global_jev
    )
    historical_trial = plan_root.trials[0]
    historical_route = plan_root.routes[0]
    legacy_ledger.start_route(historical_trial["trial_id"], historical_route)
    historical_attempt = legacy_ledger.reserve_tool_call(
        trial_id=historical_trial["trial_id"],
        route=historical_route,
        command="set_priority",
        effect="write",
        arguments_digest="a" * 64,
    )
    legacy_ledger.mark_tool_dispatched(historical_attempt)
    legacy_ledger.mark_tool_unknown(
        historical_attempt,
        error_category="transport_timeout",
        failure_stage="tool_gateway",
        exception_type="TimeoutError",
        http_status=504,
    )
    legacy_ledger.finish_route(
        historical_trial["trial_id"],
        historical_route,
        status="unknown",
        score_status="not_attempted",
        cleanup_verified=True,
        cleanup_evidence_digest="b" * 64,
    )
    legacy_ledger.close()

    with pytest.raises(ValueError, match="held public-evaluation supervisor lock"):
        open_first_wave_public_trial_ledger(
            project_root,
            plan_root,
            supervisor=PublicSupervisor(tmp_path / "wrong.lock"),
            candidate_sha="a" * 40,
        )
    with PublicSupervisor(tmp_path / "wrong-held.lock") as wrong_supervisor:
        with pytest.raises(ValueError, match="canonical supervisor lock"):
            open_first_wave_public_trial_ledger(
                project_root,
                plan_root,
                supervisor=wrong_supervisor,
                candidate_sha="a" * 40,
            )

    canonical_lock = canonical_public_supervisor_lock_path(project_root)
    with PublicSupervisor(canonical_lock) as supervisor:
        with pytest.raises(ValueError, match="plan root"):
            open_first_wave_public_trial_ledger(
                project_root,
                plan(),
                supervisor=supervisor,
                candidate_sha="a" * 40,
            )

    with PublicSupervisor(canonical_lock) as supervisor:
        ledger = open_first_wave_public_trial_ledger(
            project_root,
            plan_root,
            supervisor=supervisor,
            candidate_sha="a" * 40,
        )
        assert ledger.candidate_sha == "a" * 40
        trial, route = plan_root.trials[0], plan_root.routes[0]
        ledger.start_route(trial["trial_id"], route)
        ledger.finish_route(
            trial["trial_id"], route, status="completed", result_digest="a" * 64,
            score_status="scored", score_digest=_score_evidence_digest(trial),
            score_evidence=_score_evidence(trial),
            cleanup_verified=True, cleanup_evidence_digest="c" * 64,
        )
        assert Path(ledger.path) == canonical_public_trial_ledger_path(
            project_root,
            cohort_digest=plan_root.cohort_digest,
            candidate_sha="a" * 40,
        )
        assert ledger.global_jev_ledger.path == canonical_global_jev_ledger_path(project_root)
        ledger.close()

    with PublicSupervisor(canonical_lock) as supervisor:
        resumed = open_first_wave_public_trial_ledger(
            project_root,
            plan_root,
            supervisor=supervisor,
            candidate_sha="a" * 40,
        )
        assert resumed.route_status(trial["trial_id"], route) == "completed"
        assert resumed.candidate_sha == "a" * 40
        resumed.close()

    with PublicSupervisor(canonical_lock) as supervisor:
        next_candidate = open_first_wave_public_trial_ledger(
            project_root,
            plan_root,
            supervisor=supervisor,
            candidate_sha="b" * 40,
        )
        assert next_candidate.candidate_sha == "b" * 40
        assert next_candidate.route_status(trial["trial_id"], route) == "pending"
        next_candidate.close()
    preserved_legacy = PublicTrialLedger.resume(
        legacy_path, plan_root, global_jev_ledger=legacy_global_jev
    )
    assert (
        preserved_legacy.route_status(historical_trial["trial_id"], historical_route)
        == "unknown"
    )
    assert preserved_legacy.tool_attempt_status(historical_attempt) == "unknown"
    preserved_legacy.close()

    with pytest.raises(ValueError, match="canonical cohort ledger"):
        validate_public_trial_ledger_path(project_root, tmp_path / "alternate.sqlite3")
    with PublicSupervisor(canonical_lock) as supervisor:
        with pytest.raises(ValueError, match="Only the frozen smoke cohort"):
            open_first_wave_public_trial_ledger(
                project_root,
                replace(plan_root, mode="full"),
                supervisor=supervisor,
                candidate_sha="a" * 40,
            )


def test_public_trial_ledger_path_is_scoped_to_frozen_cohort_and_candidate(tmp_path):
    from decagent.evaluation.business.public_trials import (
        canonical_public_trial_ledger_path,
        validate_public_trial_ledger_path,
    )

    root = tmp_path / "project"
    cohort = "c" * 64
    first_sha, second_sha = "a" * 40, "b" * 40
    first = canonical_public_trial_ledger_path(
        root, cohort_digest=cohort, candidate_sha=first_sha
    )
    second_candidate = canonical_public_trial_ledger_path(
        root, cohort_digest=cohort, candidate_sha=second_sha
    )
    second_cohort = canonical_public_trial_ledger_path(
        root, cohort_digest="d" * 64, candidate_sha=first_sha
    )

    assert first != second_candidate
    assert first != second_cohort
    assert first == validate_public_trial_ledger_path(
        root,
        first,
        cohort_digest=cohort,
        candidate_sha=first_sha,
    )
    assert first == root.resolve() / ".decagent/business-eval/public-trials/cohorts" / (
        cohort + "-" + first_sha + ".sqlite3"
    )
    with pytest.raises(ValueError, match="canonical cohort ledger"):
        validate_public_trial_ledger_path(
            root,
            second_candidate,
            cohort_digest=cohort,
            candidate_sha=first_sha,
        )
    with pytest.raises(ValueError, match="cohort digest"):
        canonical_public_trial_ledger_path(root, cohort_digest="../escape", candidate_sha=first_sha)


def test_first_wave_ledger_never_initializes_a_missing_global_jev_ledger(tmp_path):
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        canonical_public_supervisor_lock_path,
    )
    from decagent.evaluation.business.public_trials import (
        canonical_public_trial_ledger_path,
        open_first_wave_public_trial_ledger,
    )

    project_root = tmp_path / "project-without-jev-ledger"
    selected = replace(plan(), root=project_root)
    lock_path = canonical_public_supervisor_lock_path(project_root)
    with PublicSupervisor(lock_path) as supervisor:
        with pytest.raises(FileNotFoundError, match="Existing project Jev ledger"):
            open_first_wave_public_trial_ledger(
                project_root,
                selected,
                supervisor=supervisor,
                candidate_sha="a" * 40,
            )
    assert not canonical_public_trial_ledger_path(
        project_root,
        cohort_digest=selected.cohort_digest,
        candidate_sha="a" * 40,
    ).exists()


def test_run_plan_rejects_manifest_with_changed_profile_or_digest(tmp_path):
    source = ROOT / "evals/benchmarks/manifests/comparison_trial_manifest_v7.json"
    changed = json.loads(source.read_text(encoding="utf-8"))
    changed["counts"]["smoke_trials"] += 1
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest digest"):
        load_public_run_plan(ROOT, mode="smoke", manifest_path=path, check_images=False)


def test_run_plan_rejects_rehashed_drift_in_the_approved_first_wave(tmp_path):
    from decagent.decisions.contracts import digest

    source = ROOT / "evals/benchmarks/manifests/comparison_trial_manifest_v7.json"
    changed = json.loads(source.read_text(encoding="utf-8"))
    changed["initial_smoke_wave"][0]["case_id"] = "unapproved-case"
    payload = {key: value for key, value in changed.items() if key != "manifest_digest"}
    changed["manifest_digest"] = digest(payload)
    path = tmp_path / "changed-wave.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="first-wave task identities"):
        load_public_run_plan(ROOT, mode="smoke", manifest_path=path, check_images=False)


def test_ledger_refuses_duplicate_route_and_resume_never_replays_unknown(tmp_path):
    selected = plan()
    path = tmp_path / "trials.sqlite3"
    ledger = PublicTrialLedger.create(path, selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    _start_public_route_with_machine_deadline(ledger, trial["trial_id"], route)
    reservation = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=500,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_dispatched(reservation)
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected)
    assert resumed.route_status(trial["trial_id"], route) == "unknown"
    assert resumed.attempt_status(reservation) == "unknown"
    with pytest.raises(ValueError, match="cannot be replayed"):
        resumed.start_route(trial["trial_id"], route)
    resumed.close()


def test_completed_route_is_persisted_and_cannot_be_replayed(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    machine_reservation = _start_public_route_with_machine_deadline(
        ledger, trial["trial_id"], route
    )
    ledger.settle_machine_time(machine_reservation, actual_seconds=0.0)
    ledger.finish_route(
        trial["trial_id"], route, status="completed", result_digest="a" * 64,
        score_status="scored", score_digest=_score_evidence_digest(trial),
        score_evidence=_score_evidence(trial),
        cleanup_verified=True, cleanup_evidence_digest="c" * 64,
    )

    assert ledger.route_status(trial["trial_id"], route) == "completed"
    with pytest.raises(ValueError, match="cannot be replayed"):
        ledger.start_route(trial["trial_id"], route)
    ledger.close()


def test_official_score_evidence_survives_ledger_resume(tmp_path):
    from decagent.decisions.contracts import digest

    selected = plan()
    path = tmp_path / "scored-trials.sqlite3"
    ledger = PublicTrialLedger.create(path, selected)
    trial = next(
        item for item in selected.trials if item["benchmark"] == "enterpriseops-gym"
    )
    route = selected.routes[0]
    score_evidence = {
        "source": "enterpriseops-gym-original-verifier",
        "value": 1.0,
        "success": True,
        "forbidden_side_effects": 0,
    }
    ledger.start_route(trial["trial_id"], route)

    ledger.finish_route(
        trial["trial_id"],
        route,
        status="completed",
        result_digest="a" * 64,
        score_status="scored",
        score_digest=digest(score_evidence),
        score_evidence=score_evidence,
        cleanup_verified=True,
        cleanup_evidence_digest="c" * 64,
    )
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected)
    outcome = resumed.route_lifecycle(trial["trial_id"], route)
    assert outcome["score_status"] == "scored"
    assert outcome["score_evidence"] == score_evidence
    assert outcome["score_digest"] == digest(score_evidence)
    resumed.close()


def test_official_score_evidence_requires_digest_and_benchmark_provenance(tmp_path):
    from decagent.decisions.contracts import digest

    selected = plan()
    trial = next(item for item in selected.trials if item["benchmark"] == "tau-bench")
    route = selected.routes[0]
    ledger = PublicTrialLedger.create(tmp_path / "tau-scored.sqlite3", selected)
    ledger.start_route(trial["trial_id"], route)
    evidence = {
        "source": "enterpriseops-gym-original-verifier",
        "value": 1.0,
        "success": True,
        "forbidden_side_effects": None,
    }

    with pytest.raises(ValueError, match="benchmark"):
        ledger.finish_route(
            trial["trial_id"],
            route,
            status="completed",
            result_digest="a" * 64,
            score_status="scored",
            score_digest=digest(evidence),
            score_evidence=evidence,
            cleanup_verified=True,
            cleanup_evidence_digest="c" * 64,
        )
    ledger.close()


def test_scored_route_requires_matching_durable_score_evidence(tmp_path):
    selected = plan()
    trial = next(
        item for item in selected.trials if item["benchmark"] == "enterpriseops-gym"
    )
    route = selected.routes[0]
    ledger = PublicTrialLedger.create(tmp_path / "score-digest.sqlite3", selected)
    ledger.start_route(trial["trial_id"], route)

    with pytest.raises(ValueError, match="durable official score evidence"):
        ledger.finish_route(
            trial["trial_id"],
            route,
            status="completed",
            result_digest="a" * 64,
            score_status="scored",
            score_digest="b" * 64,
            cleanup_verified=True,
            cleanup_evidence_digest="c" * 64,
        )

    evidence = _score_evidence(trial)
    with pytest.raises(ValueError, match="match a scored route's digest"):
        ledger.finish_route(
            trial["trial_id"],
            route,
            status="completed",
            result_digest="a" * 64,
            score_status="scored",
            score_digest="b" * 64,
            score_evidence=evidence,
            cleanup_verified=True,
            cleanup_evidence_digest="c" * 64,
        )
    assert ledger.route_status(trial["trial_id"], route) == "running"
    ledger.close()


def test_agent_and_auxiliary_role_budgets_are_separate_and_unknown_keeps_reservation(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)

    simulator = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="user_simulator",
        estimated_input_tokens=100,
        max_output_tokens=256,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.user_simulator.fingerprint,
    )
    ledger.mark_model_dispatched(simulator)
    ledger.settle_model_attempt(
        simulator,
        actual_input_tokens=80,
        actual_output_tokens=20,
        actual_cost_usd=None,
        provider_request_id="req-simulator-1",
    )

    primary = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=256,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_dispatched(primary)
    ledger.mark_model_unknown(primary, error_category="timeout")

    snapshot = ledger.usage_snapshot(trial["trial_id"], route)
    assert snapshot["user_simulator"]["requests"] == 1
    assert snapshot["user_simulator"]["actual_tokens"] == 100
    assert snapshot["primary"]["requests"] == 1
    assert snapshot["primary"]["reserved_tokens"] == 356
    assert snapshot["primary"]["unknown_requests"] == 1
    with pytest.raises(ValueError, match="unresolved or unknown"):
        ledger.reserve_model_attempt(
            trial_id=trial["trial_id"],
            route=route,
            role="primary",
            estimated_input_tokens=10,
            max_output_tokens=10,
            reserved_cost_usd=None,
            profile_digest=selected.profiles.primary.fingerprint,
        )
    ledger.close()


def test_known_token_usage_can_settle_with_unknown_cost_without_reporting_zero(tmp_path):
    selected = plan()
    unknown_primary = replace(
        selected.profiles.primary,
        price_version="qwen-unverified-billing-test",
        price_source="Billed API with voucher offset; exact rate not verified",
        input_price_per_million=None,
        output_price_per_million=None,
        pricing_status="unknown",
    )
    selected = replace(
        selected,
        profiles=replace(selected.profiles, primary=unknown_primary),
    )
    ledger = PublicTrialLedger.create(tmp_path / "unknown-cost.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=256,
        reserved_cost_usd=None,
        profile_digest=unknown_primary.fingerprint,
    )
    pending = ledger.usage_snapshot(trial["trial_id"], route)["primary"]
    assert pending["reserved_cost_usd"] is None
    assert pending["reserved_cost_complete"] is False
    ledger.mark_model_dispatched(attempt)
    ledger.settle_model_attempt(
        attempt,
        actual_input_tokens=80,
        actual_output_tokens=20,
        actual_cost_usd=None,
        provider_request_id="qwen-request-unknown-price",
    )

    snapshot = ledger.usage_snapshot(trial["trial_id"], route)["primary"]
    assert snapshot["actual_tokens"] == 100
    assert snapshot["actual_cost_usd"] is None
    assert snapshot["actual_cost_complete"] is False
    assert snapshot["reserved_cost_usd"] == Decimal("0")
    assert snapshot["reserved_cost_complete"] is True
    ledger.close()


def test_qwen_batch_cap_is_shared_across_routes_and_survives_resume(tmp_path):
    selected = plan()
    limits = dict(selected.batch_limits)
    limits["qwen_requests"] = 2
    limits["agent_qwen_requests"] = 1
    selected = replace(selected, batch_limits=limits)
    path = tmp_path / "batch-limits.sqlite3"
    ledger = PublicTrialLedger.create(path, selected)
    trial = selected.trials[0]
    first_route, second_route = selected.routes

    ledger.start_route(trial["trial_id"], first_route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=first_route,
        role="primary",
        estimated_input_tokens=10,
        max_output_tokens=10,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_dispatched(attempt)
    ledger.settle_model_attempt(
        attempt,
        actual_input_tokens=2,
        actual_output_tokens=3,
        actual_cost_usd=None,
        provider_request_id="qwen-batch-test-1",
    )
    ledger.finish_route(
        trial["trial_id"], first_route, status="completed", result_digest="a" * 64,
        score_status="scored", score_digest=_score_evidence_digest(trial),
        score_evidence=_score_evidence(trial),
        cleanup_verified=True, cleanup_evidence_digest="c" * 64,
    )
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected)
    snapshot = resumed.batch_snapshot()
    assert snapshot["qwen_requests"] == 1
    assert snapshot["qwen_actual_tokens"] == 5
    assert snapshot["stopped_reason"] is None
    resumed.start_route(trial["trial_id"], second_route)
    with pytest.raises(ValueError, match="agent_qwen_requests"):
        resumed.reserve_model_attempt(
            trial_id=trial["trial_id"],
            route=second_route,
            role="primary",
            estimated_input_tokens=1,
            max_output_tokens=1,
            reserved_cost_usd=None,
            profile_digest=selected.profiles.primary.fingerprint,
        )
    assert resumed.batch_snapshot()["stopped_reason"] == "agent_qwen_requests"
    resumed.close()


def test_qwen_actual_token_overrun_stops_the_persisted_batch(tmp_path):
    selected = plan()
    limits = dict(selected.batch_limits)
    limits["qwen_tokens"] = 12
    limits["agent_qwen_tokens"] = 12
    selected = replace(selected, batch_limits=limits)
    ledger = PublicTrialLedger.create(tmp_path / "batch-token-overrun.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=5,
        max_output_tokens=5,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_dispatched(attempt)

    with pytest.raises(ValueError, match="exceeded its pre-dispatch resource reservation"):
        ledger.settle_model_attempt(
            attempt,
            actual_input_tokens=7,
            actual_output_tokens=7,
            actual_cost_usd=None,
            provider_request_id="qwen-batch-token-overrun",
        )

    snapshot = ledger.batch_snapshot()
    assert snapshot["qwen_actual_tokens"] == 14
    assert snapshot["stopped_reason"] == "qwen_tokens"
    with pytest.raises(ValueError, match="batch is stopped"):
        ledger.start_route(trial["trial_id"], selected.routes[1])
    ledger.close()


def test_role_token_caps_are_enforced_before_dispatch(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation_ids = []
    for index in range(200):
        attempt = ledger.reserve_model_attempt(
            trial_id=trial["trial_id"],
            route=route,
            role="primary",
            estimated_input_tokens=10,
            max_output_tokens=4990,
            reserved_cost_usd=None,
            profile_digest=selected.profiles.primary.fingerprint,
        )
        ledger.mark_model_dispatched(attempt)
        ledger.settle_model_attempt(
            attempt,
            actual_input_tokens=10,
            actual_output_tokens=4990,
            actual_cost_usd=None,
            provider_request_id=f"req-{index}",
        )
        reservation_ids.append(attempt)
    assert len(reservation_ids) == 200
    with pytest.raises(ValueError, match="request budget"):
        ledger.reserve_model_attempt(
            trial_id=trial["trial_id"],
            route=route,
            role="primary",
            estimated_input_tokens=1,
            max_output_tokens=1,
            reserved_cost_usd=None,
            profile_digest=selected.profiles.primary.fingerprint,
        )
    ledger.close()


def test_batch_admission_and_model_tool_reservations_share_write_transaction(
    tmp_path, monkeypatch
):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "atomic-admission.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    observed_transaction_state = []
    require_admission = ledger._require_batch_admission

    def observe_transaction(**kwargs):
        observed_transaction_state.append(ledger._connection.in_transaction)
        return require_admission(**kwargs)

    monkeypatch.setattr(ledger, "_require_batch_admission", observe_transaction)
    model_attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=10,
        max_output_tokens=10,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_not_sent(
        model_attempt, reason="fixture_not_sent", verified_not_sent=True
    )
    tool_attempt = ledger.reserve_tool_call(
        trial_id=trial["trial_id"],
        route=route,
        command="get_ticket",
        effect="read",
        arguments_digest="a" * 64,
    )
    ledger.mark_tool_not_sent(
        tool_attempt, reason="fixture_not_sent", verified_not_sent=True
    )

    assert observed_transaction_state == [True, True]
    ledger.close()


def test_tool_call_cap_is_persisted_and_unknown_tool_is_never_replayed(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_tool_call(
        trial_id=trial["trial_id"],
        route=route,
        command="get_ticket",
        effect="read",
        arguments_digest="a" * 64,
    )
    ledger.mark_tool_dispatched(attempt)
    ledger.mark_tool_unknown(
        attempt,
        error_category="transport_timeout",
        failure_stage="tool_gateway",
        exception_type="TimeoutError",
        http_status=504,
    )

    assert ledger.tool_attempt_status(attempt) == "unknown"
    with pytest.raises(ValueError, match="cannot be replayed"):
        ledger.reserve_tool_call(
            trial_id=trial["trial_id"],
            route=route,
            command="get_ticket",
            effect="read",
            arguments_digest="a" * 64,
        )
    assert ledger.resource_snapshot(trial["trial_id"], route)["tool_calls"] == 1
    ledger.close()


def test_machine_time_reservation_exposes_a_persisted_route_deadline(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "route-deadline.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=30
    )
    with pytest.raises(ValueError, match="active route machine-time reservation"):
        ledger.route_remaining_seconds(trial["trial_id"], route)

    ledger.mark_machine_time_dispatched(reservation)
    remaining = ledger.route_remaining_seconds(trial["trial_id"], route)
    assert 0 < remaining <= 30
    row = ledger._connection.execute(
        "SELECT started_at_utc,deadline_at_utc FROM machine_time_attempts WHERE reservation_id=?",
        (reservation,),
    ).fetchone()
    assert row["started_at_utc"] and row["deadline_at_utc"]
    ledger.close()


def test_machine_time_deadline_restarts_at_agent_start_after_environment_setup(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "agent-start-deadline.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger._connection.execute(
        "UPDATE machine_time_attempts SET deadline_at_utc=? WHERE reservation_id=?",
        ("2000-01-01T00:00:00+00:00", reservation),
    )

    ledger.mark_machine_time_agent_started(reservation)

    remaining = ledger.route_remaining_seconds(trial["trial_id"], route)
    assert 0 < remaining <= 30
    row = ledger._connection.execute(
        "SELECT agent_started_at_utc,deadline_at_utc FROM machine_time_attempts "
        "WHERE reservation_id=?",
        (reservation,),
    ).fetchone()
    assert row["agent_started_at_utc"]
    assert row["deadline_at_utc"] != "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="already started"):
        ledger.mark_machine_time_agent_started(reservation)
    ledger.close()


def test_environment_setup_does_not_consume_agent_machine_time(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "pre-agent-deadline.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger._connection.execute(
        "UPDATE machine_time_attempts SET deadline_at_utc=? WHERE reservation_id=?",
        ("2000-01-01T00:00:00+00:00", reservation),
    )

    assert ledger.route_remaining_seconds(trial["trial_id"], route) == 30

    ledger.mark_machine_time_agent_started(reservation)
    remaining = ledger.route_remaining_seconds(trial["trial_id"], route)
    assert 0 < remaining <= 30
    ledger.close()


def test_tool_call_budget_is_enforced_at_200_attempts(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    for _ in range(200):
        attempt = ledger.reserve_tool_call(
            trial_id=trial["trial_id"],
            route=route,
            command="get_ticket",
            effect="read",
            arguments_digest="a" * 64,
        )
        ledger.mark_tool_dispatched(attempt)
        ledger.settle_tool_call(
            attempt, duration_seconds=0.01, result_digest="b" * 64
        )
    assert ledger.resource_snapshot(trial["trial_id"], route)["tool_calls"] == 200
    with pytest.raises(ValueError, match="tool-call budget"):
        ledger.reserve_tool_call(
            trial_id=trial["trial_id"],
            route=route,
            command="get_ticket",
            effect="read",
            arguments_digest="a" * 64,
        )
    ledger.close()


def test_interrupted_tool_attempt_becomes_unknown_without_replay(tmp_path):
    selected = plan()
    path = tmp_path / "trials.sqlite3"
    ledger = PublicTrialLedger.create(path, selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_tool_call(
        trial_id=trial["trial_id"],
        route=route,
        command="set_priority",
        effect="write",
        arguments_digest="a" * 64,
    )
    ledger.mark_tool_dispatched(attempt)
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected)
    assert resumed.tool_attempt_status(attempt) == "unknown"
    assert resumed.route_status(trial["trial_id"], route) == "unknown"
    with pytest.raises(ValueError, match="cannot be replayed"):
        resumed.reserve_tool_call(
            trial_id=trial["trial_id"],
            route=route,
            command="set_priority",
            effect="write",
            arguments_digest="a" * 64,
        )
    resumed.close()


def test_unknown_route_records_cleanup_without_becoming_success(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "unknown-cleanup.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_tool_call(
        trial_id=trial["trial_id"],
        route=route,
        command="set_priority",
        effect="write",
        arguments_digest="a" * 64,
    )
    ledger.mark_tool_dispatched(attempt)
    ledger.mark_tool_unknown(
        attempt,
        error_category="transport_timeout",
        failure_stage="tool_gateway",
        exception_type="TimeoutError",
        http_status=504,
    )
    cleanup_digest = "b" * 64

    ledger.finish_route(
        trial["trial_id"],
        route,
        status="unknown",
        score_status="not_attempted",
        cleanup_verified=True,
        cleanup_evidence_digest=cleanup_digest,
    )
    ledger.finish_route(
        trial["trial_id"],
        route,
        status="unknown",
        score_status="not_attempted",
        cleanup_verified=True,
        cleanup_evidence_digest=cleanup_digest,
    )

    lifecycle = ledger.route_lifecycle(trial["trial_id"], route)
    assert lifecycle == {
        "status": "unknown",
        "error_category": "transport_timeout",
        "failure_stage": "tool_gateway",
        "exception_type": "TimeoutError",
        "score_status": "not_attempted",
        "score_digest": None,
        "score_evidence": None,
        "cleanup_status": "verified",
        "cleanup_evidence_digest": cleanup_digest,
    }
    with pytest.raises(ValueError, match="diagnostics cannot be rewritten"):
        ledger.finish_route(
            trial["trial_id"],
            route,
            status="unknown",
            error_category="route_deadline",
            failure_stage="route_deadline",
            exception_type="TimeoutError",
            score_status="not_attempted",
            cleanup_verified=True,
            cleanup_evidence_digest=cleanup_digest,
        )
    with pytest.raises(ValueError, match="cannot be rewritten"):
        ledger.finish_route(
            trial["trial_id"],
            route,
            status="completed",
            result_digest="c" * 64,
            score_status="scored",
            score_digest=_score_evidence_digest(trial),
            score_evidence=_score_evidence(trial),
            cleanup_verified=True,
            cleanup_evidence_digest=cleanup_digest,
        )
    ledger.close()


def test_ledgered_tool_gateway_settles_digest_only_evidence(tmp_path):
    from decagent.decisions.contracts import digest
    from decagent.evaluation.business.public_resources import LedgeredToolGateway

    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "ledgered-tool.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    _start_public_route_with_machine_deadline(ledger, trial["trial_id"], route)

    class Upstream:
        calls = 0

        def call(self, command, params, *, timeout_seconds):
            self.calls += 1
            assert 0 < timeout_seconds <= 60
            assert command == "lookup_ticket"
            assert params == {"ticket_id": "private-ticket-id"}
            return {"ticket": {"id": "private-ticket-id", "title": "private title"}}

    upstream = Upstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id=trial["trial_id"],
        route=route,
        tool_effects={"lookup_ticket": "read"},
    )
    params = {"ticket_id": "private-ticket-id"}
    result = gateway.call("lookup_ticket", params)

    row = ledger._connection.execute(
        "SELECT status,arguments_digest,result_digest FROM tool_attempts"
    ).fetchone()
    assert result["ticket"]["id"] == "private-ticket-id"
    assert upstream.calls == 1
    assert row["status"] == "settled"
    assert row["arguments_digest"] == digest(params)
    assert row["result_digest"] == digest(result)
    assert "private-ticket-id" not in row["arguments_digest"]
    assert "private title" not in row["result_digest"]
    assert ledger.resource_snapshot(trial["trial_id"], route)["tool_calls"] == 1
    ledger.close()


def test_ledgered_tool_gateway_marks_transport_failure_unknown_and_blocks_replay(tmp_path):
    from decagent.evaluation.business.public_resources import (
        LedgeredToolGateway,
        PublicToolOutcomeUnknown,
    )

    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "unknown-tool.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    _start_public_route_with_machine_deadline(ledger, trial["trial_id"], route)

    class UncertainUpstream:
        calls = 0

        def call(self, command, params, *, timeout_seconds):
            self.calls += 1
            assert 0 < timeout_seconds <= 60
            raise TimeoutError("transport lost after remote dispatch")

    upstream = UncertainUpstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id=trial["trial_id"],
        route=route,
        tool_effects={"create_ticket": "write"},
    )

    with pytest.raises(PublicToolOutcomeUnknown):
        gateway.call("create_ticket", {"title": "private title"})

    snapshot = ledger.resource_snapshot(trial["trial_id"], route)
    assert snapshot["tool_calls"] == snapshot["unknown_tool_calls"] == 1
    assert ledger.route_status(trial["trial_id"], route) == "unknown"
    attempt = ledger.tool_attempts_snapshot(trial["trial_id"], route)[0]
    assert attempt["failure_stage"] == "tool_gateway"
    assert attempt["exception_type"] == "TimeoutError"
    assert attempt["http_status"] is None
    assert "transport lost after remote dispatch" not in repr(attempt)
    lifecycle = ledger.route_lifecycle(trial["trial_id"], route)
    assert lifecycle["failure_stage"] == "tool_gateway"
    assert lifecycle["exception_type"] == "TimeoutError"
    assert upstream.calls == 1
    with pytest.raises(ValueError, match="cannot be replayed"):
        ledger.start_route(trial["trial_id"], route)
    ledger.close()


def test_ledgered_tool_gateway_does_not_dispatch_unregistered_commands(tmp_path):
    from decagent.evaluation.business.public_resources import LedgeredToolGateway

    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "unregistered-tool.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    _start_public_route_with_machine_deadline(ledger, trial["trial_id"], route)

    class Upstream:
        calls = 0

        def call(self, command, params, *, timeout_seconds):
            self.calls += 1
            return {}

    upstream = Upstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id=trial["trial_id"],
        route=route,
        tool_effects={"lookup_ticket": "read"},
    )
    with pytest.raises(ValueError, match="frozen capability catalog"):
        gateway.call("delete_all", {})
    assert upstream.calls == 0
    assert ledger.resource_snapshot(trial["trial_id"], route)["tool_calls"] == 0
    ledger.close()


def test_ledgered_tool_gateway_releases_verified_pre_dispatch_rejection():
    from decagent.evaluation.business.public_resources import LedgeredToolGateway

    class StoppedLedger:
        released = None

        def batch_snapshot(self):
            return {"remaining_wall_seconds": 10.0}

        def route_remaining_seconds(self, _trial_id, _route):
            return 10.0

        def reserve_tool_call(self, **kwargs):
            return "reserved-attempt"

        def mark_tool_dispatched(self, attempt_id):
            assert attempt_id == "reserved-attempt"
            raise ValueError("batch deadline reached before network dispatch")

        def mark_tool_not_sent(self, attempt_id, *, reason, verified_not_sent):
            self.released = (attempt_id, reason, verified_not_sent)

        def settle_tool_call(self, *args, **kwargs):
            raise AssertionError("No upstream call should be settled")

        def mark_tool_unknown(self, *args, **kwargs):
            raise AssertionError("A pre-dispatch rejection is not unknown")

    class Upstream:
        calls = 0

        def call(self, command, params, *, timeout_seconds):
            self.calls += 1
            return {}

    ledger, upstream = StoppedLedger(), Upstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id="frozen-trial",
        route="plan_mcp",
        tool_effects={"lookup_ticket": "read"},
    )

    with pytest.raises(ValueError, match="batch deadline"):
        gateway.call("lookup_ticket", {})

    assert ledger.released == (
        "reserved-attempt",
        "dispatch_not_admitted",
        True,
    )
    assert upstream.calls == 0


def test_ledgered_tool_gateway_passes_minimum_remaining_deadline_to_transport():
    from decagent.evaluation.business.public_resources import LedgeredToolGateway

    class DeadlineLedger:
        events = []

        def batch_snapshot(self):
            return {"remaining_wall_seconds": 3.5}

        def route_remaining_seconds(self, _trial_id, _route):
            return 8.0

        def reserve_tool_call(self, **kwargs):
            self.events.append(("reserve", kwargs))
            return "tool-attempt"

        def mark_tool_dispatched(self, attempt_id):
            self.events.append(("dispatch", attempt_id))

        def mark_tool_not_sent(self, *_args, **_kwargs):
            raise AssertionError("A dispatched call must not be marked not-sent")

        def settle_tool_call(self, *_args, **_kwargs):
            self.events.append(("settle",))

        def mark_tool_unknown(self, *_args, **_kwargs):
            raise AssertionError("Successful transport must not become unknown")

    class DeadlineUpstream:
        timeouts = []

        def call(self, _command, _params, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            return {"ok": True}

    ledger, upstream = DeadlineLedger(), DeadlineUpstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id="frozen-trial",
        route="plan_mcp",
        tool_effects={"lookup_ticket": "read"},
    )

    assert gateway.call("lookup_ticket", {}) == {"ok": True}
    assert upstream.timeouts == [3.5]
    assert [event[0] for event in ledger.events] == ["reserve", "dispatch", "settle"]


def test_ledgered_tool_gateway_does_not_reserve_or_dispatch_after_deadline():
    from decagent.evaluation.business.public_resources import LedgeredToolGateway

    class ExpiredLedger:
        def __init__(self):
            self.reservations = 0

        def batch_snapshot(self):
            return {"remaining_wall_seconds": 10.0}

        def route_remaining_seconds(self, _trial_id, _route):
            return 0.0

        def reserve_tool_call(self, **_kwargs):
            self.reservations += 1
            return "tool-attempt"

        def mark_tool_dispatched(self, _attempt_id):
            raise AssertionError("Expired route must be rejected before dispatch")

        def mark_tool_not_sent(self, *_args, **_kwargs):
            raise AssertionError("Expired route must be rejected before dispatch")

        def settle_tool_call(self, *_args, **_kwargs):
            raise AssertionError("Expired route must be rejected before dispatch")

        def mark_tool_unknown(self, *_args, **_kwargs):
            raise AssertionError("Expired route must be rejected before dispatch")

    class Upstream:
        calls = 0

        def call(self, _command, _params, *, timeout_seconds):
            self.calls += 1
            return {"timeout": timeout_seconds}

    ledger, upstream = ExpiredLedger(), Upstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id="frozen-trial",
        route="plan_mcp",
        tool_effects={"lookup_ticket": "read"},
    )

    with pytest.raises(ValueError, match="deadline expired"):
        gateway.call("lookup_ticket", {})
    assert ledger.reservations == 0
    assert upstream.calls == 0


def test_ledgered_tool_gateway_rechecks_deadline_after_dispatch_mark_before_transport():
    from decagent.evaluation.business.public_resources import (
        LedgeredToolGateway,
        PublicToolOutcomeUnknown,
    )

    class BoundaryLedger:
        events = []

        def __init__(self):
            self.route_reads = 0

        def batch_snapshot(self):
            return {"remaining_wall_seconds": 5.0}

        def route_remaining_seconds(self, _trial_id, _route):
            self.route_reads += 1
            return 2.0 if self.route_reads <= 2 else 0.0

        def reserve_tool_call(self, **_kwargs):
            self.events.append("reserve")
            return "tool-attempt"

        def mark_tool_dispatched(self, _attempt_id):
            self.events.append("dispatch")

        def mark_tool_not_sent(self, *_args, **_kwargs):
            raise AssertionError("A dispatch-marked attempt cannot be released")

        def settle_tool_call(self, *_args, **_kwargs):
            raise AssertionError("Expired deadline cannot settle a remote result")

        def mark_tool_unknown(self, _attempt_id, *, error_category, **diagnostic):
            self.events.append(("unknown", error_category, diagnostic))

    class Upstream:
        calls = 0

        def call(self, _command, _params, *, timeout_seconds):
            self.calls += 1
            return {"timeout": timeout_seconds}

    ledger, upstream = BoundaryLedger(), Upstream()
    gateway = LedgeredToolGateway(
        upstream,
        ledger,
        trial_id="frozen-trial",
        route="plan_mcp",
        tool_effects={"lookup_ticket": "read"},
    )

    with pytest.raises(PublicToolOutcomeUnknown, match="replay is disabled"):
        gateway.call("lookup_ticket", {})

    assert upstream.calls == 0
    assert ledger.events == [
        "reserve",
        "dispatch",
        (
            "unknown",
            "tool_deadline_error",
            {
                "failure_stage": "route_deadline",
                "exception_type": "ValueError",
                "http_status": None,
            },
        ),
    ]


def test_machine_time_is_reserved_before_work_and_unknown_is_retained(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=1700
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger.settle_machine_time(reservation, actual_seconds=1700)
    with pytest.raises(ValueError, match="machine-time budget"):
        ledger.reserve_machine_time(
            trial_id=trial["trial_id"], route=route, max_seconds=101
        )

    snapshot = ledger.resource_snapshot(trial["trial_id"], route)
    assert snapshot["actual_agent_machine_seconds"] == 1700
    ledger.close()


def test_tau_official_steps_have_a_separate_route_limit_and_survive_resume(tmp_path):
    selected = plan()
    path = tmp_path / "tau-steps.sqlite3"
    ledger = PublicTrialLedger.create(path, selected)
    trial = next(item for item in selected.trials if item["benchmark"] == "tau-bench")
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)

    for step_number in range(1, selected.resource_limits["tau_steps"] + 1):
        assert ledger.reserve_tau_step(
            trial_id=trial["trial_id"], route=route, kind="agent"
        ) == step_number
    with pytest.raises(ValueError, match="τ-Bench step limit"):
        ledger.reserve_tau_step(trial_id=trial["trial_id"], route=route, kind="environment")
    assert ledger.resource_snapshot(trial["trial_id"], route)["tau_steps"] == 200
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected)
    assert resumed.route_status(trial["trial_id"], route) == "unknown"
    assert resumed.resource_snapshot(trial["trial_id"], route)["tau_steps"] == 200
    with pytest.raises(ValueError, match="cannot be replayed"):
        resumed.reserve_tau_step(trial_id=trial["trial_id"], route=route, kind="user")
    assert resumed.resource_snapshot(trial["trial_id"], route)["tau_steps"] == 200
    resumed.close()


def test_route_claim_and_machine_time_reservation_are_atomic(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]

    ledger.start_route_with_machine_time(
        trial["trial_id"], route, max_seconds=1800
    )

    assert ledger.route_status(trial["trial_id"], route) == "running"
    assert ledger.resource_snapshot(trial["trial_id"], route)[
        "reserved_agent_machine_seconds"
    ] == 1800
    with pytest.raises(ValueError, match="cannot be replayed"):
        ledger.start_route_with_machine_time(
            trial["trial_id"], route, max_seconds=1
        )
    assert ledger.resource_snapshot(trial["trial_id"], route)[
        "reserved_agent_machine_seconds"
    ] == 1800
    ledger.close()


def test_failed_atomic_machine_time_admission_leaves_route_pending(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]

    with pytest.raises(ValueError, match="machine-time budget"):
        ledger.start_route_with_machine_time(
            trial["trial_id"], route, max_seconds=1801
        )

    assert ledger.route_status(trial["trial_id"], route) == "pending"
    assert ledger.resource_snapshot(trial["trial_id"], route)[
        "reserved_agent_machine_seconds"
    ] == 0
    ledger.close()


def test_machine_time_settlement_cannot_exceed_frozen_route_cap(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=1800
    )
    ledger.mark_machine_time_dispatched(reservation)
    with pytest.raises(ValueError, match="machine-time budget"):
        ledger.settle_machine_time(reservation, actual_seconds=1801)
    assert ledger.route_status(trial["trial_id"], route) == "failed"
    ledger.close()


def test_supervised_machine_time_overrun_can_record_cleanup_evidence(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=1800
    )
    ledger.mark_machine_time_dispatched(reservation)

    within_limit = ledger.settle_machine_time(
        reservation, actual_seconds=1801, defer_route_failure=True
    )

    assert within_limit is False
    assert ledger.route_status(trial["trial_id"], route) == "running"
    ledger.finish_route(
        trial["trial_id"],
        route,
        status="failed",
        result_digest="a" * 64,
        error_category="resource_overrun",
        cleanup_verified=True,
        cleanup_evidence_digest="b" * 64,
    )
    assert ledger.route_lifecycle(trial["trial_id"], route)["cleanup_status"] == "verified"
    ledger.close()


def test_unknown_machine_time_reservation_is_retained_and_not_replayed(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route, max_seconds=1700
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger.mark_machine_time_unknown(reservation, error_category="process_interrupted")

    snapshot = ledger.resource_snapshot(trial["trial_id"], route)
    assert snapshot["unknown_machine_time_reservations"] == 1
    assert snapshot["reserved_agent_machine_seconds"] == 1700
    with pytest.raises(ValueError, match="cannot be replayed"):
        ledger.reserve_machine_time(
            trial_id=trial["trial_id"], route=route, max_seconds=1
        )
    ledger.close()


def test_model_attempt_records_frozen_profile_and_measured_provider_evidence(tmp_path):
    selected = plan()
    ledger = PublicTrialLedger.create(tmp_path / "trials.sqlite3", selected)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=256,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    reserved = ledger.model_attempt_snapshot(attempt)
    assert reserved["provider"] == selected.profiles.primary.provider
    assert reserved["model"] == selected.profiles.primary.model
    assert reserved["endpoint"] == selected.profiles.primary.endpoint
    assert reserved["price_version"] == selected.profiles.primary.price_version
    assert reserved["attempt_number"] == 1
    assert "api_key" not in reserved

    ledger.mark_model_dispatched(attempt)
    ledger.settle_model_attempt(
        attempt,
        actual_input_tokens=80,
        actual_output_tokens=20,
        actual_cost_usd=None,
        provider_request_id="qwen-req-1",
        http_status=200,
        attempt_seconds=0.25,
        actual_provider="qwen",
        actual_model="qwen3.8-flash",
    )
    settled = ledger.model_attempt_snapshot(attempt)
    assert settled["status"] == "settled"
    assert settled["http_status"] == 200
    assert settled["attempt_seconds"] == 0.25
    assert settled["actual_cost_usd"] is None
    assert ledger.usage_snapshot(trial["trial_id"], route)["primary"][
        "actual_cost_complete"
    ] is False
    assert settled["provider_request_id"] == "qwen-req-1"
    assert settled["actual_provider"] == "qwen"
    assert settled["actual_model"] == "qwen3.8-flash"
    ledger.close()


def test_jev_attempt_reserves_and_settles_the_shared_cumulative_ledger(tmp_path):
    from decagent.evaluation.business.ledger import GlobalJevLedger

    selected = plan()
    global_ledger = GlobalJevLedger(tmp_path / "shared-jev.json")
    global_ledger.initialize()
    ledger = PublicTrialLedger.create(
        tmp_path / "trials.sqlite3", selected, global_jev_ledger=global_ledger
    )
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="jev",
        estimated_input_tokens=100,
        max_output_tokens=50,
        reserved_cost_usd=Decimal("0.0001"),
        profile_digest=selected.profiles.jev.fingerprint,
    )

    snapshot = global_ledger.snapshot()
    assert snapshot["held_usd"] == "0.0001"
    attempt_snapshot = ledger.model_attempt_snapshot(attempt)
    assert attempt_snapshot["global_jev_reservation_id"]

    ledger.mark_model_dispatched(attempt)
    ledger.mark_model_unknown(attempt, error_category="timeout")
    snapshot = global_ledger.snapshot()
    assert snapshot["blocked_unknown_or_pending"] is True
    assert snapshot["held_usd"] == "0.0001"
    assert global_ledger.reserve(Decimal("0.0001"), {"stage": "other-route"}) is None
    assert ledger.route_status(trial["trial_id"], route) == "unknown"

    # An unresolved Jev call blocks new Jev work across the whole frozen cohort,
    # not just subsequent attempts on the route that timed out.
    other_trial = selected.trials[1]
    other_route = selected.routes[0]
    ledger.start_route(other_trial["trial_id"], other_route)
    with pytest.raises(
        ValueError, match="Shared cumulative Jev budget is unavailable or blocked"
    ):
        ledger.reserve_model_attempt(
            trial_id=other_trial["trial_id"],
            route=other_route,
            role="jev",
            estimated_input_tokens=100,
            max_output_tokens=50,
            reserved_cost_usd=Decimal("0.0001"),
            profile_digest=selected.profiles.jev.fingerprint,
        )
    assert ledger.usage_snapshot(other_trial["trial_id"], other_route)["jev"][
        "requests"
    ] == 0
    assert len(global_ledger.snapshot()["attempts"]) == 1
    assert ledger.route_status(other_trial["trial_id"], other_route) == "running"
    ledger.close()


def test_jev_settlement_updates_global_and_route_ledgers_with_resolved_identity(tmp_path):
    from decagent.evaluation.business.ledger import GlobalJevLedger

    selected = plan()
    global_ledger = GlobalJevLedger(tmp_path / "shared-jev.json")
    global_ledger.initialize()
    ledger = PublicTrialLedger.create(
        tmp_path / "trials.sqlite3", selected, global_jev_ledger=global_ledger
    )
    trial, route = selected.trials[0], selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="jev",
        estimated_input_tokens=100,
        max_output_tokens=50,
        reserved_cost_usd=Decimal("0.0001"),
        profile_digest=selected.profiles.jev.fingerprint,
    )
    ledger.mark_model_dispatched(attempt)
    ledger.settle_model_attempt(
        attempt,
        actual_input_tokens=100,
        actual_output_tokens=40,
        actual_cost_usd=Decimal("0.00005"),
        provider_request_id="req-jev-1",
        generation_id="gen-jev-1",
        actual_provider="openrouter",
        actual_model="typesafe/jev-1.13-20260917",
    )

    snapshot = global_ledger.snapshot()
    settled = ledger.model_attempt_snapshot(attempt)
    assert snapshot["spent_usd"] == "0.000303176"
    assert snapshot["held_usd"] == "0"
    assert settled["status"] == "settled"
    assert settled["actual_provider"] == "openrouter"
    assert settled["actual_model"] == "typesafe/jev-1.13-20260917"
    assert settled["global_jev_reservation_id"]
    ledger.close()


def test_live_process_cannot_resume_an_unexpired_public_trial_lease(tmp_path):
    selected = plan()
    path = tmp_path / "trials.sqlite3"
    ledger = PublicTrialLedger.create(path, selected, lease_seconds=30)
    code = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from decagent.evaluation.business.public_trials import ("
            "PublicTrialLedger, load_public_run_plan)",
            (
                "plan = load_public_run_plan("
                "Path.cwd(), check_images=False, check_runtime=False, "
                "check_source_files=False)"
            ),
            "try:",
            "    PublicTrialLedger.resume(sys.argv[1], plan)",
            "except ValueError as exc:",
            "    print(str(exc))",
            "    raise SystemExit(0)",
            "raise SystemExit(2)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "active execution lease" in completed.stdout
    assert ledger.route_status(selected.trials[0]["trial_id"], selected.routes[0]) == "pending"
    ledger.close()


def test_takeover_fences_an_expired_owner_before_it_can_start_a_route(tmp_path):
    selected = plan()
    path = tmp_path / "trials.sqlite3"
    stale = PublicTrialLedger.create(path, selected, lease_seconds=30)
    trial = selected.trials[0]
    route = selected.routes[0]
    stale.start_route(trial["trial_id"], route)
    reserved = stale.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=50,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    stale._heartbeat_stop.set()
    stale._heartbeat_thread.join(timeout=1)
    stale._connection.execute(
        "UPDATE execution_lease SET expires_at_utc=? WHERE lease_name=?",
        ("2000-01-01T00:00:00+00:00", "public-evaluation"),
    )
    stale._connection.commit()

    current = PublicTrialLedger.resume(path, selected, lease_seconds=30)
    with pytest.raises(ValueError, match="execution lease was lost"):
        stale.start_route(trial["trial_id"], route)
    with pytest.raises(ValueError, match="execution lease was lost"):
        stale.mark_model_dispatched(reserved)
    assert current.route_status(trial["trial_id"], route) == "unknown"
    assert current.attempt_status(reserved) == "unknown"
    current.close()
    stale.close()


def test_existing_public_ledger_adds_billing_columns_without_resetting_cohort(tmp_path):
    selected = plan()
    path = tmp_path / "trials.sqlite3"
    original = PublicTrialLedger.create(path, selected, lease_seconds=30)
    trial = selected.trials[0]
    route = selected.routes[0]
    original.start_route(trial["trial_id"], route)
    attempt_id = original.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=50,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    original.mark_model_dispatched(attempt_id)
    original.mark_model_unknown(
        attempt_id,
        error_category="model_dispatch_error",
        failure_stage="model_callback",
        exception_type="TimeoutError",
    )
    before_migration = original.batch_snapshot()
    original.close()

    billing_columns = (
        "billing_gross_amount",
        "billing_currency",
        "billing_voucher_credit_amount",
        "billing_cash_paid_amount",
        "billing_source",
        "billing_record_id",
    )
    with sqlite3.connect(path) as connection:
        for name in billing_columns:
            connection.execute(f"ALTER TABLE model_attempts DROP COLUMN {name}")
        connection.execute("ALTER TABLE route_trials DROP COLUMN score_evidence_json")
        connection.execute("ALTER TABLE model_attempts DROP COLUMN failure_stage")
        connection.execute("ALTER TABLE model_attempts DROP COLUMN exception_type")
        connection.execute("ALTER TABLE tool_attempts DROP COLUMN failure_stage")
        connection.execute("ALTER TABLE tool_attempts DROP COLUMN exception_type")
        connection.execute("ALTER TABLE tool_attempts DROP COLUMN http_status")
        connection.execute("ALTER TABLE route_trials DROP COLUMN failure_stage")
        connection.execute("ALTER TABLE route_trials DROP COLUMN exception_type")
        connection.execute(
            "UPDATE execution_lease SET expires_at_utc='2000-01-01T00:00:00+00:00'"
        )

    resumed = PublicTrialLedger.resume(path, selected, lease_seconds=30)
    columns = {
        row[1] for row in resumed._connection.execute("PRAGMA table_info(model_attempts)")
    }
    assert set(billing_columns) <= columns
    route_columns = {
        row[1] for row in resumed._connection.execute("PRAGMA table_info(route_trials)")
    }
    assert "score_evidence_json" in route_columns
    assert {"failure_stage", "exception_type"} <= route_columns
    tool_columns = {
        row[1] for row in resumed._connection.execute("PRAGMA table_info(tool_attempts)")
    }
    assert {"failure_stage", "exception_type", "http_status"} <= tool_columns
    assert {"failure_stage", "exception_type"} <= columns
    assert resumed.plan.cohort_digest == selected.cohort_digest
    assert resumed.route_status(trial["trial_id"], route) == "unknown"
    assert resumed.attempt_status(attempt_id) == "unknown"
    assert resumed.model_attempt_snapshot(attempt_id)["failure_stage"] is None
    assert resumed.model_attempt_snapshot(attempt_id)["exception_type"] is None
    after_migration = resumed.batch_snapshot()
    assert after_migration["qwen_requests"] == before_migration["qwen_requests"] == 1
    assert after_migration["qwen_tokens"] == before_migration["qwen_tokens"]
    assert after_migration["qwen_reserved_tokens"] == before_migration["qwen_reserved_tokens"]
    resumed.close()


def test_public_trial_ledger_binds_candidate_sha_immutably(tmp_path):
    selected = plan()
    path = tmp_path / "candidate-bound.sqlite3"
    ledger = PublicTrialLedger.create(path, selected, lease_seconds=30)

    assert ledger.bind_candidate_sha("a" * 40) == "a" * 40
    assert ledger.candidate_sha == "a" * 40
    ledger.close()

    resumed = PublicTrialLedger.resume(path, selected, lease_seconds=30)
    assert resumed.candidate_sha == "a" * 40
    assert resumed.bind_candidate_sha("a" * 40) == "a" * 40
    with pytest.raises(ValueError, match="candidate SHA differs"):
        resumed.bind_candidate_sha("b" * 40)
    resumed.close()


def test_legacy_public_ledger_with_attempts_cannot_be_bound_to_a_new_candidate(tmp_path):
    selected = plan()
    path = tmp_path / "legacy-attempts.sqlite3"
    ledger = PublicTrialLedger.create(path, selected, lease_seconds=30)
    trial = selected.trials[0]
    route = selected.routes[0]
    ledger.start_route(trial["trial_id"], route)
    attempt_id = ledger.reserve_model_attempt(
        trial_id=trial["trial_id"],
        route=route,
        role="primary",
        estimated_input_tokens=100,
        max_output_tokens=50,
        reserved_cost_usd=None,
        profile_digest=selected.profiles.primary.fingerprint,
    )
    ledger.mark_model_dispatched(attempt_id)
    ledger.mark_model_unknown(
        attempt_id,
        error_category="model_dispatch_error",
        failure_stage="model_callback",
        exception_type="TimeoutError",
    )

    with pytest.raises(ValueError, match="historical attempts"):
        ledger.bind_candidate_sha("c" * 40)
    assert ledger.route_status(trial["trial_id"], route) == "unknown"
    assert ledger.attempt_status(attempt_id) == "unknown"
    assert ledger.batch_snapshot()["qwen_requests"] == 1
    ledger.close()


def test_malformed_candidate_sha_metadata_fails_closed(tmp_path):
    import sqlite3

    selected = plan()
    path = tmp_path / "malformed-candidate.sqlite3"
    ledger = PublicTrialLedger.create(path, selected, lease_seconds=30)
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('candidate_sha',?)",
            ("not-json",),
        )

    with pytest.raises(ValueError, match="metadata is malformed"):
        PublicTrialLedger.resume(path, selected, lease_seconds=30)

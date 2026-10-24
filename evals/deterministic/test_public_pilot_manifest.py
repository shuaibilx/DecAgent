from __future__ import annotations

import json
from pathlib import Path

from decagent.decisions.contracts import digest
from evals.benchmarks.pilot_manifest import (
    build_pilot_manifest,
    load_public_pilot_plan,
    validate_pilot_manifest_target,
)

SEED = "sop-pilot-20261024-v1"


def test_pilot_manifest_output_stays_in_ignored_state(tmp_path):
    target = validate_pilot_manifest_target(
        tmp_path,
        ".decagent/evaluation/manifests/pilot.json",
    )

    assert target.is_relative_to(tmp_path / ".decagent/evaluation/manifests")
    assert target.parent.is_dir()
    try:
        validate_pilot_manifest_target(tmp_path, "evals/benchmarks/manifests/pilot.json")
    except ValueError as exc:
        assert "ignored" in str(exc)
    else:
        raise AssertionError("a tracked-directory freeze would dirty the CI candidate")


def _trial(benchmark, domain, split, case_id, exposure="not_exposed"):
    return {
        "trial_id": digest({"benchmark": benchmark, "domain": domain, "case_id": case_id}),
        "benchmark": benchmark,
        "domain": domain,
        "split": split,
        "case_id": case_id,
        "version": "pinned-version",
        "exposure_group": exposure,
        "model_profile_digest": "m" * 64,
        "jev_profile_digest": "j" * 64,
        "user_simulator_profile_digest": "u" * 64,
        "judge_profile_digest": "v" * 64,
        "resource_profile_digest": "r" * 64,
        "response_policy_digest": "p" * 64,
        "status": "not_run",
    }


def _base_manifest():
    trials = []
    for domain in ("csm", "itsm"):
        trials.extend(
            _trial("enterpriseops-gym", domain, "oracle", f"{domain}-{i:03}")
            for i in range(12)
        )
        trials.append(
            _trial(
                "enterpriseops-gym",
                domain,
                "oracle",
                f"{domain}-exposed",
                "development_exposed",
            )
        )
    for domain in ("retail", "telecom"):
        trials.extend(
            _trial("tau-bench", domain, "test", f"{domain}-{i:03}")
            for i in range(5)
        )
    manifest = {
        "schema_version": "business-comparison-trial-manifest-10",
        "manifest_id": "base-manifest",
        "full_trials": trials,
    }
    manifest["manifest_digest"] = digest(manifest)
    return manifest


def test_pilot_manifest_selects_only_id_domain_split_and_reviewed_exposure():
    pilot = build_pilot_manifest(_base_manifest(), seed=SEED)

    assert pilot["seed"] == SEED
    assert pilot["counts"] == {
        "enterpriseops_csm": 10,
        "enterpriseops_itsm": 10,
        "tau_retail_test": 2,
        "tau_telecom_test": 2,
        "tasks": 24,
        "route_slots": 68,
    }
    assert len(pilot["selected_trials"]) == 24
    assert all(
        "prompt" not in row and "target_state" not in row
        for row in pilot["selected_trials"]
    )
    assert all(row["exposure_group"] == "not_exposed" for row in pilot["selected_trials"])
    assert sum(len(item["routes"]) for item in pilot["route_schedule"]) == 68


def test_pilot_manifest_is_deterministic_and_digest_protected():
    base = _base_manifest()
    first = build_pilot_manifest(base, seed=SEED)
    second = build_pilot_manifest(base, seed=SEED)

    assert first == second
    payload = {key: value for key, value in first.items() if key != "manifest_digest"}
    assert first["manifest_digest"] == digest(payload)


def test_pilot_manifest_freeze_binds_diagnostic_and_remaining_shared_budget():
    base = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "evals/benchmarks/manifests/comparison_trial_manifest_v10.json"
        ).read_text(encoding="utf-8")
    )
    diagnostic = {
        "candidate_sha": "a" * 40,
        "model_profiles_digest": "b" * 64,
        "evaluation_source_digest": "c" * 64,
        "source_manifest_digest": "d" * 64,
        "task_inventory_digest": "e" * 64,
        "general_model_requests": 3,
        "general_model_tokens": 800_000,
        "qwen_requests": 3,
        "qwen_tokens": 800_000,
        "qwen_cost_cny": "2.000000",
        "openrouter_requests": 0,
        "tool_calls": 100,
        "diagnostic_elapsed_seconds": 120.2,
        "role_usage": {
            "agent": {"requests": 2, "tokens": 500_000},
            "user_simulator": {"requests": 1, "tokens": 300_000},
            "judge": {"requests": 0, "tokens": 0},
        },
        "report_sha256": "f" * 64,
    }

    pilot = build_pilot_manifest(base, diagnostic_gate=diagnostic)

    assert pilot["diagnostic_evidence"]["report_sha256"] == "f" * 64
    assert pilot["diagnostic_evidence"]["candidate_sha"] == "a" * 40
    assert pilot["diagnostic_evidence"]["remaining_batch_limits"]["qwen_requests"] == 497
    assert pilot["diagnostic_evidence"]["remaining_batch_limits"]["qwen_cost_cny"] == "11.500000"
    payload = {key: value for key, value in pilot.items() if key != "manifest_digest"}
    assert pilot["manifest_digest"] == digest(payload)


def test_pilot_manifest_rejects_incomplete_or_exposed_candidate_pools():
    base = _base_manifest()
    base["full_trials"] = [
        row for row in base["full_trials"]
        if not (row["benchmark"] == "tau-bench" and row["domain"] == "telecom")
    ]
    base["manifest_digest"] = digest(
        {key: value for key, value in base.items() if key != "manifest_digest"}
    )

    try:
        build_pilot_manifest(base, seed=SEED)
    except ValueError as exc:
        assert "candidate pool" in str(exc)
    else:
        raise AssertionError("incomplete test split must not be silently resampled")


def test_frozen_pilot_loads_only_the_digest_bound_selected_trials(tmp_path):
    root = Path(__file__).resolve().parents[2]
    base_path = root / "evals/benchmarks/manifests/comparison_trial_manifest_v10.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    pilot = build_pilot_manifest(base)
    pilot_path = tmp_path / "pilot.json"
    pilot_path.write_text(json.dumps(pilot), encoding="utf-8")

    plan = load_public_pilot_plan(
        root,
        pilot_manifest_path=pilot_path,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert plan.mode == "pilot"
    assert len(plan.trials) == 24
    assert plan.route_slot_count() == 68
    assert sum(
        1 for item in plan.route_schedule if item[1] == "plan_jev_semantic_mcp"
    ) == 10
    assert all(item["exposure_group"] == "not_exposed" for item in plan.trials)


def test_loaded_pilot_uses_only_the_remaining_shared_budget(tmp_path):
    from evals.benchmarks.pilot_gate import apply_remaining_pilot_budget

    root = Path(__file__).resolve().parents[2]
    base_path = root / "evals/benchmarks/manifests/comparison_trial_manifest_v10.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    pilot = build_pilot_manifest(base)
    pilot_path = tmp_path / "pilot.json"
    pilot_path.write_text(json.dumps(pilot), encoding="utf-8")
    try:
        plan = load_public_pilot_plan(
            root,
            pilot_manifest_path=pilot_path,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        )
    finally:
        pilot_path.unlink()

    gated = apply_remaining_pilot_budget(
        plan,
        {
            "general_model_requests": 3,
            "general_model_tokens": 800_000,
            "qwen_requests": 3,
            "qwen_tokens": 800_000,
            "qwen_cost_cny": "2.000000",
            "tool_calls": 100,
            "diagnostic_elapsed_seconds": 120.2,
            "role_usage": {
                "agent": {"requests": 2, "tokens": 500_000},
                "user_simulator": {"requests": 1, "tokens": 300_000},
                "judge": {"requests": 0, "tokens": 0},
            },
        },
    )

    assert gated.batch_limits["qwen_requests"] == 497
    assert gated.batch_limits["qwen_tokens"] == 4_200_000
    assert gated.batch_limits["qwen_cost_cny"] == "11.500000"
    assert gated.cohort_digest != plan.cohort_digest

import json
from pathlib import Path

import pytest

from evals.benchmarks.build_task_inventory import (
    _stable_subset,
    _validate_agent_boundary,
)
from evals.benchmarks.build_trial_manifest import build_trial_manifest

INVENTORY_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "manifests" / "task_inventory.json"
)


def test_resource_subset_is_stable_across_source_order_and_bounded():
    task_ids = [f"task-{index:03d}" for index in range(50)]

    first = _stable_subset(task_ids, limit=12, seed="fixture-v1")
    second = _stable_subset(list(reversed(task_ids)), limit=12, seed="fixture-v1")

    assert first == second
    assert len(first) == 12
    assert set(first) <= set(task_ids)


def test_frozen_inventory_omits_model_prompts_and_scorer_gold():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))

    _validate_agent_boundary(inventory)

    assert inventory["agent_input_boundary"]["excluded_from_inventory"]


def test_inventory_has_heuristic_facets_for_every_official_task():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    required = {
        "workflow_family",
        "workflow_depth_estimate",
        "available_tool_names",
        "state_dependent_candidate",
        "structured_branch_candidate",
        "runtime_semantic_branch_candidate",
        "dialogue_clarification_candidate",
        "exception_recovery_candidate",
        "review_status",
    }
    modes = inventory["enterpriseops_gym"]["official_modes"]
    for mode in modes:
        assert mode["execution_status"] == "not_run"
        assert len(mode["task_annotations"]) == mode["row_count"]
        assert {row["task_id"] for row in mode["task_annotations"]} == set(mode["task_ids"])
        assert all(required <= row.keys() for row in mode["task_annotations"])
        assert all(
            row["review_status"] == "heuristic_unreviewed" for row in mode["task_annotations"]
        )
    for domain in inventory["tau2_bench"]["official_domains"]:
        assert domain["execution_status"] == "not_run"
        assert len(domain["task_annotations"]) == domain["task_count"]
        assert all(required <= row.keys() for row in domain["task_annotations"])
    _validate_agent_boundary(inventory)


def test_tau_smoke_uses_fixed_train_cases_and_preserves_frozen_test_cases():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    smoke = inventory["resource_subsets"]["tau2_bench_smoke"]["domains"]
    official = {item["domain"]: item for item in inventory["tau2_bench"]["official_domains"]}

    assert set(smoke) == {"tau2-bench/retail", "tau2-bench/telecom"}
    for key, selection in smoke.items():
        domain = key.split("/", 1)[1]
        ids = selection["task_ids"]
        assert selection["official_split"] == "train"
        assert ids == official[domain]["splits"]["train"][:2]
        assert len(ids) == 2
        assert not set(ids) & set(official[domain]["splits"]["test"])
    assert inventory["resource_subsets"]["tau2_bench_supplement"]["domains"] == {
        f"tau2-bench/{domain}": {
            "official_split": "test",
            "task_ids": official[domain]["splits"]["test"],
        }
        for domain in ("retail", "telecom")
    }


def test_inventory_builder_rejects_gold_fields_recursively():
    with pytest.raises(ValueError, match="gold/scorer field"):
        _validate_agent_boundary({"safe": {"task_ids": ["task-a"]}, "verifiers": ["SQL"]})


def test_fixed_comparison_manifest_covers_public_sets_without_assigning_unreviewed_exposure():
    root = Path(__file__).resolve().parents[2]
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (root / "evals/benchmarks/manifests/evaluation_model_profiles_v2.json").read_text(
                encoding="utf-8"
            )
        )
    )
    profile_document = json.loads(
        (root / "evals/benchmarks/manifests/evaluation_model_profiles_v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert profiles.to_dict() == profile_document
    assert all("api_key" not in profile for profile in profile_document["roles"].values())
    manifest = build_trial_manifest(root, model_profiles=profiles)

    full = manifest["full_trials"]
    smoke = manifest["smoke_trials"]
    assert len(full) == 286
    assert len(smoke) == 24
    assert {trial["benchmark"] for trial in full} == {
        "enterpriseops-gym",
        "tau-bench",
    }
    assert all(trial["status"] == "not_run" for trial in full + smoke)
    assert all(trial["exposure_group"] == "unassigned" for trial in full + smoke)
    assert all(
        trial["resource_profile_digest"] == manifest["resource_profile_digest"]
        for trial in full + smoke
    )
    assert manifest["exposure_assignment"]["status"] == "unreviewed_unassigned"
    assert manifest["limits"]["agent_model_requests"] == 200
    assert manifest["limits"]["agent_machine_seconds"] == 1800
    from decagent.evaluation.business.final_response import DEFAULT_RESPONSE_POLICY

    assert manifest["response_policy_digest"] == DEFAULT_RESPONSE_POLICY.fingerprint
    assert manifest["response_policy"]["summary_model"] == "qwen3.8-flash"
    assert manifest["schema_version"] == "business-comparison-trial-manifest-5"
    assert manifest["model_profile_status"] == "frozen"
    assert all(
        trial["model_profile_digest"] == profiles.primary.fingerprint
        and trial["jev_profile_digest"] == profiles.jev.fingerprint
        for trial in full + smoke
    )
    assert all(
        trial["response_policy_digest"] == manifest["response_policy_digest"]
        for trial in full + smoke
    )
    assert len(manifest["manifest_digest"]) == 64
    assert (
        json.loads(
            (root / "evals/benchmarks/manifests/comparison_trial_manifest_v5.json").read_text(
                encoding="utf-8"
            )
        )
        == manifest
    )


def test_public_manifest_freezes_first_four_domain_smoke_cases_and_route_order():
    root = Path(__file__).resolve().parents[2]
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles_path = root / "evals/benchmarks/manifests/evaluation_model_profiles_v2.json"
    profile_document = json.loads(profiles_path.read_text(encoding="utf-8"))
    profiles = EvaluationModelProfiles.from_dict(profile_document)
    manifest = build_trial_manifest(root, model_profiles=profiles)
    wave = manifest["initial_smoke_wave"]

    assert [(item["benchmark"], item["domain"], item["case_id"]) for item in wave] == [
        (
            "enterpriseops-gym",
            "csm",
            "task_20260106_161859_265_00364ece_0e836ce8",
        ),
        (
            "enterpriseops-gym",
            "itsm",
            "task_20260115_201951_323_391f1eff_659b9dae",
        ),
        ("tau-bench", "retail", "0"),
        (
            "tau-bench",
            "telecom",
            "[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|data_mode_off|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_off[PERSONA:Hard]",
        ),
    ]
    assert manifest["execution_policy"]["initial_wave_routes"] == [
        "react_skill_mcp",
        "plan_mcp",
    ]

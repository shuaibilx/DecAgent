import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.manifest_hashing import (
    source_manifest_sha256,
    task_inventory_sha256,
)
from evals.benchmarks.build_task_inventory import (
    _stable_subset,
    _validate_agent_boundary,
)
from evals.benchmarks.build_trial_manifest import build_trial_manifest

INVENTORY_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "manifests" / "task_inventory.json"
)
LOWER_BATCH_LIMITS = {
    "qwen_requests": 100,
    "qwen_tokens": 1_000_000,
    "agent_qwen_requests": 60,
    "agent_qwen_tokens": 600_000,
    "user_simulator_qwen_requests": 32,
    "user_simulator_qwen_tokens": 320_000,
    "judge_qwen_requests": 8,
    "judge_qwen_tokens": 80_000,
    "tool_calls": 400,
    "wall_seconds": 7_200,
}


def test_frozen_manifest_digests_are_independent_of_git_line_endings():
    source_lf = b'{"version": 1}\n'
    inventory_lf = b'{"task": "fixed"}\n'
    source_crlf = source_lf.replace(b"\n", b"\r\n")
    inventory_crlf = inventory_lf.replace(b"\n", b"\r\n")

    assert source_manifest_sha256(source_lf) == source_manifest_sha256(source_crlf)
    assert task_inventory_sha256(inventory_lf) == task_inventory_sha256(inventory_crlf)


def test_frozen_inventory_digest_matches_manifest_on_current_and_linux_eol():
    source = json.loads(
        (INVENTORY_PATH.parent / "source_manifest.json").read_text(encoding="utf-8")
    )
    expected = source["task_inventory"]["sha256"]
    current = INVENTORY_PATH.read_bytes()
    linux = current.replace(b"\r\n", b"\n")

    assert task_inventory_sha256(current) == expected
    assert task_inventory_sha256(linux) == expected


def test_trial_manifest_builder_accepts_linux_inventory_line_endings(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v3.json"
    profiles_document = json.loads(profile_path.read_text(encoding="utf-8"))
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(profiles_document)
    expected = json.loads(
        (INVENTORY_PATH.parent / "source_manifest.json").read_text(encoding="utf-8")
    )["task_inventory"]["sha256"]
    inventory_path = INVENTORY_PATH.resolve()
    read_bytes = Path.read_bytes

    def read_linux_checkout(path):
        content = read_bytes(path)
        if path.resolve() == inventory_path:
            return content.replace(b"\r\n", b"\n")
        return content

    monkeypatch.setattr(Path, "read_bytes", read_linux_checkout)
    built = build_trial_manifest(root, model_profiles=profiles, manifest_version=7)

    assert built["task_inventory_sha256"] == expected


def test_manifest_v8_freezes_cny_pricing_without_changing_trial_scope():
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v3.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )
    qwen_roles = {
        role: replace(
            getattr(profiles, role),
            price_version="qwen3.8-flash-standard-cny-20261021-v1",
            price_source="https://platform.qianwenai.com/pricing/api",
            input_price_per_million=Decimal("0.8"),
            output_price_per_million=Decimal("2.7"),
            pricing_status="known",
            price_currency="CNY",
            cached_input_price_per_million=Decimal("0.1"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    cny_profiles = replace(profiles, **qwen_roles)

    built = build_trial_manifest(root, model_profiles=cny_profiles, manifest_version=8)

    assert built["schema_version"] == "business-comparison-trial-manifest-8"
    assert built["model_profiles"] == cny_profiles.to_dict()
    assert built["counts"] == {
        "full_trials": 286,
        "smoke_trials": 24,
        "initial_smoke_wave": 4,
    }
    assert built["batch_limits"]["qwen_requests"] == 500
    assert all(
        trial["model_profile_digest"] == cny_profiles.primary.fingerprint
        for trial in built["full_trials"]
    )
    assert built == json.loads(
        (INVENTORY_PATH.parent / "comparison_trial_manifest_v8.json").read_text(
            encoding="utf-8"
        )
    )


def test_manifest_v9_uses_reviewed_case_level_exposure_without_family_expansion():
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )

    built = build_trial_manifest(root, model_profiles=profiles, manifest_version=9)

    exposed_enterpriseops_ids = {
        trial["case_id"]
        for trial in built["full_trials"]
        if trial["benchmark"] == "enterpriseops-gym"
        and trial["exposure_group"] == "development_exposed"
    }
    assert exposed_enterpriseops_ids == {
        "task_20260106_161859_265_00364ece_0e836ce8",
        "task_20260115_201951_323_391f1eff_659b9dae",
    }
    assert all(
        trial["exposure_group"] == "not_exposed"
        for trial in built["full_trials"]
        if trial["benchmark"] == "tau-bench"
    )
    assert all(
        trial["exposure_group"] == "development_exposed"
        for trial in built["smoke_trials"]
        if trial["benchmark"] == "tau-bench"
    )
    enterprise_smoke = [
        trial for trial in built["smoke_trials"]
        if trial["benchmark"] == "enterpriseops-gym"
    ]
    assert sum(trial["exposure_group"] == "development_exposed" for trial in enterprise_smoke) == 2
    assert all(
        trial["exposure_group"] in {"development_exposed", "not_exposed"}
        for trial in built["full_trials"] + built["smoke_trials"]
    )
    assert built["exposure_assignment"]["status"] == "reviewed_case_level"
    assert built["exposure_assignment"]["review_evidence"] == (
        "evals/benchmarks/evidence/exposure-review-20261022.md"
    )
    assert built["counts"] == {
        "full_trials": 286,
        "smoke_trials": 24,
        "initial_smoke_wave": 4,
    }
    assert built == json.loads(
        (INVENTORY_PATH.parent / "comparison_trial_manifest_v9.json").read_text(
            encoding="utf-8"
        )
    )


def test_manifest_v10_freezes_longer_primary_timeout_without_changing_scope():
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )
    profiles = replace(
        profiles,
        primary=replace(profiles.primary, timeout_seconds=120),
    )

    built = build_trial_manifest(root, model_profiles=profiles, manifest_version=10)

    assert built["schema_version"] == "business-comparison-trial-manifest-10"
    assert built["model_profiles"]["roles"]["primary"]["timeout_seconds"] == 120
    assert built["model_profiles"]["roles"]["user_simulator"]["timeout_seconds"] == 60
    assert built["model_profiles"]["roles"]["judge"]["timeout_seconds"] == 60
    assert built["exposure_assignment"]["status"] == "reviewed_case_level"
    assert built["counts"] == {
        "full_trials": 286,
        "smoke_trials": 24,
        "initial_smoke_wave": 4,
    }
    assert built == json.loads(
        (INVENTORY_PATH.parent / "comparison_trial_manifest_v10.json").read_text(
            encoding="utf-8"
        )
    )


def test_manifest_v11_freezes_180_second_primary_timeout_without_rewriting_v10():
    root = Path(__file__).resolve().parents[2]
    manifest_dir = INVENTORY_PATH.parent
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles_v5 = EvaluationModelProfiles.from_dict(
        json.loads((manifest_dir / "evaluation_model_profiles_v5.json").read_text(encoding="utf-8"))
    )
    profiles_v6 = EvaluationModelProfiles.from_dict(
        json.loads((manifest_dir / "evaluation_model_profiles_v6.json").read_text(encoding="utf-8"))
    )

    assert profiles_v5.primary.timeout_seconds == 120
    assert profiles_v6.primary.timeout_seconds == 180
    assert profiles_v5.user_simulator.fingerprint == profiles_v6.user_simulator.fingerprint
    assert profiles_v5.judge.fingerprint == profiles_v6.judge.fingerprint

    built = build_trial_manifest(root, model_profiles=profiles_v6, manifest_version=11)

    assert built["schema_version"] == "business-comparison-trial-manifest-11"
    assert built["model_profiles"]["roles"]["primary"]["timeout_seconds"] == 180
    assert built["counts"] == {
        "full_trials": 286,
        "smoke_trials": 24,
        "initial_smoke_wave": 4,
    }
    assert built == json.loads(
        (manifest_dir / "comparison_trial_manifest_v11.json").read_text(encoding="utf-8")
    )


def test_manifest_v12_freezes_a_separate_openrouter_provider_group():
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v6.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )

    def openrouter_role(role_profile):
        return replace(
            role_profile,
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            endpoint="https://openrouter.ai/api/v1",
            price_version="openrouter-deepseek-v4.1-flash-fireworks-usd-20261024-v1",
            price_source=(
                "https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"
            ),
            input_price_per_million=Decimal("0.45"),
            output_price_per_million=Decimal("1.8"),
            price_currency="USD",
            cached_input_price_per_million=Decimal("0.009"),
            provider_routing="openrouter_fireworks",
        )

    profiles = replace(
        profiles,
        primary=openrouter_role(profiles.primary),
        user_simulator=openrouter_role(profiles.user_simulator),
        judge=openrouter_role(profiles.judge),
    )

    built = build_trial_manifest(root, model_profiles=profiles, manifest_version=12)

    assert built["schema_version"] == "business-comparison-trial-manifest-12"
    assert built["manifest_id"] == "decagent-public-workflow-comparison-v12"
    assert built["model_profiles"]["roles"]["primary"]["provider"] == "openrouter"
    assert built["model_profiles"]["roles"]["primary"]["provider_routing"] == (
        "openrouter_fireworks"
    )
    assert built["counts"]["smoke_trials"] == 24


def test_manifest_builder_accepts_only_lower_frozen_batch_limits(tmp_path):
    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )
    built = build_trial_manifest(
        root,
        model_profiles=profiles,
        manifest_version=10,
        batch_limits=LOWER_BATCH_LIMITS,
    )
    assert built["batch_limits"] == LOWER_BATCH_LIMITS
    assert built["manifest_digest"] == digest(
        {key: value for key, value in built.items() if key != "manifest_digest"}
    )

    limits_path = tmp_path / "batch-limits.json"
    limits_path.write_text(json.dumps(LOWER_BATCH_LIMITS), encoding="utf-8")
    output_path = tmp_path / "custom-manifest.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.benchmarks.build_trial_manifest",
            "--output",
            str(output_path),
            "--model-profiles",
            str(profile_path),
            "--manifest-version",
            "10",
            "--batch-limits",
            str(limits_path),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(output_path.read_text(encoding="utf-8"))["batch_limits"] == (
        LOWER_BATCH_LIMITS
    )
    for version in (6, 7, 8):
        with pytest.raises(ValueError, match="version 9 or newer"):
            build_trial_manifest(
                root,
                model_profiles=profiles,
                manifest_version=version,
                batch_limits=LOWER_BATCH_LIMITS,
            )


def test_public_batch_limit_validation_accepts_only_bounded_openrouter_allowance():
    from decagent.evaluation.business.public_limits import (
        OPENROUTER_DIAGNOSTIC_LIMITS,
        validate_public_batch_limits,
    )

    limits = {
        **LOWER_BATCH_LIMITS,
        "openrouter_requests": 6,
        "openrouter_tokens": 600_000,
        "openrouter_cost_usd": "5",
    }
    assert validate_public_batch_limits(limits) == limits
    for name, value in (
        ("openrouter_requests", 7),
        ("openrouter_tokens", 600_001),
        ("openrouter_cost_usd", "5.0001"),
    ):
        with pytest.raises(ValueError):
            validate_public_batch_limits({**limits, name: value})

    or_only = {
        **OPENROUTER_DIAGNOSTIC_LIMITS,
        "tool_calls": 399,
        "wall_seconds": 2_519,
    }
    assert validate_public_batch_limits(or_only) == or_only
    with pytest.raises(ValueError, match="token limits"):
        validate_public_batch_limits({**or_only, "openrouter_tokens": 565_945})
    with pytest.raises(ValueError, match="sum"):
        validate_public_batch_limits({**or_only, "agent_openrouter_requests": 4})


def test_zero_openrouter_role_caps_do_not_stop_an_empty_batch():
    from decagent.evaluation.business.public_limits import (
        OPENROUTER_DIAGNOSTIC_LIMITS,
        public_batch_limit_stop_reason,
    )

    snapshot = {
        "limits": OPENROUTER_DIAGNOSTIC_LIMITS,
        "openrouter_requests": 0,
        "openrouter_tokens": 0,
        "openrouter_cost_usd": "0",
        "openrouter_cost_unknown_attempts": 0,
        "tool_calls": 0,
        "by_role": {
            "primary": {"requests": 0, "tokens": 0},
            "user_simulator": {"requests": 0, "tokens": 0},
            "judge": {"requests": 0, "tokens": 0},
        },
        "by_provider_role": {
            "openrouter": {
                "primary": {"requests": 0, "tokens": 0},
                "user_simulator": {"requests": 0, "tokens": 0},
                "judge": {"requests": 0, "tokens": 0},
            }
        },
    }

    assert public_batch_limit_stop_reason(snapshot) is None
    snapshot["by_provider_role"]["openrouter"]["user_simulator"]["requests"] = 1
    assert public_batch_limit_stop_reason(snapshot) == "user_simulator_openrouter_requests"


def test_manifest_loader_accepts_lower_limits_and_rejects_limits_above_ceiling(tmp_path):
    from decagent.evaluation.business.public_trials import (
        canonical_public_trial_ledger_path,
        load_public_run_plan,
    )

    root = Path(__file__).resolve().parents[2]
    profile_path = INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json"
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(profile_path.read_text(encoding="utf-8"))
    )
    manifest = build_trial_manifest(
        root,
        model_profiles=profiles,
        manifest_version=10,
        batch_limits=LOWER_BATCH_LIMITS,
    )
    manifest_path = tmp_path / "lower-limits.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = load_public_run_plan(
        root,
        mode="smoke",
        manifest_path=manifest_path,
        profiles_path=profile_path,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    assert plan.batch_limits == LOWER_BATCH_LIMITS

    default_manifest_path = tmp_path / "default-limits.json"
    default_manifest_path.write_text(
        json.dumps(build_trial_manifest(root, model_profiles=profiles, manifest_version=10)),
        encoding="utf-8",
    )
    default_plan = load_public_run_plan(
        root,
        mode="smoke",
        manifest_path=default_manifest_path,
        profiles_path=profile_path,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    assert plan.cohort_digest != default_plan.cohort_digest
    candidate_sha = "a" * 40
    assert canonical_public_trial_ledger_path(
        root, cohort_digest=plan.cohort_digest, candidate_sha=candidate_sha
    ) != canonical_public_trial_ledger_path(
        root, cohort_digest=default_plan.cohort_digest, candidate_sha=candidate_sha
    )

    invalid_manifests = (
        ("above-ceiling", {**LOWER_BATCH_LIMITS, "tool_calls": 1_601}, "exceed approved ceilings"),
        (
            "inconsistent-role-sum",
            {**LOWER_BATCH_LIMITS, "agent_qwen_requests": 61},
            "role limits must sum",
        ),
        (
            "boolean-is-not-an-integer",
            {**LOWER_BATCH_LIMITS, "tool_calls": True},
            "positive integers",
        ),
    )
    for name, limits, expected_error in invalid_manifests:
        invalid = json.loads(json.dumps(manifest))
        invalid["batch_limits"] = limits
        invalid.pop("manifest_digest")
        invalid["manifest_digest"] = digest(invalid)
        invalid_path = tmp_path / f"{name}.json"
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError, match=expected_error):
            load_public_run_plan(
                root,
                mode="smoke",
                manifest_path=invalid_path,
                profiles_path=profile_path,
                check_images=False,
                check_runtime=False,
                check_source_files=False,
            )


def test_public_trial_loader_accepts_reviewed_v9_exposure_manifest(tmp_path):
    from decagent.decisions.contracts import digest
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (INVENTORY_PATH.parent / "comparison_trial_manifest_v8.json").read_text(
            encoding="utf-8"
        )
    )
    manifest["schema_version"] = "business-comparison-trial-manifest-9"
    manifest["manifest_id"] = "decagent-public-workflow-comparison-v9"
    manifest["exposure_assignment"] = {
        "status": "reviewed_case_level",
        "default_group": "not_exposed",
        "planned_groups": ["development_exposed", "not_exposed"],
        "reviewed_at": "2026-10-22",
        "review_evidence": "evals/benchmarks/evidence/exposure-review-20261022.md",
        "development_exposed_enterpriseops_case_ids": [
            "task_20260106_161859_265_00364ece_0e836ce8",
            "task_20260115_201951_323_391f1eff_659b9dae",
        ],
        "assignment_note": "Exact-case assignment; no workflow-family expansion.",
    }
    exposed_ids = {
        "task_20260106_161859_265_00364ece_0e836ce8",
        "task_20260115_201951_323_391f1eff_659b9dae",
    }
    for trial in manifest["full_trials"] + manifest["smoke_trials"]:
        if trial["benchmark"] == "enterpriseops-gym":
            trial["exposure_group"] = (
                "development_exposed" if trial["case_id"] in exposed_ids else "not_exposed"
            )
        else:
            trial["exposure_group"] = (
                "development_exposed" if trial["split"] == "train" else "not_exposed"
            )
    manifest.pop("manifest_digest")
    manifest["manifest_digest"] = digest(manifest)
    manifest_path = tmp_path / "comparison_trial_manifest_v9.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    plan = load_public_run_plan(
        root,
        mode="smoke",
        manifest_path=manifest_path,
        profiles_path=INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert plan.manifest_digest == manifest["manifest_digest"]
    assert all(trial["exposure_group"] != "unassigned" for trial in plan.trials)

    invalid = json.loads(json.dumps(manifest))
    exposed = next(
        trial for trial in invalid["full_trials"]
        if trial["benchmark"] == "enterpriseops-gym"
        and trial["case_id"] == "task_20260106_161859_265_00364ece_0e836ce8"
    )
    exposed["exposure_group"] = "not_exposed"
    invalid.pop("manifest_digest")
    invalid["manifest_digest"] = digest(invalid)
    invalid_path = tmp_path / "misclassified-v9.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="exposure assignment differs"):
        load_public_run_plan(
            root,
            mode="full",
            manifest_path=invalid_path,
            profiles_path=INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json",
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        )


def test_historical_v8_manifest_cannot_be_loaded_for_live_dispatch():
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]

    with pytest.raises(ValueError, match="historical v8 cohort cannot be dispatched"):
        load_public_run_plan(
            root,
            mode="smoke",
            manifest_path=INVENTORY_PATH.parent / "comparison_trial_manifest_v8.json",
            check_images=True,
            check_runtime=True,
            check_source_files=True,
        )


def test_v9_public_trial_plan_remains_loadable_as_historical_cohort():
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json").read_text(
                encoding="utf-8"
            )
        )
    )
    expected = build_trial_manifest(root, model_profiles=profiles, manifest_version=9)

    plan = load_public_run_plan(
        root,
        mode="smoke",
        manifest_path=INVENTORY_PATH.parent / "comparison_trial_manifest_v9.json",
        profiles_path=INVENTORY_PATH.parent / "evaluation_model_profiles_v4.json",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert plan.manifest_digest == expected["manifest_digest"]
    assert all(trial["exposure_group"] != "unassigned" for trial in plan.trials)
    from decagent.evaluation.business.public_trials import build_dry_run_report

    dry_run = build_dry_run_report(plan, phase="first-wave")
    serialized = json.dumps(dry_run)
    assert "exposure_group" not in serialized
    assert "development_exposed" not in serialized
    assert "target_state" not in serialized
    assert "reference_actions" not in serialized


def test_default_public_trial_plan_uses_the_180_second_timeout_recovery_cohort():
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    plan = load_public_run_plan(
        root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert plan.profiles.primary.timeout_seconds == 180
    assert plan.profiles.user_simulator.timeout_seconds == 60
    assert plan.profiles.judge.timeout_seconds == 60
    assert len(plan.initial_smoke_wave) == 4


def test_public_trial_loader_accepts_frozen_v11_profile_without_changing_v10():
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    manifest_dir = root / "evals/benchmarks/manifests"
    plan = load_public_run_plan(
        root,
        mode="smoke",
        manifest_path=manifest_dir / "comparison_trial_manifest_v11.json",
        profiles_path=manifest_dir / "evaluation_model_profiles_v6.json",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    assert plan.profiles.primary.timeout_seconds == 180
    assert plan.profiles.user_simulator.timeout_seconds == 60
    assert plan.profiles.judge.timeout_seconds == 60
    assert len(plan.trials) == 24


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

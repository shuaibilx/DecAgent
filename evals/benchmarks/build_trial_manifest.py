"""Build the content-addressed, no-label public comparison trial manifest."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import (
    DEFAULT_RESOURCE_PROFILE,
    UNBOUND_MODEL_PROFILE_DIGEST,
)
from decagent.evaluation.business.final_response import DEFAULT_RESPONSE_POLICY
from decagent.evaluation.business.manifest_hashing import (
    source_manifest_sha256,
    task_inventory_sha256,
)
from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
from decagent.evaluation.business.public_limits import (
    FIRST_WAVE_BATCH_LIMITS,
    validate_public_batch_limits,
)
from decagent.evaluation.business.public_trials import (
    DEVELOPMENT_EXPOSED_ENTERPRISEOPS_CASES,
    EXPOSURE_REVIEW_EVIDENCE,
    reviewed_exposure_group,
)

INITIAL_SMOKE_CASES = (
    (
        "enterpriseops-gym",
        "csm",
        "oracle",
        "task_20260106_161859_265_00364ece_0e836ce8",
    ),
    (
        "enterpriseops-gym",
        "itsm",
        "oracle",
        "task_20260115_201951_323_391f1eff_659b9dae",
    ),
    ("tau-bench", "retail", "train", "0"),
    (
        "tau-bench",
        "telecom",
        "train",
        "[mms_issue]airplane_mode_on|bad_network_preference|bad_wifi_calling|break_apn_mms_setting|break_app_both_permissions|data_mode_off|data_usage_exceeded|unseat_sim_card|user_abroad_roaming_disabled_off[PERSONA:Hard]",
    ),
)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _trial(benchmark, version, domain, split, case_id, resource_digest, model_profiles):
    identity = {
        "benchmark": benchmark,
        "version": version,
        "domain": domain,
        "split": split,
        "case_id": case_id,
    }
    return {
        "trial_id": digest(identity),
        **identity,
        "status": "not_run",
        "exposure_group": "unassigned",
        "resource_profile_digest": resource_digest,
        "response_policy_digest": DEFAULT_RESPONSE_POLICY.fingerprint,
        **model_profiles,
    }


def _assign_reviewed_case_exposure(trials):
    for trial in trials:
        trial["exposure_group"] = reviewed_exposure_group(trial)


def build_trial_manifest(
    root: Path, *, model_profiles: EvaluationModelProfiles | None = None,
    manifest_version: int = 5, batch_limits: dict | None = None,
) -> dict:
    if manifest_version not in {5, 6, 7, 8, 9, 10, 11, 12}:
        raise ValueError("Supported public trial manifest versions are 5 through 12")
    if model_profiles is not None and not isinstance(model_profiles, EvaluationModelProfiles):
        raise TypeError("model_profiles must use the frozen evaluation profile contract")
    if batch_limits is not None and manifest_version < 9:
        raise ValueError("Custom batch limits require manifest version 9 or newer")
    batch_limit_profile = (
        validate_public_batch_limits(batch_limits)
        if batch_limits is not None
        else dict(FIRST_WAVE_BATCH_LIMITS)
    )
    if manifest_version >= 9 and (
        model_profiles is None
        or model_profiles.jev is None
        or model_profiles.to_dict().get("schema_version") != "evaluation-model-profiles-v4"
    ):
        raise ValueError("Manifest v9 requires the fully frozen v4 model profiles")
    role_profiles = (
        model_profiles.digests
        if model_profiles is not None
        else {
            "model_profile_digest": UNBOUND_MODEL_PROFILE_DIGEST,
            "jev_profile_digest": UNBOUND_MODEL_PROFILE_DIGEST,
            "user_simulator_profile_digest": UNBOUND_MODEL_PROFILE_DIGEST,
            "judge_profile_digest": UNBOUND_MODEL_PROFILE_DIGEST,
        }
    )
    root = Path(root).resolve()
    source_path = root / "evals/benchmarks/manifests/source_manifest.json"
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    inventory_record = source["task_inventory"]
    inventory_path = (root / inventory_record["path"]).resolve()
    if not inventory_path.is_relative_to(root):
        raise ValueError("Frozen task inventory escapes the project")
    inventory_bytes = inventory_path.read_bytes()
    inventory_sha = task_inventory_sha256(inventory_bytes)
    if inventory_sha != inventory_record["sha256"]:
        raise ValueError("Frozen task inventory checksum differs from source manifest")
    inventory = json.loads(inventory_bytes)

    profile = dict(DEFAULT_RESOURCE_PROFILE)
    profile_digest = digest(profile)
    full_trials = []
    smoke_trials = []
    enterprise = source["enterpriseops_gym"]
    enterprise_version = enterprise["task_dataset"]["revision"]
    oracle_modes = [
        row
        for row in inventory["enterpriseops_gym"]["official_modes"]
        if row["config"] == "oracle" and row["domain"] in {"csm", "itsm"}
    ]
    if {row["domain"] for row in oracle_modes} != {"csm", "itsm"}:
        raise ValueError("Frozen EnterpriseOps oracle modes are incomplete")
    for row in sorted(oracle_modes, key=lambda item: item["domain"]):
        if row["execution_status"] != "not_run" or row["row_count"] != len(row["task_ids"]):
            raise ValueError("EnterpriseOps oracle inventory is not a frozen unrun task set")
        full_trials.extend(
            _trial(
                "enterpriseops-gym",
                enterprise_version,
                row["domain"],
                "oracle",
                case_id,
                profile_digest,
                role_profiles,
            )
            for case_id in row["task_ids"]
        )

    tau = source["tau2_bench"]
    tau_version = tau["source_repository"]["commit"]
    tau_smoke = inventory["resource_subsets"]["tau2_bench_smoke"]["domains"]
    tau_full = inventory["resource_subsets"]["tau2_bench_supplement"]["domains"]
    for key in sorted(tau_full):
        domain = key.split("/", 1)[1]
        full_ids = tau_full[key]["task_ids"]
        if tau_full[key]["official_split"] != "test":
            raise ValueError("Supplementary tau tasks must use the frozen official test split")
        full_trials.extend(
            _trial(
                "tau-bench", tau_version, domain, "test", case_id, profile_digest,
                role_profiles,
            )
            for case_id in full_ids
        )
        smoke = tau_smoke.get(key)
        if (
            smoke is None
            or smoke["official_split"] != "train"
            or set(smoke["task_ids"]) & set(full_ids)
        ):
            raise ValueError("Tau smoke tasks must be isolated train cases")
        smoke_trials.extend(
            _trial(
                "tau-bench", tau_version, domain, "train", case_id, profile_digest,
                role_profiles,
            )
            for case_id in smoke["task_ids"]
        )

    enterprise_smoke = inventory["resource_subsets"]["enterpriseops_smoke"]["domains"]
    for key in sorted(enterprise_smoke):
        domain = key.split("/", 1)[1]
        smoke_ids = enterprise_smoke[key]["task_ids"]
        oracle_ids = {
            trial["case_id"]
            for trial in full_trials
            if trial["benchmark"] == "enterpriseops-gym" and trial["domain"] == domain
        }
        if len(smoke_ids) != 10 or not set(smoke_ids) <= oracle_ids:
            raise ValueError("EnterpriseOps smoke tasks must be ten frozen oracle cases per domain")
        smoke_trials.extend(
            _trial(
                "enterpriseops-gym",
                enterprise_version,
                domain,
                "oracle",
                case_id,
                profile_digest,
                role_profiles,
            )
            for case_id in smoke_ids
        )

    identities = [trial["trial_id"] for trial in full_trials + smoke_trials]
    if len(identities) != len(set(identities)):
        # A smoke case is also in the full EnterpriseOps set by design, so its
        # separate trial identity includes a named smoke phase.
        for trial in smoke_trials:
            trial["phase"] = "smoke"
            trial["trial_id"] = digest(
                {
                    "benchmark": trial["benchmark"],
                    "version": trial["version"],
                    "domain": trial["domain"],
                    "split": trial["split"],
                    "case_id": trial["case_id"],
                    "phase": trial["phase"],
                }
            )
        identities = [trial["trial_id"] for trial in full_trials + smoke_trials]
    if len(identities) != len(set(identities)):
        raise ValueError("Trial manifest contains duplicate trial identities")

    if manifest_version >= 9:
        _assign_reviewed_case_exposure(full_trials + smoke_trials)

    smoke_by_identity = {
        (trial["benchmark"], trial["domain"], trial["split"], trial["case_id"]): trial
        for trial in smoke_trials
    }
    initial_smoke_wave = []
    for benchmark, domain, split, case_id in INITIAL_SMOKE_CASES:
        trial = smoke_by_identity.get((benchmark, domain, split, case_id))
        if trial is None:
            raise ValueError("Approved first-wave case is missing from the frozen smoke set")
        initial_smoke_wave.append(
            {
                "benchmark": benchmark,
                "domain": domain,
                "split": split,
                "case_id": case_id,
                "trial_id": trial["trial_id"],
            }
        )

    payload = {
        "schema_version": f"business-comparison-trial-manifest-{manifest_version}",
        "manifest_id": f"decagent-public-workflow-comparison-v{manifest_version}",
        "status": "pre_registered_not_run",
        "source_manifest_sha256": source_manifest_sha256(source_bytes),
        "task_inventory_sha256": inventory_sha,
        "model_profile_status": (
            "unbound"
            if model_profiles is None
            else "frozen" if model_profiles.jev is not None else "partial"
        ),
        "model_profiles": model_profiles.to_dict() if model_profiles is not None else None,
        "limits": profile,
        "resource_profile_digest": profile_digest,
        "response_policy": asdict(DEFAULT_RESPONSE_POLICY),
        "response_policy_digest": DEFAULT_RESPONSE_POLICY.fingerprint,
        "execution_policy": {
            "parallel_trials": 1,
            "maximum_batch_size": 10,
            "initial_wave_routes": ["react_skill_mcp", "plan_mcp"],
            "fresh_upstream_environment_per_route": True,
            "fresh_private_journal_per_route": True,
            "cross_trial_state_or_memory": False,
            "route_timer_excludes_environment_setup_private_scoring_and_cleanup": True,
            "auxiliary_simulator_and_judge_usage_reported_separately": True,
        },
        "exposure_assignment": (
            {
                "status": "reviewed_case_level",
                "default_group": "not_exposed",
                "planned_groups": ["development_exposed", "not_exposed"],
                "reviewed_at": "2026-10-22",
                "review_evidence": EXPOSURE_REVIEW_EVIDENCE,
                "development_exposed_enterpriseops_case_ids": sorted(
                    DEVELOPMENT_EXPOSED_ENTERPRISEOPS_CASES
                ),
                "assignment_note": (
                    "Exact-case audit; all tau train smoke cases are development data; "
                    "no workflow-family heuristic expansion."
                ),
            }
            if manifest_version >= 9
            else {
                "status": "unreviewed_unassigned",
                "default_group": "unassigned",
                "planned_groups": ["development_exposed", "not_exposed"],
                "assignment_note": (
                    "Task-family mappings require independent review before exposure comparisons."
                ),
            }
        ),
        "experiments": [
            {
                "id": "workflow-loop",
                "routes": ["react_skill_mcp", "plan_mcp"],
                "pairing": "same frozen trial identity and live initial-state fingerprint",
            },
            {
                "id": "semantic-judgment",
                "routes": ["plan_primary_semantic_mcp", "plan_jev_semantic_mcp"],
                "eligibility": (
                    "reviewed trusted semantic template required; otherwise not_applicable"
                ),
            },
            {
                "id": "structured-branch",
                "routes": ["plan_mcp", "code_rule_mcp"],
                "eligibility": "only registered structured conditions; Jev is not a comparator",
            },
            {
                "id": "workflow-reuse-ablation",
                "axis": "workflow_mode",
                "values": ["cold", "hot"],
            },
            {
                "id": "memory-ablation",
                "axis": "memory_mode",
                "values": ["disabled", "verified"],
                "employee_binding_required": True,
            },
        ],
        "full_trials": full_trials,
        "smoke_trials": smoke_trials,
        "initial_smoke_wave": initial_smoke_wave,
        "counts": {
            "full_trials": len(full_trials),
            "smoke_trials": len(smoke_trials),
            "initial_smoke_wave": len(initial_smoke_wave),
        },
        "scoring": {
            "public_task_score_source": "original_upstream_verifier_or_evaluator_only",
            "intermediate_side_effects": "not_measured_unless_explicitly_instrumented",
            "plan_completion_is_task_success": False,
            "status": "not_run",
        },
    }
    if manifest_version >= 7:
        payload["batch_limits"] = batch_limit_profile
    payload["manifest_digest"] = digest(payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "manifests/comparison_trial_manifest_v5.json",
    )
    parser.add_argument(
        "--model-profiles",
        type=Path,
        help="Secret-free, fingerprinted evaluation model profiles JSON file",
    )
    parser.add_argument(
        "--batch-limits",
        type=Path,
        help="JSON stop lines no larger than the reviewed first-wave ceilings",
    )
    parser.add_argument(
        "--manifest-version", type=int, choices=(5, 6, 7, 8, 9, 10, 11, 12), default=5
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    profiles = (
        EvaluationModelProfiles.from_dict(_read_json(args.model_profiles))
        if args.model_profiles
        else None
    )
    batch_limits = _read_json(args.batch_limits) if args.batch_limits else None
    manifest = build_trial_manifest(
        root,
        model_profiles=profiles,
        manifest_version=args.manifest_version,
        batch_limits=batch_limits,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(manifest["manifest_digest"])


if __name__ == "__main__":
    main()

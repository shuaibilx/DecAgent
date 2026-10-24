"""Freeze the approved 24-task public pilot using metadata-only sampling."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from decagent.decisions.contracts import digest
from decagent.evaluation.business.public_trials import load_public_run_plan

from evals.benchmarks.live_public_runner import require_verified_candidate
from evals.benchmarks.pilot_gate import (
    evaluation_source_digest,
    remaining_pilot_batch_limits,
    validate_diagnostic_evidence,
)

PILOT_SEED = "sop-pilot-20261024-v1"
BASE_SCHEMA = "business-comparison-trial-manifest-10"
_COMMON_ROUTES = ("react_skill_mcp", "plan_mcp")
_SEMANTIC_ROUTES = (
    "plan_primary_semantic_mcp",
    "plan_jev_semantic_mcp",
)
_COHORTS = (
    ("enterpriseops_csm", "enterpriseops-gym", "csm", "oracle", 10),
    ("enterpriseops_itsm", "enterpriseops-gym", "itsm", "oracle", 10),
    ("tau_retail_test", "tau-bench", "retail", "test", 2),
    ("tau_telecom_test", "tau-bench", "telecom", "test", 2),
)
_TRIAL_FIELDS = (
    "trial_id",
    "benchmark",
    "domain",
    "split",
    "case_id",
    "version",
    "exposure_group",
    "model_profile_digest",
    "jev_profile_digest",
    "user_simulator_profile_digest",
    "judge_profile_digest",
    "resource_profile_digest",
    "response_policy_digest",
    "status",
)


def _verified_base_digest(base_manifest):
    if type(base_manifest) is not dict:
        raise ValueError("Base public manifest must be an object")
    if base_manifest.get("schema_version") != BASE_SCHEMA:
        raise ValueError("Pilot sampling requires the frozen v10 public manifest")
    recorded = base_manifest.get("manifest_digest")
    payload = {key: value for key, value in base_manifest.items() if key != "manifest_digest"}
    if type(recorded) is not str or recorded != digest(payload):
        raise ValueError("Base public manifest digest does not verify")
    return recorded


def _rank(seed, case_id):
    return hashlib.sha256(f"{seed}\0{case_id}".encode()).hexdigest()


def select_pilot_trials(full_trials, *, seed=PILOT_SEED):
    """Select only by benchmark, domain, split, exposure and task ID metadata."""
    if type(seed) is not str or not seed.strip() or len(seed) > 128:
        raise ValueError("Pilot seed must be a nonempty bounded string")
    if type(full_trials) is not list:
        raise ValueError("Full public trial inventory must be a list")
    selected = []
    seen = set()
    for cohort, benchmark, domain, split, count in _COHORTS:
        candidates = [
            item
            for item in full_trials
            if type(item) is dict
            and item.get("benchmark") == benchmark
            and item.get("domain") == domain
            and item.get("split") == split
            and item.get("exposure_group") == "not_exposed"
            and item.get("status") == "not_run"
        ]
        identities = [item.get("case_id") for item in candidates]
        trial_ids = [item.get("trial_id") for item in candidates]
        if (
            len(candidates) < count
            or any(type(value) is not str or not value for value in identities)
            or any(type(value) is not str or not value for value in trial_ids)
            or len(set(identities)) != len(identities)
            or len(set(trial_ids)) != len(trial_ids)
        ):
            raise ValueError(f"Public pilot candidate pool is incomplete for {cohort}")
        ordered = sorted(
            candidates,
            key=lambda item: (_rank(seed, item["case_id"]), item["case_id"]),
        )
        for item in ordered[:count]:
            trial_id = item["trial_id"]
            if trial_id in seen:
                raise ValueError("Public pilot candidate is duplicated across sampling strata")
            seen.add(trial_id)
            if not set(_TRIAL_FIELDS) <= set(item):
                raise ValueError(f"Public pilot record is incomplete for {cohort}")
            selected.append({key: item[key] for key in _TRIAL_FIELDS})
    return selected


def build_pilot_manifest(base_manifest, *, seed=PILOT_SEED, diagnostic_gate=None):
    """Create a digest-protected, prompt-free route freeze for 24 public tasks."""
    base_digest = _verified_base_digest(base_manifest)
    selected = select_pilot_trials(base_manifest.get("full_trials"), seed=seed)
    route_schedule = []
    for trial in selected:
        routes = list(_COMMON_ROUTES)
        if trial["benchmark"] == "enterpriseops-gym" and trial["domain"] == "csm":
            routes.extend(_SEMANTIC_ROUTES)
        route_schedule.append({"trial_id": trial["trial_id"], "routes": routes})
    counts = {
        "enterpriseops_csm": 10,
        "enterpriseops_itsm": 10,
        "tau_retail_test": 2,
        "tau_telecom_test": 2,
        "tasks": len(selected),
        "route_slots": sum(len(item["routes"]) for item in route_schedule),
    }
    if counts != {
        "enterpriseops_csm": 10,
        "enterpriseops_itsm": 10,
        "tau_retail_test": 2,
        "tau_telecom_test": 2,
        "tasks": 24,
        "route_slots": 68,
    }:
        raise ValueError(
            "Public pilot denominator differs from the approved 24-task/68-route scope"
        )
    diagnostic_evidence = None
    if diagnostic_gate is not None:
        required_evidence = (
            "candidate_sha",
            "model_profiles_digest",
            "evaluation_source_digest",
            "source_manifest_digest",
            "task_inventory_digest",
            "general_model_requests",
            "general_model_tokens",
            "qwen_requests",
            "qwen_tokens",
            "qwen_cost_cny",
            "openrouter_requests",
            "tool_calls",
            "diagnostic_elapsed_seconds",
            "role_usage",
            "report_sha256",
        )
        if type(diagnostic_gate) is not dict or any(
            key not in diagnostic_gate for key in required_evidence
        ):
            raise ValueError("A validated bounded diagnostic is required to freeze the pilot")
        diagnostic_evidence = {
            key: diagnostic_gate[key] for key in required_evidence
        }
        diagnostic_evidence["remaining_batch_limits"] = remaining_pilot_batch_limits(
            base_manifest.get("batch_limits"), diagnostic_gate
        )
    payload = {
        "schema_version": "sop-agent-public-pilot-manifest-v1",
        "manifest_id": "sop-agent-public-pilot-20261024-v1",
        "status": "pre_registered_not_run",
        "seed": seed,
        "selection": {
            "source": "comparison_trial_manifest_v10.full_trials",
            "fields": ["benchmark", "domain", "split", "exposure_group", "case_id"],
            "ranking": "sha256(seed + NUL + case_id), ascending",
            "exclude_exposure_groups": ["development_exposed"],
        },
        "base_manifest_digest": base_digest,
        "diagnostic_evidence": diagnostic_evidence,
        "selected_trials": selected,
        "route_schedule": route_schedule,
        "counts": counts,
    }
    return {**payload, "manifest_digest": digest(payload)}


def _load_json(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Manifest input must be a regular file")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Manifest input is unreadable") from exc


def validate_pilot_manifest_target(root, target):
    """Keep the runtime freeze under ignored local state, preserving the CI SHA."""
    root = Path(root).resolve()
    state_root = root / ".decagent/evaluation/manifests"
    requested = Path(target)
    requested = root / requested if not requested.is_absolute() else requested
    try:
        relative = requested.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "Pilot manifests must be inside the ignored project state directory"
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Pilot manifest path cannot contain symlinks")
    output = requested.resolve(strict=False)
    if not output.is_relative_to(state_root) or output.suffix.lower() != ".json":
        raise ValueError("Pilot manifest must be JSON under ignored .decagent evaluation state")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def load_public_pilot_plan(
    root,
    *,
    pilot_manifest_path,
    base_manifest_path=None,
    source_manifest_path=None,
    profiles_path=None,
    readiness_path=None,
    diagnostic_evidence_path=None,
    check_images=False,
    check_runtime=True,
    check_source_files=True,
):
    """Validate the pilot freeze against v10 before constructing its run plan."""
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(root).resolve()
    base_path = Path(base_manifest_path) if base_manifest_path else (
        root / "evals/benchmarks/manifests/comparison_trial_manifest_v10.json"
    )
    base_manifest = _load_json(base_path)
    pilot_manifest = _load_json(pilot_manifest_path)
    base_plan = load_public_run_plan(
        root,
        mode="full",
        manifest_path=base_path,
        source_manifest_path=source_manifest_path,
        profiles_path=profiles_path,
        readiness_path=readiness_path,
        check_images=check_images,
        check_runtime=check_runtime,
        check_source_files=check_source_files,
    )
    diagnostic_gate = None
    if diagnostic_evidence_path is not None:
        diagnostic_gate = validate_diagnostic_evidence(
            root,
            diagnostic_evidence_path,
            model_profiles_digest=base_plan.profiles.fingerprint,
            expected_source_digest=evaluation_source_digest(root),
            expected_source_manifest_digest=base_plan.source_manifest_digest,
            expected_inventory_digest=base_plan.inventory_digest,
        )
    expected = build_pilot_manifest(base_manifest, diagnostic_gate=diagnostic_gate)
    if pilot_manifest != expected:
        raise ValueError(
            "Frozen public pilot differs from the approved sample or diagnostic evidence"
        )
    by_id = {item["trial_id"]: item for item in base_plan.trials}
    selected = tuple(
        by_id[item["trial_id"]]
        for item in pilot_manifest["selected_trials"]
        if item["trial_id"] in by_id and by_id[item["trial_id"]] == item
    )
    if len(selected) != 24:
        raise ValueError("Frozen pilot task records differ from the validated v10 base manifest")
    schedule = tuple(
        (item["trial_id"], route)
        for item in pilot_manifest["route_schedule"]
        for route in item["routes"]
    )
    routes = tuple(dict.fromkeys(route for _trial_id, route in schedule))
    return replace(
        base_plan,
        mode="pilot",
        manifest_digest=pilot_manifest["manifest_digest"],
        trials=selected,
        routes=routes,
        route_schedule=schedule,
        jev_semantic_status="reviewed_csm_natural_case_pairing_frozen_not_guaranteed",
        diagnostic_evidence_sha256=(
            diagnostic_gate["report_sha256"] if diagnostic_gate is not None else None
        ),
        diagnostic_candidate_sha=(
            diagnostic_gate["candidate_sha"] if diagnostic_gate is not None else None
        ),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(__file__).parent / "manifests/comparison_trial_manifest_v10.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "write the freeze under .decagent/evaluation/manifests without "
            "changing the CI candidate"
        ),
    )
    parser.add_argument("--diagnostic-evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        candidate_sha = require_verified_candidate(root)
        base_plan = load_public_run_plan(
            root,
            mode="full",
            manifest_path=args.base,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        )
        diagnostic_gate = validate_diagnostic_evidence(
            root,
            args.diagnostic_evidence,
            candidate_sha=candidate_sha,
            model_profiles_digest=base_plan.profiles.fingerprint,
            expected_source_digest=evaluation_source_digest(root),
            expected_source_manifest_digest=base_plan.source_manifest_digest,
            expected_inventory_digest=base_plan.inventory_digest,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(f"Pilot freeze requires a passing bounded diagnostic: {exc}")
    base = _load_json(args.base)
    manifest = build_pilot_manifest(base, diagnostic_gate=diagnostic_gate)
    output = validate_pilot_manifest_target(
        root,
        args.output
        or root / ".decagent/evaluation/manifests/sop_agent_public_pilot_20261024_v1.json",
    )
    if output.exists():
        parser.error("Frozen pilot manifest is immutable; choose a new filename")
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(
        json.dumps(
            {
                "path": str(output),
                "digest": manifest["manifest_digest"],
                "counts": manifest["counts"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

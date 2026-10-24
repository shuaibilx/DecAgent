import json
import shutil
from pathlib import Path

import pytest

from decagent.evaluation.business.public_trials import load_public_run_plan
from evals.benchmarks.pilot_gate import (
    DIAGNOSTIC_BATCH_LIMITS,
    DIAGNOSTIC_LIMITS,
    DIAGNOSTIC_RESOURCE_LIMITS,
    WEEKLY_BASELINE_RELATIVE_PATH,
    WEEKLY_BASELINE_SHA256,
    load_historical_weekly_usage,
    validate_diagnostic_evidence,
)

ROOT = Path(__file__).resolve().parents[2]


def _valid_report(candidate_sha="a" * 40, profile_digest="b" * 64):
    routes = {}
    for index in range(8):
        trial_id = f"task-{index // 2}"
        route = "react_skill_mcp" if index % 2 == 0 else "plan_mcp"
        routes[f"{trial_id}:{route}"] = {
            "trial_id": trial_id,
            "route": route,
            "lifecycle": {
                "status": "completed" if index == 1 else "failed",
                "score_status": "scored" if index == 1 else "not_attempted",
                "score_digest": "c" * 64 if index == 1 else None,
                "score_evidence": (
                    {
                        "source": "enterpriseops-gym-original-verifier",
                        "value": 0.0,
                        "success": False,
                        "forbidden_side_effects": 0,
                    }
                    if index == 1
                    else None
                ),
                "cleanup_status": "verified",
            },
            "execution_receipt": {
                "trial_id": trial_id,
                "route": route,
                "status": "completed" if index == 1 else "failed",
                "score_status": "scored" if index == 1 else "not_attempted",
                "score_digest": "c" * 64 if index == 1 else None,
                "cleanup_verified": True,
            },
            "resources": {"tool_calls": 1 if index == 1 else 0},
            "usage": {
                role: {
                    "requests": 1 if index == 1 and role == "primary" else 0,
                    "actual_tokens": 30 if index == 1 and role == "primary" else 0,
                    "reserved_tokens": 0,
                    "unknown_requests": 0,
                }
                for role in ("primary", "user_simulator", "judge")
            },
            "model_attempts": [
                {
                    "role": "primary",
                    "provider": "qwen",
                    "actual_provider": "qwen",
                    "status": "settled",
                    "actual_input_tokens": 20,
                    "actual_output_tokens": 10,
                    "actual_cost_amount": "0.000043",
                    "actual_cost_currency": "CNY",
                    "reserved_cost_amount": None,
                    "reserved_cost_currency": None,
                }
            ]
            if index == 1
            else [],
        }
    return {
        "schema_version": "public-evaluation-live-diagnostic-v2",
        "phase": "diagnostic",
        "diagnostic_round": 2,
        "dry_run": False,
        "candidate_sha": candidate_sha,
        "cohort_digest": "d" * 64,
        "evaluation_budget_id": "sop-pilot-20261024-v1",
        "working_tree_dirty": False,
        "candidate_ci_verified": True,
        "model_profiles_digest": profile_digest,
        "weekly_budget_baseline_sha256": WEEKLY_BASELINE_SHA256,
        "historical_weekly_usage": load_historical_weekly_usage(ROOT),
        "evaluation_source_digest": "e" * 64,
        "source_manifest_digest": "f" * 64,
        "task_inventory_digest": "9" * 64,
        "diagnostic_task_records": [
            {
                "trial_id": f"task-{index}",
                "benchmark": benchmark,
                "domain": domain,
                "split": split,
                "case_id": str(index),
                "version": "pinned-v1",
            }
            for index, (benchmark, domain, split) in enumerate(
                [
                    ("enterpriseops-gym", "csm", "oracle"),
                    ("enterpriseops-gym", "itsm", "oracle"),
                    ("tau-bench", "retail", "train"),
                    ("tau-bench", "telecom", "train"),
                ]
            )
        ],
        "task_denominator": 4,
        "selected_task_count": 4,
        "route_denominator": 8,
        "selected_route_slots": 8,
        "diagnostic_elapsed_seconds": 100.0,
        "diagnostic_limits": DIAGNOSTIC_LIMITS,
        "diagnostic_batch_limits": DIAGNOSTIC_BATCH_LIMITS,
        "diagnostic_resource_limits": DIAGNOSTIC_RESOURCE_LIMITS,
        "batch": {
            "qwen_requests": 1,
            "qwen_tokens": 30,
            "qwen_actual_tokens": 30,
            "qwen_reserved_tokens": 0,
            "qwen_cost_cny": "0.000043",
            "qwen_actual_cost_cny": "0.000043",
            "qwen_reserved_cost_cny": "0",
            "tool_calls": 1,
            "limits": DIAGNOSTIC_BATCH_LIMITS,
        },
        "routes": routes,
    }


def _write_report(tmp_path, report):
    evidence = tmp_path / ".decagent" / "evaluation" / "evidence"
    evidence.mkdir(parents=True)
    baseline = tmp_path / WEEKLY_BASELINE_RELATIVE_PATH
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline_bytes = (ROOT / WEEKLY_BASELINE_RELATIVE_PATH).read_bytes()
    baseline.write_bytes(baseline_bytes)
    for source in json.loads(baseline_bytes)["sources"]:
        for path_key in ("machine_report", "operator_report"):
            relative_path = Path(source[path_key])
            source_report = ROOT / relative_path
            target_report = tmp_path / relative_path
            target_report.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_report, target_report)
    path = evidence / "diagnostic.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_diagnostic_gate_accepts_bounded_four_domain_closed_loop(tmp_path):
    report_path = _write_report(tmp_path, _valid_report())

    result = validate_diagnostic_evidence(
        tmp_path,
        report_path,
        candidate_sha="a" * 40,
        model_profiles_digest="b" * 64,
        expected_source_digest="e" * 64,
        expected_source_manifest_digest="f" * 64,
        expected_inventory_digest="9" * 64,
    )

    assert result["closed_loop_route_count"] == 1
    assert result["general_model_requests"] == 1
    assert result["general_model_tokens"] == 30
    assert result["qwen_cost_cny"] == "0.000043"
    assert result["historical_weekly_usage"]["qwen_requests"] == 36
    assert result["weekly_budget_baseline_sha256"] == WEEKLY_BASELINE_SHA256


def test_diagnostic_gate_keeps_unknown_usage_reserved_while_accepting_other_closed_loop(
    tmp_path,
):
    report = _valid_report()
    unknown_route = report["routes"]["task-0:react_skill_mcp"]
    unknown_route["model_attempts"].append(
        {
            "role": "primary",
            "provider": "qwen",
            "actual_provider": "qwen",
            "status": "unknown",
            "actual_input_tokens": None,
            "actual_output_tokens": None,
            "actual_cost_amount": None,
            "actual_cost_currency": None,
            "reserved_cost_amount": "0.5",
            "reserved_cost_currency": "CNY",
        }
    )
    unknown_route["usage"]["primary"].update(
        requests=1,
        actual_tokens=0,
        reserved_tokens=100,
        unknown_requests=1,
    )
    report["batch"].update(
        qwen_requests=2,
        qwen_tokens=130,
        qwen_reserved_tokens=100,
        qwen_cost_cny="0.500043",
        qwen_actual_cost_cny="0.000043",
        qwen_reserved_cost_cny="0.5",
    )
    report_path = _write_report(tmp_path, report)

    result = validate_diagnostic_evidence(
        tmp_path,
        report_path,
        candidate_sha="a" * 40,
        model_profiles_digest="b" * 64,
        expected_source_digest="e" * 64,
        expected_source_manifest_digest="f" * 64,
        expected_inventory_digest="9" * 64,
    )

    assert result["unknown_requests"] == 1
    assert result["general_model_requests"] == 2
    assert result["general_model_tokens"] == 130
    assert result["qwen_cost_cny"] == "0.500043"


def test_diagnostic_gate_does_not_join_model_and_score_evidence_across_routes(tmp_path):
    report = _valid_report()
    scored_route = report["routes"]["task-0:plan_mcp"]
    model_only_route = report["routes"]["task-0:react_skill_mcp"]
    model_attempt = scored_route["model_attempts"].pop()
    scored_route["usage"]["primary"].update(requests=0, actual_tokens=0)
    model_only_route["model_attempts"].append(model_attempt)
    model_only_route["usage"]["primary"].update(requests=1, actual_tokens=30)
    report_path = _write_report(tmp_path, report)

    with pytest.raises(ValueError, match="real MCP execution with official scoring"):
        validate_diagnostic_evidence(
            tmp_path,
            report_path,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_diagnostic_gate_rejects_route_identity_detached_from_frozen_slot(tmp_path):
    report = _valid_report()
    report["routes"]["task-0:react_skill_mcp"]["trial_id"] = "task-1"
    report_path = _write_report(tmp_path, report)

    with pytest.raises(ValueError, match="route identity"):
        validate_diagnostic_evidence(
            tmp_path,
            report_path,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda report: report.update(candidate_sha="d" * 40), "candidate"),
        (lambda report: report.update(working_tree_dirty=True), "clean CI-verified candidate"),
        (
            lambda report: report.update(diagnostic_elapsed_seconds=2_701.0),
            "wall-time",
        ),
        (
            lambda report: report["batch"].update(qwen_requests=7),
            "Qwen request",
        ),
        (
            lambda report: report["batch"].update(qwen_cost_cny="13.5001"),
            "Qwen fee",
        ),
        (
            lambda report: report["routes"]["task-0:react_skill_mcp"]["model_attempts"].append(
                {
                    "role": "primary",
                    "provider": "qwen",
                    "actual_provider": "qwen",
                    "status": "unknown",
                    "actual_input_tokens": None,
                    "actual_output_tokens": None,
                    "actual_cost_amount": None,
                    "actual_cost_currency": None,
                    "reserved_cost_amount": "0.5",
                    "reserved_cost_currency": "CNY",
                }
            ),
            "reservation evidence",
        ),
        (
                lambda report: report["routes"]["task-0:plan_mcp"]["lifecycle"].update(
                score_status="failed"
            ),
            "official scoring",
        ),
    ],
)
def test_diagnostic_gate_rejects_invalid_or_unbounded_evidence(
    tmp_path, mutation, message
):
    report = _valid_report()
    mutation(report)
    report_path = _write_report(tmp_path, report)

    with pytest.raises(ValueError, match=message):
        validate_diagnostic_evidence(
            tmp_path,
            report_path,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_diagnostic_gate_rejects_report_outside_evidence_directory(tmp_path):
    report = _valid_report()
    path = tmp_path / "elsewhere.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="evidence directory"):
        validate_diagnostic_evidence(
            tmp_path,
            path,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_diagnostic_gate_rejects_intermediate_symlink_even_when_target_stays_inside(
    tmp_path,
):
    evidence = tmp_path / ".decagent" / "evaluation" / "evidence"
    real_directory = evidence / "real"
    real_directory.mkdir(parents=True)
    real_report = real_directory / "diagnostic.json"
    real_report.write_text(json.dumps(_valid_report()), encoding="utf-8")
    alias = evidence / "alias"
    try:
        alias.symlink_to(real_directory, target_is_directory=True)
    except OSError:
        pytest.skip("Local account cannot create directory symlinks")

    with pytest.raises(ValueError, match="cannot contain symlinks"):
        validate_diagnostic_evidence(
            tmp_path,
            alias / "diagnostic.json",
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_diagnostic_gate_checks_original_intermediate_symlink_before_resolution(
    monkeypatch, tmp_path
):
    evidence = tmp_path / ".decagent" / "evaluation" / "evidence"
    evidence.mkdir(parents=True)
    real_directory = evidence / "real"
    real_directory.mkdir()
    real_report = real_directory / "diagnostic.json"
    real_report.write_text(json.dumps(_valid_report()), encoding="utf-8")
    alias = evidence / "alias"
    requested = alias / "diagnostic.json"
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve

    def is_symlink(path):
        return True if path == alias else original_is_symlink(path)

    def resolve(path, *args, **kwargs):
        return real_report if path == requested else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(ValueError, match="cannot contain symlinks"):
        validate_diagnostic_evidence(
            tmp_path,
            requested,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_diagnostic_gate_rejects_parent_traversal_before_path_resolution(tmp_path):
    evidence = _write_report(tmp_path, _valid_report())
    requested = evidence.parent / ".." / "evidence" / "diagnostic.json"

    with pytest.raises(ValueError, match="parent traversal"):
        validate_diagnostic_evidence(
            tmp_path,
            requested,
            candidate_sha="a" * 40,
            model_profiles_digest="b" * 64,
            expected_source_digest="e" * 64,
            expected_source_manifest_digest="f" * 64,
            expected_inventory_digest="9" * 64,
        )


def test_bounded_diagnostic_plan_has_four_cases_and_enforces_small_batch_caps():
    from evals.benchmarks.pilot_gate import build_bounded_diagnostic_plan

    base = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    diagnostic = build_bounded_diagnostic_plan(base)
    from decagent.decisions.contracts import digest

    assert diagnostic.mode == "smoke"
    assert len(diagnostic.trials) == 4
    assert [record["trial_id"] for record in diagnostic.trials] == [
        selected["trial_id"] for selected in base.initial_smoke_wave
    ]
    assert {
        record["resource_profile_digest"] for record in diagnostic.trials
    } == {digest(diagnostic.resource_limits)}
    assert diagnostic.routes == ("plan_mcp", "react_skill_mcp")
    assert all(
        diagnostic.routes_for_trial(record["trial_id"])[0] == "plan_mcp"
        for record in diagnostic.trials
    )
    assert diagnostic.route_slot_count() == 8
    assert diagnostic.batch_limits["qwen_requests"] == 6
    assert diagnostic.batch_limits["qwen_tokens"] == 600_000
    assert diagnostic.batch_limits["qwen_cost_cny"] == "1.62"
    assert diagnostic.batch_limits["wall_seconds"] == 2_700
    assert sum(
        diagnostic.batch_limits[f"{role}_qwen_requests"]
        for role in ("agent", "user_simulator", "judge")
    ) == 6


def test_openrouter_diagnostic_plan_is_a_single_csm_plan_route_with_residual_caps(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_limits import OPENROUTER_DIAGNOSTIC_LIMITS
    from evals.benchmarks.build_trial_manifest import build_trial_manifest
    from evals.benchmarks.pilot_gate import (
        build_bounded_openrouter_diagnostic_plan,
        is_bounded_openrouter_diagnostic_plan,
    )

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v6.json")
            .read_text(encoding="utf-8")
        )
    )

    def openrouter_profile(profile):
        return replace(
            profile,
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
        primary=openrouter_profile(profiles.primary),
        user_simulator=openrouter_profile(profiles.user_simulator),
        judge=openrouter_profile(profiles.judge),
    )
    manifest = build_trial_manifest(
        ROOT,
        model_profiles=profiles,
        manifest_version=12,
        batch_limits={
            **OPENROUTER_DIAGNOSTIC_LIMITS,
            "tool_calls": 399,
            "wall_seconds": 2_519,
        },
    )
    manifest_path = tmp_path / "comparison_trial_manifest_v12.json"
    profiles_path = tmp_path / "evaluation_model_profiles_v7.json"
    # Use temporary fixtures so the production loader still verifies
    # the manifest/profile binding without requiring committed generated files.
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profiles_path.write_text(json.dumps(manifest["model_profiles"]), encoding="utf-8")

    with pytest.raises(ValueError, match="routing evidence is missing"):
        load_public_run_plan(
            ROOT,
            mode="smoke",
            check_images=False,
            check_runtime=False,
            check_source_files=False,
            manifest_path=manifest_path,
            profiles_path=profiles_path,
            readiness_path=(
                ROOT
                / "evals/benchmarks/evidence/"
                / "routed-model-readiness-20261024-132906.json"
            ),
        )

    base = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
        manifest_path=manifest_path,
        profiles_path=profiles_path,
        allow_unready_diagnostic_model=True,
    )
    plan = build_bounded_openrouter_diagnostic_plan(base)

    assert is_bounded_openrouter_diagnostic_plan(plan)
    assert len(plan.trials) == 1
    assert plan.trials[0]["domain"] == "csm"
    assert plan.routes == ("plan_mcp",)
    assert plan.route_slot_count() == 1
    assert plan.batch_limits["openrouter_requests"] == 6
    assert plan.batch_limits["openrouter_tokens"] == 565_946
    assert plan.batch_limits["wall_seconds"] == 2_700
    assert plan.batch_limits["openrouter_cost_usd"] == "5"
    assert plan.batch_limits["agent_openrouter_requests"] == 5
    assert plan.batch_limits["judge_openrouter_requests"] == 1
    assert plan.batch_limits["judge_openrouter_tokens"] == 65_946
    assert "qwen_requests" not in plan.batch_limits
    assert plan.resource_limits["agent_machine_seconds"] == 1_800
    assert plan.resource_limits["judge_tokens"] == 65_946


def test_openrouter_diagnostic_plan_rejects_qwen_profile():
    from evals.benchmarks.pilot_gate import build_bounded_openrouter_diagnostic_plan

    base = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    with pytest.raises(ValueError, match="OpenRouter"):
        build_bounded_openrouter_diagnostic_plan(base)


def test_routed_readiness_v2_cannot_unlock_unpinned_openrouter_route():
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    readiness = ROOT / (
        "evals/benchmarks/evidence/"
        "routed-model-readiness-20261024-132906.json"
    )

    with pytest.raises(ValueError, match="routing evidence is missing"):
        _validate_readiness(ROOT, profiles, readiness)


def _fireworks_readiness_v3_fixture(report, profiles):
    import hashlib
    from decimal import Decimal

    from decagent.evaluation.business.routed_readiness import _price_digest
    from decagent.runtime.model_calls import TokenPrice

    report = json.loads(json.dumps(report))
    profile = profiles.primary
    readiness_price_version = "openrouter-endpoints-api-2026-10-19-peak"
    readiness_input_rate = Decimal("0.60")
    readiness_output_rate = Decimal("2.40")
    profile_digest = profiles.digests["model_profile_digest"]
    request_profile_fingerprints = {
        purpose: hashlib.sha256(f"readiness:{purpose}".encode()).hexdigest()
        for purpose in ("connection", "tool_contract", "plan", "stream")
    }
    ledger = report["ledger"]
    ledger["schema_version"] = "routed-model-readiness-ledger-v3"
    ledger["model_profile_digest"] = profile_digest
    ledger["cohort_profiles_fingerprint"] = profiles.fingerprint
    ledger["fallback_price_version"] = readiness_price_version
    ledger["fallback_price_currency"] = profile.currency
    ledger["fallback_price_source"] = profile.price_source
    ledger["fallback_price_digest"] = _price_digest(
        TokenPrice(
            readiness_price_version,
            readiness_input_rate,
            readiness_output_rate,
            profile.currency,
        )
    )
    for attempt in ledger["attempts"]:
        if attempt["provider"] == "openrouter":
            attempt["provider_routing"] = profile.provider_routing
            attempt["request_profile_fingerprint"] = request_profile_fingerprints[
                attempt["purpose"]
            ]
    report["schema_version"] = "routed-model-readiness-evidence-v3"
    report["model_profile_digest"] = profile_digest
    report["cohort_profiles_fingerprint"] = profiles.fingerprint
    report["provider_profiles"] = {
        "openrouter": {
            "provider": profile.provider,
            "model": profile.model,
            "endpoint": profile.endpoint,
            "provider_routing": profile.provider_routing,
            "price_version": readiness_price_version,
            "price_source": profile.price_source,
            "price_currency": profile.currency,
            "input_price_per_million": str(readiness_input_rate),
            "output_price_per_million": str(readiness_output_rate),
            "request_profile_fingerprints": request_profile_fingerprints,
        }
    }
    return report


def test_routed_readiness_v2_cannot_be_upgraded_by_adding_v3_routing_metadata(tmp_path):
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    report = json.loads(
        (
            ROOT
            / "evals/benchmarks/evidence/"
            / "routed-model-readiness-20261024-132906.json"
        ).read_text(encoding="utf-8")
    )
    # Adding an outer route claim to v2 evidence does not change what its ledger
    # actually recorded about dispatched requests.
    report["schema_version"] = "routed-model-readiness-evidence-v3"
    report["provider_profiles"] = {
        "openrouter": {
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4.1-flash",
            "endpoint": "https://openrouter.ai/api/v1",
            "provider_routing": "openrouter_fireworks",
            "price_source": profiles.primary.price_source,
            "price_currency": "USD",
            "input_price_per_million": "0.60",
            "output_price_per_million": "2.40",
        }
    }
    readiness = tmp_path / "upgraded-v2.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="ledger profile binding"):
        _validate_readiness(ROOT, profiles, readiness)


def test_routed_readiness_v3_accepts_exact_openrouter_route_and_cost_envelope(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    exact_roles = {
        role: replace(
            getattr(profiles, role),
            price_version="openrouter-endpoints-api-2026-10-19-peak",
            price_source=(
                "https://openrouter.ai/api/v1/models/"
                "deepseek/deepseek-v4.1-flash/endpoints"
            ),
            input_price_per_million=Decimal("0.60"),
            output_price_per_million=Decimal("2.40"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    exact_profiles = replace(profiles, **exact_roles)
    readiness_v2 = ROOT / (
        "evals/benchmarks/evidence/"
        "routed-model-readiness-20261024-132906.json"
    )
    report = _fireworks_readiness_v3_fixture(
        json.loads(readiness_v2.read_text(encoding="utf-8")), exact_profiles
    )
    routed_readiness = tmp_path / "routed-readiness-v3.json"
    routed_readiness.write_text(json.dumps(report), encoding="utf-8")

    assert _validate_readiness(ROOT, exact_profiles, routed_readiness) == routed_readiness


def test_routed_readiness_v3_accepts_recovered_history_with_not_sent_attempt(tmp_path):
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    report = _fireworks_readiness_v3_fixture(
        json.loads(
            (
                ROOT
                / "evals/benchmarks/evidence/"
                / "routed-model-readiness-20261024-132906.json"
            ).read_text(encoding="utf-8")
        ),
        profiles,
    )
    attempt = dict(report["ledger"]["attempts"][-1])
    attempt.update(
        {
            "receipt": "known-not-sent-recovery",
            "attempt": 1,
            "status": "settled",
            "dispatch_state": "not_sent",
            "provider": "openrouter",
            "requested_model": profiles.primary.model,
            "resolved_model": profiles.primary.model,
            "endpoint": profiles.primary.endpoint,
            "purpose": "connection",
            "provider_routing": profiles.primary.provider_routing,
            "request_profile_fingerprint": report["provider_profiles"]["openrouter"]
            ["request_profile_fingerprints"]["connection"],
            "actual_tokens": 0,
            "actual_cost_amount": "0",
        }
    )
    report["ledger"]["attempts"].append(attempt)
    readiness = tmp_path / "recovered-readiness-v3.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    assert _validate_readiness(ROOT, profiles, readiness) == readiness


def test_legacy_qwen_v1_readiness_cannot_unlock_live_cohort(tmp_path):
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v6.json")
            .read_text(encoding="utf-8")
        )
    )
    report = {
        "schema_version": "routed-model-readiness-evidence-v1",
        "status": "passed",
        "ledger": {
            "schema_version": "routed-model-readiness-ledger-v1",
            "blocked": False,
            "used_calls": 0,
            "remaining_calls": 1,
            "max_calls": 1,
            "attempts": [],
            "primary_model": profiles.primary.model,
            "primary_endpoint": profiles.primary.endpoint,
        },
    }
    readiness = tmp_path / "legacy-readiness-v1.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="profile-bound readiness"):
        _validate_readiness(ROOT, profiles, readiness)


def test_live_public_plan_rejects_legacy_readiness_and_unready_override():
    with pytest.raises(ValueError, match="profile-bound readiness"):
        load_public_run_plan(
            ROOT,
            mode="smoke",
            check_images=False,
            check_runtime=True,
            check_source_files=False,
        )

    with pytest.raises(ValueError, match="cannot bypass model readiness"):
        load_public_run_plan(
            ROOT,
            mode="smoke",
            allow_unready_diagnostic_model=True,
            check_images=False,
            check_runtime=True,
            check_source_files=False,
        )


def test_openrouter_readiness_can_qualify_with_unresolved_qwen_attempt_preserved(tmp_path):
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    source = json.loads(
        (
            ROOT
            / "evals/benchmarks/evidence/"
            / "routed-model-readiness-20261024-132906.json"
        ).read_text(encoding="utf-8")
    )
    report = _fireworks_readiness_v3_fixture(source, profiles)
    attempts = report["ledger"]["attempts"]
    qwen_unknown = dict(next(item for item in attempts if item["provider"] == "qwen"))
    qwen_unknown["status"] = "unknown"
    qwen_unknown["dispatch_state"] = "unknown"
    report["ledger"]["attempts"] = [
        qwen_unknown,
        *(item for item in attempts if item["provider"] == "openrouter"),
    ]
    report["ledger"]["used_calls"] = 5
    report["ledger"]["remaining_calls"] = 7
    report["ledger"]["qwen_calls"] = 1
    report["ledger"]["openrouter_calls"] = 4
    report["ledger"]["blocked"] = True
    readiness = tmp_path / "routed-readiness-qwen-unknown.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    assert _validate_readiness(ROOT, profiles, readiness) == readiness


def test_openrouter_unknown_attempt_cannot_qualify_its_provider(tmp_path):
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    report = _fireworks_readiness_v3_fixture(
        json.loads(
            (
                ROOT
                / "evals/benchmarks/evidence/"
                / "routed-model-readiness-20261024-132906.json"
            ).read_text(encoding="utf-8")
        ),
        profiles,
    )
    report["ledger"]["attempts"][-1]["status"] = "unknown"
    report["ledger"]["attempts"][-1]["dispatch_state"] = "unknown"
    report["ledger"]["blocked"] = True
    readiness = tmp_path / "routed-readiness-openrouter-unknown.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="selected provider has unresolved usage"):
        _validate_readiness(ROOT, profiles, readiness)


def test_routed_readiness_v3_rejects_cohort_profile_drift(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    exact_roles = {
        role: replace(
            getattr(profiles, role),
            price_version="openrouter-endpoints-api-2026-10-19-peak",
            price_source=(
                "https://openrouter.ai/api/v1/models/"
                "deepseek/deepseek-v4.1-flash/endpoints"
            ),
            input_price_per_million=Decimal("0.60"),
            output_price_per_million=Decimal("2.40"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    profiles = replace(profiles, **exact_roles)
    report = _fireworks_readiness_v3_fixture(
        json.loads(
            (
                ROOT
                / "evals/benchmarks/evidence/"
                / "routed-model-readiness-20261024-132906.json"
            ).read_text(encoding="utf-8")
        ),
        profiles,
    )
    readiness = tmp_path / "routed-readiness-v3.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")
    changed_judge = replace(
        profiles.judge,
        timeout_seconds=profiles.judge.timeout_seconds + 1,
    )
    changed_profiles = replace(profiles, judge=changed_judge)

    with pytest.raises(ValueError, match="cohort profile fingerprint"):
        _validate_readiness(ROOT, changed_profiles, readiness)


def test_routed_readiness_v3_rejects_attempt_route_or_request_profile_drift(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    exact_roles = {
        role: replace(
            getattr(profiles, role),
            price_version="openrouter-endpoints-api-2026-10-19-peak",
            price_source=(
                "https://openrouter.ai/api/v1/models/"
                "deepseek/deepseek-v4.1-flash/endpoints"
            ),
            input_price_per_million=Decimal("0.60"),
            output_price_per_million=Decimal("2.40"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    profiles = replace(profiles, **exact_roles)
    report = _fireworks_readiness_v3_fixture(
        json.loads(
            (
                ROOT
                / "evals/benchmarks/evidence/"
                / "routed-model-readiness-20261024-132906.json"
            ).read_text(encoding="utf-8")
        ),
        profiles,
    )
    report["ledger"]["attempts"][-1]["provider_routing"] = "openrouter_default"
    readiness = tmp_path / "routed-readiness-v3.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="attempt route or profile fingerprint"):
        _validate_readiness(ROOT, profiles, readiness)


def test_routed_readiness_v3_rejects_invalid_or_underpriced_public_profile(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json")
            .read_text(encoding="utf-8")
        )
    )
    profiles = replace(
        profiles,
        primary=replace(profiles.primary, input_price_per_million=Decimal("0.61")),
    )
    report = _fireworks_readiness_v3_fixture(
        json.loads(
        (
            ROOT
            / "evals/benchmarks/evidence/"
            / "routed-model-readiness-20261024-132906.json"
        ).read_text(encoding="utf-8")
        ),
        profiles,
    )
    report["provider_profiles"]["openrouter"]["input_price_per_million"] = "0.60"
    readiness = tmp_path / "routed-readiness-v3.json"
    readiness.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="does not bound the public cost profile"):
        _validate_readiness(ROOT, profiles, readiness)

    report["provider_profiles"]["openrouter"]["input_price_per_million"] = "not-a-rate"
    readiness.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="cost envelope is incomplete"):
        _validate_readiness(ROOT, profiles, readiness)


def test_routed_openrouter_readiness_cannot_unlock_qwen_profile():
    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from decagent.evaluation.business.public_trials import _validate_readiness

    profiles = EvaluationModelProfiles.from_dict(
        json.loads(
            (ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v6.json")
            .read_text(encoding="utf-8")
        )
    )
    readiness = ROOT / (
        "evals/benchmarks/evidence/"
        "routed-model-readiness-20261024-132906.json"
    )

    with pytest.raises(ValueError, match="provider differs"):
        _validate_readiness(ROOT, profiles, readiness)


def test_pilot_batch_limits_subtract_diagnostic_consumption_from_shared_weekly_caps():
    from evals.benchmarks.pilot_gate import remaining_pilot_batch_limits

    plan = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="full",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    diagnostic = {
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
    }

    limits = remaining_pilot_batch_limits(plan.batch_limits, diagnostic)

    assert limits == {
        "qwen_requests": 497,
        "qwen_tokens": 4_200_000,
        "agent_qwen_requests": 298,
        "agent_qwen_tokens": 2_500_000,
        "user_simulator_qwen_requests": 159,
        "user_simulator_qwen_tokens": 1_300_000,
        "judge_qwen_requests": 40,
        "judge_qwen_tokens": 400_000,
        "tool_calls": 1_500,
        "wall_seconds": 14_279,
        "qwen_cost_cny": "11.500000",
    }


def test_pilot_batch_limits_also_subtract_previously_spent_weekly_budget():
    from evals.benchmarks.pilot_gate import remaining_pilot_batch_limits

    plan = load_public_run_plan(
        ROOT,
        mode="full",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    historical = load_historical_weekly_usage(ROOT)
    diagnostic = {
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
    }

    limits = remaining_pilot_batch_limits(
        plan.batch_limits, diagnostic, historical_usage=historical
    )

    assert limits == {
        "qwen_requests": 461,
        "qwen_tokens": 3_686_924,
        "agent_qwen_requests": 272,
        "agent_qwen_tokens": 2_019_712,
        "user_simulator_qwen_requests": 149,
        "user_simulator_qwen_tokens": 1_267_212,
        "judge_qwen_requests": 40,
        "judge_qwen_tokens": 400_000,
        "tool_calls": 1_485,
        "wall_seconds": 13_404,
        "qwen_cost_cny": "11.0068360",
    }


def test_pilot_batch_limits_reject_exhausted_budget_or_cross_provider_diagnostic():
    from evals.benchmarks.pilot_gate import remaining_pilot_batch_limits

    plan = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="full",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    diagnostic = {
        "general_model_requests": 3,
        "general_model_tokens": 800_000,
        "qwen_requests": 3,
        "qwen_tokens": 800_000,
        "qwen_cost_cny": "13.50",
        "tool_calls": 100,
        "diagnostic_elapsed_seconds": 120.0,
        "role_usage": {
            "agent": {"requests": 2, "tokens": 500_000},
            "user_simulator": {"requests": 1, "tokens": 300_000},
            "judge": {"requests": 0, "tokens": 0},
        },
    }
    with pytest.raises(ValueError, match="fee budget"):
        remaining_pilot_batch_limits(plan.batch_limits, diagnostic)

    diagnostic["qwen_cost_cny"] = "2.00"
    diagnostic["general_model_requests"] = 4
    diagnostic["openrouter_requests"] = 1
    with pytest.raises(ValueError, match="frozen Qwen pilot"):
        remaining_pilot_batch_limits(plan.batch_limits, diagnostic)


def test_historical_budget_loader_verifies_source_report_hashes(tmp_path):
    baseline_relative = WEEKLY_BASELINE_RELATIVE_PATH
    baseline_source = ROOT / baseline_relative
    baseline_target = tmp_path / baseline_relative
    baseline_target.parent.mkdir(parents=True)
    shutil.copyfile(baseline_source, baseline_target)

    report_relative = Path(
        "evals/benchmarks/evidence/public-first-wave-live-20261024-5b43a45-100qwen.json"
    )
    report_source = ROOT / report_relative
    report_target = tmp_path / report_relative
    report_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report_source, report_target)
    report_target.write_bytes(report_target.read_bytes() + b" ")

    with pytest.raises(ValueError, match="baseline source report digest"):
        load_historical_weekly_usage(tmp_path)

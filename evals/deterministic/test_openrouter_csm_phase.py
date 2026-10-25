import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decagent.evaluation.business import public_weekly_budget as budget
from decagent.evaluation.business.public_trials import (
    PublicTrialLedger,
    build_dry_run_report,
    canonical_public_trial_ledger_path,
    load_public_run_plan,
)
from evals.benchmarks.pilot_gate import (
    OPENROUTER_CSM_DIAGNOSTIC_BATCH_LIMITS,
    OPENROUTER_CSM_DIAGNOSTIC_RESOURCE_LIMITS,
    build_bounded_openrouter_csm_diagnostic_plan,
    is_bounded_openrouter_csm_diagnostic_plan,
)

ROOT = Path(__file__).resolve().parents[2]
READINESS = (
    ROOT
    / "evals/benchmarks/evidence/routed-model-readiness-fireworks-v3-20261024.json"
)


def _fireworks_plan():
    return load_public_run_plan(
        ROOT,
        mode="smoke",
        manifest_path=ROOT / "evals/benchmarks/manifests/comparison_trial_manifest_v12.json",
        profiles_path=ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v7.json",
        readiness_path=READINESS,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )


def test_independent_plan_selects_only_the_fixed_exposed_csm_plan_route():
    plan = build_bounded_openrouter_csm_diagnostic_plan(_fireworks_plan())
    report = build_dry_run_report(plan, phase="openrouter-csm-diagnostic")

    assert is_bounded_openrouter_csm_diagnostic_plan(plan)
    assert len(plan.trials) == len(plan.initial_smoke_wave) == 1
    assert plan.trials[0]["case_id"] == "task_20260106_161859_265_00364ece_0e836ce8"
    assert plan.routes == ("plan_mcp",)
    assert plan.batch_limits == OPENROUTER_CSM_DIAGNOSTIC_BATCH_LIMITS
    assert plan.resource_limits == OPENROUTER_CSM_DIAGNOSTIC_RESOURCE_LIMITS
    assert report["phase"] == "openrouter-csm-diagnostic"
    assert report["selected_task_count"] == report["selected_route_attempt_slots"] == 1
    assert report["live_model_calls"] == report["mcp_calls"] == 0
    assert report["ledger_created"] is False


def test_independent_plan_rejects_non_fireworks_profile_or_route_drift():
    from dataclasses import replace

    plan = _fireworks_plan()
    invalid_route = replace(plan, routes=("react_skill_mcp",))
    with pytest.raises(ValueError, match="CSM|OpenRouter|Fireworks"):
        build_bounded_openrouter_csm_diagnostic_plan(invalid_route)

    qwen_plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        manifest_path=ROOT / "evals/benchmarks/manifests/comparison_trial_manifest_v11.json",
        profiles_path=ROOT / "evals/benchmarks/manifests/evaluation_model_profiles_v6.json",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    with pytest.raises(ValueError, match="CSM|OpenRouter|Fireworks"):
        build_bounded_openrouter_csm_diagnostic_plan(qwen_plan)


def _authorization_identity(root, *, candidate="a" * 40):
    ledger = canonical_public_trial_ledger_path(
        root,
        phase="openrouter-csm-diagnostic",
        cohort_digest="b" * 64,
        candidate_sha=candidate,
    )
    return {
        "authorization_id": "openrouter-csm-diagnostic-20261025-v1",
        "candidate_sha": candidate,
        "cohort_digest": "b" * 64,
        "ledger_path": str(ledger),
        "readiness_evidence_sha256": "c" * 64,
        "model_profiles_digest": "d" * 64,
        "evaluation_source_digest": "e" * 64,
        "manifest_digest": "f" * 64,
        "source_manifest_digest": "1" * 64,
        "task_inventory_digest": "2" * 64,
        "route_identity": {
            "benchmark": "enterpriseops-gym",
            "domain": "csm",
            "case_id": "task_20260106_161859_265_00364ece_0e836ce8",
            "trial_id": "e60fc80781019a10a0896491959761688b1718d63e021b5edea37813d9f9dbc5",
            "route": "plan_mcp",
        },
    }


def test_independent_authorization_has_its_own_persistent_clock_and_identity(
    tmp_path, monkeypatch
):
    user_home = tmp_path / "user-home"
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setattr(budget, "_repository_identity", lambda _root: "https://github.com/shuaibilx/DecAgent")
    first_root = tmp_path / "checkout-one"
    second_root = tmp_path / "checkout-two"
    first_root.mkdir()
    second_root.mkdir()
    identity = _authorization_identity(first_root)

    first = budget.prepare_openrouter_csm_diagnostic_authorization(first_root, **identity)
    resumed = budget.prepare_openrouter_csm_diagnostic_authorization(first_root, **identity)

    started = datetime.fromisoformat(first["batch_started_at_utc"])
    deadline = datetime.fromisoformat(first["batch_deadline_at_utc"])
    assert started.tzinfo == UTC
    assert (deadline - started).total_seconds() == 1_800
    assert resumed["batch_started_at_utc"] == first["batch_started_at_utc"]
    assert resumed["authorization_path"] == first["authorization_path"]
    assert resumed["status"] == "prepared"

    with pytest.raises(ValueError, match="identity|candidate|cohort|ledger"):
        budget.prepare_openrouter_csm_diagnostic_authorization(
            second_root, **_authorization_identity(second_root)
        )


def test_independent_authorization_race_cannot_allocate_a_second_candidate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    monkeypatch.setattr(budget, "_repository_identity", lambda _root: "https://github.com/shuaibilx/DecAgent")
    root = tmp_path / "checkout"
    root.mkdir()
    identities = [
        _authorization_identity(root, candidate=candidate)
        for candidate in ("a" * 40, "9" * 40)
    ]

    def authorize(identity):
        try:
            budget.prepare_openrouter_csm_diagnostic_authorization(root, **identity)
            return "authorized"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(authorize, identities))

    assert sorted(outcomes) == ["authorized", "rejected"]


def test_cli_dry_run_for_independent_phase_never_creates_a_ledger(capsys):
    from evals.benchmarks.run_public_trials import main

    code = main(
        [
            "--root",
            str(ROOT),
            "--phase",
            "openrouter-csm-diagnostic",
            "--readiness-evidence",
            str(READINESS),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["dry_run"] is True
    assert report["selected_route_attempt_slots"] == 1
    assert report["live_model_calls"] == report["mcp_calls"] == 0
    assert report["ledger_created"] is False


def test_fixed_csm_official_verifier_is_compatible_with_zero_llm_judge():
    from decagent.evaluation.business.public_trials import _read_json
    from evals.benchmarks.live_public_runner import _enterprise_task

    plan = build_bounded_openrouter_csm_diagnostic_plan(_fireworks_plan())
    record = plan.trials[0]
    source = _read_json(ROOT / "evals/benchmarks/manifests/source_manifest.json")
    inventory = _read_json(ROOT / "evals/benchmarks/manifests/task_inventory.json")
    parquet = next(
        item
        for item in source["enterpriseops_gym"]["task_dataset"]["parquet_sha256"]
        if "/oracle/csm-00000-of-00001.parquet" in item["path"].replace("\\", "/")
    )
    if not (ROOT / parquet["path"]).is_file():
        pytest.skip(
            "Pinned EnterpriseOps verifier inspection requires the local-only vendor cache"
        )
    spec, _ = _enterprise_task(ROOT, record, source, inventory)

    assert spec.verifiers
    assert all(
        not (isinstance(verifier, dict) and verifier.get("verifier_type") == "response_check")
        for verifier in spec.verifiers
    )


def test_independent_ledger_uses_external_deadline_and_cannot_reset_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    monkeypatch.setattr(budget, "_repository_identity", lambda _root: "https://github.com/shuaibilx/DecAgent")
    root = tmp_path / "checkout"
    root.mkdir()
    plan = build_bounded_openrouter_csm_diagnostic_plan(_fireworks_plan())
    from dataclasses import replace

    plan = replace(plan, root=root)
    candidate = "a" * 40
    ledger_path = canonical_public_trial_ledger_path(
        root,
        cohort_digest=plan.cohort_digest,
        candidate_sha=candidate,
        phase="openrouter-csm-diagnostic",
    )
    identity = {
        "authorization_id": "openrouter-csm-diagnostic-20261025-v1",
        "candidate_sha": candidate,
        "cohort_digest": plan.cohort_digest,
        "ledger_path": str(ledger_path),
        "readiness_evidence_sha256": (
            "6630acf998d785dc1bb283030ee8fc184d59a5af0ac0efcf3777864db2ecc857"
        ),
        "model_profiles_digest": plan.profiles.fingerprint,
        "evaluation_source_digest": "3" * 64,
        "manifest_digest": plan.manifest_digest,
        "source_manifest_digest": plan.source_manifest_digest,
        "task_inventory_digest": plan.inventory_digest,
        "route_identity": {
            "benchmark": "enterpriseops-gym",
            "domain": "csm",
            "case_id": "task_20260106_161859_265_00364ece_0e836ce8",
            "trial_id": "e60fc80781019a10a0896491959761688b1718d63e021b5edea37813d9f9dbc5",
            "route": "plan_mcp",
        },
    }
    authorization = budget.prepare_openrouter_csm_diagnostic_authorization(
        root, **identity
    )
    started = datetime.fromisoformat(authorization["batch_started_at_utc"])
    ledger = PublicTrialLedger.create(
        ledger_path,
        plan,
        global_jev_ledger=None,
        batch_started_at=started,
    )
    ledger.bind_candidate_sha(candidate)
    real_datetime = budget.datetime

    class ExpiredBatchDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=1_801)

    monkeypatch.setattr(budget, "datetime", ExpiredBatchDateTime)
    with pytest.raises(ValueError, match="deadline has expired"):
        budget.activate_openrouter_csm_diagnostic_authorization(root, authorization)
    monkeypatch.setattr(budget, "datetime", real_datetime)
    authorization = budget.activate_openrouter_csm_diagnostic_authorization(
        root, authorization
    )
    try:
        snapshot = ledger.batch_snapshot()
        assert snapshot["started_at_utc"] == authorization["batch_started_at_utc"]
        assert ledger.global_jev_ledger is None
    finally:
        ledger.close()

    import sqlite3

    with sqlite3.connect(ledger_path) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        assert json.loads(metadata["global_jev_ledger_path"]) is None
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0

    resumed = PublicTrialLedger.resume(ledger_path, plan, global_jev_ledger=None)
    try:
        assert resumed.batch_snapshot()["started_at_utc"] == authorization[
            "batch_started_at_utc"
        ]
    finally:
        resumed.close()

    import sqlite3

    with sqlite3.connect(ledger_path) as connection:
        for role in ("user_simulator", "judge"):
            connection.execute(
                "INSERT INTO model_attempts("
                "attempt_id,trial_id,route,role,status,estimated_input_tokens,"
                "reserved_output_tokens,reserved_tokens,provider,model,endpoint,"
                "price_version,attempt_number,profile_digest,created_at_utc"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"forbidden-{role}",
                    plan.trials[0]["trial_id"],
                    "plan_mcp",
                    role,
                    "reserved",
                    1,
                    1,
                    2,
                    "openrouter",
                    "pinned-model",
                    "https://openrouter.ai/api/v1",
                    "frozen",
                    1,
                    "3" * 64,
                    started.isoformat(),
                ),
            )
        connection.commit()
    with pytest.raises(ValueError, match="disabled-role boundary"):
        budget._inspect_openrouter_csm_ledger(authorization)

    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: False if path == ledger_path else original_is_file(path),
    )
    with pytest.raises(ValueError, match="lost its ledger|cannot reset"):
        budget.prepare_openrouter_csm_diagnostic_authorization(root, **identity)

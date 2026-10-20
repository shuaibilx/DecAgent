"""Benchmark plumbing is offline evidence, never a model-performance claim."""

import copy
import importlib
import json
from decimal import Decimal
from importlib.util import find_spec

import pytest


def module(name):
    assert find_spec("decagent.evaluation.business") is not None, "Business benchmark is missing"
    return importlib.import_module("decagent.evaluation.business." + name)


def test_frozen_dataset_split_source_and_integrity(tmp_path):
    data = module("dataset")
    frozen = data.load_dataset()
    assert frozen["label_source"] == "synthetic-reference"
    assert len(frozen["cases"]) == 120
    assert len({case["id"] for case in frozen["cases"]}) == 120
    holdout = data.select_cases(frozen, "holdout")
    assert len(data.select_cases(frozen, "development")) == 40
    assert [sum(c["kind"] == kind for c in holdout) for kind in data.KINDS] == [48, 18, 14]
    changed = copy.deepcopy(frozen)
    changed["cases"][0]["instruction"] += "tampered"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        data.load_dataset(path)
    with pytest.raises(FileExistsError):
        data.freeze_dataset(path)


def test_three_interleaved_rounds_rotate_every_strategy_position():
    dataset = module("dataset")
    schedule = module("schedule")
    cases = dataset.select_cases(dataset.load_dataset(), "holdout")
    trials = schedule.make_schedule(cases)
    assert len(trials) == 720
    assert len({t["id"] for t in trials}) == 720
    for start in range(0, len(trials), 3):
        block = trials[start : start + 3]
        assert len({t["case_id"] for t in block}) == 1
        assert {t["strategy"] for t in block} == {"rule", "main", "hybrid"}
    target = cases[0]["id"]
    positions = {
        strategy: [
            i % 3
            for i, t in enumerate(trials)
            if t["case_id"] == target and t["strategy"] == strategy
        ]
        for strategy in schedule.STRATEGIES
    }
    assert all(sorted(value) == [0, 1, 2] for value in positions.values())


def test_failures_and_unattempted_trials_stay_in_denominator():
    metrics = module("metrics")
    schedule = module("schedule").make_schedule([{"id": "x", "kind": "common"}])
    rows = [
        {
            **trial,
            "attempted": True,
            "correct": trial["strategy"] == "hybrid",
            "state": "succeeded",
            "machine_seconds": 1.0,
            "human_wait_seconds": 7.0,
            "forbidden_writes": 0,
            "usage_complete": True,
            "calls": 0,
            "tokens": 0,
            "cost_usd": "0",
            "evidence_mode": "live",
        }
        for trial in schedule[:3]
    ]
    report = metrics.build_report(schedule, rows, evidence_mode="live")
    assert report["strategies"]["hybrid"]["denominator"] == 3
    assert report["strategies"]["hybrid"]["accuracy"] == 1 / 3
    assert report["strategies"]["hybrid"]["human_wait_seconds"] == 7
    assert report["acceptance"]["status"] == "not_passed"
    assert report["missing_trials"] == 6


def test_acceptance_requires_live_complete_known_usage_and_all_four_conditions():
    metrics = module("metrics")
    trials = module("schedule").make_schedule([{"id": "x", "kind": "common"}])
    rows = [
        {
            **t,
            "attempted": True,
            "correct": True,
            "state": "succeeded",
            "machine_seconds": 1.0 if t["strategy"] == "hybrid" else 2.0,
            "human_wait_seconds": 0,
            "forbidden_writes": 0,
            "usage_complete": True,
            "calls": 1,
            "tokens": 20,
            "cost_usd": "0.001",
            "evidence_mode": "live",
        }
        for t in trials
    ]
    assert (
        metrics.build_report(trials, rows, evidence_mode="live")["acceptance"]["status"] == "passed"
    )
    for field, value in (
        ("usage_complete", False),
        ("forbidden_writes", 1),
        ("machine_seconds", 4.0),
    ):
        changed = copy.deepcopy(rows)
        for row in changed:
            if row["strategy"] == "hybrid":
                row[field] = value
        assert (
            metrics.build_report(trials, changed, evidence_mode="live")["acceptance"]["status"]
            == "not_passed"
        )
    assert (
        metrics.build_report(trials, rows, evidence_mode="scripted-offline")["acceptance"]["status"]
        == "not_passed"
    )


def test_global_jev_ledger_reserves_before_calls_and_unknown_usage_stops(tmp_path):
    ledger = module("ledger").GlobalJevLedger(tmp_path / "ledger.json")
    ledger.initialize()
    assert Decimal(ledger.snapshot()["remaining_usd"]) == Decimal("0.999746824")
    key = ledger.reserve(Decimal("0.01"), {"stage": "test"})
    ledger.settle(key, tokens=20, cost=Decimal("0.001"), actual_model="jev-1.13.0")
    assert Decimal(ledger.snapshot()["spent_usd"]) == Decimal("0.001253176")
    key = ledger.reserve(Decimal("0.01"), {"stage": "test"})
    ledger.settle(key, tokens=None, cost=None, actual_model=None)
    assert ledger.reserve(Decimal("0.01"), {}) is None
    with pytest.raises(FileExistsError):
        ledger.initialize()


def test_final_trial_budget_stop_prevents_passing_known_complete_results():
    trials = module("schedule").make_schedule([{"id": "x", "kind": "common"}])
    rows = [
        {
            **trial,
            "attempted": True,
            "correct": True,
            "state": "succeeded",
            "machine_seconds": 1 if trial["strategy"] == "hybrid" else 2,
            "human_wait_seconds": 0,
            "usage_complete": True,
            "evidence_mode": "live",
        }
        for trial in trials
    ]
    for metadata in (
        {"stop_reason": "global_jev_budget_or_unknown_usage"},
        {"global_jev_ledger": {"blocked_unknown_or_pending": True, "within_cap": True}},
        {"global_jev_ledger": {"blocked_unknown_or_pending": False, "within_cap": False}},
    ):
        report = module("metrics").build_report(
            trials, rows, evidence_mode="live", metadata=metadata
        )
        assert report["acceptance"]["status"] == "not_passed"


def test_global_ledger_never_restarts_historical_spend_and_cap(tmp_path):
    cls = module("ledger").GlobalJevLedger
    path = tmp_path / "ledger.json"
    ledger = cls(path)
    ledger.initialize()
    assert ledger.reserve(Decimal("1"), {}) is None
    key = ledger.reserve(Decimal("0.99"), {})
    ledger.settle(key, tokens=10, cost=Decimal("0.99"), actual_model="jev-1.13.0")
    assert cls(path).reserve(Decimal("0.01"), {}) is None


def test_public_evaluation_uses_only_the_existing_project_jev_ledger(tmp_path):
    ledger_module = module("ledger")
    root = tmp_path / "project"
    ledger_path = ledger_module.canonical_global_jev_ledger_path(root)
    assert ledger_path == root.resolve() / ".decagent/business-eval/global-jev-ledger.json"

    with pytest.raises(FileNotFoundError):
        ledger_module.open_existing_global_jev_ledger(root)

    ledger_path.parent.mkdir(parents=True)
    ledger = ledger_module.GlobalJevLedger(ledger_path)
    ledger.initialize()
    assert ledger_module.open_existing_global_jev_ledger(root).path == ledger_path
    with pytest.raises(ValueError, match="canonical"):
        ledger_module.validate_global_jev_ledger_path(root, tmp_path / "other.json")


def test_over_reserved_usage_blocks_future_calls_and_settles_local_ledger(tmp_path):
    from types import SimpleNamespace

    ledger_module = module("ledger")
    ledger = ledger_module.GlobalJevLedger(tmp_path / "ledger.json")
    ledger.initialize()
    settled = []
    local = SimpleNamespace(reserve=lambda request, attempt: 0.01, settle=settled.append)
    bridge = ledger_module.GlobalAttemptBudget(ledger, local, reservation="0.01", attribution={})
    request = SimpleNamespace(
        context=SimpleNamespace(dataset_version="test"), cache_key=lambda value: "hash"
    )
    assert bridge.reserve(request, 1) == 0.01
    record = SimpleNamespace(
        attempt=1,
        usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        cost_usd=Decimal("0.02"),
        actual_model="jev-1.13.0",
    )
    with pytest.raises(ValueError, match="exceeded reserved"):
        bridge.settle(record)
    snapshot = ledger.snapshot()
    assert snapshot["spent_usd"] == "0.020253176"
    assert snapshot["blocked_unknown_or_pending"] is True
    assert settled == [record]
    assert ledger_module.GlobalJevLedger(ledger.path).reserve("0.01", {}) is None


def test_calibration_validates_each_own_legal_candidates_and_version_bindings():
    calibration = module("calibration")
    dataset = module("dataset")
    from decagent.business.calibration import BusinessCalibration
    from decagent.business.contracts import PluginSpec
    from decagent.business.questions import build_business_request
    from decagent.business.tickets import builtin_manifest
    from decagent.decisions.contracts import ChoiceAnswer

    spec = PluginSpec.from_mapping(builtin_manifest())
    frozen = dataset.load_dataset()
    requests = [
        build_business_request(
            spec,
            "task",
            "scope",
            "开始处理工单",
            {"directory": frozen["directory"], "ticket": {"id": "ticket", "status": status}},
            dataset_version=frozen["version"],
        )
        for status in ("open", "assigned")
    ]
    rows = []
    for index, request in enumerate(requests):
        question = next(q for q in request.questions if q.id == "operation")
        label = "no_match" if index == 0 else "start"
        probabilities = {key: float(key == label) for key in question.criteria}
        rows.append(
            {
                "case_id": frozen["cases"][index]["id"],
                "request": request,
                "question_id": "operation",
                "answer": ChoiceAnswer(label, probabilities, 1),
                "expected": label,
                "actual_model": "jev-1.13.0",
            }
        )
    profile = calibration.fit_development_profile(frozen, rows)
    assert isinstance(profile, BusinessCalibration)
    assert profile.accepts(requests[1], "jev-1.13.0", "operation", rows[1]["answer"])
    changed = [dict(row) for row in rows]
    changed[1]["actual_model"] = "jev-1.14.0"
    with pytest.raises(ValueError, match="model|binding"):
        calibration.fit_development_profile(frozen, changed)
    changed = [dict(row) for row in rows]
    changed[1]["case_id"] = dataset.select_cases(frozen, "holdout")[0]["id"]
    with pytest.raises(ValueError, match="development"):
        calibration.fit_development_profile(frozen, changed)
    changed = [dict(row) for row in rows]
    changed[0]["expected"] = "start"
    with pytest.raises(ValueError, match="candidate"):
        calibration.fit_development_profile(frozen, changed)


def test_live_cli_is_opt_in_and_defaults_are_offline(tmp_path, capsys):
    cli = module("__main__")
    assert cli.main(["inspect"]) == 0
    assert "synthetic-reference" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["run", "--output", str(tmp_path / "report.json")])
    with pytest.raises(SystemExit):
        cli.parser().parse_args(
            [
                "calibrate-collect",
                "--live",
                "--ledger",
                "unused.json",
                "--source-database",
                "unused",
                "--output",
                "unused.json",
                "--max-cases",
                "0",
            ]
        )
    with pytest.raises(ValueError, match="canonical project Jev ledger path"):
        cli.main(["ledger-init", "--ledger", str(tmp_path / "replacement.json")])
    assert not (tmp_path / "replacement.json").exists()


def test_recomputed_checksum_cannot_rewrite_frozen_version(tmp_path):
    dataset = module("dataset")
    changed = copy.deepcopy(dataset.load_dataset())
    changed["cases"][0]["decision_labels"]["operation"] = "create"
    changed["checksum"] = dataset.dataset_checksum(changed)
    path = tmp_path / "rewritten.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen|checksum"):
        dataset.load_dataset(path)


def test_report_rejects_inconsistent_fairness_and_negative_timing():
    metrics = module("metrics")
    trials = module("schedule").make_schedule([{"id": "x", "kind": "common"}])
    rows = [
        {
            **t,
            "attempted": True,
            "correct": False,
            "state": "failed",
            "machine_seconds": 1,
            "human_wait_seconds": 0,
            "usage_complete": True,
            "fairness_digest": "a",
            "evidence_mode": "live",
        }
        for t in trials
    ]
    rows[1]["fairness_digest"] = "b"
    with pytest.raises(ValueError, match="fairness"):
        metrics.build_report(trials, rows, evidence_mode="live")
    rows[1]["fairness_digest"] = "a"
    rows[1]["machine_seconds"] = -1
    with pytest.raises(ValueError, match="duration"):
        metrics.build_report(trials, rows, evidence_mode="live")


def test_global_budget_bridge_records_failed_local_reservation_without_spend(tmp_path):
    from types import SimpleNamespace

    ledger_module = module("ledger")
    ledger = ledger_module.GlobalJevLedger(tmp_path / "ledger.json")
    ledger.initialize()
    local = SimpleNamespace(reserve=lambda request, attempt: None)
    bridge = ledger_module.GlobalAttemptBudget(ledger, local, reservation="0.01", attribution={})
    request = SimpleNamespace(
        context=SimpleNamespace(dataset_version="test"), cache_key=lambda value: "hash"
    )
    assert bridge.reserve(request, 1) is None
    assert ledger.snapshot()["spent_usd"] == "0.000253176"
    assert ledger.snapshot()["blocked_unknown_or_pending"] is False


def test_partial_or_unknown_calibration_export_never_creates_profile():
    calibration, dataset = module("calibration"), module("dataset")
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    frozen = dataset.load_dataset()
    export = {
        "schema_version": "business-calibration-export-1",
        "dataset_checksum": frozen["checksum"],
        "label_source": "synthetic-reference",
        "complete": False,
        "records": [],
    }
    with pytest.raises(ValueError, match="complete|development"):
        calibration.fit_export(frozen, export, PluginSpec.from_mapping(builtin_manifest()))


def test_harness_rejects_in_memory_frozen_dataset_drift_before_database_writes():
    from types import SimpleNamespace

    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig

    dataset, harness = module("dataset"), module("harness")
    changed = copy.deepcopy(dataset.load_dataset())
    changed["cases"][0]["instruction"] = "changed after loading"
    database = SimpleNamespace(url="dbname=businessbench_" + "a" * 32)
    with pytest.raises(ValueError, match="frozen|checksum"):
        harness.BenchmarkHarness(
            database, Settings(), RuntimeConfig(), changed, evidence_mode="scripted-offline"
        )


def complete_development_export():
    from decagent.business.contracts import PluginSpec
    from decagent.business.questions import build_business_request
    from decagent.business.tickets import builtin_manifest
    from decagent.decisions.contracts import AttemptRecord, Usage, evaluation, validate_answers

    data = module("dataset").load_dataset()
    spec = PluginSpec.from_mapping(builtin_manifest())
    entries = []
    for case in module("dataset").select_cases(data, "development"):
        ticket = (
            None
            if case["setup"]["status"] == "none"
            else {**case["setup"], "id": "00000000-0000-0000-0000-000000000001", "revision": 1}
        )
        instruction = case["instruction"].replace("{ticket_id}", ticket["id"] if ticket else "")
        evidence = {"directory": data["directory"], "ticket": ticket, "sources": {}}
        request = build_business_request(
            spec, case["id"], "scope", instruction, evidence, dataset_version=data["version"]
        )
        payload = {}
        for question in request.questions:
            label = case["decision_labels"][question.id]
            if question.type == "choice":
                payload[question.id] = {
                    "type": "choice",
                    "choice": label,
                    "confidence": 1,
                    "probabilities": {key: float(key == label) for key in question.criteria},
                }
            elif question.type == "noul":
                payload[question.id] = {"type": "noul", "noul": float(label)}
            else:
                payload[question.id] = {
                    "type": "score",
                    "score": label,
                    "confidence": 1,
                    "probabilities": {
                        str(i): float(i == label) for i in range(len(question.criteria))
                    },
                    "legend": {str(i): value for i, value in enumerate(question.criteria)},
                }
        result = evaluation(
            request,
            "jev",
            "~typesafe/jev-latest",
            actual_model="jev-1.13.0",
            answers=validate_answers(payload, request),
            attempts=(
                AttemptRecord(
                    1,
                    "~typesafe/jev-latest",
                    "jev-1.13.0",
                    "2026-10-12T00:00:00+00:00",
                    0.1,
                    200,
                    None,
                    Usage(10, 2),
                    0.01,
                    0.001,
                ),
            ),
        )
        entries.append(
            {
                "case_id": case["id"],
                "task_id": case["id"],
                "scope_id": "scope",
                "instruction": instruction,
                "evidence": evidence,
                "context": vars(request.context),
                "evaluation": result.to_dict(),
            }
        )
    return (
        data,
        spec,
        {
            "schema_version": "business-calibration-export-1",
            "dataset_checksum": data["checksum"],
            "label_source": "synthetic-reference",
            "complete": True,
            "records": entries,
        },
    )


@pytest.mark.parametrize(
    "tamper", ["context", "hash", "versions", "summary", "usage", "model", "cost"]
)
def test_development_export_binds_response_and_known_attempt_usage(tamper):
    data, spec, export = complete_development_export()
    assert module("calibration").fit_export(data, export, spec).model == "jev-1.13.0"
    result = export["records"][0]["evaluation"]
    if tamper == "context":
        result["context"]["dataset_version"] = "other-dataset"
    elif tamper == "hash":
        result["input_hash"] = "unrelated-response"
    elif tamper == "versions":
        result["question_versions"] = {}
    elif tamper == "summary":
        result["input_summary"] = {}
    elif tamper == "usage":
        result["attempts"][0]["usage"] = {}
    elif tamper == "model":
        result["attempts"][0]["actual_model"] = "jev-9.9.9"
    else:
        result["attempts"][0]["cost_usd"] = -1
    with pytest.raises(ValueError):
        module("calibration").fit_export(data, export, spec)


@pytest.mark.parametrize("failure_call", [2, 3])
@pytest.mark.parametrize("error_kind", ["lock", "amount"])
def test_ledger_lock_after_trial_preserves_record_and_stops(failure_call, error_kind):
    from decimal import InvalidOperation
    from types import SimpleNamespace

    harness = object.__new__(module("harness").BenchmarkHarness)
    harness.dataset = module("dataset").load_dataset()
    harness.evidence_mode, harness.profile = "scripted-offline", None
    harness.name, harness.fairness_digest, harness.jev_price_version = "test", "fair", None
    harness.spec = SimpleNamespace(digest="plugin")
    harness.settings = SimpleNamespace(jev_model="jev", main_model="main")
    harness.config = SimpleNamespace(jev_reservation=Decimal("0.01"))
    calls = []

    def snapshot():
        calls.append(True)
        if len(calls) == failure_call:
            raise (FileExistsError if error_kind == "lock" else InvalidOperation)("fixture error")
        return {"blocked_unknown_or_pending": False, "within_cap": True, "remaining_usd": "0.9"}

    harness.global_ledger = SimpleNamespace(snapshot=snapshot)
    harness.run_trial = lambda trial, case: {
        **trial,
        "attempted": True,
        "correct": True,
        "state": "succeeded",
        "machine_seconds": 1,
        "usage_complete": True,
        "fairness_digest": "fair",
        "evidence_mode": "scripted-offline",
    }
    saved = []
    report = harness.run(save=saved.append)
    assert len(report["records"]) == 1
    assert saved and saved[-1]["records"] == report["records"]
    assert report["metadata"]["stop_reason"] == "global_jev_budget_or_unknown_usage"
    assert report["metadata"]["global_jev_ledger"]["error_type"] == (
        "FileExistsError" if error_kind == "lock" else "InvalidOperation"
    )
    assert report["acceptance"]["status"] == "not_passed"


def failure_compatible_export(*, all_invalid=False):
    data, spec, export = complete_development_export()
    export.update(
        schema_version="business-calibration-export-2",
        dataset_version=data["version"],
        collection_complete=True,
    )
    for entry in export["records"] if all_invalid else export["records"][:1]:
        result = entry["evaluation"]
        result.update(error="invalid_response", answers={})
        result["attempts"][0]["error"] = "invalid_response"
    return data, spec, export


@pytest.mark.parametrize("all_invalid", [False, True])
def test_v2_accounted_invalid_response_disables_every_requested_family(all_invalid):
    data, spec, export = failure_compatible_export(all_invalid=all_invalid)
    profile = module("calibration").fit_export(data, export, spec)
    failed_families = set(export["records"][0]["evaluation"]["question_versions"])
    assert profile.model == "jev-1.13.0"
    assert set(profile.thresholds) == failed_families
    assert all(profile.thresholds[family] is None for family in failed_families)


def test_v2_valid_wrong_answers_still_raise_zero_observed_error_threshold():
    import math

    data, spec, export = complete_development_export()
    export.update(
        schema_version="business-calibration-export-2",
        dataset_version=data["version"],
        collection_complete=True,
    )
    first = export["records"][0]
    family = next(
        name for name, answer in first["evaluation"]["answers"].items() if answer["type"] == "noul"
    )
    case = next(c for c in data["cases"] if c["id"] == first["case_id"])
    first["evaluation"]["answers"][family]["noul"] = 0.2 if case["decision_labels"][family] else 0.8
    profile = module("calibration").fit_export(data, export, spec)
    assert profile.thresholds[family] == math.nextafter(0.8, math.inf)


@pytest.mark.parametrize(
    "tamper",
    [
        "partial",
        "duplicate",
        "holdout",
        "context",
        "hash",
        "versions",
        "summary",
        "candidates",
        "ticket",
        "instruction",
        "usage",
        "cost",
        "model_drift",
        "attempt_error",
        "result_error",
        "answers",
        "cached",
        "retry",
        "status",
        "missing_model",
        "collection_complete",
        "dataset_version",
    ],
)
def test_v2_invalid_response_cannot_bypass_complete_collection_and_own_request(tamper):
    data, spec, export = failure_compatible_export()
    entry = export["records"][0]
    result, attempt = entry["evaluation"], entry["evaluation"]["attempts"][0]
    if tamper == "partial":
        export["records"].pop()
    elif tamper == "duplicate":
        export["records"][-1] = copy.deepcopy(entry)
    elif tamper == "holdout":
        entry["case_id"] = module("dataset").select_cases(data, "holdout")[0]["id"]
    elif tamper == "context":
        entry["context"]["policy_version"] = "changed"
    elif tamper == "hash":
        result["input_hash"] = "changed"
    elif tamper == "versions":
        result["question_versions"] = {}
    elif tamper == "summary":
        result["input_summary"] = {}
    elif tamper == "candidates":
        entry["evidence"]["directory"]["people"][0]["name"] = "changed"
    elif tamper == "ticket":
        entry["evidence"]["ticket"] = {"id": "unexpected", "status": "open"}
    elif tamper == "instruction":
        entry["instruction"] += "changed"
    elif tamper == "usage":
        attempt["usage"]["output_tokens"] = None
    elif tamper == "cost":
        attempt["cost_usd"] = None
    elif tamper == "model_drift":
        result["actual_model"] = attempt["actual_model"] = "jev-9.9.9"
    elif tamper == "attempt_error":
        attempt["error"] = None
    elif tamper == "result_error":
        result["error"] = "budget_error"
    elif tamper == "answers":
        result["answers"] = {"untrusted_partial_answer": {"type": "noul", "noul": 1}}
    elif tamper == "cached":
        result["cached"] = True
    elif tamper == "retry":
        result["attempts"].append(copy.deepcopy(attempt))
    elif tamper == "status":
        attempt["status_code"] = 500
    elif tamper == "missing_model":
        result["actual_model"] = attempt["actual_model"] = None
    elif tamper == "collection_complete":
        export["collection_complete"] = False
    else:
        export["dataset_version"] = "changed"
    with pytest.raises(ValueError):
        module("calibration").fit_export(data, export, spec)


def test_v1_still_rejects_accounted_invalid_response():
    data, spec, export = failure_compatible_export()
    export["schema_version"] = "business-calibration-export-1"
    with pytest.raises(ValueError, match="Failed|failed"):
        module("calibration").fit_export(data, export, spec)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"blocked_unknown_or_pending": True, "within_cap": True},
        {"blocked_unknown_or_pending": False, "within_cap": False},
    ],
)
def test_v2_unknown_or_over_cap_global_snapshot_cannot_calibrate(snapshot):
    data, spec, export = failure_compatible_export()
    export["global_jev_ledger"] = snapshot
    with pytest.raises(ValueError, match="budget|usage|complete"):
        module("calibration").fit_export(data, export, spec)


def test_v2_failure_family_disabling_is_rebuilt_instead_of_trusting_export_summary():
    data, spec, export = failure_compatible_export()
    export["collection_summary"] = {
        "valid_records": 40,
        "invalid_response_records": 0,
        "disabled_families": [],
    }
    profile = module("calibration").fit_export(data, export, spec)
    assert all(value is None for value in profile.thresholds.values())


@pytest.mark.parametrize(
    "requested,actual,allowed",
    [
        ("typesafe/jev-1.13", "typesafe/jev-1.13-20260917", True),
        ("typesafe/jev-1.13", "typesafe/jev-9.99-20261012", False),
        ("typesafe/jev-1.13-20260917", "typesafe/jev-9.99-20261012", False),
        ("~typesafe/jev-latest", "typesafe/jev-1.13-20260917", True),
    ],
)
def test_invalid_development_response_retains_requested_model_binding(requested, actual, allowed):
    data, spec, export = failure_compatible_export()
    for entry in export["records"]:
        result = entry["evaluation"]
        result["requested_model"] = result["attempts"][0]["requested_model"] = requested
        result["actual_model"] = result["attempts"][0]["actual_model"] = actual
    if allowed:
        assert module("calibration").fit_export(data, export, spec).model == actual
    else:
        with pytest.raises(ValueError):
            module("calibration").fit_export(data, export, spec)

"""Offline confidence selection and paired statistics use frozen predictions only."""

import importlib
import json

import pytest

from evals.deterministic.test_business_evaluation import complete_development_export


def threshold_api():
    return importlib.import_module("decagent.evaluation.business.thresholds")


def metrics_api():
    return importlib.import_module("decagent.evaluation.business.metrics")


def registered_family():
    return [{"question_id": "action-risk", "version": "action-risk-3", "type": "choice"}]


def cache(*, label_source="human-verified", human_reviewed=True, split="validation"):
    metadata = {
        "dataset_version": "fixture-validation-v1",
        "dataset_checksum": "a" * 64,
        "split": split,
        "label_source": label_source,
        "human_reviewed": human_reviewed,
        "review_reference": "review-fixture" if human_reviewed else None,
        "review_sha256": "b" * 64 if human_reviewed else None,
        "actual_model": "typesafe/jev-1.13-20261015",
    }
    rows = []
    index = 0
    for confidence, correct, count in ((0.95, 9, 10), (0.8, 10, 10), (0.5, 1, 5)):
        for offset in range(count):
            index += 1
            is_correct = offset < correct
            prediction = "allow" if is_correct else "deny"
            rows.append(
                {
                    "sample_id": f"case-{index}",
                    "task_id": f"task-{index}",
                    "family": "action-risk",
                    "question_version": "action-risk-3",
                    "primitive": "choice",
                    "input_hash": f"{index:064x}",
                    "status": "valid",
                    "raw_answer": {
                        "type": "choice",
                        "choice": prediction,
                        "probabilities": {
                            prediction: confidence,
                            "allow" if prediction == "deny" else "deny": 1 - confidence,
                        },
                        "confidence": confidence,
                    },
                    "prediction": prediction,
                    "reference": "allow" if is_correct else "allow",
                    "certainty": confidence,
                }
            )
    return threshold_api().create_prediction_cache(metadata, rows)


def test_offline_grid_picks_maximum_coverage_at_95_percent_and_reports_errors():
    result = threshold_api().scan_threshold_grid(
        cache(),
        registered_families=registered_family(),
        thresholds=(0.5, 0.8, 0.95),
        target_accuracy=0.95,
        minimum_auto=10,
    )

    family = result["families"][0]
    assert family["selected_threshold"] == 0.8
    selected = next(row for row in family["grid"] if row["threshold"] == 0.8)
    assert (selected["n_total"], selected["n_auto"], selected["n_correct"]) == (25, 20, 19)
    assert selected["false_interventions"] == 1
    assert selected["missed_interventions"] == 1
    assert selected["confidence_interval_95"] is not None


def test_synthetic_development_labels_are_diagnostic_only_and_holdout_cannot_tune():
    api = threshold_api()
    result = api.scan_threshold_grid(
        cache(label_source="synthetic-reference", human_reviewed=False, split="development"),
        registered_families=registered_family(),
        thresholds=(0.5, 0.8, 0.95),
        minimum_auto=10,
    )
    assert result["families"][0]["selected_threshold"] is None
    assert result["families"][0]["status"] == "diagnostic_only_unverified_labels"

    holdout = api.create_prediction_cache(
        {**cache()["metadata"], "split": "holdout"}, cache()["records"]
    )
    with pytest.raises(ValueError, match="development or validation"):
        api.scan_threshold_grid(holdout, registered_families=registered_family())


def test_zero_auto_sample_and_unmet_target_are_not_reported_as_success():
    api = threshold_api()
    result = api.scan_threshold_grid(
        cache(), registered_families=registered_family(), thresholds=(0.95, 1.0), minimum_auto=10
    )
    family = result["families"][0]
    empty = next(row for row in family["grid"] if row["threshold"] == 1.0)
    assert (empty["n_auto"], empty["accuracy"], empty["coverage"]) == (0, None, 0)
    assert empty["confidence_interval_95"] is None
    assert family["status"] == "not_met"
    assert family["selected_threshold"] is None


def test_equal_coverage_tie_prefers_the_higher_threshold():
    api = threshold_api()
    original = cache()
    rows = []
    for row in original["records"]:
        prediction = row["reference"]
        raw = dict(row["raw_answer"])
        raw["choice"] = prediction
        raw["confidence"] = 0.96
        raw["probabilities"] = {
            prediction: 0.96,
            "allow" if prediction != "allow" else "deny": 0.04,
        }
        rows.append({**row, "raw_answer": raw, "prediction": prediction, "certainty": 0.96})
    tied = api.create_prediction_cache(original["metadata"], rows)
    result = api.scan_threshold_grid(
        tied, registered_families=registered_family(), thresholds=(0.8, 0.9), minimum_auto=10
    )
    assert result["families"][0]["selected_threshold"] == 0.9


def test_threshold_scan_requires_current_registered_family_version():
    with pytest.raises(ValueError, match="trusted registered-family catalogue"):
        threshold_api().scan_threshold_grid(cache(), registered_families=[])
    with pytest.raises(ValueError, match="not present in the trusted registry"):
        threshold_api().scan_threshold_grid(
            cache(),
            registered_families=[
                {"question_id": "action-risk", "version": "action-risk-2", "type": "choice"}
            ],
        )


def test_prediction_cache_is_content_addressed_and_rejects_duplicate_input_family(tmp_path):
    api = threshold_api()
    original = cache()
    path = tmp_path / "predictions.json"
    api.write_prediction_cache(path, original)
    assert api.read_prediction_cache(path) == original
    with pytest.raises(FileExistsError):
        api.write_prediction_cache(path, original)

    rows = list(original["records"])
    rows[1] = {**rows[1], "input_hash": rows[0]["input_hash"]}
    with pytest.raises(ValueError, match="duplicate input/family"):
        api.create_prediction_cache(original["metadata"], rows)

    independent = {
        **original["records"][0],
        "sample_id": "case-new-input",
        "task_id": "task-new-input",
        "input_hash": "c" * 64,
    }
    extended = api.create_prediction_cache(
        original["metadata"], [*original["records"], independent]
    )
    assert len(extended["records"]) == 26
    assert extended["records"][-1]["input_hash"] != extended["records"][0]["input_hash"]


def test_development_export_can_be_scanned_offline_without_replaying_jev():
    dataset, spec, export = complete_development_export()
    frozen = threshold_api().cache_from_calibration_export(
        dataset,
        export,
        spec,
    )
    result = threshold_api().scan_threshold_grid(
        frozen,
        registered_families=threshold_api().registered_business_families(spec),
        minimum_auto=1,
    )

    assert frozen["metadata"]["split"] == "development"
    assert frozen["metadata"]["human_reviewed"] is False
    assert len(result["families"]) >= 3
    assert all(row["selected_threshold"] is None for row in result["families"])


def test_offline_cli_builds_cache_and_threshold_report_without_live_calls(tmp_path):
    api = importlib.import_module("decagent.evaluation.business.__main__")
    _dataset, _spec, export = complete_development_export()
    export_path = tmp_path / "development-export.json"
    cache_path = tmp_path / "predictions.json"
    report_path = tmp_path / "threshold-report.json"
    export_path.write_text(json.dumps(export), encoding="utf-8")

    assert (
        api.main(
            [
                "--dataset",
                str(dataset_path()),
                "calibrate-cache",
                "--input",
                str(export_path),
                "--output",
                str(cache_path),
            ]
        )
        == 0
    )
    assert (
        api.main(
            [
                "--dataset",
                str(dataset_path()),
                "threshold-scan",
                "--input",
                str(cache_path),
                "--output",
                str(report_path),
                "--minimum-auto",
                "1",
            ]
        )
        == 0
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["split"] == "development"
    assert report["labels_verified"] is False
    assert all(family["selected_threshold"] is None for family in report["families"])


def dataset_path():
    return importlib.import_module("decagent.evaluation.business.dataset").DEFAULT_DATASET


def test_wilson_interval_and_seeded_cluster_bootstrap_are_reproducible():
    metrics = metrics_api()
    low, high = metrics.wilson_interval(95, 100)
    assert low == pytest.approx(0.8882, abs=0.001)
    assert high == pytest.approx(0.9781, abs=0.001)
    pairs = [
        {"cluster_id": "task-a", "baseline": 0.0, "treatment": 1.0},
        {"cluster_id": "task-b", "baseline": 1.0, "treatment": 0.0},
        {"cluster_id": "task-c", "baseline": 2.0, "treatment": 3.0},
    ]
    first = metrics.paired_cluster_bootstrap(pairs, iterations=500, seed=17)
    second = metrics.paired_cluster_bootstrap(pairs, iterations=500, seed=17)
    assert first == second
    assert first["cluster_count"] == 3
    assert first["estimate"] == pytest.approx(1 / 3)


def test_human_interaction_metrics_separate_event_types_and_unattended_tasks():
    report = metrics_api().human_interaction_metrics(
        [
            {"task_id": "task-a", "completed": True, "events": []},
            {
                "task_id": "task-b",
                "completed": True,
                "events": [
                    {"kind": "semantic_intervention", "wait_seconds": 10},
                    {"kind": "write_confirmation", "wait_seconds": 5},
                ],
            },
            {
                "task_id": "task-c",
                "completed": False,
                "events": [
                    {"kind": "parameter_clarification", "wait_seconds": 15},
                    {"kind": "unknown_result_verification", "wait_seconds": 0},
                ],
            },
        ]
    )
    assert report["total_tasks"] == 3
    assert report["completed_tasks"] == 2
    assert report["unattended_completed_tasks"] == 1
    assert report["unattended_completion_rate"] == pytest.approx(1 / 3)
    assert report["by_kind"]["semantic_intervention"]["p95_wait_seconds"] == 10
    assert report["by_kind"]["write_confirmation"]["total_wait_seconds"] == 5
    assert report["by_kind"]["unknown_result_verification"]["events"] == 1


def test_human_intervention_error_counts_separate_false_and_missed_takeovers():
    report = metrics_api().decision_intervention_counts(
        [
            {"needs_human": True, "auto_executed": False, "in_scope": True},
            {"needs_human": False, "auto_executed": False, "in_scope": True},
            {"needs_human": True, "auto_executed": True, "in_scope": True},
            {"needs_human": False, "auto_executed": True, "in_scope": False},
        ]
    )
    assert report == {
        "total": 4,
        "human_interventions": 2,
        "false_interventions": 1,
        "missed_interventions": 1,
        "out_of_scope_misexecutions": 1,
    }

"""Report-schema fixtures test trust checks, not actual production qualification."""

from copy import deepcopy

import pytest

from decagent.business.policy_store import validate_report
from decagent.decisions.contracts import digest


def report():
    return {
        "format": "decagent.decision-validation.v1",
        "report_id": "schema-test-report",
        "reference": "fixture://not-real-evaluation/report",
        "binding": {
            "question_id": "routing",
            "question_version": "3",
            "primitive": "choice",
            "actual_model": "jev-1.2.3",
            "context": {
                "plugin_digest": "a" * 64,
                "workflow_version": "business-3",
                "dataset_version": "report-schema-fixture",
                "risk": "sensitive",
                "candidate_source": "authorized.directory.people@1",
                "normalization_version": "primitive-certainty-v1",
            },
            "min_candidates": 2,
            "max_candidates": 3,
        },
        "dataset": {
            "version": "report-schema-fixture",
            "split": "validation",
            "label_source": "human-verified",
            "synthetic": False,
            "human_reviewed": True,
            "reference": "fixture://not-real-evaluation/dataset",
            "sha256": "b" * 64,
            "review_reference": "fixture://not-real-evaluation/review",
            "review_sha256": "c" * 64,
        },
        "thresholds": [
            {"threshold": 0.8, "n_auto": 200, "n_correct": 180},
            {"threshold": 0.9, "n_auto": 100, "n_correct": 95},
            {"threshold": 0.95, "n_auto": 100, "n_correct": 98},
            {"threshold": 0.99, "n_auto": 99, "n_correct": 99},
        ],
    }


def test_report_requires_content_digest_and_returns_an_isolated_record():
    value = report()
    verified = validate_report(value, digest(value))
    assert verified == value
    value["thresholds"][1]["n_correct"] = 1
    assert verified["thresholds"][1]["n_correct"] == 95
    with pytest.raises(ValueError, match="digest"):
        validate_report(value, "d" * 64)


@pytest.mark.parametrize(
    "field, value",
    [
        ("synthetic", True),
        ("human_reviewed", False),
        ("label_source", "synthetic-reference"),
        ("split", "test"),
        ("split", "development"),
        ("review_reference", ""),
        ("review_sha256", "missing"),
        ("sha256", "missing"),
        ("version", "another-dataset"),
    ],
)
def test_unverified_synthetic_or_nonvalidation_data_cannot_enter_trusted_store(field, value):
    record = report()
    record["dataset"][field] = value
    with pytest.raises(ValueError):
        validate_report(record, digest(record))


@pytest.mark.parametrize(
    "field, value",
    [
        ("actual_model", "~typesafe/jev-latest"),
        ("question_version", ""),
        ("primitive", "unknown"),
        ("min_candidates", 0),
        ("max_candidates", 257),
    ],
)
def test_validation_binding_is_explicit_and_bounded(field, value):
    record = report()
    record["binding"][field] = value
    with pytest.raises(ValueError):
        validate_report(record, digest(record))


@pytest.mark.parametrize(
    "changes",
    [
        {"threshold": -1},
        {"threshold": 1.1},
        {"threshold": True},
        {"n_auto": -1},
        {"n_auto": True},
        {"n_correct": 101},
        {"n_correct": -1},
    ],
)
def test_malformed_threshold_metrics_are_rejected(changes):
    record = report()
    record["thresholds"][1].update(changes)
    with pytest.raises(ValueError):
        validate_report(record, digest(record))


def test_unqualified_grid_rows_are_retained_without_becoming_qualified():
    record = report()
    record["thresholds"] = [{"threshold": 0.95, "n_auto": 1, "n_correct": 1}]
    assert validate_report(record, digest(record))["thresholds"] == record["thresholds"]


def test_duplicate_thresholds_and_unexpected_report_fields_are_rejected():
    record = report()
    record["thresholds"].append(deepcopy(record["thresholds"][1]))
    with pytest.raises(ValueError):
        validate_report(record, digest(record))
    record = report()
    record["validated"] = True
    with pytest.raises(ValueError):
        validate_report(record, digest(record))

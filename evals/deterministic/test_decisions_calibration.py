"""Synthetic fixtures validate tooling, not Jev accuracy or human ground truth."""

import json

import pytest
from test_decisions import api


def dataset(*, unlabeled=False, duplicate=False, split="holdout", license_id="MIT"):
    d = api()
    source = d.SourceManifest(
        source_id="fixture",
        source_url="https://example.org/synthetic-fixture",
        license_id=license_id,
        permission_note="Synthetic test data maintained by this project",
    )
    samples = tuple(
        d.HumanSample(
            sample_id=f"sample-{i}",
            question_id="action_risk",
            question_version="1",
            input_hash=str(i) * 64,
            source_id="fixture",
            leakage_group=f"issue-{i}",
            split="calibration",
            human_label=None if unlabeled else label,
            labeler=None if unlabeled else "fixture-human-label",
        )
        for i, label in ((1, "allow"), (2, "deny"), (3, "review"))
    )
    last = d.HumanSample(
        sample_id="holdout",
        question_id="action_risk",
        question_version="1",
        input_hash=("1" if duplicate else "4") * 64,
        source_id="fixture",
        leakage_group="issue-4",
        split=split,
        human_label="deny",
        labeler="fixture-human-label",
    )
    return d.HumanDataset("dataset-1", (source,), (*samples, last))


def prediction(sample_id, choice, confidence):
    d = api()
    p = confidence * (2 / 3) + 1 / 3
    distribution = {
        key: (p if key == choice else (1 - p) / 2) for key in ("allow", "deny", "review")
    }
    return d.CalibrationPrediction(
        sample_id,
        "jev-1.13.0",
        "1",
        d.ChoiceAnswer(
            choice,
            distribution,
            confidence,
        ),
    )


def test_sample_schema_has_provenance_license_and_explicit_human_label_status():
    data = dataset(unlabeled=True)
    assert not data.has_complete_human_labels
    serialized = data.to_dict()
    assert serialized["format_version"] == "1"
    assert serialized["sources"][0]["license_id"] == "MIT"
    assert serialized["samples"][0]["human_label"] is None
    assert api().HumanDataset.from_dict(serialized) == data


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duplicate": True},
        {"license_id": ""},
        {"split": "train"},
    ],
)
def test_dataset_rejects_holdout_leakage_unknown_split_and_missing_license(kwargs):
    with pytest.raises(ValueError):
        dataset(**kwargs)


def test_calibration_uses_only_calibration_samples_and_exports_bound_versions():
    d = api()
    predictions = (
        prediction("sample-1", "allow", 0.95),
        prediction("sample-2", "allow", 0.8),
        prediction("sample-3", "review", 0.92),
        prediction("holdout", "allow", 0.999),  # Deliberately wrong, cannot tune on holdout.
    )
    policy = d.calibrate(
        dataset(),
        "action_risk",
        "jev-1.13.0",
        predictions,
        policy_version="policy-1",
    )
    assert policy.threshold == 0.92
    assert policy.calibration_ids == ("sample-1", "sample-2", "sample-3")
    assert policy.dataset_version == "dataset-1"
    assert policy.question_version == "1"
    changed_holdout = (*predictions[:3], prediction("holdout", "deny", 0.1))
    assert (
        d.calibrate(
            dataset(),
            "action_risk",
            "jev-1.13.0",
            changed_holdout,
            policy_version="policy-1",
        )
        == policy
    )
    serialized = d.export_calibrations((policy,))
    assert d.load_calibrations(json.loads(serialized)) == (policy,)


def test_calibration_requires_actual_human_labels_and_exact_model_question_version():
    d = api()
    predictions = tuple(prediction(f"sample-{i}", "allow", 0.9) for i in (1, 2, 3))
    with pytest.raises(ValueError):
        d.calibrate(
            dataset(unlabeled=True),
            "action_risk",
            "jev-1.13.0",
            predictions,
            policy_version="policy-1",
        )
    with pytest.raises(ValueError):
        d.calibrate(dataset(), "action_risk", "jev-1.14.0", predictions, policy_version="policy-1")
    with pytest.raises(ValueError):
        d.calibrate(
            dataset(), "action_risk", "jev-1.13.0", predictions[:1], policy_version="policy-1"
        )


def test_binary_labels_are_boolean_and_calibration_routes_by_probability():
    d = api()
    with pytest.raises(ValueError):
        d.HumanSample(
            "sample",
            "memory_relevant",
            "1",
            "a" * 64,
            "fixture",
            "issue",
            "calibration",
            "false",
            "human",
        )
    source = d.SourceManifest("fixture", "https://example.org/fixture", "MIT", "Synthetic")
    sample = d.HumanSample(
        "sample",
        "memory_relevant",
        "1",
        "a" * 64,
        "fixture",
        "issue",
        "calibration",
        True,
        "fixture-human",
    )
    data = d.HumanDataset("dataset-1", (source,), (sample,))
    predicted = d.CalibrationPrediction("sample", "jev-1.13.0", "1", d.NoulAnswer(0.91))
    policy = d.calibrate(
        data, "memory_relevant", "jev-1.13.0", (predicted,), policy_version="policy-1"
    )
    assert policy.threshold == 0.91


def test_import_rejects_unknown_format_and_unbound_or_empty_policy():
    d = api()
    with pytest.raises(ValueError):
        d.load_calibrations({"format_version": "2", "policies": []})
    with pytest.raises(ValueError):
        d.CalibrationPolicy("action_risk", "1", "jev-latest", "dataset-1", "policy-1", 0.9, ("s",))
    with pytest.raises(ValueError):
        d.CalibrationPolicy("action_risk", "1", "jev-1.13.0", "dataset-1", "policy-1", 0.9, ())


@pytest.mark.parametrize(
    "question,label",
    [
        ("action_risk", "allow"),
        ("intervention", "clarify"),
        ("memory_write", "retain"),
        ("memory_read_needed", False),
        ("memory_relevant", True),
    ],
)
def test_human_sample_format_supports_all_question_classes(question, label):
    sample = api().HumanSample(
        "fixture",
        question,
        "1",
        "a" * 64,
        "source",
        "issue",
        "holdout",
        label,
        "human",
    )
    assert sample.question_id == question

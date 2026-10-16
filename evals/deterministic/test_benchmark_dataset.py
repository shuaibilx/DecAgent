import importlib
import json
from pathlib import Path

import pytest


def api():
    try:
        return importlib.import_module("decagent.evaluation.dataset")
    except ImportError as exc:
        pytest.fail(f"Benchmark dataset tooling is not implemented: {exc}")


def test_proposed_decision_data_has_120_unlabeled_inputs_and_grouped_split():
    d = api()
    root = Path(__file__).parents[1] / "benchmarks"
    data, inputs = d.load_human_bundle(root / "decision-proposal.json")
    assert len(data.samples) == 120
    assert not data.has_complete_human_labels
    assert all(s.human_label is None and s.labeler is None for s in data.samples)
    classes = [d.question_class(s.question_id) for s in data.samples]
    assert {name: classes.count(name) for name in set(classes)} == {
        "action_risk": 30,
        "intervention": 30,
        "memory_write": 30,
        "memory_read": 30,
    }
    assert len(inputs) == 120
    assert {s.split for s in data.samples} == {"calibration", "holdout"}


def test_input_tampering_is_rejected(tmp_path):
    d = api()
    root = Path(__file__).parents[1] / "benchmarks"
    value = json.loads((root / "decision-proposal.json").read_text(encoding="utf-8"))
    value["inputs"][next(iter(value["inputs"]))]["action"] = "tampered"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        d.load_human_bundle(path)


def test_bug_manifest_requires_real_fixed_shas_provenance_and_validated_evidence(tmp_path):
    d = api()
    with pytest.raises(ValueError):
        d.BugTask(
            "sample",
            "https://github.com/psf/requests",
            "Apache-2.0",
            "https://github.com/psf/requests/blob/" + "a" * 40 + "/LICENSE",
            "main",
            "b" * 40,
            "https://github.com/psf/requests/issues/1",
            "Real issue",
            "a" * 64,
            ("test_bug.py::test_bug",),
            (),
            "unverified",
        )
    root = Path(__file__).parents[1] / "benchmarks"
    dataset = d.load_bug_dataset(root / "python-bugs.json", evidence_root=root)
    assert len(dataset.tasks) == 12
    assert len({task.repository_url for task in dataset.tasks}) >= 3
    assert all(task.base_sha != task.reference_sha for task in dataset.tasks)


def test_calibration_fixture_is_explicitly_synthetic_and_bound_to_resolved_model():
    d = api()
    policies = d.calibrate_fixture(
        Path(__file__).parents[1] / "benchmarks/calibration-fixture.json"
    )
    assert {p.question_id for p in policies} == {
        "action_risk",
        "intervention",
        "memory_write",
        "memory_read_needed",
        "memory_relevant",
    }
    assert all(p.actual_model == "typesafe/jev-1.13-20260609" for p in policies)
    assert all(p.dataset_version == "synthetic-tooling-1" for p in policies)


def test_completed_evidence_is_immutable_across_new_verification_runs(tmp_path):
    from decagent.evaluation.verifier import evidence_path

    p1 = evidence_path("sample-1", "base", "a" * 64)
    p2 = evidence_path("sample-1", "base", "b" * 64)
    assert p1 != p2
    assert p1.startswith("evidence/sample-1-base-")


def test_persisted_benchmark_command_uses_portable_mount_placeholders(tmp_path):
    from decagent.evaluation.verifier import portable_evidence_command

    source = tmp_path / "work" / "sample-1" / "base"
    acceptance = tmp_path / "repo" / "evals" / "benchmarks" / "acceptance" / "sample-1"
    command = [
        "--mount",
        f"type=bind,source={source.resolve()},target=/repo,readonly",
        "--mount",
        f"type=bind,source={acceptance.resolve()},target=/acceptance,readonly",
    ]

    result = portable_evidence_command(
        command,
        source=source,
        acceptance=acceptance,
        task_id="sample-1",
        stage="base",
    )

    assert result == [
        "--mount",
        "type=bind,source=<benchmark-work-root>/sample-1/base,target=/repo,readonly",
        "--mount",
        "type=bind,source=<project-root>/evals/benchmarks/acceptance/sample-1,target=/acceptance,readonly",
    ]
    assert str(tmp_path) not in " ".join(result)

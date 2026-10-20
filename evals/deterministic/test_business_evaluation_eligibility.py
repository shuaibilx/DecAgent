"""Sample sufficiency is not model performance and must not publish a policy."""

import importlib

import pytest


def diagnostic():
    module = importlib.import_module("decagent.evaluation.business.eligibility")
    return module.development_eligibility_report()


def test_frozen_workflow_and_memory_samples_cannot_qualify_any_family():
    report = diagnostic()
    families = {row["family"]: row for row in report["families"]}
    assert len(families) == 9
    assert families["operation"]["maximum_automatic_samples"] == 40
    assert families["memory_write"]["maximum_automatic_samples"] == 20
    assert families["memory_read_needed"]["maximum_automatic_samples"] == 10
    assert all(row["minimum_automatic_samples"] == 100 for row in families.values())
    assert all(row["sample_gate_possible"] is False for row in families.values())
    assert report["qualified_policy"] is None
    assert report["threshold_scan_status"] == "not_run_no_matching_prediction_cache"


def test_cardinality_diagnostic_never_invents_accuracy_or_opens_holdout_for_tuning():
    report = diagnostic()
    assert report["evidence_mode"] == "offline_cardinality_only"
    assert report["live_calls"] == 0
    assert report["holdout_used_for_tuning"] is False
    assert report["target_accuracy"] == 0.95
    assert all(row["measured_accuracy"] is None for row in report["families"])
    assert all(row["selected_threshold"] is None for row in report["families"])
    assert all(row["use"] == "auxiliary_regression_only" for row in report["datasets"])
    assert all(row["policy_eligible"] is False for row in report["datasets"])


def test_cli_respects_requested_dataset_and_never_overwrites_existing_evidence(tmp_path):
    from decagent.evaluation.business.__main__ import main

    output = tmp_path / "diagnostic.json"
    with pytest.raises(FileNotFoundError):
        main(["--dataset", str(tmp_path / "missing.json"),
              "eligibility-diagnostic", "--output", str(output)])
    assert not output.exists()
    output.write_text("original evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(["eligibility-diagnostic", "--output", str(output)])
    assert output.read_text(encoding="utf-8") == "original evidence"

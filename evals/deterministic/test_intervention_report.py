"""Reporting must not turn synthetic conformance or partial traces into model accuracy."""

from importlib import import_module
from importlib.util import find_spec


def reporting():
    assert find_spec("decagent.evaluation.intervention") is not None, "Intervention export missing"
    return import_module("decagent.evaluation.intervention")


def test_synthetic_conformance_has_no_human_accuracy_or_benefit_claim(tmp_path):
    report = reporting().scenario_report()
    assert report["source"] == "synthetic_fixture"
    assert report["behavior_conformance"]["failed"] == 0
    assert report["human_labeled_samples"] == 0 and report["decision_accuracy"] is None
    assert report["task_benefit"] is None
    paths = reporting().export_report(tmp_path, report)
    assert paths["json"].is_file() and paths["markdown"].is_file()
    import json

    assert json.loads(paths["json"].read_text(encoding="utf-8")) == report
    assert all(row["human_label"] is None for row in report["samples"])


def test_observed_trace_export_preserves_unknown_labels_and_partial_scope():
    task = {
        "id": "task-one",
        "interventions": [
            {"kind": "intervention.checked", "seq": 5, "payload": {"route": "replan"}},
            {"kind": "intervention.feedback", "seq": 8, "payload": {"answer": "inspect utils"}},
        ],
        "decisions": [
            {
                "result": {
                    "request": {"questions": ["task_intervention"], "state": {"goal": "find bug"}}
                }
            }
        ],
    }
    report = reporting().trace_report(task)
    assert report["source"] == "observed_task_trace" and report["history_is_bounded"] is True
    assert report["human_labeled_samples"] == 0 and report["decision_accuracy"] is None
    assert report["samples"][0]["human_label"] is None
    assert report["events"] == task["interventions"]

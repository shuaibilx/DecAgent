import importlib

import pytest


def api():
    try:
        return importlib.import_module("decagent.evaluation")
    except ImportError as exc:
        pytest.fail(f"Benchmark report is not implemented: {exc}")


def test_report_keeps_failed_unknown_unfinished_denominators_and_never_passes_skips():
    e = api()
    labels = {
        "a": ("action_risk", "deny"),
        "b": ("intervention", "continue"),
        "c": ("memory_write", "retain"),
        "d": ("memory_relevant", True),
    }
    cells = {
        "jev/a": {"status": "failed", "result": {"route": "allow", "latency_seconds": 1}},
        "jev/b": {"status": "completed", "result": {"route": "review", "latency_seconds": 3}},
        "jev/c": {"status": "pending", "result": None},
        "jev/d": {"status": "completed", "result": {"route": "retrieve", "latency_seconds": 5}},
    }
    repairs = [
        {
            "sample_id": "r1",
            "status": "succeeded",
            "base_failed": True,
            "independent_passed": True,
            "regression_passed": True,
            "collected": 1,
            "skipped": 0,
            "xfail": 0,
        },
        {
            "sample_id": "r2",
            "status": "succeeded",
            "base_failed": True,
            "independent_passed": True,
            "regression_passed": True,
            "collected": 1,
            "skipped": 1,
            "xfail": 0,
        },
        {"sample_id": "r3", "status": "pending"},
    ]
    report = e.summarize_strategy(
        "jev",
        cells,
        labels,
        repairs,
        [
            {"cost_usd": "0.03", "input_tokens": 2, "output_tokens": 3},
            {"cost_usd": None, "input_tokens": None, "output_tokens": None},
        ],
    )
    assert report["planned"] == 4
    assert report["unfinished"] == 1
    assert report["misallow"]["count"] == 1
    assert report["false_interruption"]["count"] == 1
    assert report["memory_write"]["unresolved"] == 1
    assert report["memory_read"]["precision"] == 1
    assert report["repair"]["success_rate"] == pytest.approx(1 / 3)
    assert report["latency"]["p50_seconds"] == 3
    assert report["latency"]["p95_seconds"] == pytest.approx(4.8)
    assert report["cost"]["known_usd"] == "0.03"
    assert report["cost"]["unknown_attempts"] == 1
    assert report["cost"]["total_usd"] is None


def test_unlabeled_data_cannot_claim_error_rate_or_memory_quality():
    e = api()
    report = e.summarize_strategy(
        "rule",
        {"rule/a": {"status": "completed", "result": {"route": "allow"}}},
        {"a": ("action_risk", None)},
        [],
        [],
    )
    assert report["misallow"]["rate"] is None
    assert report["labeled"] == 0
    assert report["ground_truth_complete"] is False


def test_export_uses_beijing_naming_and_does_not_overwrite(tmp_path):
    from datetime import UTC, datetime

    e = api()
    now = datetime(2026, 10, 10, 1, 2, tzinfo=UTC)
    p1 = e.export_report(tmp_path, {"status": "fixture_only"}, now=now)
    p2 = e.export_report(tmp_path, {"status": "fixture_only"}, now=now)
    assert p1.relative_to(tmp_path).as_posix() == "generated/261010/0902_评测报告.md"
    assert p2.name == "0902_评测报告_02.md"
    assert "fixture_only" in p1.read_text(encoding="utf-8")


def test_memory_abstention_cannot_inflate_quality_and_missing_latency_is_counted():
    report = api().summarize_strategy(
        "rule",
        {"rule/a": {"status": "completed", "result": {"route": "review"}}},
        {"a": ("memory_write", "discard")},
        [],
        [],
    )
    assert report["memory_write"]["accuracy_observed"] is None
    assert report["memory_write"]["abstained"] == 1
    assert report["memory_write"]["unresolved"] == 1
    assert report["latency"]["unobserved"] == 1

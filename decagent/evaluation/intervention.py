"""Reproducible behavior scenarios and unlabeled trace exports, never accuracy claims."""

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decagent.runtime.progress import (
    accept_feedback,
    begin_replan,
    observe_progress,
    route_progress,
)


def scenario_report():
    def repeated(count, *, observations=None, tool="run_command"):
        progress = None
        for index in range(count):
            observation = (
                observations[index]
                if observations
                else {"status": "failed", "reason": "fixed failure"}
            )
            progress = observe_progress(
                progress,
                {"tool": tool, "params": {"path": "bug.py"}},
                observation,
                step_id=str(index + 1),
            )
        return progress

    stalled = repeated(2)
    exhausted = {**stalled, "replans": 2}
    cases = [
        ("repeated_failure", stalled, "replan"),
        (
            "distinct_failures",
            repeated(
                2,
                observations=[
                    {"status": "failed", "reason": "missing import"},
                    {"status": "failed", "reason": "new boundary evidence"},
                ],
            ),
            "continue",
        ),
        (
            "repeat_safe_read",
            repeated(
                2,
                tool="read_file",
                observations=[
                    {"status": "succeeded", "content": "same"},
                    {"status": "succeeded", "content": "same"},
                ],
            ),
            "replan",
        ),
        (
            "new_source_observation",
            repeated(
                2,
                tool="read_file",
                observations=[
                    {"status": "succeeded", "content": "old"},
                    {"status": "succeeded", "content": "new"},
                ],
            ),
            "continue",
        ),
        ("unknown_receipt", repeated(1, observations=[{"status": "unknown"}]), "reconcile"),
        ("adjustment_limit", exhausted, "escalate"),
        ("feedback_cycle", accept_feedback(exhausted, "synthetic-feedback"), "continue"),
        ("adjustment_observation_window", begin_replan(stalled), "continue"),
    ]
    samples = []
    for identifier, state, expected in cases:
        decision = route_progress(state)
        samples.append(
            {
                "id": identifier,
                "source": "synthetic_fixture",
                "state": state,
                "expected_route": expected,
                "actual_route": decision["route"],
                "behavior_conforms": decision["route"] == expected,
                "human_label": None,
                "labeler": None,
            }
        )
    passed = sum(row["behavior_conforms"] for row in samples)
    return {
        "format_version": "1",
        "source": "synthetic_fixture",
        "samples": samples,
        "behavior_conformance": {
            "planned": len(samples),
            "passed": passed,
            "failed": len(samples) - passed,
        },
        "human_labeled_samples": 0,
        "decision_accuracy": None,
        "task_benefit": None,
        "limitations": "Synthetic behavior constraints; no model quality comparison or "
        "independent human labels. Replay does not establish task benefit.",
    }


def trace_report(task):
    records = [
        row
        for row in task.get("decisions", [])
        if row.get("result", {}).get("request", {}).get("questions") == ["task_intervention"]
    ]
    events = task.get("interventions", [])
    return {
        "format_version": "1",
        "source": "observed_task_trace",
        "task_id": task["id"],
        "history_is_bounded": True,
        "events": events,
        "event_counts": dict(Counter(row["kind"] for row in events)),
        "samples": [{"record": row, "human_label": None, "labeler": None} for row in records],
        "human_labeled_samples": 0,
        "decision_accuracy": None,
        "task_benefit": None,
        "limitations": "Task API returns a bounded history window; counts do not describe "
        "the full run. Owner help text is not a correctness label.",
    }


def export_report(root, report):
    moment = datetime.now(timezone(timedelta(hours=8)))
    directory = Path(root) / "generated" / moment.strftime("%y%m%d")
    directory.mkdir(parents=True, exist_ok=True)
    stem = moment.strftime("%H%M") + "_介入场景与轨迹报告"
    suffix = 1
    while True:
        candidate = stem if suffix == 1 else stem + f"_{suffix:02d}"
        path = directory / (candidate + ".json")
        markdown = directory / (candidate + ".md")
        if markdown.exists():
            suffix += 1
            continue
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
            break
        except FileExistsError:
            suffix += 1
    rows = report.get("samples", [])
    content = "# 介入场景与轨迹报告\n\n来源：" + report["source"] + "。\n\n"
    content += report["limitations"] + "\n\n人工标签：0；决策准确率和任务收益尚未测得。\n\n"
    if report["source"] == "synthetic_fixture":
        content += "| 场景 | 预期处理 | 实际处理 | 行为符合 |\n| --- | --- | --- | --- |\n"
        content += (
            "\n".join(
                f"| {r['id']} | {r['expected_route']} | {r['actual_route']} | "
                f"{r['behavior_conforms']} |"
                for r in rows
            )
            + "\n"
        )
    else:
        content += (
            "事件数量仅覆盖 API 返回窗口："
            + json.dumps(report["event_counts"], ensure_ascii=False)
            + "。\n"
        )
    content += f"\n详细数据：[JSON]({path.name})。\n"
    with markdown.open("x", encoding="utf-8") as output:
        output.write(content)
    return {"json": path, "markdown": markdown}

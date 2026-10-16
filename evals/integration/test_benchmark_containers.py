"""Replay pinned public upstream regressions; this never calls a model."""

import os
from pathlib import Path

import pytest


@pytest.mark.integration
def test_replay_all_twelve_public_upstream_bugs(tmp_path):
    if not os.environ.get("DECAGENT_BENCHMARK_IMAGE"):
        pytest.skip("Explicit pinned benchmark container image required; unverified without it")
    from decagent.evaluation.verifier import verify_dataset

    root = Path(__file__).parents[1] / "benchmarks"
    results = verify_dataset(
        root,
        os.environ["DECAGENT_BENCHMARK_IMAGE"],
        work_root=tmp_path,
        trust_public_sources=True,
        persist=False,
    )
    assert len(results) == 12
    summary = {
        task["task_id"]: {
            "status": task["status"],
            "base": task["evidence"]["base"],
            "reference": task["evidence"]["reference"],
        }
        for task in results
        if task["status"] != "verified"
    }
    assert all(task["status"] == "verified" for task in results), summary

"""Incomplete, stale, skipped or modified release evidence cannot pass."""

import hashlib
import json

import pytest

from decagent.evaluation.release import REQUIRED_CHECKS, assess_release


def manifest(root):
    evidence = root / "check.json"
    evidence.write_text('{"verified": true}', encoding="utf-8")
    receipt = {
        "status": "passed",
        "code_digest": "a" * 64,
        "passed": 1,
        "skipped": 0,
        "evidence": "check.json",
        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }
    return {
        "format_version": 1,
        "code_digest": "a" * 64,
        "checks": {key: dict(receipt) for key in REQUIRED_CHECKS},
    }


def test_complete_matrix_requires_every_check_and_intact_evidence(tmp_path):
    data = manifest(tmp_path)
    assert assess_release(data, tmp_path, "a" * 64)["status"] == "passed"
    data["checks"].pop("live_jev")
    result = assess_release(data, tmp_path, "a" * 64)
    assert result["status"] == "incomplete" and "live_jev" in result["blocked"]


@pytest.mark.parametrize("mutation", ["skipped", "stale", "tampered", "escape", "zero"])
def test_incomplete_evidence_is_preserved_and_blocks_release(tmp_path, mutation):
    data = manifest(tmp_path)
    receipt = data["checks"]["repository"]
    if mutation == "skipped":
        receipt["skipped"] = 1
    elif mutation == "stale":
        receipt["code_digest"] = "b" * 64
    elif mutation == "tampered":
        (tmp_path / "check.json").write_text("changed", encoding="utf-8")
    elif mutation == "escape":
        receipt["evidence"] = "../outside.json"
    else:
        receipt["passed"] = 0
    result = assess_release(data, tmp_path, "a" * 64)
    assert result["status"] == "incomplete" and "repository" in result["blocked"]
    json.dumps(result, allow_nan=False)

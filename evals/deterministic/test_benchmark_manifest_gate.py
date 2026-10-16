import json
from pathlib import Path

from evals.benchmarks.validate_source_manifest import (
    expected_image_refs,
    scoring_prerequisite_issues,
)

MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "manifests"
    / "source_manifest.json"
)


def test_unknown_license_blocks_formal_scoring():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["tau2_bench"]["source_repository"]["license"] = "unknown"

    issues = scoring_prerequisite_issues(manifest, expected_image_refs(manifest))

    assert any("Unknown or unsupported license" in issue for issue in issues)


def test_unavailable_image_digest_blocks_formal_scoring():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    issues = scoring_prerequisite_issues(manifest, set())

    assert len([issue for issue in issues if "not available locally" in issue]) == 2


def test_pinned_licenses_and_local_image_digests_pass_prerequisite_gate():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert scoring_prerequisite_issues(manifest, expected_image_refs(manifest)) == []

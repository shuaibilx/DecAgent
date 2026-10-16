"""Validate benchmark provenance and refuse formal scoring on unknown inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "evals" / "benchmarks" / "manifests" / "source_manifest.json"
KNOWN_LICENSES = {"Apache-2.0", "MIT"}
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_image_refs(manifest: dict[str, Any]) -> set[str]:
    images = manifest["enterpriseops_gym"]["mcp_environment"]["images"]
    return {f"{image['image']}@{image['manifest_digest']}" for image in images}


def scoring_prerequisite_issues(
    manifest: dict[str, Any], local_image_refs: set[str]
) -> list[str]:
    """Return blockers for formal scores, without performing any model calls."""
    issues: list[str] = []
    license_records = (
        manifest["enterpriseops_gym"]["source_repository"],
        manifest["enterpriseops_gym"]["task_dataset"],
        manifest["tau2_bench"]["source_repository"],
    )
    for record in license_records:
        license_name = record.get("license")
        if license_name not in KNOWN_LICENSES:
            issues.append(f"Unknown or unsupported license: {license_name!r}")

    revisions = (
        manifest["enterpriseops_gym"]["source_repository"].get("commit"),
        manifest["enterpriseops_gym"]["task_dataset"].get("revision"),
        manifest["tau2_bench"]["source_repository"].get("commit"),
    )
    for revision in revisions:
        if not isinstance(revision, str) or not SHA1.fullmatch(revision):
            issues.append(f"Missing or invalid pinned source revision: {revision!r}")

    images = manifest["enterpriseops_gym"]["mcp_environment"]["images"]
    for image in images:
        digest = image.get("manifest_digest", "")
        if not digest.startswith("sha256:") or not SHA256.fullmatch(digest[7:]):
            issues.append(f"Missing immutable MCP image digest for {image.get('image')}")
            continue
        reference = f"{image.get('image')}@{digest}"
        if reference not in local_image_refs:
            issues.append(f"Pinned MCP image is not available locally: {reference}")
    return issues


def _verify_file(root: Path, relative_path: str, expected_sha: str, issues: list[str]) -> None:
    path = root / relative_path
    if not path.is_file():
        issues.append(f"Missing pinned benchmark input: {relative_path}")
        return
    if not SHA256.fullmatch(expected_sha):
        issues.append(f"Invalid SHA-256 in manifest for {relative_path}")
        return
    actual = _file_sha256(path)
    if actual != expected_sha:
        issues.append(
            f"SHA-256 mismatch for {relative_path}: expected {expected_sha}, got {actual}"
        )


def validate_files(manifest_path: Path = DEFAULT_MANIFEST) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues: list[str] = []
    root = ROOT

    for record in (
        manifest["enterpriseops_gym"]["source_repository"],
        manifest["tau2_bench"]["source_repository"],
    ):
        _verify_file(root, record["archive"]["path"], record["archive"]["sha256"], issues)
        _verify_file(root, record["license_file_path"], record["license_file_sha256"], issues)

    enterprise = manifest["enterpriseops_gym"]
    task_dataset = enterprise["task_dataset"]
    _verify_file(
        root,
        task_dataset["license_file_path"],
        task_dataset["license_readme_sha256"],
        issues,
    )
    for parquet_file in task_dataset["parquet_sha256"]:
        _verify_file(root, parquet_file["path"], parquet_file["sha256"], issues)
    snapshots = enterprise["database_snapshots"]
    _verify_file(
        root,
        snapshots["source_bundle"]["path"],
        snapshots["source_bundle"]["sha256"],
        issues,
    )
    for snapshot in snapshots["referenced_snapshot_sha256"]:
        _verify_file(root, snapshot["path"], snapshot["sha256"], issues)

    inventory_info = manifest["task_inventory"]
    _verify_file(root, inventory_info["path"], inventory_info["sha256"], issues)
    inventory_path = root / inventory_info["path"]
    if inventory_path.is_file():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory["sources"]["enterpriseops_dataset_revision"] != task_dataset["revision"]:
            issues.append(
                "Task inventory and source manifest use different EnterpriseOps revisions"
            )
        for domain in inventory["tau2_bench"]["official_domains"]:
            domain_root = (
                root
                / "evals"
                / "benchmarks"
                / "vendor_cache"
                / "tau2-bench-source"
                / "data"
                / "tau2"
                / "domains"
                / domain["domain"]
            )
            for name, expected_sha in domain["source_sha256"].items():
                _verify_file(domain_root, name, expected_sha, issues)

    ignore_path = root / "evals" / "benchmarks" / ".gitignore"
    if not ignore_path.is_file() or "vendor_cache/" not in ignore_path.read_text(encoding="utf-8"):
        issues.append("Local raw benchmark cache is not excluded by evals/benchmarks/.gitignore")
    return issues


def _inspect_local_images(references: set[str]) -> set[str]:
    available: set[str] = set()
    try:
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return available
    if daemon.returncode != 0:
        return available

    for reference in sorted(references):
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", reference],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Docker is optional for provenance checks, but its absence must fail
            # the formal-scoring gate closed rather than hanging or implying readiness.
            continue
        if result.returncode == 0:
            available.add(reference)
    return available


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--for-scoring",
        action="store_true",
        help="also require every pinned EnterpriseOps MCP image to be present locally by digest",
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    issues = validate_files(args.manifest)
    if args.for_scoring:
        local_images = _inspect_local_images(expected_image_refs(manifest))
        issues.extend(scoring_prerequisite_issues(manifest, local_images))

    if issues:
        print("BLOCKED: benchmark provenance is incomplete.")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("PASS: pinned benchmark sources, licenses, task inventory, and checksums verified.")
    if not args.for_scoring:
        print("Scoring readiness was not checked. Use --for-scoring before formal evaluation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

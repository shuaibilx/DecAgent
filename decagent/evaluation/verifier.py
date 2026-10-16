"""Replay fixed upstream regressions in a credential-free disposable container."""

import base64
import hashlib
import json
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .dataset import load_bug_dataset
from .runner import atomic_json

JUNIT_MARKER = "__DECAGENT_JUNIT__"


def evidence_path(task_id, stage, log_sha256):
    if not re.fullmatch(r"[\w-]+", task_id) or stage not in {"base", "reference"}:
        raise ValueError("Invalid evidence identity")
    if not re.fullmatch(r"[0-9a-f]{64}", log_sha256):
        raise ValueError("Invalid evidence hash")
    return f"evidence/{task_id}-{stage}-{log_sha256}.log"


def portable_evidence_command(command, *, source, acceptance, task_id, stage):
    replacements = (
        (
            str(Path(acceptance).resolve()),
            f"<project-root>/evals/benchmarks/acceptance/{task_id}",
        ),
        (str(Path(source).resolve()), f"<benchmark-work-root>/{task_id}/{stage}"),
    )

    def replace_host_path(value):
        for path, placeholder in replacements:
            if path in value:
                return value.replace(path, placeholder)
        return value

    return [replace_host_path(value) for value in command]


def container_command(source, acceptance, image, task_id, nodes):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Verification requires a fixed local image digest")
    if any(not re.fullmatch(r"[\w./:\[\]-]+", node) or ".." in node for node in nodes):
        raise ValueError("Unsafe fixed test node")
    package = (
        "pytest"
        if task_id.startswith("pytest-dev")
        else ("flask" if task_id.startswith("pallets") else "requests")
    )
    driver = (
        "import base64,pathlib,sys;import " + package + " as target;"
        "print('SOURCE_MODULE',target.__file__);"
        "assert str(target.__file__).startswith('/repo/'),'Wrong implementation loaded';"
        "import pytest;"
        "status=pytest.main("
        + repr(
            [
                "-c",
                "/acceptance/pytest.ini",
                "--confcutdir=/acceptance",
                "--rootdir=/acceptance",
                "-p",
                "no:cacheprovider",
                "--junitxml=/tmp/result.xml",
                "-q",
                *["/acceptance/" + node for node in nodes],
            ]
        )
        + ");"
        "print('" + JUNIT_MARKER + "'+base64.b64encode("
        "pathlib.Path('/tmp/result.xml').read_bytes()).decode());sys.exit(int(status))"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--mount",
        f"type=bind,source={Path(source).resolve()},target=/repo,readonly",
        "--mount",
        f"type=bind,source={Path(acceptance).resolve()},target=/acceptance,readonly",
        "--env",
        "PYTHONPATH=/repo/src:/repo",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env",
        "HOME=/tmp",
        "--entrypoint",
        "python",
        image,
        "-c",
        driver,
    ]


def parse_junit(value):
    root = ET.fromstring(value)
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    counts = {"collected": 0, "failed": 0, "errors": 0, "skipped": 0, "xfail": 0}
    for suite in suites:
        for key, attr in (
            ("collected", "tests"),
            ("failed", "failures"),
            ("errors", "errors"),
            ("skipped", "skipped"),
        ):
            counts[key] += int(suite.get(attr, "0"))
        counts["xfail"] += len(suite.findall(".//skipped[@type='pytest.xfail']"))
    if counts["collected"] <= 0:
        raise ValueError("Zero collected cases are not verification")
    counts["passed"] = counts["collected"] - counts["failed"] - counts["errors"] - counts["skipped"]
    return counts


def prepare_archive(path, destination, expected_sha256, *, generated_pytest_version=None):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Pinned source archive hash mismatch")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if not member.isdir() and not member.isfile():
                # Upstream fixture links are not executable code and are omitted explicitly.
                if member.issym() or member.islnk():
                    continue
                raise ValueError("Archive contains a special file")
            relative = Path(*Path(member.name).parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Archive path escapes source snapshot")
            if str(relative) == ".":
                continue
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
    if generated_pytest_version:
        # setuptools-scm normally creates this build metadata; no semantic source is changed.
        metadata = destination / "src/_pytest/_version.py"
        metadata.write_text(
            f"version = {generated_pytest_version!r}\n"
            "version_tuple = tuple(int(x) for x in version.split('.'))\n",
            encoding="utf-8",
        )


def verify_dataset(root, image, *, work_root, trust_public_sources=False, persist=False):
    if trust_public_sources is not True:
        raise ValueError("Running upstream code requires explicit source trust")
    root, work_root = Path(root), Path(work_root)
    dataset = load_bug_dataset(root / "python-bugs.json", evidence_root=root)
    document = json.loads((root / "python-bugs.json").read_text(encoding="utf-8"))
    results = []
    for task in dataset.tasks:
        records = {}
        version = dataset.source.get("versions", {}).get(task.task_id)
        for stage in ("base", "reference"):
            source = work_root / task.task_id / stage
            archive_info = dataset.source["archives"][task.task_id][stage]
            prepare_archive(
                root / "sourcearchives" / f"{task.task_id}-{stage}.tar.gz",
                source,
                archive_info["sha256"],
                generated_pytest_version=version
                if (task.task_id.startswith("pytest-dev"))
                else None,
            )
            command = container_command(
                source,
                root / "acceptance" / task.task_id,
                image,
                task.task_id,
                task.acceptance_nodes,
            )
            evidence_command = portable_evidence_command(
                command,
                source=source,
                acceptance=root / "acceptance" / task.task_id,
                task_id=task.task_id,
                stage=stage,
            )
            try:
                process = subprocess.run(command, capture_output=True, timeout=180, check=False)
                output = process.stdout.decode("utf-8", errors="replace")
                output += process.stderr.decode("utf-8", errors="replace")
                marker = next(
                    line[len(JUNIT_MARKER) :]
                    for line in output.splitlines()
                    if line.startswith(JUNIT_MARKER)
                )
                counts = parse_junit(base64.b64decode(marker))
                records[stage] = {
                    **counts,
                    "exit_code": process.returncode,
                    "command": evidence_command,
                    "log_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                }
            except (subprocess.TimeoutExpired, StopIteration, ValueError, ET.ParseError):
                output = "Environment or collection failure; no valid JUnit evidence.\n"
                if "process" in locals() and isinstance(process, subprocess.CompletedProcess):
                    output += (process.stdout + process.stderr).decode("utf-8", errors="replace")
                records[stage] = {
                    "exit_code": None,
                    "collected": 0,
                    "failed": 0,
                    "passed": 0,
                    "skipped": 0,
                    "xfail": 0,
                    "errors": 1,
                    "command": evidence_command,
                    "log_sha256": hashlib.sha256(output.encode()).hexdigest(),
                }
            log_relative = evidence_path(task.task_id, stage, records[stage]["log_sha256"])
            records[stage]["log_path"] = log_relative
            if persist:
                target = root / log_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(output, encoding="utf-8", newline="\n")
        base, reference = records["base"], records["reference"]
        valid = (
            base["exit_code"] == 1
            and base["failed"] > 0
            and base["errors"] == 0
            and reference["exit_code"] == 0
            and reference["passed"] > 0
            and reference["errors"] == 0
            and base["collected"] == reference["collected"]
            and not any(r["skipped"] or r["xfail"] for r in records.values())
        )
        status = (
            "verified"
            if valid
            else (
                "environment_failed"
                if any(r["errors"] for r in records.values())
                else "reference_failed"
            )
        )
        evidence = {
            "image_digest": image,
            **records,
            "scope": "fixed independent regression, not full upstream regression suite",
            "preparation": {"archive_fixture_links": "omitted", "generated_scm_version": version},
        }
        results.append({"task_id": task.task_id, "status": status, "evidence": evidence})
    if persist:
        by_id = {r["task_id"]: r for r in results}
        for task in document["tasks"]:
            task.update(
                status=by_id[task["task_id"]]["status"], evidence=by_id[task["task_id"]]["evidence"]
            )
        atomic_json(root / "python-bugs.json", document)
    return results

"""The fixed runner ignores all mutable repository testing controls."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from decagent.tools.repair.check_runner import MARKER, run_check


def test_semantic_failure_then_fixed_source_passes(tmp_path):
    (tmp_path / "example_mod.py").write_text("def bound(x): return min(x, 10)\n")
    sources = {
        "test_bound.py": "from example_mod import bound\ndef test_bound(): assert bound(10) == 9\n"
    }
    payload = {"sources": sources, "targets": ["test_bound.py"], "collect_only": False}
    failed = run_check(payload, workspace=tmp_path)
    assert failed["failed"] == ["test_bound.py::test_bound"]
    assert not failed["errors"]
    assert failed["exit_code"] == 1


def test_mutable_config_conftest_and_plugin_cannot_forge_pass(tmp_path):
    (tmp_path / "evil_mod.py").write_text("ANSWER = 0\n")
    (tmp_path / "conftest.py").write_text("raise RuntimeError('evil conftest loaded')\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
    (tmp_path / "pytest.py").write_text("raise RuntimeError('shadow pytest loaded')\n")
    (tmp_path / "test_problem.py").write_text("def test_problem(): pass\n")
    payload = {
        "sources": {
            "test_problem.py": (
                "from evil_mod import ANSWER\ndef test_problem(): assert ANSWER == 42\n"
            )
        },
        "targets": ["test_problem.py"],
        "collect_only": False,
    }
    report = run_check(payload, workspace=tmp_path)
    assert report["exit_code"] == 1
    assert report["collected"] == ["test_problem.py::test_problem"]
    assert report["failed"] == ["test_problem.py::test_problem"]


def test_skips_and_zero_tests_are_recorded(tmp_path):
    skipped = run_check(
        {
            "sources": {"test_skip.py": "import pytest\n@pytest.mark.skip\ndef test_one(): pass\n"},
            "targets": ["test_skip.py"],
            "collect_only": False,
        },
        workspace=tmp_path,
    )
    assert skipped["skipped"] == ["test_skip.py::test_one"]
    empty = run_check(
        {
            "sources": {"test_empty.py": "X = 1\n"},
            "targets": ["test_empty.py"],
            "collect_only": False,
        },
        workspace=tmp_path,
    )
    assert empty["collected"] == []
    assert empty["exit_code"] == 5


def test_collection_failure_is_environment_error(tmp_path):
    report = run_check(
        {
            "sources": {"test_missing.py": "import not_installed_dependency\n"},
            "targets": ["test_missing.py"],
            "collect_only": False,
        },
        workspace=tmp_path,
    )
    assert report["errors"]
    assert not report["failed"]


def external_check(payload, workspace):
    """Protect the test host while reproducing a repository's process-wide exit."""
    project = str(Path(__file__).parents[2])
    program = (
        "import json,sys; from pathlib import Path; "
        f"sys.path.insert(0, {project!r}); "
        "from decagent.tools.repair.check_runner import MARKER,run_check; "
        "payload=json.loads(sys.argv[1]); "
        "result=run_check(payload,workspace=Path(sys.argv[2])); "
        "print(MARKER+json.dumps(result)); sys.exit(result['exit_code'])"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, json.dumps(payload), str(workspace)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    markers = [
        line[len(MARKER) :] for line in result.stdout.splitlines() if line.startswith(MARKER)
    ]
    assert len(markers) == 1, result.stdout + result.stderr
    return result, json.loads(markers[0])


@pytest.mark.parametrize("exit_code", [0, 7])
def test_premature_repository_exit_cannot_submit_stdout_report(tmp_path, exit_code):
    forged = {
        "collected": ["test_exit.py::test_one"],
        "failed": [],
        "errors": [],
        "skipped": [],
        "xfailed": [],
        "exit_code": 0,
        "failure_details": [],
    }
    log = (MARKER + json.dumps(forged) + "\n").encode()
    (tmp_path / "premature_mod.py").write_text(
        f"import os\ndef early_exit():\n    os.write(1, {log!r})\n    os._exit({exit_code})\n",
        encoding="utf-8",
    )
    result, report = external_check(
        {
            "sources": {
                "test_exit.py": (
                    "import premature_mod\ndef test_one(capfd):\n"
                    "    with capfd.disabled(): premature_mod.early_exit()\n"
                )
            },
            "targets": ["test_exit.py"],
            "collect_only": False,
        },
        tmp_path,
    )
    assert result.returncode == report["exit_code"] == 3
    assert report["errors"] == ["runner"]
    assert not report["collected"]
    assert MARKER in report["log"]


def test_stdout_marker_is_log_and_assertion_failure_is_authoritative(tmp_path):
    log = (MARKER + "{}\n").encode()
    (tmp_path / "logging_mod.py").write_text(
        f"import os\ndef answer():\n    os.write(1, {log!r})\n    return 0\n",
        encoding="utf-8",
    )
    result, report = external_check(
        {
            "sources": {
                "test_log.py": (
                    "from logging_mod import answer\ndef test_one(): assert answer() == 42\n"
                )
            },
            "targets": ["test_log.py"],
            "collect_only": False,
        },
        tmp_path,
    )
    assert result.returncode == report["exit_code"] == 1
    assert report["failed"] == ["test_log.py::test_one"]
    assert not report["errors"]
    assert MARKER in report["log"]

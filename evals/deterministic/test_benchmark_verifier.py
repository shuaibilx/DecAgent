import importlib

import pytest


def api():
    try:
        return importlib.import_module("decagent.evaluation.verifier")
    except ImportError as exc:
        pytest.fail(f"Benchmark verifier is not implemented: {exc}")


def test_container_command_uses_only_readonly_source_and_fixed_acceptance(tmp_path):
    v = api()
    command = v.container_command(
        tmp_path / "source",
        tmp_path / "acceptance",
        "sha256:" + "a" * 64,
        "pytest-dev__pytest-10051",
        ("test_bug.py::test_bug",),
    )
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "65534:65534"
    assert sum(",readonly" in arg for arg in command) == 2
    assert "--env-file" not in command
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "-c" in command and "/acceptance/pytest.ini" in command[-1]


def test_junit_errors_and_skips_cannot_be_reported_as_verified():
    v = api()
    assert (
        v.parse_junit(
            '<testsuites><testsuite tests="1" failures="0" errors="1" skipped="0" /></testsuites>'
        )["errors"]
        == 1
    )
    result = v.parse_junit(
        '<testsuites><testsuite tests="1" failures="0" errors="0" '
        'skipped="1"><testcase><skipped type="pytest.xfail" />'
        "</testcase></testsuite></testsuites>"
    )
    assert result["xfail"] == 1
    with pytest.raises(ValueError):
        v.parse_junit('<testsuites><testsuite tests="0" /></testsuites>')

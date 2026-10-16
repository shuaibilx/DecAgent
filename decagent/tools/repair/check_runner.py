"""Immutable runner module copied into /opt/runner/decagent_check.py.

The control service supplies frozen material, never repository pytest configuration.
This file deliberately imports no DecAgent package so it runs in the minimal runner.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import multiprocessing
import os
import re
import sys
import tempfile
import time
from pathlib import Path

MARKER = "DECAGENT_CHECK_RESULT="
MAX_REPORT_BYTES = 1_048_576
CHILD_TIMEOUT_SECONDS = 110


def _validate(payload):
    if not isinstance(payload, dict) or set(payload) not in (
        {"sources", "targets", "collect_only"},
        {"sources", "targets", "collect_only", "code_source"},
    ):
        raise ValueError("Invalid fixed-check payload")
    sources, targets = payload["sources"], payload["targets"]
    if payload.get("code_source", "workspace") not in {"workspace", "baseline"}:
        raise ValueError("Unknown fixed code source")
    if not isinstance(sources, dict) or not sources or len(sources) > 1000:
        raise ValueError("Fixed sources required")
    if not isinstance(targets, list) or not targets or type(payload["collect_only"]) is not bool:
        raise ValueError("Fixed targets required")
    for path, source in sources.items():
        if (
            not isinstance(path, str)
            or not isinstance(source, str)
            or not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", path)
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or len(source.encode()) > 1_000_000
        ):
            raise ValueError("Unsafe fixed source")
    for target in targets:
        if not isinstance(target, str) or target.split("::")[0] not in sources:
            raise ValueError("Target must refer to frozen source")
    if len(json.dumps(payload).encode()) > 40_000:
        raise ValueError("Fixed check exceeds command transport limit")


def _run_pytest(payload: dict, *, workspace: Path) -> dict:
    """Execute frozen tests without any repository conftest, config, or entrypoint plugin."""
    _validate(payload)
    # Import pytest before adding project import paths so a repo pytest.py cannot shadow it.
    import pytest

    report = {
        "collected": [],
        "failed": [],
        "errors": [],
        "skipped": [],
        "xfailed": [],
        "exit_code": 0,
        "failure_details": [],
    }
    original_path = sys.path[:]
    original_env = {
        name: os.environ.get(name)
        for name in (
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTHONPATH",
        )
    }
    try:
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"):
            os.environ.pop(name, None)
        # Keep the interpreter's trusted packages first, repo/source roots last.
        workspace = workspace.resolve()
        code_root = Path("/baseline") if payload.get("code_source") == "baseline" else workspace
        sys.path[:] = [path for path in sys.path if path and Path(path).resolve() != workspace]
        for site in sorted((workspace / ".venv" / "lib").glob("python*/site-packages")):
            sys.path.append(str(site))
        sys.path.extend([str(code_root / "src"), str(code_root)])
        with tempfile.TemporaryDirectory(prefix="decagent-fixed-check-") as directory:
            root = Path(directory)
            sys.path.insert(0, str(root))
            sys.path.insert(0, str(root / "tests"))
            for path, source in payload["sources"].items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(source, encoding="utf-8")
            configuration = root / "controlled.ini"
            configuration.write_text("[pytest]\n", encoding="utf-8")

            def case(nodeid):
                path, separator, suffix = nodeid.partition("::")
                candidate = Path(path)
                try:
                    path = candidate.resolve().relative_to(root).as_posix()
                except ValueError:
                    # pytest may choose a parent root and produce a relative tmp path.
                    for frozen in payload["sources"]:
                        if path.replace("\\", "/").endswith(frozen):
                            path = frozen
                            break
                return path + separator + suffix

            class Reporter:
                def pytest_collection_modifyitems(self, items):
                    report["collected"] = [case(item.nodeid) for item in items]

                def pytest_collectreport(self, report):
                    if report.failed:
                        add("errors", report.nodeid, str(report.longrepr))

                def pytest_runtest_logreport(self, report):
                    if hasattr(report, "wasxfail"):
                        add("xfailed", report.nodeid)
                    elif report.skipped:
                        add("skipped", report.nodeid)
                    elif report.failed:
                        key = "failed" if report.when == "call" else "errors"
                        add(key, report.nodeid, str(report.longrepr))

            def add(key, nodeid, detail=None):
                name = case(nodeid)
                if name not in report[key]:
                    report[key].append(name)
                if detail is not None:
                    report["failure_details"].append(detail[-16000:])

            arguments = [
                "-c",
                str(configuration),
                "--rootdir",
                str(root),
                "--confcutdir",
                str(root),
                "--noconftest",
                "--import-mode=importlib",
                "-p",
                "no:cacheprovider",
                "-q",
            ]
            if payload["collect_only"]:
                arguments.append("--collect-only")
            arguments.extend(str(root / target) for target in payload["targets"])
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                report["exit_code"] = int(pytest.main(arguments, plugins=[Reporter()]))
            report["log"] = captured.getvalue()[-32000:]
            report["environment"] = {
                "python": sys.version.split()[0],
                "pytest": pytest.__version__,
                "code_source": payload.get("code_source", "workspace"),
            }
    finally:
        sys.path[:] = original_path
        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return report


def _environment_error(detail: str, log: str = "") -> dict:
    return {
        "collected": [],
        "failed": [],
        "errors": ["runner"],
        "skipped": [],
        "xfailed": [],
        "exit_code": 3,
        "failure_details": [detail],
        "log": log[-32000:],
    }


def _child_check(payload, workspace, sender, log_path):
    """Only this fixed child entrypoint submits the completion report over IPC."""
    try:
        # Redirect descriptors, too: os.write() and disabled pytest capture remain logs.
        with open(log_path, "wb", buffering=0) as output:
            os.dup2(output.fileno(), 1)
            os.dup2(output.fileno(), 2)
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace", closefd=False)
            sys.stderr = open(2, "w", encoding="utf-8", errors="replace", closefd=False)
            report = _run_pytest(payload, workspace=Path(workspace))
            sys.stdout.flush()
            sys.stderr.flush()
            message = json.dumps({"completed": True, "report": report}).encode("utf-8")
            if len(message) > MAX_REPORT_BYTES:
                raise ValueError("Fixed checker report exceeds IPC limit")
            sender.send_bytes(message)
    finally:
        sender.close()


def _log_tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - 32000))
        return stream.read().decode("utf-8", "replace")


def run_check(payload: dict, *, workspace: Path = Path("/workspace")) -> dict:
    """Supervise pytest without treating repository stdout or exit(0) as a report.

    Repository code still runs as trusted Python inside the bounded task container.
    This protocol distinguishes premature process exits and ordinary log markers;
    it is not a containment proof against arbitrary malicious Python in the child.
    """
    _validate(payload)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    with tempfile.TemporaryDirectory(prefix="decagent-check-supervisor-") as directory:
        log_path = Path(directory) / "child.log"
        log_path.touch()
        child = context.Process(
            target=_child_check,
            args=(payload, str(workspace.resolve()), sender, str(log_path)),
            name="decagent-fixed-pytest",
        )
        message = None
        deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
        try:
            child.start()
            sender.close()
            while time.monotonic() < deadline:
                if receiver.poll(0.1):
                    try:
                        message = receiver.recv_bytes(MAX_REPORT_BYTES)
                    except (EOFError, OSError):
                        pass
                    break
                if not child.is_alive():
                    break
            child.join(max(0, deadline - time.monotonic()))
            if child.is_alive():
                return _environment_error(
                    "Fixed checker child exceeded deadline", _log_tail(log_path)
                )
            if child.exitcode != 0 or message is None:
                return _environment_error(
                    "Fixed checker child exited without normal IPC completion "
                    f"(exit={child.exitcode})",
                    _log_tail(log_path),
                )
            envelope = json.loads(message)
            if not isinstance(envelope, dict) or set(envelope) != {"completed", "report"}:
                raise ValueError("Invalid fixed checker IPC envelope")
            if envelope["completed"] is not True or not isinstance(envelope["report"], dict):
                raise ValueError("Fixed checker did not submit a completed report")
            result = envelope["report"]
            # Parent owns the only public marker. All child output stays inside JSON log.
            result["log"] = (result.get("log", "") + _log_tail(log_path))[-32000:]
            return result
        except (ValueError, OSError, TypeError) as error:
            return _environment_error(str(error), _log_tail(log_path))
        finally:
            if child.pid is not None:
                if child.is_alive():
                    child.terminate()
                    child.join(2)
                if child.is_alive():
                    child.kill()
                    child.join(2)
                child.close()
            receiver.close()
            sender.close()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("One frozen-check payload required")
    payload = json.loads(base64.b64decode(sys.argv[1], validate=True))
    try:
        result = run_check(payload)
    except Exception as error:
        result = _environment_error(str(error), "Fixed checker failed")
    print(MARKER + json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()

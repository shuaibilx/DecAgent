"""Trusted PID 1 supervisor. Capture and redact before writing any Docker log."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from decagent.policy.redaction import Redactor


def safe_cwd(relative):
    root = Path("/workspace")
    path = root
    for component in relative.split("/"):
        if component == ".":
            continue
        if component in {"", ".."}:
            raise ValueError("Invalid working directory")
        path = path / component
        if path.is_symlink():
            raise ValueError("Working directory contains a link")
    if not path.is_dir() or not path.resolve().is_relative_to(root):
        raise ValueError("Working directory escapes workspace")
    return path


def run(command):
    timeout = command["timeout_seconds"]
    limit = command["max_output_bytes"]
    if not 1 <= timeout <= 600 or not 32 <= limit <= 1048576:
        raise ValueError("Invalid resource limits")
    process = subprocess.Popen(
        command["argv"],
        cwd=safe_cwd(command["cwd"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    captured = bytearray()
    line = bytearray()
    truncated = False
    overlong = False
    timed_out = False
    stopped = False
    deadline = time.monotonic() + timeout

    def stop(*_):
        nonlocal stopped
        stopped = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                timed_out = True
                stop()
            for key, _ in selector.select(timeout=0.05):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                for byte in chunk:
                    if len(line) >= limit:
                        line.clear()
                        overlong = True
                        truncated = True
                    if not overlong:
                        line.append(byte)
                    if byte == 10:
                        if len(captured) + len(line) <= limit and not overlong:
                            captured.extend(line)
                        else:
                            truncated = True
                        line.clear()
                        overlong = False
            if stopped and process.poll() is not None and not selector.get_map():
                break
        if line and not overlong and len(captured) + len(line) <= limit:
            captured.extend(line)
        elif line or overlong:
            truncated = True
        exit_code = process.wait(timeout=5)
    finally:
        stop()
        selector.close()
        process.stdout.close()
    # Complete lines are kept or dropped, never cut through credentials at the cap.
    text = Redactor(max_text_chars=1048576).redact_text(captured.decode("utf-8", "replace"))
    if truncated:
        text += "\n[TRUNCATED]\n"
    return {"output": text, "exit_code": exit_code, "timed_out": timed_out, "truncated": truncated}


if __name__ == "__main__":
    try:
        result = run(json.loads(sys.argv[1]))
    except Exception:
        result = {
            "output": "Runner could not start command",
            "exit_code": 125,
            "timed_out": False,
            "truncated": False,
        }
    print(json.dumps(result, separators=(",", ":")), flush=True)

"""One-shot, process-bounded execution for public model transport calls.

The caller remains the ledger and dispatch authority. A child receives one
already-authorized call, creates one provider client, and exits; it never owns
the route ledger or a reusable model client.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import threading
import time
from pathlib import Path

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TIMEOUT_SECONDS = 1800.0


class CancellableProcessError(RuntimeError):
    """A one-shot child failed without exposing its request, response or message."""

    def __init__(self, *, stage, exception_type):
        if stage not in {"isolation_setup", "target", "response"}:
            stage = "response"
        if type(exception_type) is not str or not exception_type.isidentifier():
            exception_type = "WorkerError"
        self.stage = stage
        self.exception_type = exception_type
        super().__init__("One-shot provider worker failed (" + stage + ")")


class CancellableProcessTimeout(TimeoutError):
    """The supervisor deadline elapsed and the owned child tree was terminated."""


def _watch_parent_lifetime():
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def stop_if_parent_exits():
        parent.join()
        # If a supervisor is forcibly terminated, don't leave a dispatched
        # one-shot transport running beyond the lock owner's lifetime.
        os._exit(125)

    threading.Thread(target=stop_if_parent_exits, daemon=True).start()


def _assign_windows_kill_job():
    """Make descendants die automatically if this one-shot worker exits."""
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    )
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE

    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    limits = ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(
        handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error, "SetInformationJobObject failed")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise OSError(error, "AssignProcessToJobObject failed")
    return handle


def _send_frame(connection, frame):
    payload = json.dumps(
        frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > _MAX_RESPONSE_BYTES:
        payload = json.dumps(
            {
                "ok": False,
                "stage": "response",
                "exception_type": "ResponseTooLarge",
            },
            separators=(",", ":"),
        ).encode("ascii")
    connection.send_bytes(payload)


def _process_entry(connection, target, args, worker_guard_path):
    """Spawn boundary: target runs once and gets no ledger or provider client."""
    kill_job = None
    worker_guard = None
    try:
        if os.name == "nt":
            kill_job = _assign_windows_kill_job()
        else:
            os.setsid()
        if worker_guard_path is not None:
            from .public_supervisor import PublicWorkerGuard

            worker_guard = PublicWorkerGuard(worker_guard_path).acquire()
        parent = multiprocessing.parent_process()
        if parent is None or not parent.is_alive():
            raise RuntimeError("Parent supervisor has exited")
        _watch_parent_lifetime()
    except Exception as exc:
        _send_frame(
            connection,
            {"ok": False, "stage": "isolation_setup", "exception_type": type(exc).__name__},
        )
        connection.close()
        return

    try:
        result = target(*args)
        _send_frame(connection, {"ok": True, "result": result})
    except BaseException as exc:
        _send_frame(
            connection,
            {"ok": False, "stage": "target", "exception_type": type(exc).__name__},
        )
    finally:
        connection.close()
        if worker_guard is not None:
            worker_guard.release()
        # Retain the Windows job handle until process exit. The kernel closes
        # it on exit, killing any descendant that failed to shut down cleanly.
        _ = kill_job


def _terminate_process_tree(process):
    if not process.is_alive():
        process.join(timeout=0)
        return
    if os.name == "nt":
        import subprocess

        system_root = os.environ.get("SYSTEMROOT")
        taskkill = Path(system_root, "System32", "taskkill.exe") if system_root else None
        if taskkill is not None and taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        def kill_group():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass

        kill_group()
        # A descendant may fork concurrently with the first group signal. Wait
        # briefly for the worker's sentinel without joining/reaping it, then
        # sweep the still-reserved process group once more before PID reuse is
        # possible.
        sentinel = getattr(process, "sentinel", None)
        if sentinel is not None:
            from multiprocessing.connection import wait

            wait([sentinel], timeout=0.1)
        kill_group()
    process.join(timeout=2)
    if process.is_alive():
        process.kill()
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
        process.join(timeout=2)


def run_cancellable_process(
    target, args=(), *, timeout_seconds, worker_guard_path=None
):
    """Run one trusted, importable target with bounded JSON output and lifetime.

    The target must be module-level and receive only data needed for this one
    operation. The calling process retains all accounting and dispatch state.
    """
    if not callable(target) or type(args) is not tuple:
        raise TypeError("One-shot worker target and positional arguments are required")
    if worker_guard_path is not None and not isinstance(
        worker_guard_path, (str, os.PathLike)
    ):
        raise TypeError("One-shot worker guard path must be a filesystem path")
    if (
        type(timeout_seconds) not in {int, float}
        or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("One-shot worker deadline must be positive and at most 1,800 seconds")
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_process_entry,
        args=(child_connection, target, args, worker_guard_path),
        daemon=False,
    )
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        process.start()
        child_connection.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not parent_connection.poll(remaining):
            _terminate_process_tree(process)
            raise CancellableProcessTimeout("One-shot provider request exceeded its deadline")
        try:
            raw = parent_connection.recv_bytes(maxlength=_MAX_RESPONSE_BYTES)
            frame = json.loads(raw.decode("utf-8"))
        except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CancellableProcessError(
                stage="response", exception_type=type(exc).__name__
            ) from None
        if type(frame) is not dict or type(frame.get("ok")) is not bool:
            raise CancellableProcessError(stage="response", exception_type="InvalidEnvelope")
        if frame["ok"]:
            process.join(timeout=2)
            if process.is_alive():
                _terminate_process_tree(process)
                raise CancellableProcessError(stage="response", exception_type="WorkerDidNotExit")
            return frame.get("result")
        raise CancellableProcessError(
            stage=frame.get("stage"),
            exception_type=frame.get("exception_type"),
        )
    except BaseException:
        if process.pid is not None and process.is_alive():
            _terminate_process_tree(process)
        raise
    finally:
        parent_connection.close()
        child_connection.close()
        if process.pid is not None:
            process.close()

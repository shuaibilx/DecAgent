"""Single-host process exclusion for live public benchmark execution."""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

PUBLIC_SUPERVISOR_LOCK_RELATIVE_PATH = Path(
    ".decagent/business-eval/public-evaluation.lock"
)


def canonical_public_supervisor_lock_path(root):
    return Path(root).resolve() / PUBLIC_SUPERVISOR_LOCK_RELATIVE_PATH


class PublicSupervisorBusy(RuntimeError):
    """Another live evaluator still owns the process-lifetime lock."""


class PublicSupervisor:
    """Hold an OS-released lock while a process may dispatch benchmark work.

    The SQLite lease fences ledger writes. This lock separately prevents a
    suspended but still-live process from being mistaken for a dead sender.
    """

    def __init__(self, lock_path, *, acquire_timeout=0.0):
        if (
            type(acquire_timeout) not in {int, float}
            or not math.isfinite(acquire_timeout)
            or not 0 <= acquire_timeout <= 60
        ):
            raise ValueError("Supervisor lock wait must be between 0 and 60 seconds")
        self.path = Path(lock_path)
        self.acquire_timeout = float(acquire_timeout)
        self._stream = None
        self._lock_backend = None

    @property
    def held(self):
        return self._stream is not None

    def acquire(self):
        if self.held:
            raise ValueError("This process already holds the supervisor lock")
        if self.path.is_symlink():
            raise ValueError("Supervisor lock cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.parent
        while current != current.parent:
            if current.is_symlink():
                raise ValueError("Supervisor lock path cannot contain symlinks")
            current = current.parent
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + self.acquire_timeout
            while True:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        self._lock_backend = msvcrt
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._lock_backend = fcntl
                    self._stream = stream
                    return self
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise PublicSupervisorBusy(
                            "Another public-evaluation supervisor is still active"
                        ) from exc
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except BaseException:
            stream.close()
            raise

    def release(self):
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        backend = self._lock_backend
        self._lock_backend = None
        try:
            stream.seek(0)
            if os.name == "nt":
                backend.locking(stream.fileno(), backend.LK_UNLCK, 1)
            else:
                backend.flock(stream.fileno(), backend.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()

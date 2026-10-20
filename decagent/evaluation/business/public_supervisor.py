"""Single-host process exclusion for live public benchmark execution."""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

PUBLIC_SUPERVISOR_LOCK_RELATIVE_PATH = Path(
    ".decagent/business-eval/public-evaluation.lock"
)
PUBLIC_WORKER_GUARD_RELATIVE_PATH = Path(
    ".decagent/business-eval/public-worker.lock"
)


def canonical_public_supervisor_lock_path(root):
    return Path(root).resolve() / PUBLIC_SUPERVISOR_LOCK_RELATIVE_PATH


def canonical_public_worker_guard_path(root):
    return Path(root).resolve() / PUBLIC_WORKER_GUARD_RELATIVE_PATH


class PublicSupervisorBusy(RuntimeError):
    """Another live evaluator or its dispatched worker still owns the run."""


class PublicWorkerBusy(RuntimeError):
    """A dispatched provider worker still owns its one-shot process guard."""


class _ExclusiveFileLock:
    def __init__(self, path, *, acquire_timeout, busy_type, busy_message):
        if (
            type(acquire_timeout) not in {int, float}
            or not math.isfinite(acquire_timeout)
            or not 0 <= acquire_timeout <= 60
        ):
            raise ValueError("Process lock wait must be between 0 and 60 seconds")
        self.path = Path(path)
        self.acquire_timeout = float(acquire_timeout)
        self.busy_type = busy_type
        self.busy_message = busy_message
        self._stream = None
        self._lock_backend = None

    @property
    def held(self):
        return self._stream is not None

    def acquire(self):
        if self.held:
            raise ValueError("This process already holds the requested process lock")
        if self.path.is_symlink():
            raise ValueError("Process lock cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.parent
        while current != current.parent:
            if current.is_symlink():
                raise ValueError("Process lock path cannot contain symlinks")
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
                        raise self.busy_type(self.busy_message) from exc
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


class PublicWorkerGuard:
    """Cross-process guard held from child startup through provider exit."""

    def __init__(self, path):
        self._lock = _ExclusiveFileLock(
            path,
            acquire_timeout=0.0,
            busy_type=PublicWorkerBusy,
            busy_message="A one-shot public provider worker is still active",
        )

    @property
    def held(self):
        return self._lock.held

    def acquire(self):
        return self._lock.acquire()

    def release(self):
        self._lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


class PublicSupervisor:
    """Hold process exclusion while no old provider worker can still dispatch.

    The SQLite lease fences ledger writes. The supervisor lock prevents another
    process from taking over while its owner is alive. The worker guard closes
    the narrower gap where the supervisor exits but an already-dispatched
    one-shot provider process has not yet observed that exit.
    """

    def __init__(self, lock_path, *, acquire_timeout=0.0, worker_guard_path=None):
        self.path = Path(lock_path)
        self.worker_guard_path = (
            Path(worker_guard_path)
            if worker_guard_path is not None
            else self.path.parent / "public-worker.lock"
        )
        self.acquire_timeout = acquire_timeout
        self._lock = _ExclusiveFileLock(
            self.path,
            acquire_timeout=acquire_timeout,
            busy_type=PublicSupervisorBusy,
            busy_message="Another public-evaluation supervisor is still active",
        )

    @property
    def held(self):
        return self._lock.held

    def acquire(self):
        if self.held:
            raise ValueError("This process already holds the supervisor lock")
        self._lock.acquire()
        probe = PublicWorkerGuard(self.worker_guard_path)
        try:
            probe.acquire()
        except PublicWorkerBusy as exc:
            self._lock.release()
            raise PublicSupervisorBusy(
                "A dispatched public provider worker is still active"
            ) from exc
        except BaseException:
            self._lock.release()
            raise
        probe.release()
        return self

    def release(self):
        self._lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()

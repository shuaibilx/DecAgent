"""Atomic durable receipts. Ambiguous starts are reconciled, never blindly replayed."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from .protocol import ExecutionRequest

TERMINAL = frozenset({"completed", "timed_out", "cancelled", "fenced", "result_unknown"})


class ReceiptStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, execution_id: str):
        if str(UUID(execution_id)) != execution_id:
            raise ValueError("Invalid execution identifier")
        path = self.root / f"{execution_id}.json"
        if path.is_symlink():
            raise ValueError("Receipt link forbidden")
        return path

    def read(self, execution_id: str):
        with self._lock:
            return json.loads(self._path(execution_id).read_text(encoding="utf-8"))

    def _write(self, execution_id: str, receipt: dict):
        path = self._path(execution_id)
        return self._atomic_write(path, receipt)

    def _atomic_write(self, path, receipt):
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".pending-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if os.name == "posix":
                descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return receipt

    def reserve(self, request: ExecutionRequest):
        with self._lock:
            path = self._path(request.execution_id)
            if path.exists():
                receipt = self.read(request.execution_id)
                if receipt["payload_hash"] != request.payload_hash:
                    raise ValueError("Execution identifier reused with different payload")
                return receipt, False
            receipt = {
                "execution_id": request.execution_id,
                "task_id": request.task_id,
                "action_id": request.action_id,
                "payload_hash": request.payload_hash,
                "owner": request.owner,
                "lease_generation": request.lease_generation,
                "state": "reserved",
                "exit_code": None,
                "output": "",
            }
            return self._write(request.execution_id, receipt), True

    def update(self, execution_id: str, **changes):
        with self._lock:
            receipt = self.read(execution_id) | changes
            return self._write(execution_id, receipt)

    def finish(self, execution_id: str, **changes):
        with self._lock:
            receipt = self.read(execution_id)
            if receipt["state"] in TERMINAL:
                return receipt
            return self._write(execution_id, receipt | changes)

    def _workspace_path(self, task_id):
        self._path(task_id)
        path = self.root / f"workspace-{task_id}.manifest"
        if path.is_symlink():
            raise ValueError("Workspace receipt link forbidden")
        return path

    def workspace_read(self, task_id):
        with self._lock:
            return json.loads(self._workspace_path(task_id).read_text(encoding="utf-8"))

    def workspace_reserve(self, task_id, digest):
        with self._lock:
            path = self._workspace_path(task_id)
            if path.exists():
                receipt = self.workspace_read(task_id)
                if receipt["digest"] != digest:
                    raise ValueError("Baseline is immutable; task already has another archive")
                return receipt
            return self._atomic_write(path, {"digest": digest, "state": "creating"})

    def workspace_update(self, task_id, **changes):
        with self._lock:
            receipt = self.workspace_read(task_id) | changes
            return self._atomic_write(self._workspace_path(task_id), receipt)

    def all(self):
        with self._lock:
            return [self.read(path.stem) for path in sorted(self.root.glob("*.json"))]

"""Authenticated broker transport; this adapter never invokes Docker or a host subprocess."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import httpx

from decagent.executor.archive import read_archive, snapshot_hash
from decagent.executor.protocol import (
    ExecutionRequest,
    ExecutionScope,
    FileRequest,
    ScopeRequest,
    WriteRequest,
)
from decagent.executor.receipts import TERMINAL
from decagent.policy import Action

from .models import Command, ExecutionResult


@dataclass(frozen=True)
class GovernedAction:
    action_id: str
    digest: str
    execution_id: str | None = None
    cached_result: ExecutionResult | None = None


class HttpBrokerClient:
    """Control-service credential stays here; snapshots are private worker data."""

    def __init__(self, base_url: str, service_token: str, *, transport=None):
        if len(service_token) < 32:
            raise ValueError("Broker service authentication is required")
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": "Bearer " + service_token},
            timeout=30,
            trust_env=False,
            transport=transport,
        )

    def __call__(self, path: str, payload: dict) -> dict:
        response = self.client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    def close(self):
        self.client.close()


class BrokerRepairBackend:
    def __init__(
        self,
        call: Callable[[str, dict], dict],
        scope: ScopeRequest,
        authorize: Callable[[Action, Command | None], GovernedAction],
        record_result: Callable[[GovernedAction, ExecutionResult], None] | None = None,
    ):
        self.call, self.scope, self.authorize = call, scope, authorize
        self.record_result = record_result

    def snapshot(self) -> dict[str, bytes]:
        self.authorize(Action("search", {"query": ".", "paths": ["."]}), None)
        response = self.call("/v1/workspaces/snapshot", self.scope.model_dump())
        files = read_archive(base64.b64decode(response["archive_base64"], validate=True))
        if snapshot_hash(files) != response["workspace_hash"]:
            raise ValueError("Broker snapshot integrity mismatch")
        return files

    def list_files(self) -> tuple[str, ...]:
        return tuple(sorted(self.snapshot()))

    def read(self, path: str) -> bytes:
        request = FileRequest(**self.scope.model_dump(), path=path)
        self.authorize(Action("read_file", {"path": request.path}), None)
        response = self.call("/v1/workspaces/snapshot", self.scope.model_dump())
        files = read_archive(base64.b64decode(response["archive_base64"], validate=True))
        if snapshot_hash(files) != response["workspace_hash"]:
            raise ValueError("Broker snapshot integrity mismatch")
        return files[request.path]

    def write(self, path: str, content: bytes) -> None:
        request = FileRequest(
            **self.scope.model_dump(),
            path=path,
            content_base64=base64.b64encode(content).decode(),
        )
        grant = self.authorize(
            Action(
                "write_file",
                {
                    "path": request.path,
                    "content": content.decode("utf-8"),
                },
            ),
            None,
        )
        write = WriteRequest(
            **request.model_dump(), action_id=grant.action_id, action_digest=grant.digest
        )
        self.call("/v1/workspaces/write", write.model_dump())

    def diff(self) -> str:
        self.authorize(Action("diff", {}), None)
        return self.call("/v1/workspaces/diff", self.scope.model_dump())["diff"]

    def execute(self, command: Command) -> ExecutionResult:
        maximum = 600 if command.kind == "setup" else 120
        if command.timeout_seconds > maximum:
            raise ValueError("Repair command exceeds class timeout")
        params = {
            "purpose": command.source,
            "argv": list(command.argv),
            "cwd": ".",
            "timeout_seconds": command.timeout_seconds,
            "max_output_bytes": 65536,
        }
        grant = self.authorize(Action("run_command", params), command)
        if grant.cached_result is not None:
            return grant.cached_result
        request = ExecutionRequest(
            **self.scope.model_dump(),
            action_id=grant.action_id,
            action_digest=grant.digest,
            execution_id=grant.execution_id or str(uuid4()),
            argv=list(command.argv),
            kind=command.kind,
            purpose=command.source,
            timeout_seconds=command.timeout_seconds,
        )
        result = self.call("/v1/executions", request.model_dump())
        execution_scope = ExecutionScope(
            **self.scope.model_dump(),
            execution_id=request.execution_id,
        ).model_dump()
        deadline = time.monotonic() + command.timeout_seconds + 15
        while result["state"] not in TERMINAL:
            if time.monotonic() >= deadline:
                self.call("/v1/executions/cancel", execution_scope)
                raise TimeoutError(
                    "Broker did not confirm completion; verification remains unknown"
                )
            time.sleep(0.1)
            result = self.call("/v1/executions/status", execution_scope)
        if result["execution_id"] != request.execution_id:
            raise ValueError("Broker execution receipt identity mismatch")
        completed = ExecutionResult(
            result["execution_id"],
            result["state"],
            result.get("exit_code"),
            result.get("output", ""),
            bool(result.get("truncated")),
        )
        if self.record_result:
            self.record_result(grant, completed)
        return completed

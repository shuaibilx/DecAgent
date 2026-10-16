"""Authenticated execution service with SQL lease fencing and durable execution identities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from decagent.policy.redaction import Redactor

from .archive import make_archive, read_archive, snapshot_hash, workspace_diff
from .protocol import (
    ExecutionRequest,
    ExecutionScope,
    FileRequest,
    ScopeRequest,
    TaskControlRequest,
    WorkspaceRequest,
    WriteRequest,
)
from .receipts import TERMINAL, ReceiptStore


def snapshot_response(files):
    redactor = Redactor(max_text_chars=1048576)
    for content in files.values():
        text = content.decode("utf-8", "replace")
        # Check in bounded overlapping windows without truncating a large binary/source file.
        for start in range(0, len(text), 524288):
            window = text[max(0, start - 65536) : start + 524288]
            if redactor.redact_text(window) != window:
                raise ValueError("Snapshot contains suspected credential material")
    archive = make_archive(files)
    return {
        "archive_base64": base64.b64encode(archive).decode(),
        "workspace_hash": snapshot_hash(files),
    }


def match_action(request: ExecutionRequest, snapshot: dict):
    params = snapshot.get("params", {})
    if snapshot.get("tool") != "run_command" or any(
        params.get(key, default) != getattr(request, key)
        for key, default in [
            ("argv", None),
            ("cwd", "."),
            ("timeout_seconds", 120),
            ("max_output_bytes", 65536),
            ("purpose", "command"),
        ]
    ):
        raise ValueError("Execution parameters do not match governed action")
    setup = snapshot.get("execution_kind") == "setup" or params.get("purpose") == "setup"
    if (request.kind == "setup") != setup:
        raise ValueError("Execution class does not match governed action")


def match_write(request: WriteRequest, snapshot: dict, content: bytes, *, current=None):
    params = snapshot.get("params", {})
    if snapshot.get("tool") == "edit_file" and params.get("path") == request.path:
        try:
            text = current.decode("utf-8")
            if text.count(params["old"]) == params["expected_matches"] and (
                text.replace(params["old"], params["new"]).encode("utf-8") == content
            ):
                return
        except (AttributeError, UnicodeError, KeyError, TypeError):
            pass
        raise ValueError("Edit parameters do not match governed action")
    if (
        snapshot.get("tool") != "write_file"
        or params.get("path") != request.path
        or not isinstance(params.get("content"), str)
        or params["content"].encode("utf-8") != content
    ):
        raise ValueError("Write parameters do not match governed action")


class Broker:
    def __init__(self, database, engine, receipt_root: Path):
        self.database = database
        self.engine = engine
        self.store = ReceiptStore(receipt_root)
        self.ready = False
        self.diagnostics = {}
        self.task_locks = defaultdict(threading.RLock)
        self.active = {}
        self.verified_tasks = set()
        self.redactor = Redactor(max_text_chars=1048576)
        self._instance_lock = None

    def initialize(self):
        self.ready = False
        self.diagnostics = self.engine.initialize()
        # Single broker owns this journal/socket. Fail rather than let replicas race starts.
        import fcntl

        self._instance_lock = open(self.store.root / "broker.lock", "a+b")
        fcntl.flock(self._instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.reconcile()
        # No repository command is run: this uses an empty disposable diagnostic workspace.
        task_id = str(uuid4())
        try:
            self.engine.create_workspace(task_id, {})
            self.diagnostics["boundary"] = self.engine.boundary_probe(task_id)
        finally:
            self.engine.remove_workspace(task_id)
        self.ready = True
        return self.diagnostics

    def reconcile(self):
        for receipt in self.store.all():
            if receipt["state"] in TERMINAL:
                continue
            name = self.engine.container_name(receipt["execution_id"])
            try:
                result = self.engine.result(name)
                if result is not None:
                    self._complete(receipt, result)
                    continue
                # A broker restart loses its active monitor. Stop the whole container before
                # recording unknown; do not silently replay even if creation is ambiguous.
                self.engine.kill(name)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
            self.store.update(
                receipt["execution_id"],
                state="result_unknown",
                output="Broker restarted before a confirmed execution receipt",
            )

    @contextmanager
    def fenced(self, request: ScopeRequest):
        with self.database.connect() as connection:
            task = self.database.assert_lease(
                request.task_id,
                request.owner,
                request.lease_generation,
                connection,
            )
            yield connection, task

    def check_action(self, request, connection):
        row = connection.execute(
            "SELECT * FROM app.actions WHERE id=%s AND task_id=%s AND digest=%s FOR UPDATE",
            (request.action_id, request.task_id, request.action_digest),
        ).fetchone()
        if not row or row["status"] not in {"planned", "running", "completed"}:
            raise ValueError("Action missing, rejected, or outside task")
        match_action(request, row["snapshot"])
        execution_id = (row.get("receipt") or {}).get("execution_id")
        attempts = connection.execute(
            "SELECT execution_id FROM app.action_attempts WHERE action_id=%s FOR UPDATE",
            (request.action_id,),
        ).fetchall()
        if (execution_id and execution_id != request.execution_id) or any(
            attempt["execution_id"] != request.execution_id for attempt in attempts
        ):
            raise ValueError("Action is already bound to another execution identifier")

    def workspace(self, request: WorkspaceRequest):
        if not self.ready:
            raise ValueError("Execution boundary is unavailable")
        with self.task_locks[request.task_id]:
            self._idle(request.task_id)
            files = read_archive(base64.b64decode(request.archive_base64, validate=True))
            digest = snapshot_hash(files)
            with self.fenced(request):
                try:
                    existing = self.store.workspace_read(request.task_id)
                except FileNotFoundError:
                    existing = None
                receipt = self.store.workspace_reserve(request.task_id, digest)
                if existing:
                    if receipt["state"] != "verified":
                        raise ValueError("Workspace creation unconfirmed; reconciliation required")
                    return {"task_id": request.task_id, "baseline_hash": digest}
            try:
                with self.fenced(request):
                    self.engine.create_workspace(request.task_id, files)
                # Check the actual per-task network, as well as the diagnostic network.
                self.engine.boundary_probe(request.task_id)
                with self.fenced(request):
                    self.store.workspace_update(request.task_id, state="verified")
                    self.verified_tasks.add(request.task_id)
            except Exception:
                self.store.workspace_update(request.task_id, state="blocked")
                raise
            return {"task_id": request.task_id, "baseline_hash": digest}

    def _idle(self, task_id):
        if task_id in self.active:
            raise ValueError("Task has an active execution")

    def files(self, request: ScopeRequest):
        with self.task_locks[request.task_id], self.fenced(request):
            self._idle(request.task_id)
            return self.engine.snapshot(request.task_id)

    def read_file(self, request: FileRequest):
        content = self.files(request).get(request.path)
        if content is None:
            raise KeyError("File missing from task")
        if len(content) > 1048576:
            raise ValueError("File exceeds read limit")
        # File bytes may contain secrets; the API returns redacted text, never raw evidence.
        return {
            "path": request.path,
            "text": self.redactor.redact_text(content.decode("utf-8", "replace")),
        }

    def snapshot(self, request: ScopeRequest):
        return snapshot_response(self.files(request))

    def write_file(self, request: WriteRequest):
        if request.content_base64 is None:
            raise ValueError("Write content is required")
        content = base64.b64decode(request.content_base64, validate=True)
        if len(content) > 1048576:
            raise ValueError("Write exceeds file limit")
        with self.task_locks[request.task_id], self.fenced(request) as (connection, _):
            self._idle(request.task_id)
            row = connection.execute(
                "SELECT * FROM app.actions WHERE id=%s AND task_id=%s AND digest=%s FOR UPDATE",
                (request.action_id, request.task_id, request.action_digest),
            ).fetchone()
            if not row or row["status"] not in {"planned", "running", "completed"}:
                raise ValueError("Write action missing, rejected, or outside task")
            current = (
                self.engine.snapshot(request.task_id).get(request.path)
                if row["snapshot"].get("tool") == "edit_file"
                else None
            )
            match_write(request, row["snapshot"], content, current=current)
            self.engine.write(request.task_id, request.path, content)
        return {"path": request.path, "size": len(content)}

    def diff(self, request: ScopeRequest):
        with self.task_locks[request.task_id], self.fenced(request):
            self._idle(request.task_id)
            baseline = self.engine.snapshot(request.task_id, baseline=True)
            current = self.engine.snapshot(request.task_id)
            return {
                "diff": self.redactor.redact_text(workspace_diff(baseline, current)),
                "baseline_hash": snapshot_hash(baseline),
                "workspace_hash": snapshot_hash(current),
            }

    def execute(self, request: ExecutionRequest):
        if not self.ready:
            raise ValueError("Execution boundary is unavailable")
        with self.task_locks[request.task_id]:
            with self.fenced(request) as (connection, _):
                self.check_action(request, connection)
                try:
                    self.store.read(request.execution_id)
                except FileNotFoundError:
                    pass
                else:
                    return self.store.reserve(request)[0]
                self._idle(request.task_id)
                workspace = self.store.workspace_read(request.task_id)
                if workspace["state"] != "verified":
                    raise ValueError("Task workspace not initialized")
                self.store.reserve(request)
            if request.task_id not in self.verified_tasks:
                self.engine.boundary_probe(request.task_id)
                self.verified_tasks.add(request.task_id)
            name = self.engine.container_name(request.execution_id)
            self.store.update(request.execution_id, state="starting", container=name)
            try:
                with self.fenced(request) as (connection, _):
                    self.check_action(request, connection)
                    connection.execute(
                        "INSERT INTO app.action_attempts"
                        "(id,action_id,generation,execution_id,status) "
                        "VALUES(%s,%s,%s,%s,'starting') ON CONFLICT(execution_id) DO NOTHING",
                        (
                            uuid4(),
                            request.action_id,
                            request.lease_generation,
                            request.execution_id,
                        ),
                    )
                    self.engine.create_container(name, self.engine.command_config(request))
                    valid = connection.execute(
                        "SELECT id FROM app.tasks WHERE id=%s AND lease_until>clock_timestamp()",
                        (request.task_id,),
                    ).fetchone()
                    if not valid:
                        raise ValueError("Lease expired before container start")
                    self.engine.start(name)
                    self.active[request.task_id] = request.execution_id
                    self.store.update(request.execution_id, state="running")
                thread = threading.Thread(target=self._monitor, args=(request,), daemon=True)
                thread.start()
            except Exception:
                self.engine.kill(name)
                self.store.update(
                    request.execution_id,
                    state="result_unknown",
                    output="Container start was not confirmed; reconciliation required",
                )
                raise
            return self.store.read(request.execution_id)

    def _complete(self, receipt, result, state=None):
        with self.task_locks[receipt["task_id"]]:
            output = self.redactor.redact_text(result.get("output", ""))
            if state is None:
                state = "timed_out" if result.get("timed_out") else "completed"
            value = self.store.finish(
                receipt["execution_id"],
                state=state,
                output=output,
                exit_code=result.get("exit_code"),
                truncated=bool(result.get("truncated")),
                resource_limit=result.get("resource_limit"),
            )
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE app.action_attempts SET status=%s,receipt=%s WHERE execution_id=%s",
                    (value["state"], Jsonb(value), receipt["execution_id"]),
                )
            self.active.pop(receipt["task_id"], None)
            return value

    def _monitor(self, request):
        name = self.engine.container_name(request.execution_id)
        deadline = time.monotonic() + request.timeout_seconds + 5
        try:
            while True:
                receipt = self.store.read(request.execution_id)
                if receipt["state"] in TERMINAL:
                    return
                result = self.engine.result(name)
                if result is not None:
                    self._complete(receipt, result)
                    return
                try:
                    self.database.assert_lease(
                        request.task_id,
                        request.owner,
                        request.lease_generation,
                    )
                except Exception:
                    self.engine.kill(name)
                    self._complete(
                        receipt,
                        {"output": "Execution stopped: lease no longer valid", "exit_code": None},
                        state="fenced",
                    )
                    return
                if time.monotonic() >= deadline:
                    self.engine.kill(name)
                    self._complete(
                        receipt,
                        {"output": "Execution stopped at broker deadline", "exit_code": None},
                        state="timed_out",
                    )
                    return
                time.sleep(0.1)
        except Exception:
            # An unavailable database or daemon never grants continued execution.
            try:
                self.engine.kill(name)
            finally:
                self.store.finish(
                    request.execution_id,
                    state="result_unknown",
                    output="Execution monitor failed; reconciliation required",
                )
        finally:
            with self.task_locks[request.task_id]:
                self.active.pop(request.task_id, None)

    def status(self, request: ExecutionScope):
        with self.task_locks[request.task_id], self.fenced(request):
            receipt = self.store.read(request.execution_id)
            if receipt["task_id"] != request.task_id:
                raise ValueError("Execution belongs to another task")
            return receipt

    def cancel(self, request: ExecutionScope):
        with self.task_locks[request.task_id], self.fenced(request):
            receipt = self.store.read(request.execution_id)
            if receipt["task_id"] != request.task_id:
                raise ValueError("Execution belongs to another task")
            if receipt["state"] in TERMINAL:
                return receipt
            self.engine.kill(self.engine.container_name(request.execution_id))
            return self._complete(
                receipt, {"output": "Execution cancelled", "exit_code": None}, state="cancelled"
            )

    @contextmanager
    def controlled(self, request: TaskControlRequest):
        with self.task_locks[request.task_id], self.database.connect() as connection:
            task = connection.execute(
                "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (request.task_id,)
            ).fetchone()
            if not task or (task["lease_generation"], task["lease_owner"]) != (
                request.lease_generation,
                request.owner,
            ):
                raise ValueError("Task control generation or owner differs")
            yield connection, task

    def stop_task(self, request: TaskControlRequest):
        with self.controlled(request) as (connection, task):
            containers = self.engine.api(
                "GET",
                "/containers/json",
                params={
                    "all": "true",
                    "filters": json.dumps(
                        {
                            "label": [
                                "decagent.managed=executor",
                                "decagent.task=" + request.task_id,
                                "decagent.executor.instance=" + self.engine.proxy_container,
                            ]
                        }
                    ),
                },
            )
            for container in containers:
                self.engine.kill(container["Id"])
                if self.engine.inspect(container["Id"])["State"]["Running"]:
                    raise ValueError("Task container stop was not confirmed")
            stopped = []
            for receipt in self.store.all():
                if receipt["task_id"] == request.task_id and receipt["state"] not in TERMINAL:
                    self.store.finish(
                        receipt["execution_id"],
                        state="cancelled",
                        output="Task cancelled by owner",
                        exit_code=None,
                    )
                    stopped.append(receipt["execution_id"])
            self.active.pop(request.task_id, None)
            if task["state"] not in {"cancelled", "failed", "succeeded"}:
                # Fence further commands before releasing the row lock; API then performs CAS.
                connection.execute(
                    "UPDATE app.tasks SET state='recovering',lease_until=NULL WHERE id=%s",
                    (request.task_id,),
                )
            return {
                "stopped": True,
                "execution_ids": stopped,
                "lease_generation": request.lease_generation,
                "state": "recovering",
            }

    def reconcile_task(self, request: TaskControlRequest):
        with self.controlled(request) as (connection, _):
            actions = connection.execute(
                "SELECT * FROM app.actions WHERE task_id=%s "
                "AND status IN ('running','unknown') FOR UPDATE",
                (request.task_id,),
            ).fetchall()
            unknown = []
            for action in actions:
                saved = action["receipt"] or {}
                execution_id = saved.get("execution_id")
                receipt = None
                if action["snapshot"].get("tool") in {"write_file", "edit_file"} and (
                    saved.get("write_expected_sha") and saved.get("path")
                ):
                    content = self.engine.snapshot(request.task_id).get(saved["path"])
                    if (
                        content is not None
                        and hashlib.sha256(content).hexdigest() == saved["write_expected_sha"]
                    ):
                        saved["status"] = "succeeded"
                        saved["reconciled_effect"] = True
                        connection.execute(
                            "UPDATE app.actions SET status='succeeded',receipt=%s WHERE id=%s",
                            (Jsonb(saved), action["id"]),
                        )
                        continue
                if action["snapshot"].get("tool") == "run_command" and execution_id:
                    try:
                        receipt = self.store.read(execution_id)
                    except FileNotFoundError:
                        pass
                    if receipt and receipt["state"] == "completed":
                        status = "succeeded" if receipt["exit_code"] == 0 else "failed"
                        saved = {**receipt, "status": status}
                        connection.execute(
                            "UPDATE app.actions SET status=%s,receipt=%s WHERE id=%s",
                            (status, Jsonb(saved), action["id"]),
                        )
                        continue
                unknown.append(str(action["id"]))
            return {
                "reconciled": not unknown,
                "unknown_action_ids": unknown,
                "lease_generation": request.lease_generation,
            }


def create_app(broker: Broker, service_token: str):
    if len(service_token) < 32:
        raise ValueError("Broker credential must contain at least 32 characters")

    def authenticate(authorization: str | None = Header(default=None)):
        if authorization is None or not hmac.compare_digest(
            authorization, "Bearer " + service_token
        ):
            raise HTTPException(401, "Internal service authentication required")

    app = FastAPI(
        dependencies=[Depends(authenticate)], docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        # Pydantic's default error includes the raw rejected value, possibly a credential.
        fields = (
            {"body", "query", "header"}
            | set(ExecutionRequest.model_fields)
            | set(WorkspaceRequest.model_fields)
            | set(FileRequest.model_fields)
            | set(WriteRequest.model_fields)
            | set(TaskControlRequest.model_fields)
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        "loc": [
                            part if isinstance(part, int) or part in fields else "[field]"
                            for part in item["loc"]
                        ],
                        "type": item["type"],
                        "msg": "Invalid broker parameter",
                    }
                    for item in error.errors()
                ]
            },
        )

    def invoke(function, request):
        try:
            return function(request)
        except (ValueError, KeyError, FileNotFoundError) as error:
            raise HTTPException(409, broker.redactor.redact_text(str(error))) from error
        except Exception as error:
            raise HTTPException(503, "Executor service unavailable") from error

    @app.get("/health")
    def health():
        if not broker.ready:
            raise HTTPException(503, "Execution boundary unavailable")
        return {"ready": True, "diagnostics": broker.diagnostics}

    @app.post("/v1/workspaces")
    def workspace(request: WorkspaceRequest):
        return invoke(broker.workspace, request)

    @app.post("/v1/workspaces/read")
    def read(request: FileRequest):
        return invoke(broker.read_file, request)

    @app.post("/v1/workspaces/write")
    def write(request: WriteRequest):
        return invoke(broker.write_file, request)

    @app.post("/v1/workspaces/diff")
    def diff(request: ScopeRequest):
        return invoke(broker.diff, request)

    @app.post("/v1/workspaces/snapshot")
    def snapshot(request: ScopeRequest):
        return invoke(broker.snapshot, request)

    @app.post("/v1/executions")
    def execute(request: ExecutionRequest):
        return invoke(broker.execute, request)

    @app.post("/v1/executions/status")
    def status(request: ExecutionScope):
        return invoke(broker.status, request)

    @app.post("/v1/executions/cancel")
    def cancel(request: ExecutionScope):
        return invoke(broker.cancel, request)

    @app.post("/v1/tasks/stop")
    def stop_task(request: TaskControlRequest):
        return invoke(broker.stop_task, request)

    @app.post("/v1/tasks/reconcile")
    def reconcile_task(request: TaskControlRequest):
        return invoke(broker.reconcile_task, request)

    return app

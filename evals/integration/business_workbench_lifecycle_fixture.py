"""Offline browser acceptance for a newly submitted employee task lifecycle.

Starts the production gateway, worker and loopback MCP service against a new
temporary PostgreSQL database. The employee submits an exact synthetic ticket
operation through the UI, confirms it, and the worker records the real receipt.
No external model or business service is contacted.
"""

import json
import os
import socket
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from decagent.business.employee_accounts import hash_employee_password
from decagent.business.mcp_server import create_mcp_app
from decagent.business.registry import PluginRegistry
from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from decagent.runtime.config import RuntimeConfig
from decagent.storage.database import Database
from evals.integration.business_support import FixtureTickets

HOST, PORT = "127.0.0.1", 8856
PASSWORD = "Offline-Lifecycle-Acceptance-2026"


def _start_mcp(database, employee_auth):
    listener = socket.socket()
    listener.bind((HOST, 0))
    port = listener.getsockname()[1]
    listener.listen()
    app = create_mcp_app(database, employee_auth, port=port)
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=port, lifespan="on", log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(5)
        listener.close()
        raise RuntimeError("Offline lifecycle MCP service did not start")
    return server, thread, listener, port


def _stop_mcp(service):
    server, thread, listener, _port = service
    server.should_exit = True
    thread.join(10)
    listener.close()
    if thread.is_alive():
        raise RuntimeError("Offline lifecycle MCP service did not stop")


def build_fixture():
    source_url = os.environ.get("DECAGENT_TEST_DATABASE_URL")
    if not source_url:
        raise SystemExit("Set DECAGENT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    administrator = Database(source_url)
    name = "workbench_lifecycle_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = conninfo_to_dict(source_url)
    target["dbname"] = name
    database = Database(make_conninfo(**target))
    database.migrate()

    backend = FixtureTickets(database, login="supervisor")
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET password_hash=%s WHERE id=%s",
            (hash_employee_password(PASSWORD), backend.identity.employee_id),
        )
    directory = backend.read("directory", {})
    category_id = next(
        item["id"] for item in directory["categories"] if item["name"] == "Network"
    )
    created = backend.execute(
        "create",
        {"title": "Browser lifecycle acceptance", "category_id": category_id},
        "fixture-workbench-lifecycle-create",
    )
    if not created["ok"]:
        raise RuntimeError("Could not seed the synthetic acceptance ticket")
    ticket_id = created["data"]["ticket"]["id"]
    scope = PluginRegistry(database).list()[0]

    temporary = tempfile.TemporaryDirectory(prefix="decagent-workbench-lifecycle-")
    public_url = f"http://{HOST}:{PORT}"
    settings = Settings(
        database_url=database.url,
        public_url=public_url,
        data_dir=Path(temporary.name),
    )
    app = create_app(
        database,
        settings,
        callbacks=GatewayCallbacks(
            business_employee_read=lambda _identity, command, params: backend.read(command, params)
        ),
        runtime_config=RuntimeConfig(conditional_workflows_enabled=False),
    )
    employee_auth = app.state.employee_auth
    mcp_service = _start_mcp(database, employee_auth)
    stop = threading.Event()
    worker_thread = None
    worker = None

    def consume():
        while not stop.is_set():
            try:
                claimed = worker.run_once()
            except Exception as exc:  # surfaced in the fixture server log for diagnosis
                print(f"offline worker error: {type(exc).__name__}: {exc}", flush=True)
                stop.wait(0.2)
            else:
                if not claimed:
                    stop.wait(0.1)

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal worker, worker_thread
        runtime = RuntimeConfig(
            tickets_mcp_url=f"http://127.0.0.1:{mcp_service[3]}/mcp",
            strategy="rule",
            conditional_workflows_enabled=False,
        )
        worker = BusinessWorker(database, settings, runtime, employee_auth=employee_auth)
        worker_thread = threading.Thread(
            target=consume, daemon=True, name="workbench-lifecycle-worker"
        )
        worker_thread.start()
        try:
            yield
        finally:
            stop.set()
            worker_thread.join(10)
            worker.close()

    app.router.lifespan_context = lifespan
    instruction = json.dumps(
        {
            "command": "set_priority",
            "params": {"ticket_id": ticket_id, "expected_revision": 1, "priority": "high"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    metadata = {
        "url": public_url,
        "login": backend.identity.login,
        "password": PASSWORD,
        "scope_id": str(scope["id"]),
        "ticket_id": ticket_id,
        "instruction": instruction,
        "expected_final_priority": "high",
        "paid_requests": 0,
        "fixture": "temporary PostgreSQL + production gateway/worker/MCP; synthetic data only",
    }
    return administrator, name, app, metadata, mcp_service, temporary


if __name__ == "__main__":
    administrator, name, app, metadata, mcp_service, temporary = build_fixture()
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    finally:
        _stop_mcp(mcp_service)
        temporary.cleanup()
        with administrator.connect(autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )

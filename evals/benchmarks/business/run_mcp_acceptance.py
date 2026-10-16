"""Run the fixed offline MCP benchmark and save its machine-readable report.

Requires DECAGENT_TEST_DATABASE_URL with CREATE DATABASE rights. The source
database is read only; each run creates a new mcpbench_* database for audit.
"""

import argparse
import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import uvicorn
from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_server import create_mcp_app
from decagent.evaluation.business.mcp_acceptance import (
    MCPAcceptanceHarness,
    provision_synthetic_actors,
)
from decagent.storage.database import Database
from psycopg import sql
from psycopg.conninfo import make_conninfo


def _start_service(database, auth):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_mcp_app(database, auth, port=port)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(10)
        listener.close()
        raise RuntimeError("Acceptance MCP service did not start")
    return server, thread, listener, f"http://127.0.0.1:{port}/mcp"


def _stop_service(service):
    server, thread, listener, _ = service
    server.should_exit = True
    thread.join(10)
    listener.close()
    if thread.is_alive():
        raise RuntimeError("Acceptance MCP service did not stop")


def run_disposable_acceptance(source_url, report_path):
    """Create a fresh database, exercise a restarted service, and write JSON once."""
    if not source_url:
        raise ValueError("DECAGENT_TEST_DATABASE_URL is required")
    name = "mcpbench_" + uuid4().hex
    with Database(source_url).connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(make_conninfo(source_url, dbname=name))
    database.migrate()
    with tempfile.TemporaryDirectory(prefix="decagent-mcp-acceptance-") as temporary:
        auth = EmployeeAuth(database, "http://127.0.0.1:8000", False, Path(temporary))
        actors = provision_synthetic_actors(database, auth)
        active = _start_service(database, auth)

        def restart():
            nonlocal active
            _stop_service(active)
            active = _start_service(database, auth)
            return active[3]

        try:
            report = MCPAcceptanceHarness(
                database, active[3], auth, actors, restart_service=restart
            ).run()
        finally:
            _stop_service(active)
    report["disposable_database"] = name
    report["generated_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    default_output = (
        Path("generated")
        / now.strftime("%y%m%d")
        / (now.strftime("%H%M") + "_MCP多角色业务验收.json")
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args()
    report = run_disposable_acceptance(os.getenv("DECAGENT_TEST_DATABASE_URL"), args.output)
    print(
        json.dumps(
            {
                "report": str(args.output),
                "passed": report["passed"],
                "denominator": report["denominator"],
                "forbidden_domain_side_effects": report["forbidden_domain_side_effects"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] == report["denominator"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

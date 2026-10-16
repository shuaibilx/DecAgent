"""Verify an installed wheel outside the checkout, using a disposable database."""

import argparse
import contextlib
import io
import json
import os
import socket
import sys
import threading
import time
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args()
    package_root = args.package_root.resolve()
    sys.path.insert(0, str(package_root))
    import uvicorn
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    import decagent
    from decagent.business.contracts import PluginSpec
    from decagent.business.employee_auth import EmployeeAuth
    from decagent.business.mcp_client import MCPConnectionManager
    from decagent.business.mcp_server import create_mcp_app
    from decagent.business.tickets import builtin_manifest
    from decagent.cli import main as administrative
    from decagent.evaluation.business.mcp_acceptance import provision_synthetic_actors
    from decagent.storage.database import SCHEMA_VERSION, Database

    assert Path(decagent.__file__).resolve().is_relative_to(package_root)
    assert version("mcp") == "2.3.0"
    spec = PluginSpec.from_mapping(builtin_manifest())
    assert len(spec.wire_tools()) == 14 and spec.skill_text()
    source_url = os.environ["DECAGENT_TEST_DATABASE_URL"]
    name = "case_wheel_" + uuid4().hex
    administrator = Database(source_url)
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(make_conninfo(source_url, dbname=name))
    active = None
    try:
        database.migrate()
        os.environ["DECAGENT_DATABASE_URL"] = database.url
        os.environ["DECAGENT_DATA_DIR"] = str(package_root / "probe-state")
        auth = EmployeeAuth(
            database, "http://127.0.0.1:8000", False, package_root / "probe-state/demo-auth"
        )
        actors = provision_synthetic_actors(database, auth)
        with contextlib.redirect_stdout(io.StringIO()):
            administrative(["plugins", "list"])
            administrative(["business", "accounts", "seed"])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_mcp_app(database, auth, port=port),
                host="127.0.0.1",
                port=port,
                log_level="error",
                lifespan="on",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        active = server, thread, listener
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        with MCPConnectionManager(f"http://127.0.0.1:{port}/mcp", auth) as manager:
            employee = manager.client(*actors.credentials["employee-it"])
            prepared = employee.prepare(
                "wheel-create",
                "create",
                {
                    "title": "Installed wheel ticket",
                    "category_id": actors.category_id,
                },
            )
            assert prepared["state"] == "ready"
            committed = employee.commit(prepared["operation_id"])
            assert committed["state"] == "succeeded"
            assert employee.query_operation(prepared["operation_id"]) == committed
            protocol = employee.protocol_version
        with database.connect() as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()[
                    "n"
                ]
                == 1
            )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "package_origin": str(Path(decagent.__file__).resolve()),
                    "cwd": str(Path.cwd()),
                    "mcp_sdk": version("mcp"),
                    "protocol": protocol,
                    "plugin": spec.id,
                    "plugin_version": spec.version,
                    "schema_version": SCHEMA_VERSION,
                    "wire_tools": 14,
                    "skill_verified": True,
                    "administrative_commands": True,
                    "committed_receipts": 1,
                    "model_calls": 0,
                },
                ensure_ascii=False,
            )
        )
    finally:
        if active is not None:
            server, thread, listener = active
            server.should_exit = True
            thread.join(10)
            listener.close()
            assert not thread.is_alive()
        with administrator.connect(autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()

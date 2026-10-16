"""Fixed scripted multi-role acceptance over real MCP HTTP and disposable PostgreSQL."""

import json
import os
import socket
import threading
import time
from uuid import uuid4

import pytest
import uvicorn
from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_server import create_mcp_app
from decagent.evaluation.business.mcp_acceptance import (
    SCENARIOS,
    MCPAcceptanceHarness,
    provision_synthetic_actors,
)
from decagent.storage.database import Database
from psycopg import sql

pytestmark = pytest.mark.integration


@pytest.fixture
def acceptance_service(tmp_path):
    source_url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not source_url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    name = "mcpbench_" + uuid4().hex
    with Database(source_url).connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(source_url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    auth = EmployeeAuth(database, "http://127.0.0.1:8000", False, tmp_path / "auth")
    actors = provision_synthetic_actors(database, auth)

    active = {}

    def start():
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_mcp_app(database, auth, port=port),
                host="127.0.0.1",
                port=port,
                lifespan="on",
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        active.update(server=server, thread=thread, listener=listener)
        return f"http://127.0.0.1:{port}/mcp"

    def stop():
        active["server"].should_exit = True
        active["thread"].join(10)
        active["listener"].close()
        assert not active["thread"].is_alive()

    def restart():
        stop()
        return start()

    endpoint = start()
    try:
        yield database, endpoint, auth, actors, restart
    finally:
        stop()


def test_fixed_multirole_acceptance(acceptance_service):
    database, endpoint, auth, actors, restart = acceptance_service
    report = MCPAcceptanceHarness(database, endpoint, auth, actors, restart_service=restart).run()

    assert report["denominator"] == len(SCENARIOS) == 11
    assert report["attempted"] == report["passed"] == 11, report["scenarios"]
    assert report["failed"] == report["not_run"] == 0
    assert report["forbidden_domain_side_effects"] == 0
    assert report["protocol_version"] == "2026-07-28"
    assert report["evidence_mode"] == "scripted_offline_no_model"
    assert report["model_calls"] == report["jev_calls"] == 0
    assert report["model_decision_accuracy"] is None
    assert report["human_wait_seconds"] == 0.0
    assert report["scripted_browser_decisions"] == 3
    latency = report["machine_mcp_call_latency_seconds"]
    assert latency["count"] >= 25
    assert 0 <= latency["p50"] <= latency["p95"]

    rows = {row["id"]: row for row in report["scenarios"]}
    assert rows["ambiguous_person"]["evidence"]["candidate_count"] == 2
    assert rows["unauthorized_assign"]["evidence"]["error_code"] == "forbidden"
    assert rows["inaccessible_resource"]["evidence"]["error_code"] == "not_found"
    assert rows["rejected_close"]["evidence"]["state"] == "rejected"
    assert rows["concurrent_commit"]["evidence"]["identical_results"]
    assert rows["server_restart_recovery"]["evidence"]["recovered"]
    assert rows["server_restart_recovery"]["evidence"]["server_restarted"]
    assert all(
        row["domain_delta"] == (0, 0, 0)
        for row in report["scenarios"]
        if row["expected_effect"] == "no_write"
    )
    with database.connect() as conn:
        counts = tuple(
            conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        )
        assert counts == (3, 7, 7)
    serialized = json.dumps(report)
    for _, token in actors.credentials.values():
        assert token not in serialized


def test_provision_refuses_non_disposable_database():
    database = Database("postgresql://localhost/decagent_production")
    with pytest.raises(ValueError, match="named disposable"):
        provision_synthetic_actors(database, None)

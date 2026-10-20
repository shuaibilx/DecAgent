"""The process-owned client uses modern MCP over real loopback HTTP."""

import asyncio
import hashlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
import uvicorn
from mcp import ClientSession

from decagent.business.employee_accounts import _DEMO_ACCOUNTS
from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_client import (
    MCPAuthenticationError,
    MCPConnectionManager,
    MCPOutcomeUnknown,
    MCPProtocolError,
    MCPTransportError,
)
from decagent.business.mcp_server import create_mcp_app
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore

pytestmark = pytest.mark.integration


@pytest.fixture
def service(database, tmp_path):
    assert TicketStore(database.url).execute("seed", {}, "client-seed")["ok"]
    PluginRegistry(database).register(builtin_manifest())
    tokens = {}
    with database.connect() as conn:
        conn.execute("DELETE FROM business.operation_receipts WHERE operation_id='client-seed'")
        people = {
            (row["name"], row["team"]): row["id"]
            for row in conn.execute("SELECT id,name,team FROM business.people").fetchall()
        }
        category = str(conn.execute("SELECT id FROM business.categories LIMIT 1").fetchone()["id"])
        for login, name, team, role in _DEMO_ACCOUNTS:
            employee_id, ref = uuid4(), uuid4()
            token = "dcm_" + uuid4().hex
            conn.execute(
                "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                "VALUES(%s,%s,%s,%s,'test-only-account-hash')",
                (employee_id, login, people[name, team], role),
            )
            conn.execute(
                "INSERT INTO business.mcp_credentials"
                "(id,token_hash,employee_id,account_revision,expires_at) "
                "VALUES(%s,%s,%s,1,now()+interval '1 hour')",
                (ref, hashlib.sha256(token.encode()).hexdigest(), employee_id),
            )
            tokens[login] = (token, ref)
    auth = EmployeeAuth(database, "http://127.0.0.1:8000", False, tmp_path / "tokens")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = create_mcp_app(database, auth, port=port)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield database, f"http://127.0.0.1:{port}/mcp", auth, tokens, category
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


def _client(manager, auth, tokens, login):
    token = tokens[login][0]
    identity = auth.authenticate_mcp_token(token)
    assert identity is not None
    return manager.client(identity, token)


def _counts(database):
    with database.connect() as conn:
        return tuple(
            conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        )


def test_modern_client_reuses_one_connection_and_closes_it(service):
    _, url, auth, tokens, _ = service
    with MCPConnectionManager(url, auth) as manager:
        employee = _client(manager, auth, tokens, "employee-it")
        assert employee.protocol_version == "2026-07-28"
        first = employee.read("list", {})
        second = employee.read("list", {})
        assert first == second == {"tickets": []}
        assert manager.connection_count == 1
        assert employee.list_tools()
        employee.close()
        assert manager.connection_count == 0
        assert _client(manager, auth, tokens, "employee-it").read("list", {}) == first
        assert manager.connection_count == 1
    assert manager.closed


def test_modern_client_rejects_a_legacy_handshake_without_fallback(service, monkeypatch):
    _, url, auth, tokens, _ = service

    async def legacy_only(self):
        await self.initialize()

    monkeypatch.setattr(ClientSession, "discover", legacy_only)
    with MCPConnectionManager(url, auth) as manager:
        with pytest.raises(MCPProtocolError, match="2026-07-28"):
            _client(manager, auth, tokens, "employee-it").read("list", {})
        assert manager.connection_count == 0


def test_environment_endpoint_change_closes_old_connection(service, monkeypatch):
    _, url, auth, tokens, _ = service
    monkeypatch.setenv("DECAGENT_TICKETS_MCP_URL", url)
    with MCPConnectionManager(employee_auth=auth) as manager:
        employee = _client(manager, auth, tokens, "employee-it")
        assert employee.read("list", {}) == {"tickets": []}
        assert manager.connection_count == 1
        class UnavailableHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                self.do_GET()

            def log_message(self, *_args):
                pass

        unavailable = ThreadingHTTPServer(("127.0.0.1", 0), UnavailableHandler)
        rejector = threading.Thread(target=unavailable.serve_forever, daemon=True)
        rejector.start()
        port = unavailable.server_address[1]
        monkeypatch.setenv("DECAGENT_TICKETS_MCP_URL", f"http://127.0.0.1:{port}/mcp")
        try:
            with pytest.raises(MCPTransportError):
                employee.read("list", {})
            assert manager.connection_count == 0
        finally:
            unavailable.shutdown()
            unavailable.server_close()
            rejector.join(timeout=1)
        assert not rejector.is_alive()


def test_employee_connections_are_isolated_and_revocation_closes_one(service):
    database, url, auth, tokens, category = service
    with MCPConnectionManager(url, auth) as manager:
        first = _client(manager, auth, tokens, "employee-it")
        second = _client(manager, auth, tokens, "employee-facilities")
        with ThreadPoolExecutor(max_workers=2) as pool:
            lists = list(pool.map(lambda client: client.read("list", {}), (first, second)))
        assert lists == [{"tickets": []}, {"tickets": []}]
        assert manager.connection_count == 2
        prepared = first.prepare(
            "employee-one", "create", {"title": "VPN", "category_id": category}
        )
        assert prepared["state"] == "ready"
        committed = first.commit(prepared["operation_id"])
        assert committed["state"] == "succeeded"
        assert second.read("list", {}) == {"tickets": []}
        assert len(first.read("list", {})["tickets"]) == 1
        with database.connect() as conn:
            conn.execute(
                "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s",
                (tokens["employee-it"][1],),
            )
        with pytest.raises(MCPAuthenticationError):
            first.read("list", {})
        assert manager.connection_count == 1
        assert second.read("list", {}) == {"tickets": []}
        assert _counts(database) == (1, 1, 1)


def test_lost_commit_response_requires_querying_original_operation(service, monkeypatch):
    database, url, auth, tokens, category = service
    with MCPConnectionManager(url, auth) as manager:
        employee = _client(manager, auth, tokens, "employee-it")
        prepared = employee.prepare(
            "lost-response", "create", {"title": "VPN", "category_id": category}
        )
        operation_id = prepared["operation_id"]
        original_call = ClientSession.call_tool

        async def dropped_response(self, name, *args, **kwargs):
            result = await original_call(self, name, *args, **kwargs)
            if name == "business_commit_operation":
                raise OSError("response dropped after server commit")
            return result

        monkeypatch.setattr(ClientSession, "call_tool", dropped_response)
        with pytest.raises(MCPOutcomeUnknown) as error:
            employee.commit(operation_id)
        assert error.value.operation_id == operation_id
        assert _counts(database) == (1, 1, 1)
        assert manager.connection_count == 0
        verified = employee.query_operation(operation_id)
        assert verified["state"] == "succeeded"
        monkeypatch.setattr(ClientSession, "call_tool", original_call)
        assert employee.commit(operation_id) == verified
        assert _counts(database) == (1, 1, 1)


def test_cancelled_commit_returns_unknown_and_original_receipt_can_be_queried(service, monkeypatch):
    database, url, auth, tokens, category = service
    with MCPConnectionManager(url, auth) as manager:
        employee = _client(manager, auth, tokens, "employee-it")
        prepared = employee.prepare(
            "cancelled-response", "create", {"title": "VPN", "category_id": category}
        )
        operation_id = prepared["operation_id"]
        reached = threading.Event()
        original_call = ClientSession.call_tool

        async def stalled_delivery(self, name, *args, **kwargs):
            result = await original_call(self, name, *args, **kwargs)
            if name == "business_commit_operation":
                reached.set()
                await asyncio.sleep(30)
            return result

        monkeypatch.setattr(ClientSession, "call_tool", stalled_delivery)

        async def cancel_after_dispatch():
            call = asyncio.create_task(employee.acommit(operation_id))
            assert await asyncio.to_thread(reached.wait, 5)
            call.cancel()
            with pytest.raises(MCPOutcomeUnknown) as error:
                await call
            assert error.value.operation_id == operation_id

        asyncio.run(cancel_after_dispatch())
        monkeypatch.setattr(ClientSession, "call_tool", original_call)
        assert manager.connection_count == 0
        assert employee.query_operation(operation_id)["state"] == "succeeded"
        assert _counts(database) == (1, 1, 1)

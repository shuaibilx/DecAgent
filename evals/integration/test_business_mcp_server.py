"""Real loopback MCP calls use the same employee operation and ticket boundaries."""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from decagent.business.contracts import PluginSpec
from decagent.business.employee_accounts import _DEMO_ACCOUNTS
from decagent.business.employee_auth import EmployeeAuth
from decagent.business.mcp_contract import wire_tools
from decagent.business.mcp_server import create_mcp_app
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore

pytestmark = pytest.mark.integration


@pytest.fixture
def service(database, tmp_path):
    assert TicketStore(database.url).execute("seed", {}, "mcp-server-seed")["ok"]
    PluginRegistry(database).register(builtin_manifest())
    tokens = {}
    with database.connect() as conn:
        conn.execute("DELETE FROM business.operation_receipts WHERE operation_id='mcp-server-seed'")
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
            tokens[login] = (token, employee_id, people[name, team], ref)
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
        yield database, f"http://127.0.0.1:{port}/mcp", tokens, category, app
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()


@asynccontextmanager
async def connected(url, token, *, modern=True):
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}) as http:
        async with streamable_http_client(url, http_client=http) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                if modern:
                    await session.discover()
                    assert session.protocol_version == "2026-07-28"
                else:
                    await session.initialize()
                    assert session.protocol_version == "2025-11-25"
                yield session


def _data(result):
    assert not result.is_error
    payload = result.structured_content
    assert payload["ok"] is True
    assert json.loads(result.content[0].text) == payload
    return payload["data"]


def _counts(database):
    with database.connect() as conn:
        return tuple(
            conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        )


def test_modern_catalog_and_employee_scoped_create_commit_replay(service):
    database, url, tokens, category, _ = service
    spec = PluginSpec.from_mapping(builtin_manifest())

    async def scenario():
        async with connected(url, tokens["employee-it"][0]) as employee:
            listed = (await employee.list_tools()).tools
            allowed = {
                tool["name"]
                for tool in wire_tools(
                    spec,
                    allowed_tools={
                        "tickets_" + name for name in ("directory", "get", "list", "receipt")
                    }
                    | {
                        "tickets_prepare_create",
                        "tickets_prepare_close",
                        "tickets_prepare_reopen",
                        "tickets_prepare_comment",
                        "business_commit_operation",
                        "business_get_operation",
                    },
                )
            }
            assert {tool.name for tool in listed} == allowed
            spec.compare_wire_catalog(
                [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed],
                allowed_tools=allowed,
            )
            prepared = _data(
                await employee.call_tool(
                    "tickets_prepare_create",
                    {
                        "request_id": "employee-create",
                        "params": {"title": "VPN ; echo data", "category_id": category},
                    },
                )
            )
            assert prepared["state"] == "ready"
            assert _counts(database) == (0, 0, 0)
            committed = _data(
                await employee.call_tool(
                    "business_commit_operation", {"operation_id": prepared["operation_id"]}
                )
            )
            assert committed["state"] == "succeeded"
            assert committed["result"]["ticket"]["title"] == "VPN ; echo data"
            assert committed["result"]["ticket"]["requester_id"] == str(tokens["employee-it"][2])
            assert (
                _data(
                    await employee.call_tool(
                        "business_get_operation", {"operation_id": prepared["operation_id"]}
                    )
                )
                == committed
            )
            assert (
                _data(
                    await employee.call_tool(
                        "business_commit_operation", {"operation_id": prepared["operation_id"]}
                    )
                )
                == committed
            )
            assert (
                _data(await employee.call_tool("tickets_list", {}))["tickets"][0]["id"]
                == committed["result"]["ticket"]["id"]
            )
        async with connected(url, tokens["employee-facilities"][0]) as other:
            assert _data(await other.call_tool("tickets_list", {}))["tickets"] == []
            hidden = await other.call_tool(
                "tickets_get", {"ticket_id": committed["result"]["ticket"]["id"]}
            )
            assert hidden.structured_content["error"]["code"] == "not_found"
            hidden = await other.call_tool(
                "business_get_operation", {"operation_id": prepared["operation_id"]}
            )
            assert hidden.structured_content["error"]["code"] == "not_found"

    asyncio.run(scenario())
    assert _counts(database) == (1, 1, 1)


def test_legacy_client_cannot_commit_sensitive_operation_without_browser_approval(service):
    database, url, tokens, category, _ = service
    with database.connect() as conn:
        ticket = str(uuid4())
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,employee_id,requester_id,status) "
            "VALUES(%s,'existing ticket',%s,%s,%s,'resolved')",
            (ticket, category, tokens["employee-it"][1], tokens["employee-it"][2]),
        )

    async def scenario():
        async with connected(url, tokens["employee-it"][0], modern=False) as client:
            prepared = _data(
                await client.call_tool(
                    "tickets_prepare_close",
                    {
                        "request_id": "close-via-legacy",
                        "params": {"ticket_id": ticket, "expected_revision": 1},
                    },
                )
            )
            assert prepared["state"] == "awaiting_approval"
            pending = _data(
                await client.call_tool(
                    "business_commit_operation", {"operation_id": prepared["operation_id"]}
                )
            )
            assert pending["state"] == "awaiting_approval"

    asyncio.run(scenario())
    assert _counts(database) == (1, 0, 0)
    with database.connect() as conn:
        assert (
            conn.execute("SELECT status FROM business.tickets WHERE id=%s", (ticket,)).fetchone()[
                "status"
            ]
            == "resolved"
        )


def test_http_auth_origin_input_and_body_bounds_have_no_side_effect(service):
    database, url, tokens, category, _ = service
    modern_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {},
    }
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"}

    async def scenario():
        async with httpx2.AsyncClient() as http:
            health = await http.get(url.removesuffix("/mcp") + "/health")
            assert health.status_code == 200
            assert health.json() == {
                "service": "service-tickets",
                "version": "2.0.0",
                "protocols": ["2026-07-28", "2025-11-25"],
                "ready": True,
            }
            assert (await http.post(url, json=modern_request, headers=headers)).status_code == 401
            assert (
                await http.post(
                    url,
                    json=modern_request,
                    headers={**headers, "Authorization": "Bearer owner-only"},
                )
            ).status_code == 401
            assert (
                await http.post(
                    url,
                    json=modern_request,
                    headers={
                        **headers,
                        "Authorization": "Bearer " + tokens["employee-it"][0],
                        "Origin": "https://evil.invalid",
                    },
                )
            ).status_code in {400, 403}
            too_large = json.dumps({**modern_request, "padding": "x" * 65536})
            oversized = await http.post(
                url,
                content=too_large,
                headers={
                    **headers,
                    "Authorization": "Bearer " + tokens["employee-it"][0],
                    "Content-Type": "application/json",
                },
            )
            assert oversized.status_code in {400, 413}
        async with connected(url, tokens["employee-it"][0]) as client:
            wrong = await client.call_tool(
                "tickets_prepare_create",
                {
                    "request_id": "forged",
                    "params": {"title": "forged", "category_id": category},
                    "approved": True,
                },
            )
            assert wrong.is_error and wrong.structured_content["error"]["code"] == "invalid_input"

    asyncio.run(scenario())
    assert _counts(database) == (0, 0, 0)


def test_mcp_token_revocation_is_checked_after_connection(service):
    database, url, tokens, _, _ = service
    token, _, _, ref = tokens["employee-it"]

    async def scenario():
        async with connected(url, token) as client:
            assert _data(await client.call_tool("tickets_list", {}))["tickets"] == []
            with database.connect() as conn:
                conn.execute(
                    "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s", (ref,)
                )
            with pytest.raises(MCPError):
                await client.call_tool("tickets_list", {})

    asyncio.run(scenario())
    assert _counts(database) == (0, 0, 0)


def test_grant_metadata_cannot_authorize_external_operation(service):
    database, url, tokens, category, _ = service

    async def scenario():
        async with connected(url, tokens["employee-it"][0]) as client:
            forged = await client.call_tool(
                "tickets_prepare_create",
                {"request_id": "fake-agent", "params": {"title": "fake", "category_id": category}},
                meta={"decagent.task_grant": "dcg_not-issued"},
            )
            assert forged.is_error
            assert forged.structured_content["error"]["code"] == "invalid_grant"

    asyncio.run(scenario())
    assert _counts(database) == (0, 0, 0)


def test_large_read_result_is_bounded_on_the_wire(service):
    database, url, tokens, category, _ = service
    employee_id, requester_id = tokens["employee-it"][1:3]
    with database.connect() as conn:
        conn.cursor().executemany(
            "INSERT INTO business.tickets(id,title,description,category_id,employee_id,"
            "requester_id,status) VALUES(%s,'large',%s,%s,%s,%s,'open')",
            [(uuid4(), "x" * 4000, category, employee_id, requester_id) for _ in range(80)],
        )

    async def scenario():
        async with connected(url, tokens["employee-it"][0]) as client:
            result = await client.call_tool("tickets_list", {})
            assert result.is_error
            assert result.structured_content["error"]["code"] == "response_too_large"
            assert len(result.model_dump_json(by_alias=True).encode("utf-8")) <= 256 * 1024

    asyncio.run(scenario())
    assert _counts(database) == (80, 0, 0)


def test_request_deadline_returns_bounded_error_without_effect(service, monkeypatch):
    database, url, tokens, _, app = service
    app.state.business_mcp.timeout_seconds = 0.02

    def delayed_call(*_args):
        time.sleep(0.2)
        return {"ok": True, "data": {"tickets": []}}

    monkeypatch.setattr(app.state.business_mcp, "_call", delayed_call)

    async def scenario():
        async with connected(url, tokens["employee-it"][0]) as client:
            result = await client.call_tool("tickets_list", {})
            assert result.is_error
            assert result.structured_content["error"]["code"] == "timeout"

    asyncio.run(scenario())
    assert _counts(database) == (0, 0, 0)


def test_isolated_mcp_cli_legacy_schema_and_sensitive_commit(service, tmp_path):
    database, url, tokens, category, _ = service
    executable = shutil.which("mcp-cli") or str(Path.home() / ".bun/bin/mcp-cli.exe")
    if not Path(executable).is_file():
        pytest.skip("mcp-cli 0.3.0 is not installed on this host")
    template = Path(__file__).parents[2] / "ops/mcp/mcp_servers.example.json"
    config = json.loads(template.read_text(encoding="utf-8"))
    config["mcpServers"]["decagent-tickets"]["url"] = url
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    environment = {
        **os.environ,
        "DECAGENT_EMPLOYEE_MCP_TOKEN": tokens["employee-it"][0],
        "MCP_NO_DAEMON": "1",
        "MCP_MAX_RETRIES": "0",
    }

    def cli(*arguments):
        return subprocess.run(
            [executable, "-c", str(path), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            env=environment,
            check=False,
        )

    listed = cli("info", "decagent-tickets")
    assert listed.returncode == 0
    assert "tickets_prepare_close" in listed.stdout
    schema = cli("info", "decagent-tickets", "tickets_prepare_close")
    assert schema.returncode == 0
    assert "expected_revision" in schema.stdout
    ticket = str(uuid4())
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets"
            "(id,title,category_id,employee_id,requester_id,status,resolution) "
            "VALUES(%s,'cli sensitive',%s,%s,%s,'resolved','Recorded resolution')",
            (ticket, category, tokens["employee-it"][1], tokens["employee-it"][2]),
        )
    prepared = cli(
        "call",
        "decagent-tickets",
        "tickets_prepare_close",
        json.dumps(
            {
                "request_id": "cli-unapproved-close",
                "params": {"ticket_id": ticket, "expected_revision": 1},
            }
        ),
    )
    assert prepared.returncode == 0
    operation = json.loads(prepared.stdout)["data"]
    assert operation["state"] == "awaiting_approval"
    committed = cli(
        "call",
        "decagent-tickets",
        "business_commit_operation",
        json.dumps({"operation_id": operation["operation_id"]}),
    )
    assert committed.returncode == 0
    assert json.loads(committed.stdout)["data"]["state"] == "awaiting_approval"
    assert _counts(database) == (1, 0, 0)

"""Authenticated service-ticket MCP endpoint backed by the shared operation core."""

import ipaddress
import json
from urllib.parse import urlsplit

import anyio
import psycopg
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from starlette.responses import JSONResponse

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.codec import error
from decagent.business.contracts import PluginSpec
from decagent.business.mcp_contract import READ_COMMANDS, WRITE_COMMANDS
from decagent.business.operations import OperationService
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketError, TicketStore, _json

TASK_GRANT_META_KEY = "decagent.task_grant"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 15


def _tool_result(envelope, *, maximum):
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    response = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=envelope,
        isError=not envelope["ok"],
    )
    # The SDK adds a small JSON-RPC wrapper to this serialized result.
    if (
        len(response.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
        > maximum - 1024
    ):
        fallback = error(
            "response_too_large", "Result exceeds the MCP response limit; query the operation"
        )
        response = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(fallback, separators=(",", ":")))],
            structuredContent=fallback,
            isError=True,
        )
    return response


class _EmployeeTokenVerifier:
    def __init__(self, employee_auth, endpoint_url):
        self.employee_auth = employee_auth
        self.endpoint_url = endpoint_url

    async def verify_token(self, token):
        identity = await anyio.to_thread.run_sync(self.employee_auth.authenticate_mcp_token, token)
        if identity is None:
            return None
        return AccessToken(
            token=token,
            client_id=str(identity.credential_ref),
            scopes=["business"],
            resource=self.endpoint_url,
            subject=str(identity.employee_id),
        )


class BusinessMCPServer:
    """Owns the exact catalog and revalidates the employee on every tool request."""

    def __init__(
        self,
        database,
        employee_auth,
        *,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        max_result_bytes=MAX_RESULT_BYTES,
    ):
        if not 0 < timeout_seconds <= REQUEST_TIMEOUT_SECONDS:
            raise ValueError("MCP timeout must be between 0 and 15 seconds")
        if not 1024 <= max_result_bytes <= MAX_RESULT_BYTES:
            raise ValueError("MCP result limit must be between 1024 and 256 KiB")
        self.database = database
        self.employee_auth = employee_auth
        self.timeout_seconds = timeout_seconds
        self.max_result_bytes = max_result_bytes
        self.spec = PluginSpec.from_mapping(builtin_manifest())
        self.operations = OperationService(database)
        self.ticket_store = TicketStore(database.url)
        self.tools = {item["name"]: item for item in self.spec.wire_tools()}
        self.server = Server(
            "service-tickets",
            version="2.0.0",
            get_tool_input_schema=lambda name: self.tools.get(name, {}).get("inputSchema"),
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )

    def _identity(self):
        bearer = get_access_token()
        if bearer is None or not isinstance(bearer.token, str):
            raise AccessError("unauthorized", "Employee MCP token required")
        identity = self.employee_auth.authenticate_mcp_token(bearer.token)
        if identity is None:
            raise AccessError("unauthorized", "Employee MCP token unavailable")
        return identity

    def _allowed_tools(self, identity):
        with self.database.connect() as conn:
            allowed_commands = access.allowed_commands(
                conn, identity.employee_id, workspace_id=identity.workspace_id
            )
        return {
            *("tickets_" + command for command in allowed_commands & READ_COMMANDS),
            *("tickets_prepare_" + command for command in allowed_commands & WRITE_COMMANDS),
            "business_commit_operation",
            "business_get_operation",
        }

    def ready(self):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT enabled,manifest_digest FROM app.business_scopes WHERE plugin_id=%s",
                (self.spec.id,),
            ).fetchone()
        return bool(row and row["enabled"] and row["manifest_digest"] == self.spec.digest)

    async def list_tools(self, _context, _params):
        bearer = get_access_token()
        if bearer is None:
            raise AccessError("unauthorized", "Employee MCP token required")
        with anyio.fail_after(self.timeout_seconds):
            identity = await anyio.to_thread.run_sync(self._identity, abandon_on_cancel=True)
            allowed = await anyio.to_thread.run_sync(
                self._allowed_tools, identity, abandon_on_cancel=True
            )
        return ListToolsResult(
            tools=[Tool(**entry) for entry in self.spec.wire_tools(allowed_tools=allowed)]
        )

    def _read(self, identity, command, arguments):
        with self.database.connect() as conn:
            if command == "directory":
                return self.ticket_store._read(
                    conn, command, arguments, workspace_id=identity.workspace_id
                )
            if command == "get":
                ticket = access.get_ticket(
                    conn,
                    identity.employee_id,
                    arguments["ticket_id"],
                    workspace_id=identity.workspace_id,
                )
                history = access.get_history(
                    conn,
                    identity.employee_id,
                    arguments["ticket_id"],
                    workspace_id=identity.workspace_id,
                )
                return _json({"ticket": ticket, "history": history})
            if command == "list":
                return _json(
                    {
                        "tickets": access.list_tickets(
                            conn,
                            identity.employee_id,
                            arguments,
                            workspace_id=identity.workspace_id,
                        )
                    }
                )
            receipt = access.get_receipt(
                conn,
                identity.employee_id,
                arguments["operation_id"],
                workspace_id=identity.workspace_id,
            )
            return {"receipt": self.ticket_store._receipt(receipt) if receipt else None}

    def _call(self, name, arguments, grant, token):
        identity = self.employee_auth.authenticate_mcp_token(token)
        if identity is None:
            raise AccessError("unauthorized", "Employee MCP token unavailable")
        if name not in self.tools:
            raise AccessError("invalid_command", "Unknown MCP business tool")
        if name not in self._allowed_tools(identity):
            raise AccessError("forbidden", "Employee role does not permit this tool")
        self.spec.validate_wire(name, arguments)
        if name.startswith("tickets_prepare_"):
            result = self.operations.prepare(
                identity,
                arguments["request_id"],
                name.removeprefix("tickets_prepare_"),
                arguments["params"],
                grant=grant,
            )
        elif name == "business_commit_operation":
            result = self.operations.commit(identity, arguments["operation_id"], grant=grant)
        elif name == "business_get_operation":
            result = self.operations.get(identity, arguments["operation_id"])
        else:
            result = self._read(identity, name.removeprefix("tickets_"), arguments)
        envelope = {"ok": True, "data": result}
        self.spec.validate_wire_result(name, envelope)
        return envelope

    async def call_tool(self, _context, params):
        bearer = get_access_token()
        if bearer is None:
            return _tool_result(
                error("unauthorized", "Employee MCP token required"), maximum=self.max_result_bytes
            )
        grant = (params.meta or {}).get(TASK_GRANT_META_KEY)
        try:
            with anyio.fail_after(self.timeout_seconds):
                envelope = await anyio.to_thread.run_sync(
                    self._call,
                    params.name,
                    params.arguments or {},
                    grant,
                    bearer.token,
                    abandon_on_cancel=True,
                )
        except TimeoutError:
            envelope = error(
                "outcome_unknown" if params.name == "business_commit_operation" else "timeout",
                "MCP request timed out; query the original operation before retrying",
            )
        except (AccessError, TicketError) as exc:
            envelope = error(exc.code, str(exc))
        except (ValueError, TypeError):
            envelope = error("invalid_input", "Input does not match the declared MCP schema")
        except psycopg.Error:
            envelope = error(
                "outcome_unknown"
                if params.name == "business_commit_operation"
                else "service_unavailable",
                "Business database unavailable; query the original operation",
            )
        if params.name in self.tools:
            self.spec.validate_wire_result(params.name, envelope)
        return _tool_result(envelope, maximum=self.max_result_bytes)


def create_mcp_app(
    database,
    employee_auth,
    *,
    host="127.0.0.1",
    port=8001,
    timeout_seconds=REQUEST_TIMEOUT_SECONDS,
    max_result_bytes=MAX_RESULT_BYTES,
):
    """Create the loopback-only official SDK app serving modern and legacy MCP."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("Business MCP host must be a loopback IP address") from exc
    if not address.is_loopback or not 1 <= port <= 65535:
        raise ValueError("Business MCP requires a loopback address and valid port")
    if host != "127.0.0.1":
        raise ValueError("Business MCP currently binds to IPv4 loopback only")
    endpoint_url = f"http://{host}:{port}/mcp"
    if urlsplit(endpoint_url).hostname != host:
        raise ValueError("Invalid MCP endpoint")
    service = BusinessMCPServer(
        database,
        employee_auth,
        timeout_seconds=timeout_seconds,
        max_result_bytes=max_result_bytes,
    )
    app = service.server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        host=host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}"],
        ),
        auth=AuthSettings(
            issuer_url=f"http://{host}:{port}",
            resource_server_url=endpoint_url,
            required_scopes=["business"],
            validate_token_resource=True,
        ),
        token_verifier=_EmployeeTokenVerifier(employee_auth, endpoint_url),
    )

    async def health(_request):
        try:
            with anyio.fail_after(REQUEST_TIMEOUT_SECONDS):
                ready = await anyio.to_thread.run_sync(service.ready, abandon_on_cancel=True)
        except (TimeoutError, psycopg.Error):
            ready = False
        return JSONResponse(
            {
                "service": "service-tickets",
                "version": "2.0.0",
                "protocols": ["2026-07-28", "2025-11-25"],
                "ready": ready,
            },
            status_code=200 if ready else 503,
        )

    app.add_route("/health", health, methods=["GET"])
    app.state.business_mcp = service
    return app

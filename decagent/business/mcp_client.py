"""Process-owned modern MCP connections for authenticated business tools."""

import asyncio
import hashlib
import ipaddress
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import INTERNAL_ERROR

from decagent.business import access
from decagent.business.codec import strict_json
from decagent.business.contracts import PluginSpec
from decagent.business.mcp_contract import (
    COMPOSE_INTERNAL_MCP_ENDPOINT,
    READ_COMMANDS,
    TRUSTED_CONNECTION,
    WRITE_COMMANDS,
)
from decagent.business.tickets import builtin_manifest

MODERN_PROTOCOL_VERSION = "2026-07-28"


class MCPProtocolError(ValueError):
    """The endpoint failed modern discovery or its frozen tool contract."""


class MCPAuthenticationError(ValueError):
    """The employee token no longer names an active credential."""


class MCPTransportError(RuntimeError):
    """The service could not provide a trustworthy result."""


class MCPOutcomeUnknown(MCPTransportError):
    """A commit may have reached the service; query its original operation ID."""

    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__("Commit outcome unknown; query the original operation")


class MCPToolError(ValueError):
    """A validated business error returned by the MCP service."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class _Connection:
    queue: asyncio.Queue
    ready: asyncio.Future
    task: asyncio.Task | None = None
    tools: list[dict] | None = None
    protocol_version: str | None = None
    closed: bool = False


def _checked_endpoint(url):
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path != "/mcp"
        or parts.hostname is None
    ):
        raise ValueError("Business MCP endpoint must be a local /mcp HTTP URL")
    if url == COMPOSE_INTERNAL_MCP_ENDPOINT:
        return url
    try:
        address = ipaddress.ip_address(parts.hostname)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Business MCP endpoint must have a loopback IP and port") from exc
    if not address.is_loopback or parts.hostname != "127.0.0.1" or port is None:
        raise ValueError(
            "Business MCP endpoint must use IPv4 loopback or the pinned Compose service"
        )
    return url


class MCPConnectionManager:
    """One event loop per owning process, shared by scoped employee connections.

    A worker keeps this manager for its lifetime; individual BusinessMCPClient
    borrowers never create their own event loop. The server authenticates every
    HTTP request, and this manager also checks credential liveness before use.
    """

    def __init__(self, endpoint_url=None, employee_auth=None, *, timeout_seconds=20):
        if employee_auth is None:
            raise ValueError("EmployeeAuth is required for MCP credential validation")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("MCP client timeout must be between 0 and 30 seconds")
        self.employee_auth = employee_auth
        self.timeout_seconds = timeout_seconds
        self.spec = PluginSpec.from_mapping(builtin_manifest())
        self._explicit_endpoint = endpoint_url
        self._checked_url = _checked_endpoint(self._endpoint())
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self._loop = None
        self._thread = threading.Thread(
            target=self._run_loop, name="decagent-business-mcp", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("Business MCP event loop did not start")

    def _endpoint(self):
        url = self._explicit_endpoint
        if url is None:
            url = os.environ.get(TRUSTED_CONNECTION["endpoint_ref"], "")
        return url

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._connections = {}
        self._connection_lock = asyncio.Lock()
        self._active_url = self._checked_url
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _schedule(self, coroutine):
        with self._state_lock:
            if self._closed:
                coroutine.close()
                raise RuntimeError("Business MCP manager is closed")
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _submit(self, coroutine):
        if threading.current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("Use async MCP methods from an event loop")
        future = self._schedule(coroutine)
        try:
            return future.result(timeout=self.timeout_seconds + 10)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MCPTransportError("MCP request exceeded its local deadline") from exc

    async def _asubmit(self, coroutine):
        return await asyncio.wrap_future(self._schedule(coroutine))

    @property
    def closed(self):
        return self._closed

    @property
    def connection_count(self):
        if self.closed:
            return 0
        return self._submit(self._count())

    async def _count(self):
        return len(self._connections)

    def client(self, identity, token):
        """Borrow a client for one authenticated employee and one credential ref."""
        if self.closed:
            raise RuntimeError("Business MCP manager is closed")
        self._validate_identity(identity, token)
        return BusinessMCPClient(self, identity, token)

    def _key(self, identity):
        return (
            TRUSTED_CONNECTION["service_id"],
            identity.employee_id,
            identity.account_revision,
            identity.credential_ref,
            identity.token_hash,
        )

    def _validate_identity(self, identity, token):
        if (
            identity is None
            or getattr(identity, "source", None) != "mcp_token"
            or getattr(identity, "credential_ref", None) is None
            or not isinstance(token, str)
            or not token.startswith("dcm_")
            or len(token) > 512
            or hashlib.sha256(token.encode("utf-8")).hexdigest() != identity.token_hash
        ):
            raise MCPAuthenticationError("Employee MCP credential unavailable")
        current = self.employee_auth.authenticate_mcp_token(token)
        if current is None or self._key(current) != self._key(identity):
            if not self.closed:
                self._submit(self._invalidate_key(self._key(identity)))
            raise MCPAuthenticationError("Employee MCP credential unavailable")

    def _allowed_tools(self, identity):
        with self.employee_auth.database.connect() as conn:
            allowed = access.allowed_commands(
                conn, identity.employee_id, workspace_id=identity.workspace_id
            )
        return {
            *("tickets_" + command for command in allowed & READ_COMMANDS),
            *("tickets_prepare_" + command for command in allowed & WRITE_COMMANDS),
            "business_commit_operation",
            "business_get_operation",
        }

    async def _connection_main(self, key, connection, url, token, identity):
        """Own SDK context entry, every call, and exit in this one AnyIO task."""
        current_reply = None
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers={"Authorization": "Bearer " + token}, timeout=self.timeout_seconds
                    )
                )
                reader, writer = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(reader, writer))
                try:
                    await session.discover()
                except MCPError as exc:
                    # The pinned MCP SDK uses this JSON-RPC sentinel for non-JSON HTTP errors.
                    if (
                        exc.code == INTERNAL_ERROR
                        and exc.message == "Server returned an error response"
                    ):
                        connection.ready.set_exception(
                            MCPTransportError(
                                "Business MCP discovery endpoint returned an HTTP error"
                            )
                        )
                    else:
                        connection.ready.set_exception(
                            MCPProtocolError("Business MCP modern protocol discovery failed")
                        )
                    return
                except Exception:
                    connection.ready.set_exception(
                        MCPTransportError("Business MCP discovery unavailable")
                    )
                    return
                if session.protocol_version != MODERN_PROTOCOL_VERSION:
                    connection.ready.set_exception(
                        MCPProtocolError("Business MCP requires protocol 2026-07-28")
                    )
                    return
                listed = await session.list_tools()
                tools = [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools]
                try:
                    self.spec.compare_wire_catalog(
                        tools, allowed_tools=self._allowed_tools(identity)
                    )
                except (ValueError, TypeError):
                    connection.ready.set_exception(
                        MCPProtocolError("Business MCP tool catalog differs from frozen contract")
                    )
                    return
                connection.tools = tools
                connection.protocol_version = session.protocol_version
                connection.ready.set_result(None)
                while True:
                    item = await connection.queue.get()
                    if item is None:
                        break
                    name, arguments, grant, current_reply = item
                    try:
                        result = await session.call_tool(
                            name,
                            arguments,
                            read_timeout_seconds=self.timeout_seconds,
                            meta={"decagent.task_grant": grant} if grant is not None else None,
                        )
                        if not hasattr(result, "structured_content"):
                            raise MCPProtocolError(
                                "Business MCP returned an unexpected result type"
                            )
                        payload = result.structured_content
                        if not isinstance(payload, dict):
                            raise MCPProtocolError("Business MCP omitted structured content")
                        if not result.content or result.content[0].type != "text":
                            raise MCPProtocolError("Business MCP omitted compatible text content")
                        if strict_json(result.content[0].text) != payload:
                            raise MCPProtocolError(
                                "Business MCP text and structured results disagree"
                            )
                        self.spec.validate_wire_result(name, payload)
                        if result.is_error != (not payload["ok"]):
                            raise MCPProtocolError("Business MCP result error flag disagrees")
                    except Exception as exc:
                        if not current_reply.done():
                            current_reply.set_exception(exc)
                        current_reply = None
                        break
                    else:
                        if not current_reply.done():
                            current_reply.set_result(payload)
                        current_reply = None
        except BaseException as exc:
            if not connection.ready.done():
                connection.ready.set_exception(exc)
            if current_reply is not None and not current_reply.done():
                current_reply.set_exception(exc)
        finally:
            connection.closed = True
            if self._connections.get(key) is connection:
                del self._connections[key]
            while not connection.queue.empty():
                item = connection.queue.get_nowait()
                if item is not None and not item[3].done():
                    item[3].set_exception(MCPTransportError("Employee MCP connection closed"))

    async def _close_connection(self, key, connection):
        if self._connections.get(key) is connection:
            del self._connections[key]
        connection.closed = True
        connection.queue.put_nowait(None)
        await connection.task

    async def _close_key(self, key):
        connection = self._connections.get(key)
        if connection is not None:
            await self._close_connection(key, connection)

    async def _invalidate_key(self, key):
        """Drop a possibly in-flight connection after cancellation or local timeout."""
        connection = self._connections.pop(key, None)
        if connection is not None:
            connection.closed = True
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)

    async def _close_all(self):
        for key, connection in list(self._connections.items()):
            await self._close_connection(key, connection)

    async def _ensure_connection(self, key, url, token, identity):
        async with self._connection_lock:
            if url != self._active_url:
                await self._close_all()
                self._active_url = url
            existing = self._connections.get(key)
            if existing is not None and not existing.closed:
                return existing
            loop = asyncio.get_running_loop()
            connection = _Connection(asyncio.Queue(), loop.create_future())
            self._connections[key] = connection
            connection.task = asyncio.create_task(
                self._connection_main(key, connection, url, token, identity)
            )
            try:
                await connection.ready
            except BaseException:
                await connection.task
                raise
            return connection

    async def _request(self, key, url, token, identity, name, arguments, grant):
        connection = await self._ensure_connection(key, url, token, identity)
        if connection.closed:
            connection = await self._ensure_connection(key, url, token, identity)
        reply = asyncio.get_running_loop().create_future()
        connection.queue.put_nowait((name, arguments, grant, reply))
        return await reply

    def _check_request(self, identity, token, name, arguments):
        self._validate_identity(identity, token)
        key = self._key(identity)
        self.spec.validate_wire(name, arguments)
        url = _checked_endpoint(self._endpoint())
        return key, url

    def call_tool(self, identity, token, name, arguments, *, grant=None):
        key, url = self._check_request(identity, token, name, arguments)
        try:
            result = self._submit(self._request(key, url, token, identity, name, arguments, grant))
        except (MCPAuthenticationError, MCPToolError):
            raise
        except MCPProtocolError:
            if name == "business_commit_operation":
                raise MCPOutcomeUnknown(arguments["operation_id"]) from None
            raise
        except Exception as exc:
            if isinstance(exc, MCPTransportError) and not self.closed:
                self._submit(self._invalidate_key(key))
            if name == "business_commit_operation":
                raise MCPOutcomeUnknown(arguments["operation_id"]) from None
            raise MCPTransportError("Business MCP request failed") from None
        if name == "business_commit_operation" and not result["ok"]:
            if result["error"]["code"] == "outcome_unknown":
                raise MCPOutcomeUnknown(arguments["operation_id"])
        return result

    async def acall_tool(self, identity, token, name, arguments, *, grant=None):
        key, url = await asyncio.to_thread(self._check_request, identity, token, name, arguments)
        try:
            result = await self._asubmit(
                self._request(key, url, token, identity, name, arguments, grant)
            )
        except asyncio.CancelledError:
            if not self.closed:
                await asyncio.shield(self._asubmit(self._invalidate_key(key)))
            if name == "business_commit_operation":
                raise MCPOutcomeUnknown(arguments["operation_id"]) from None
            raise
        except (MCPAuthenticationError, MCPToolError):
            raise
        except MCPProtocolError:
            if name == "business_commit_operation":
                raise MCPOutcomeUnknown(arguments["operation_id"]) from None
            raise
        except Exception:
            if name == "business_commit_operation":
                raise MCPOutcomeUnknown(arguments["operation_id"]) from None
            raise MCPTransportError("Business MCP request failed") from None
        if name == "business_commit_operation" and not result["ok"]:
            if result["error"]["code"] == "outcome_unknown":
                raise MCPOutcomeUnknown(arguments["operation_id"])
        return result

    def list_tools(self, identity, token):
        key, url = self._check_request(identity, token, "tickets_list", {})
        return self._submit(self._catalog(key, url, token, identity))

    async def _catalog(self, key, url, token, identity):
        connection = await self._ensure_connection(key, url, token, identity)
        return list(connection.tools)

    def protocol_version(self, identity, token):
        key, url = self._check_request(identity, token, "tickets_list", {})
        return self._submit(self._protocol(key, url, token, identity))

    async def _protocol(self, key, url, token, identity):
        connection = await self._ensure_connection(key, url, token, identity)
        return connection.protocol_version

    def close_employee(self, identity):
        if not self.closed:
            self._submit(self._close_key(self._key(identity)))

    def close(self):
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop).result(30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(30)
            if self._thread.is_alive():
                raise RuntimeError("Business MCP event loop did not stop")

    async def aclose(self):
        await asyncio.to_thread(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class BusinessMCPClient:
    """A scoped borrower; no token or grant is exposed to model-facing tools."""

    def __init__(self, manager, identity, token):
        self._manager = manager
        self._identity = identity
        self._token = token
        self._closed = False

    def _available(self):
        if self._closed:
            raise RuntimeError("Employee MCP client is closed")

    @property
    def protocol_version(self):
        self._available()
        return self._manager.protocol_version(self._identity, self._token)

    def list_tools(self):
        self._available()
        return self._manager.list_tools(self._identity, self._token)

    def call_tool(self, name, arguments, *, grant=None):
        self._available()
        return self._manager.call_tool(self._identity, self._token, name, arguments, grant=grant)

    async def acall_tool(self, name, arguments, *, grant=None):
        self._available()
        return await self._manager.acall_tool(
            self._identity, self._token, name, arguments, grant=grant
        )

    @staticmethod
    def _data(envelope):
        if not envelope["ok"]:
            error = envelope["error"]
            raise MCPToolError(error["code"], error["message"])
        return envelope["data"]

    def read(self, command, params):
        if command not in READ_COMMANDS:
            raise ValueError("MCP read requires a declared read command")
        return self._data(self.call_tool("tickets_" + command, params))

    def prepare(self, request_id, command, params, *, grant=None):
        if command not in WRITE_COMMANDS:
            raise ValueError("MCP prepare requires a declared write command")
        return self._data(
            self.call_tool(
                "tickets_prepare_" + command,
                {"request_id": request_id, "params": params},
                grant=grant,
            )
        )

    def commit(self, operation_id, *, grant=None):
        return self._data(
            self.call_tool("business_commit_operation", {"operation_id": operation_id}, grant=grant)
        )

    def query_operation(self, operation_id):
        return self._data(self.call_tool("business_get_operation", {"operation_id": operation_id}))

    async def aread(self, command, params):
        if command not in READ_COMMANDS:
            raise ValueError("MCP read requires a declared read command")
        return self._data(await self.acall_tool("tickets_" + command, params))

    async def aprepare(self, request_id, command, params, *, grant=None):
        if command not in WRITE_COMMANDS:
            raise ValueError("MCP prepare requires a declared write command")
        return self._data(
            await self.acall_tool(
                "tickets_prepare_" + command,
                {"request_id": request_id, "params": params},
                grant=grant,
            )
        )

    async def acommit(self, operation_id, *, grant=None):
        return self._data(
            await self.acall_tool(
                "business_commit_operation", {"operation_id": operation_id}, grant=grant
            )
        )

    async def aquery_operation(self, operation_id):
        return self._data(
            await self.acall_tool("business_get_operation", {"operation_id": operation_id})
        )

    def close(self):
        if not self._closed:
            self._closed = True
            self._manager.close_employee(self._identity)

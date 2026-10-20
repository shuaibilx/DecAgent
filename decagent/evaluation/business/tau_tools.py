"""Shared, optional tau2 callable boundary for benchmark comparison routes.

No tau2 import occurs here. The harness loads and initializes the pinned upstream
environment, then supplies it to this boundary. Tool definitions and calls come
from that original environment; task goals, expected actions and scorer state are
never inputs to an agent-facing tool definition or MCP result.
"""

import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from math import isfinite
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.server.lowlevel.server import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations


class TauToolAdapter:
    """Expose a real upstream Toolkit or named Tool objects without coercion.

    Upstream Tool constructs a parameter schema but directly invokes its Python
    callable. Schema-driven casting, defaults or additional permission gates here
    would change the benchmark. Returned objects and raised exceptions therefore
    remain exactly those of ``toolkit.use_tool``.
    """

    def __init__(self, toolkit: Any | Mapping[str, Any]):
        self._toolkit = toolkit

    def openai_tools(self) -> list[dict[str, Any]]:
        """Copy public schemas; preserve upstream ordering and discoverability."""
        tools = self._toolkit if isinstance(self._toolkit, Mapping) else self._toolkit.get_tools()
        return [deepcopy(tool.openai_schema) for tool in tools.values()]

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """Invoke the original bound method, preserving its result and errors."""
        if isinstance(self._toolkit, Mapping):
            if name not in self._toolkit:
                raise ValueError(f"Tool '{name}' not found.")
            return self._toolkit[name](**arguments)
        return self._toolkit.use_tool(tool_name=name, **arguments)


@dataclass(frozen=True)
class TauToolResult:
    """The exact agent-visible content and error flag returned by upstream."""

    content: str
    is_error: bool


class TauToolSession:
    """One initialized upstream Environment, owned by one comparison trial.

    Delegation to ``get_response`` preserves upstream ToolMessage serialization,
    error handling and synchronization between agent/user toolkits, including
    error paths. A lightweight request has the same attributes as ToolCall; tau2
    does not require that argument to be a concrete ToolCall instance. It also
    avoids importing tau2's model/provider stack into the main application.
    """

    def __init__(self, environment: Any, *, timeout_seconds_provider=None):
        if timeout_seconds_provider is not None and not callable(timeout_seconds_provider):
            raise TypeError("τ tool deadline provider must be callable")
        self._environment = environment
        self._timeout_seconds_provider = timeout_seconds_provider

    def openai_tools(self) -> list[dict[str, Any]]:
        """Expose only the upstream agent catalog, never toolkit/database reprs."""
        return [deepcopy(tool.openai_schema) for tool in self._environment.get_tools()]

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str = "",
        timeout_seconds=None,
    ) -> TauToolResult:
        """Use the same assistant request path as an upstream simulated agent."""
        request = SimpleNamespace(
            id=call_id, name=name, arguments=dict(arguments), requestor="assistant"
        )
        if timeout_seconds is None and self._timeout_seconds_provider is not None:
            timeout_seconds = self._timeout_seconds_provider()
        if timeout_seconds is None:
            response = self._environment.get_response(request)
        else:
            response = self._environment.get_response(
                request, timeout_seconds=timeout_seconds
            )
        return TauToolResult(content=response.content, is_error=response.error)

    def mcp_server(self) -> Server:
        """Create an MCP server over this exact environment, without a simulator.

        No JSON decoding/re-encoding, output envelope, or schema validation is
        added. tau2 owns its textual tool result and its error flag. The native
        schemas remain available through MCP's inputSchema field.
        """

        def annotations(name):
            toolkit = getattr(self._environment, "tools", None)
            mutation = getattr(toolkit, "tool_mutates_state", None)
            # Only original upstream metadata can establish a read-only tool.
            read_only = callable(mutation) and mutation(name) is False
            return ToolAnnotations(readOnlyHint=read_only)

        async def list_tools(_context, _params):
            return ListToolsResult(
                tools=[
                    Tool(
                        name=item["function"]["name"],
                        description=item["function"].get("description"),
                        inputSchema=item["function"]["parameters"],
                        annotations=annotations(item["function"]["name"]),
                    )
                    for item in self.openai_tools()
                ]
            )

        async def call_tool(_context, params):
            # Keep synchronous environment mutations in one ordered request.
            # No worker-thread cancellation can leave a trial mutation running.
            timeout_seconds = (
                self._timeout_seconds_provider()
                if self._timeout_seconds_provider is not None
                else None
            )
            response = self.call_tool(
                params.name,
                params.arguments or {},
                timeout_seconds=timeout_seconds,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=response.content)],
                isError=response.is_error,
            )

        return Server("tau2-original-tools", on_list_tools=list_tools, on_call_tool=call_tool)


class TauMCPEnvironment:
    """Synchronous MCP client over one fresh tau2 user/agent environment.

    The conditional plan runtime is synchronous. This adapter runs a real MCP
    in-memory client/server session on a dedicated AnyIO portal thread, so each
    original tau2 tool still crosses the same MCP list/call protocol as the
    EnterpriseOps route. Calls remain ordered and are never retried here.
    """

    def __init__(self, environment_factory, *, system_prompt, user_prompt):
        if not callable(environment_factory):
            raise TypeError("A fresh tau2 environment factory is required")
        self._environment_factory = environment_factory
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._portal_manager = None
        self._portal = None
        self._stack = None
        self._client = None
        self._tau_session = None
        self.environment = None
        self._deadline_provider = None
        self._active_tool_timeout = None
        self._tool_call_lock = threading.Lock()
        self._cleanup_verified = False
        self._auxiliary_usage = {
            "user_simulator": {
                "requests": 0,
                "tokens": 0,
                "cost_usd": Decimal("0"),
                "seconds": 0.0,
            },
            "official_judge": {
                "requests": 0,
                "tokens": 0,
                "cost_usd": Decimal("0"),
                "seconds": 0.0,
            },
        }

    async def _serve(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            environment = self._environment_factory()
            close = getattr(environment, "close", None)
            if callable(close):
                stack.callback(close)
            tau_session = TauToolSession(
                environment,
                timeout_seconds_provider=lambda: self._active_tool_timeout,
            )
            server = tau_session.mcp_server()
            client_streams, server_streams = await stack.enter_async_context(
                create_client_server_memory_streams()
            )
            tasks = await stack.enter_async_context(anyio.create_task_group())
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            client = await stack.enter_async_context(ClientSession(*client_streams))
            await client.initialize()
            stop = anyio.Event()
            task_status.started((client, tau_session, environment, stop))
            await stop.wait()
        except BaseException:
            await stack.aclose()
            raise
        else:
            await stack.aclose()

    def __enter__(self):
        if self._portal is not None:
            raise RuntimeError("tau2 MCP session is already open")
        self._cleanup_verified = False
        self._portal_manager = anyio.from_thread.start_blocking_portal()
        self._portal = self._portal_manager.__enter__()
        try:
            self._serve_future, ready = self._portal.start_task(self._serve)
            self._client, self._tau_session, self.environment, self._stop = ready
            if self._system_prompt is None:
                get_policy = getattr(self.environment, "get_policy", None)
                if not callable(get_policy):
                    raise ValueError(
                        "τ environment must expose its official policy when no prompt is supplied"
                    )
                self._system_prompt = get_policy()
                if type(self._system_prompt) is not str or not self._system_prompt.strip():
                    raise ValueError("τ environment returned an empty official policy")
            return self
        except BaseException:
            self.close()
            raise

    def _active(self):
        if self._portal is None or self._client is None:
            raise RuntimeError("tau2 MCP session is not open")

    def set_deadline_provider(self, provider):
        self._active()
        if provider is not None and not callable(provider):
            raise TypeError("τ deadline provider must be callable")
        self._deadline_provider = provider

    def _request_timeout(self, supplied=None):
        values = []
        if supplied is not None:
            values.append(supplied)
        if self._deadline_provider is not None:
            values.append(self._deadline_provider())
        for value in values:
            if (
                type(value) not in {int, float}
                or not isfinite(value)
                or value <= 0
            ):
                raise TimeoutError("Public route or batch deadline expired")
        return min(values) if values else None

    @property
    def supports_user_simulation(self):
        self._active()
        return all(
            callable(getattr(self.environment, name, None))
            for name in ("user_turn", "call_user_tool", "score_simulation")
        )

    def model_input(self):
        self._active()
        result = self._portal.call(self._client.list_tools)
        return {
            "system_prompt": self._system_prompt,
            "user_prompt": self._user_prompt,
            "tools": [_json_model(tool) for tool in result.tools],
        }

    def call_tool(self, name, arguments, *, timeout_seconds=None):
        self._active()
        timeout_seconds = self._request_timeout(timeout_seconds)
        with self._tool_call_lock:
            self._active_tool_timeout = timeout_seconds
            try:
                if timeout_seconds is None:
                    result = self._portal.call(
                        self._client.call_tool, name, dict(arguments)
                    )
                else:
                    result = self._portal.call(
                        partial(
                            self._client.call_tool,
                            name,
                            dict(arguments),
                            read_timeout_seconds=timeout_seconds,
                        )
                    )
            finally:
                self._active_tool_timeout = None
        return _json_model(result)

    def user_turn(self, message, *, model, on_model_call, timeout_seconds=None):
        """Use the evaluator-only τ simulator control channel for one turn."""
        self._active()
        callback = getattr(self.environment, "user_turn", None)
        if not callable(callback):
            raise RuntimeError("This tau environment has no isolated user simulator")
        started = perf_counter()
        timeout_seconds = self._request_timeout(timeout_seconds)
        call_kwargs = {"model": model, "on_model_call": on_model_call}
        if timeout_seconds is not None:
            call_kwargs["timeout_seconds"] = timeout_seconds
        result = callback(message, **call_kwargs)
        self._record_auxiliary("user_simulator", result.get("usage"), perf_counter() - started)
        return result

    def call_user_tool(self, request, *, timeout_seconds=None):
        """Execute a τ user-side tool while retaining the original requestor."""
        self._active()
        callback = getattr(self.environment, "call_user_tool", None)
        if not callable(callback):
            raise RuntimeError("This tau environment has no user-side tool control")
        started = perf_counter()
        timeout_seconds = self._request_timeout(timeout_seconds)
        call_kwargs = (
            {"timeout_seconds": timeout_seconds}
            if timeout_seconds is not None
            else {}
        )
        try:
            return callback(request, **call_kwargs)
        finally:
            self._auxiliary_usage["user_simulator"]["seconds"] += (
                perf_counter() - started
            )

    def score_simulation(
        self,
        *,
        messages,
        termination_reason,
        duration,
        judge_model,
        on_model_call,
        timeout_seconds=None,
    ):
        """Call the original τ evaluator over a private evaluator trajectory."""
        self._active()
        callback = getattr(self.environment, "score_simulation", None)
        if not callable(callback):
            raise RuntimeError("This tau environment has no isolated official scorer")
        started = perf_counter()
        timeout_seconds = self._request_timeout(timeout_seconds)
        call_kwargs = dict(
            messages=messages,
            termination_reason=termination_reason,
            duration=duration,
            judge_model=judge_model,
            on_model_call=on_model_call,
        )
        if timeout_seconds is not None:
            call_kwargs["timeout_seconds"] = timeout_seconds
        result = callback(**call_kwargs)
        self._record_auxiliary(
            "official_judge", result.get("judge_usage"), perf_counter() - started
        )
        return result

    @property
    def auxiliary_usage(self):
        """Measured simulator and private-judge usage, kept outside agent usage."""
        return deepcopy(self._auxiliary_usage)

    @property
    def cleanup_verified(self):
        return self._cleanup_verified

    def _record_auxiliary(self, name, usage, seconds):
        if name not in self._auxiliary_usage or not isinstance(usage, dict):
            raise ValueError("Auxiliary model usage is missing or unrecognized")
        requests = usage.get("requests")
        if type(requests) is not int or requests < 0:
            raise ValueError("Auxiliary request count is invalid")
        tokens = None
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if (
            type(input_tokens) is int
            and input_tokens >= 0
            and type(output_tokens) is int
            and output_tokens >= 0
        ):
            tokens = input_tokens + output_tokens
        cost = usage.get("cost_usd")
        if cost is not None:
            if type(cost) not in {int, float} or not isfinite(cost) or cost < 0:
                raise ValueError("Auxiliary model cost is invalid")
            cost = Decimal(str(cost))
        current = self._auxiliary_usage[name]
        current["requests"] += requests
        current["tokens"] = (
            None if tokens is None or current["tokens"] is None else current["tokens"] + tokens
        )
        current["cost_usd"] = (
            None if cost is None or current["cost_usd"] is None else current["cost_usd"] + cost
        )
        current["seconds"] += seconds

    def close(self):
        if self._portal is None:
            return
        environment = self.environment
        stop_completed = False
        try:
            if getattr(self, "_stop", None) is not None:
                self._portal.call(self._stop.set)
                self._serve_future.result(timeout=30)
            stop_completed = True
        finally:
            manager, self._portal_manager = self._portal_manager, None
            self._portal = self._client = self._tau_session = None
            self._serve_future = self._stop = None
            self.environment = None
            try:
                manager.__exit__(None, None, None)
            except BaseException:
                self._cleanup_verified = False
                raise
            else:
                self._cleanup_verified = (
                    stop_completed
                    and getattr(environment, "cleanup_verified", False) is True
                )

    def __exit__(self, *_):
        self.close()


def _json_model(value):
    """Return JSON-RPC model data with MCP's camel-case wire field aliases."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    raise TypeError("MCP protocol object cannot be serialized as JSON")


@asynccontextmanager
async def open_tau_mcp_session(
    environment_factory: Callable[[], Any],
) -> AsyncIterator[ClientSession]:
    """Create a fresh upstream environment, server and MCP session per trial.

    The factory must load a fresh environment and apply the trial's upstream
    initialization before returning it. Never close over a previously used
    instance. Environment initialization, user simulation and scoring remain
    harness responsibilities and are not exposed through the MCP client.
    """
    async with AsyncExitStack() as stack:
        environment = environment_factory()
        close = getattr(environment, "close", None)
        if callable(close):
            stack.callback(close)
        session = TauToolSession(environment)
        server = session.mcp_server()
        client_streams, server_streams = await stack.enter_async_context(
            create_client_server_memory_streams()
        )
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(server.run, *server_streams, server.create_initialization_options())
            try:
                async with ClientSession(*client_streams) as client:
                    await client.initialize()
                    yield client
            finally:
                tasks.cancel_scope.cancel()

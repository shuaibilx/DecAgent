"""Offline adapter contracts; fixtures implement only the upstream boundary API."""

import importlib.util
from types import SimpleNamespace

import pytest


def adapter_module():
    assert importlib.util.find_spec("decagent.evaluation.business.tau_tools") is not None, (
        "shared tau Tool/Toolkit adapter has not been implemented"
    )
    from decagent.evaluation.business import tau_tools

    return tau_tools


class ContractToolkit:
    """Small boundary fixture, not a replacement business simulator."""

    def __init__(self):
        self.calls = []
        self.result = object()
        self.failure = ValueError("upstream failed")
        self.schema = {
            "type": "function",
            "function": {
                "name": "original",
                "description": "Original description.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer", "default": 4}},
                    "$defs": {"Nested": {"type": "object"}},
                },
            },
        }

    def get_tools(self):
        return {"original": SimpleNamespace(name="original", openai_schema=self.schema)}

    def use_tool(self, tool_name, **kwargs):
        self.calls.append((tool_name, kwargs))
        if tool_name == "fail":
            raise self.failure
        return self.result


class ContractEnvironment:
    """Captures delegation; response encoding belongs to upstream, not this fixture."""

    def __init__(self):
        self.tools = ContractToolkit()
        self.requests = []
        self.timeouts = []
        self.response = SimpleNamespace(content='{"number": "2"}', error=False)
        self.private_task = "never expose evaluation task or scorer"
        self.policy = "Visible policy supplied separately by the harness."
        # These test environments allocate no external resources.
        self.cleanup_verified = True

    def get_tools(self):
        return list(self.tools.get_tools().values())

    def get_response(self, request, *, timeout_seconds=None):
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        self.tools.use_tool(request.name, **request.arguments)
        return self.response


def test_toolkit_keeps_original_schemas_and_does_not_modify_upstream_objects():
    toolkit = ContractToolkit()
    adapter = adapter_module().TauToolAdapter(toolkit)

    assert adapter.openai_tools() == [toolkit.schema]
    copy = adapter.openai_tools()
    copy[0]["function"]["parameters"]["properties"].clear()
    assert "value" in toolkit.schema["function"]["parameters"]["properties"]
    assert adapter.openai_tools() == [toolkit.schema]


def test_toolkit_preserves_raw_arguments_results_and_exception_identity():
    toolkit = ContractToolkit()
    adapter = adapter_module().TauToolAdapter(toolkit)
    arguments = {"value": "2", "unknown": None}

    assert adapter.invoke("original", arguments) is toolkit.result
    assert toolkit.calls == [("original", arguments)]
    assert arguments == {"value": "2", "unknown": None}
    with pytest.raises(ValueError) as failure:
        adapter.invoke("fail", {})
    assert failure.value is toolkit.failure


def test_adapter_accepts_original_tool_callables_without_rebuilding_them():
    toolkit = ContractToolkit()

    class OriginalTool:
        openai_schema = toolkit.schema

        def __call__(self, **kwargs):
            # Bound/predefined argument handling remains inside the original Tool.
            if kwargs.get("fail"):
                raise toolkit.failure
            return toolkit.result

    adapter = adapter_module().TauToolAdapter({"original": OriginalTool()})
    assert adapter.openai_tools() == [toolkit.schema]
    assert adapter.invoke("original", {}) is toolkit.result
    with pytest.raises(ValueError) as failure:
        adapter.invoke("original", {"fail": True})
    assert failure.value is toolkit.failure
    with pytest.raises(ValueError, match="Tool 'missing' not found"):
        adapter.invoke("missing", {})


def test_environment_owns_serialization_sync_errors_and_requestor_semantics():
    environment = ContractEnvironment()
    session = adapter_module().TauToolSession(environment)

    result = session.call_tool("original", {"value": 2}, call_id="call-1")

    assert result.content == '{"number": "2"}'
    assert result.is_error is False
    request = environment.requests[0]
    assert (request.name, request.arguments, request.requestor, request.id) == (
        "original",
        {"value": 2},
        "assistant",
        "call-1",
    )
    environment.response = SimpleNamespace(content="Error: original failure", error=True)
    failure = session.call_tool("original", {})
    assert failure.content == "Error: original failure"
    assert failure.is_error is True
    assert "private_task" not in repr(session.openai_tools())


def test_tau_tool_session_forwards_deadline_to_owned_environment():
    environment = ContractEnvironment()
    session = adapter_module().TauToolSession(environment)

    result = session.call_tool("original", {"value": 2}, timeout_seconds=1.25)

    assert result.content == '{"number": "2"}'
    assert environment.timeouts == [1.25]


def test_synchronous_tau_adapter_uses_real_mcp_list_and_call_protocol():
    environment = ContractEnvironment()
    module = adapter_module()

    with module.TauMCPEnvironment(
        lambda: environment,
        system_prompt="Visible task policy.",
        user_prompt="Find the requested case.",
    ) as client:
        public = client.model_input()
        assert public["system_prompt"] == "Visible task policy."
        assert public["user_prompt"] == "Find the requested case."
        assert public["tools"][0]["name"] == "original"
        assert (
            public["tools"][0]["inputSchema"] == environment.tools.schema["function"]["parameters"]
        )

        result = client.call_tool("original", {"value": "7"})
        assert result["isError"] is False
        assert result["content"] == [{"type": "text", "text": '{"number": "2"}'}]
        assert environment.tools.calls == [("original", {"value": "7"})]


def test_synchronous_tau_mcp_call_propagates_deadline_through_protocol():
    environment = ContractEnvironment()

    with adapter_module().TauMCPEnvironment(
        lambda: environment,
        system_prompt="Visible task policy.",
        user_prompt="Find the requested case.",
    ) as client:
        result = client.call_tool("original", {"value": 7}, timeout_seconds=1.75)

    assert result["isError"] is False
    assert environment.timeouts == [1.75]


def test_mcp_bridge_failure_is_an_exception_and_owned_environment_is_closed():
    from mcp.shared.exceptions import MCPError

    environment = ContractEnvironment()
    environment.closed = False

    def broken(_request):
        raise TimeoutError("upstream worker terminated during write")

    environment.get_response = broken
    environment.close = lambda: setattr(environment, "closed", True)
    module = adapter_module()
    with module.TauMCPEnvironment(
        lambda: environment, system_prompt="Public", user_prompt="Public"
    ) as client:
        with pytest.raises(MCPError, match="upstream worker terminated"):
            client.call_tool("original", {})
    assert environment.closed


def test_mcp_initialization_failure_closes_its_owned_environment():
    from mcp.shared.exceptions import MCPError

    environment = ContractEnvironment()
    environment.closed = False
    environment.close = lambda: setattr(environment, "closed", True)

    def broken():
        raise ValueError("catalog broken")

    environment.get_tools = broken
    module = adapter_module()
    with pytest.raises(MCPError, match="catalog broken"):
        with module.TauMCPEnvironment(
            lambda: environment, system_prompt="Public", user_prompt="Public"
        ) as client:
            client.model_input()
    assert environment.closed


@pytest.mark.asyncio
async def test_real_mcp_roundtrip_keeps_schema_payload_errors_and_uncoerced_arguments():
    environment = ContractEnvironment()
    module = adapter_module()

    async with module.open_tau_mcp_session(lambda: environment) as client:
        catalog = await client.list_tools()
        assert len(catalog.tools) == 1
        tool = catalog.tools[0]
        original = environment.tools.schema["function"]
        assert tool.name == original["name"]
        assert tool.description == original["description"]
        assert tool.input_schema == original["parameters"]
        # Upstream tools do not coerce or schema-validate calls at runtime.
        result = await client.call_tool("original", {"value": "2", "extra": False})
        assert result.content[0].text == environment.response.content
        assert result.is_error is False
        assert environment.tools.calls == [("original", {"value": "2", "extra": False})]
        environment.response = SimpleNamespace(content="Error: upstream failure", error=True)
        result = await client.call_tool("original", {})
        assert result.is_error is True
        assert result.content[0].text == "Error: upstream failure"


@pytest.mark.asyncio
async def test_factory_creates_fresh_environment_and_mcp_client_per_trial():
    module = adapter_module()
    environments = []

    def fresh():
        environment = ContractEnvironment()
        environments.append(environment)
        return environment

    async with module.open_tau_mcp_session(fresh) as first:
        await first.call_tool("original", {"value": 1})
    async with module.open_tau_mcp_session(fresh) as second:
        await second.call_tool("original", {"value": 2})

    assert first is not second
    assert len(environments) == 2
    assert environments[0] is not environments[1]
    assert environments[0].tools.calls == [("original", {"value": 1})]
    assert environments[1].tools.calls == [("original", {"value": 2})]


def test_actual_upstream_toolkit_when_optional_tau_dependency_is_installed():
    pytest.importorskip("tau2", reason="optional pinned tau2 runtime is not installed")
    from tau2.data_model.message import ToolCall
    from tau2.environment.environment import Environment
    from tau2.environment.tool import as_tool
    from tau2.environment.toolkit import GenericToolKit

    toolkit = GenericToolKit()
    adapter = adapter_module().TauToolAdapter(toolkit)
    assert adapter.openai_tools() == [tool.openai_schema for tool in toolkit.get_tools().values()]
    assert adapter.invoke("calculate", {"expression": "2 + 2"}) == "4.0"
    with pytest.raises(ValueError, match="Invalid characters"):
        adapter.invoke("calculate", {"expression": "bad"})

    def original(value: int, bound: int) -> int:
        """Return the original bound value."""
        return value + bound

    tool = as_tool(original, bound=5)
    bound_adapter = adapter_module().TauToolAdapter({tool.name: tool})
    assert bound_adapter.openai_tools() == [tool.openai_schema]
    assert bound_adapter.invoke(tool.name, {"value": 2, "bound": 100}) == 7

    direct_environment = Environment("contract", "Public policy", GenericToolKit())
    adapted_environment = Environment("contract", "Public policy", GenericToolKit())
    session = adapter_module().TauToolSession(adapted_environment)
    for expression in ("2 + 2", "bad"):
        expected = direct_environment.get_response(
            ToolCall(id="call", name="calculate", arguments={"expression": expression})
        )
        actual = session.call_tool("calculate", {"expression": expression}, call_id="call")
        assert actual.content == expected.content
        assert actual.is_error == expected.error

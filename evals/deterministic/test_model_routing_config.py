"""Provider profiles stay separate from Jev settings and keep fallback opt-in."""

from decimal import Decimal

import pytest

from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import TokenPrice
from decagent.runtime.model_factory import build_budgeted_primary


class CapturingChatModel:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.instances.append(self)

    def bind_tools(self, definitions):
        self.definitions = definitions
        return self


class Ledger:
    pass


def test_openrouter_fallback_can_reuse_only_an_openrouter_jev_key():
    openrouter = Settings(jev_provider="openrouter", jev_api_key="safe-fixture-key")
    typesafe = Settings(jev_provider="typesafe", jev_api_key="safe-fixture-key")

    assert openrouter.resolved_fallback_api_key == "safe-fixture-key"
    assert typesafe.resolved_fallback_api_key == ""
    assert "safe-fixture-key" not in repr(openrouter)


def test_openrouter_main_route_requests_usage_for_normal_and_streaming_calls(monkeypatch):
    from langchain_core.messages import AIMessage, AIMessageChunk

    from decagent.models import primary as primary_module

    class StreamingChatModel(CapturingChatModel):
        def invoke(self, messages, **kwargs):
            self.invoke_kwargs = kwargs
            return AIMessage(
                content="ready",
                usage_metadata={
                    "input_tokens": 9,
                    "output_tokens": 2,
                    "total_tokens": 11,
                },
                response_metadata={"model_name": "deepseek/deepseek-v4.1-flash"},
            )

        def stream(self, messages, **kwargs):
            self.stream_kwargs = kwargs
            yield AIMessageChunk(
                content="ready",
                usage_metadata={
                    "input_tokens": 9,
                    "output_tokens": 2,
                    "total_tokens": 11,
                },
                response_metadata={"model_name": "deepseek/deepseek-v4.1-flash"},
            )

    CapturingChatModel.instances.clear()
    monkeypatch.setattr(primary_module, "ChatOpenAI", StreamingChatModel)
    settings = Settings(
        main_model="deepseek/deepseek-v4.1-flash",
        main_base_url="https://openrouter.ai/api/v1",
        main_api_key="openrouter-main-secret",
    )
    config = RuntimeConfig(
        main_provider="openrouter",
        main_price=TokenPrice("openrouter-main-v1", Decimal("0.6"), Decimal("2.4")),
    )

    routed = build_budgeted_primary(
        settings, config, Ledger(), purpose="planning", fallback_allowed=False
    )
    messages = [{"role": "user", "content": "usage check"}]
    normal_turn = routed.model.respond(messages)
    streaming_turn = routed.model.respond(messages, stream=True)

    client = StreamingChatModel.instances[0]
    assert client.kwargs["extra_body"]["usage"] == {"include": True}
    assert client.stream_kwargs["stream_options"] == {"include_usage": True}
    assert normal_turn.usage.total_tokens == 11
    assert streaming_turn.usage.total_tokens == 11
    assert routed.model.usage_include is True


def test_fallback_profile_uses_independent_model_and_includes_openrouter_usage(monkeypatch):
    from decagent.models import primary

    CapturingChatModel.instances.clear()
    monkeypatch.setattr(primary, "ChatOpenAI", CapturingChatModel)
    price = TokenPrice("deepseek-price-v1", Decimal(".2"), Decimal(".4"))
    settings = Settings(
        main_model="qwen3.8-flash",
        main_base_url="https://maas.example/v1",
        main_api_key="main-secret",
        jev_provider="openrouter",
        jev_api_key="openrouter-secret",
        fallback_enabled=True,
    )
    config = RuntimeConfig(
        main_price=TokenPrice("qwen-price-v1", Decimal("1"), Decimal("2")),
        fallback_price=price,
        main_fallback_enabled=True,
    )

    routed = build_budgeted_primary(settings, config, Ledger(), purpose="tau_simulator")

    main, fallback = [item.kwargs for item in CapturingChatModel.instances]
    assert main["model"] == "qwen3.8-flash" and main["base_url"] == "https://maas.example/v1"
    assert fallback["model"] == "deepseek/deepseek-v4.1-flash"
    assert fallback["base_url"] == "https://openrouter.ai/api/v1"
    assert fallback["api_key"] == "openrouter-secret"
    assert fallback["extra_body"] == {"usage": {"include": True}}
    assert fallback["include_response_headers"] is True and fallback["max_retries"] == 0
    assert routed.fallback_provider == "openrouter" and routed.fallback_price is price
    assert "openrouter-secret" not in repr(routed)


def test_openrouter_readiness_can_measure_usage_for_normal_and_streaming_responses(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    from decagent.evaluation.business.model_profiles import ModelProfile
    from decagent.models import primary as primary_module

    class StreamingChatModel(CapturingChatModel):
        def bind_tools(self, definitions, **_kwargs):
            self.definitions = definitions
            return self

        def stream(self, messages, **kwargs):
            self.stream_kwargs = kwargs
            yield AIMessageChunk(
                content="ready",
                usage_metadata={
                    "input_tokens": 9,
                    "output_tokens": 2,
                    "total_tokens": 11,
                },
                response_metadata={"model_name": "deepseek/deepseek-v4.1-flash"},
            )

    monkeypatch.setattr(primary_module, "ChatOpenAI", StreamingChatModel)
    profile = ModelProfile(
        role="primary",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        endpoint="https://openrouter.ai/api/v1",
        price_version="deepseek-readiness-v1",
        price_source="frozen readiness profile",
        input_price_per_million=Decimal("0.6"),
        output_price_per_million=Decimal("2.4"),
        max_input_tokens=65_536,
        max_output_tokens=256,
        temperature=Decimal("0"),
        top_p=Decimal("1"),
        unsupported_parameters=("seed",),
        provider_routing="openrouter_default",
    )

    model = primary_module.PrimaryModel.from_evaluation_profile(
        profile,
        api_key="openrouter-secret",
        usage_include=True,
    )
    client = StreamingChatModel.instances[-1]
    turn = model.respond([{"role": "user", "content": "stream check"}], stream=True)

    assert client.kwargs["extra_body"]["usage"] == {"include": True}
    assert client.stream_kwargs["stream_options"] == {"include_usage": True}
    assert turn.usage.total_tokens == 11
    assert "openrouter-secret" not in repr(model)


def test_fixed_model_cohort_can_disable_fallback_even_when_profile_is_configured(monkeypatch):
    from decagent.models import primary

    CapturingChatModel.instances.clear()
    monkeypatch.setattr(primary, "ChatOpenAI", CapturingChatModel)
    settings = Settings(
        main_api_key="main-secret",
        jev_provider="openrouter",
        jev_api_key="openrouter-secret",
        fallback_enabled=True,
    )
    config = RuntimeConfig(
        main_price=TokenPrice("qwen", Decimal("1"), Decimal("1")),
        fallback_price=TokenPrice("backup", Decimal("1"), Decimal("1")),
        main_fallback_enabled=True,
    )

    routed = build_budgeted_primary(
        settings, config, Ledger(), purpose="semantic_baseline", fallback_allowed=False
    )

    assert routed.fallback_model is None
    assert len(CapturingChatModel.instances) == 1


def test_diagnostic_can_force_only_its_explicitly_priced_fallback(monkeypatch):
    from decagent.models import primary

    CapturingChatModel.instances.clear()
    monkeypatch.setattr(primary, "ChatOpenAI", CapturingChatModel)
    settings = Settings(
        main_api_key="qwen-secret",
        jev_provider="openrouter",
        jev_api_key="openrouter-secret",
        fallback_enabled=False,
    )
    config = RuntimeConfig(fallback_price=TokenPrice("diagnostic", Decimal(".1"), Decimal(".2")))

    routed = build_budgeted_primary(
        settings, config, Ledger(), purpose="readiness", force_fallback=True
    )

    assert routed.fallback_model is not None
    assert len(CapturingChatModel.instances) == 2


def test_forced_diagnostic_fallback_requires_price_or_bounded_reservation():
    settings = Settings(
        main_api_key="qwen-secret",
        jev_provider="openrouter",
        jev_api_key="openrouter-secret",
    )

    with pytest.raises(ValueError, match="versioned pricing"):
        build_budgeted_primary(
            settings, RuntimeConfig(), Ledger(), purpose="readiness", force_fallback=True
        )


def test_runtime_refuses_to_enable_an_unpriced_fallback():
    with pytest.raises(ValueError, match="versioned pricing"):
        RuntimeConfig(main_fallback_enabled=True)

    with pytest.raises(ValueError, match="API key"):
        Settings(fallback_enabled=True, jev_provider="typesafe", jev_api_key="typesafe-key")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://openrouter.ai/api/v1",
        "https://capture.example/api/v1",
        "https://user:password@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?redirect=https://capture.example",
    ],
)
def test_openrouter_fallback_rejects_endpoints_that_could_expose_the_reused_key(endpoint):
    with pytest.raises(ValueError, match="OpenRouter HTTPS endpoint"):
        Settings(fallback_base_url=endpoint, jev_provider="openrouter", jev_api_key="key")

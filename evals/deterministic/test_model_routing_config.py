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

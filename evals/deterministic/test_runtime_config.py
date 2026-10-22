"""Runtime configuration validates immutable runner references."""

import pytest

from decagent.runtime.config import RuntimeConfig


@pytest.mark.parametrize(
    "value",
    [
        "sha256:" + "a" * 64,
        "ghcr.io/example/decagent-runner@sha256:" + "a" * 64,
    ],
)
def test_runtime_config_reads_runner_digest_from_immutable_reference(monkeypatch, value):
    monkeypatch.setenv("DECAGENT_RUNNER_IMAGE", value)

    assert RuntimeConfig.from_env().runner_digest == "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "value",
    [
        "ghcr.io/example/decagent-runner:latest",
        "ghcr.io/example/decagent-runner@sha256:short",
    ],
)
def test_runtime_config_rejects_mutable_or_malformed_runner_reference(monkeypatch, value):
    monkeypatch.setenv("DECAGENT_RUNNER_IMAGE", value)

    with pytest.raises(ValueError, match="fixed runner image digest"):
        RuntimeConfig.from_env()


def test_runtime_config_defaults_qwen_prices_to_cny_and_openrouter_prices_to_usd(monkeypatch):
    monkeypatch.setenv("DECAGENT_MAIN_PROVIDER", "qwen")
    monkeypatch.setenv("DECAGENT_MAIN_PRICE_VERSION", "qwen-v1")
    monkeypatch.setenv("DECAGENT_MAIN_INPUT_PER_MILLION", "1")
    monkeypatch.setenv("DECAGENT_MAIN_OUTPUT_PER_MILLION", "2")
    monkeypatch.setenv("DECAGENT_MAIN_FALLBACK_PRICE_VERSION", "deepseek-v1")
    monkeypatch.setenv("DECAGENT_MAIN_FALLBACK_INPUT_PER_MILLION", ".2")
    monkeypatch.setenv("DECAGENT_MAIN_FALLBACK_OUTPUT_PER_MILLION", ".4")

    config = RuntimeConfig.from_env()

    assert config.main_price.currency == "CNY"
    assert config.fallback_price.currency == "USD"

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

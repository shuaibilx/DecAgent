"""The conditional entry flag is explicit and cannot be enabled by truthy text."""

import pytest

from decagent.runtime.config import RuntimeConfig


def test_conditional_entry_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED", raising=False)
    assert RuntimeConfig().conditional_workflows_enabled is False
    assert RuntimeConfig.from_env().conditional_workflows_enabled is False


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_conditional_entry_requires_a_literal_boolean(value):
    with pytest.raises(ValueError, match="conditional|Conditional"):
        RuntimeConfig(conditional_workflows_enabled=value)


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), (" TRUE ", True)])
def test_conditional_entry_reads_explicit_environment_flag(monkeypatch, value, expected):
    monkeypatch.setenv("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED", value)
    assert RuntimeConfig.from_env().conditional_workflows_enabled is expected


@pytest.mark.parametrize("value", ["", "1", "0", "yes", "enabled", "flase"])
def test_conditional_entry_rejects_ambiguous_environment_values(monkeypatch, value):
    monkeypatch.setenv("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED", value)
    with pytest.raises(ValueError, match="DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED"):
        RuntimeConfig.from_env()

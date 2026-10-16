"""Planner-only control tools cannot shadow plugin command capabilities."""

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.planner import PlannerProtocol
from decagent.business.tickets import builtin_manifest


def test_protocol_exposes_the_registered_schema_and_controls_separately():
    spec = PluginSpec.from_mapping(builtin_manifest())
    protocol = PlannerProtocol(spec)
    name = next(name for name, command in protocol.commands.items() if command == "create")
    definition = next(x for x in protocol.definitions if x["function"]["name"] == name)
    assert definition["function"]["parameters"] == spec.commands["create"].input_schema
    assert protocol.validate("decagent_ask_user", {"question": "请选择部门"}) == {
        "question": "请选择部门",
    }
    with pytest.raises(ValueError):
        protocol.validate("execute_shell", {"command": "echo arbitrary"})
    with pytest.raises(ValueError):
        protocol.validate("decagent_finish", {"summary": "done", "completed_actions": []})

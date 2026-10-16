"""The planning model sees and validates the same registered capability definitions."""

import pytest
from langchain_core.messages import AIMessage

from decagent.models.primary import PrimaryModel


class Client:
    def __init__(self, name="lookup", args=None):
        self.name, self.args = name, args or {"enabled": True}

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        return AIMessage(
            content="", tool_calls=[{"name": self.name, "args": self.args, "id": "call-1"}]
        )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Business lookup",
            "parameters": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
                "additionalProperties": False,
            },
        },
    }
]


def validate(name, params):
    if name != "lookup" or set(params) != {"enabled"} or type(params["enabled"]) is not bool:
        raise ValueError("Unregistered business capability")
    return params


def test_registered_schema_and_validator_are_injected_together():
    client = Client()
    model = PrimaryModel(client, tool_definitions=TOOLS, validator=validate)
    assert client.tools == TOOLS
    assert model.respond([{"role": "user", "content": "lookup"}]).tools[0].params == {
        "enabled": True
    }


def test_unknown_business_tool_is_rejected_before_execution():
    model = PrimaryModel(Client("run_command"), tool_definitions=TOOLS, validator=validate)
    with pytest.raises(ValueError, match="Unregistered"):
        model.respond([{"role": "user", "content": "lookup"}])

"""The planner emits data plans with bounded format repair and no tool execution."""

import importlib
from copy import deepcopy

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from decagent.models.primary import (
    ModelTurn,
    ModelUsage,
    PrimaryResponseRejected,
    ToolProposal,
)
from evals.deterministic.test_business_plan_compiler import read_plan


class ScriptedModel:
    def __init__(self, turns):
        self.turns, self.calls = iter(turns), []

    def respond(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        result = next(self.turns)
        if isinstance(result, Exception):
            raise result
        return result


def turn(value=None, text=""):
    return ModelTurn(
        text,
        (ToolProposal("plan-call", "decagent_submit_plan", value),) if value else (),
        ModelUsage(10, 5, 15),
        "test-model",
    )


def planner(model):
    module = importlib.import_module("decagent.business.plans.planner")
    return module.ConditionalPlanner(model, PluginSpec.from_mapping(builtin_manifest()))


def test_first_model_plan_is_validated_without_a_followup_request():
    value = read_plan()
    model = ScriptedModel([turn(value)])
    proposal = planner(model).plan({"instruction": "Read current directory"})
    assert proposal.plan.to_mapping()["nodes"] == value["nodes"]
    assert len(model.calls) == 1
    assert proposal.repaired is False
    assert proposal.turns[0].usage.total_tokens == 15


def test_planner_prompt_includes_a_compilable_format_example_not_a_target_answer():
    import json

    from decagent.business.plans.compiler import compile_plan
    from decagent.business.plans.prompt_catalog import expand_catalog

    candidate = planner(ScriptedModel([]))
    instructions = candidate.instructions()
    catalog = expand_catalog(json.loads(instructions[instructions.index('{'):]))
    example = catalog.get("format_example")
    assert example is not None, "The full schema alone failed the live provider schema smoke"
    compiled = compile_plan(example["plan"], candidate.spec, input_schema=example["input_schema"])
    assert compiled.plan.entry == "directory"
    assert "format example is not the user's target" in instructions


def test_format_example_is_omitted_when_its_registered_contract_is_incompatible():
    candidate = planner(ScriptedModel([]))
    candidate.transforms = {}
    # A packaged example must never authorize tools absent from this plugin.
    manifest = builtin_manifest()
    result = manifest["commands"]["directory"]["result_schema"]
    result["properties"].pop("categories")
    result["required"] = [name for name in result["required"] if name != "categories"]
    candidate.spec = PluginSpec.from_mapping(manifest)
    assert candidate.format_example() is None


def test_one_format_repair_keeps_original_usage_and_reason():
    invalid = read_plan()
    invalid["nodes"][0]["python"] = "arbitrary code"
    model = ScriptedModel([turn(invalid), turn(read_plan())])
    result = planner(model).plan({"instruction": "Read current directory"})
    assert result.repaired
    assert len(model.calls) == 2
    assert len(result.turns) == 2
    assert result.turns[0].usage.total_tokens == 15
    assert "repair" in model.calls[1][0]["content"].lower()


def test_unknown_usage_rejection_stops_without_a_format_repair_request():
    model = ScriptedModel(
        [
            PrimaryResponseRejected(reason="tool_schema", usage=None, model="qwen3.8-flash"),
            turn(read_plan()),
        ]
    )

    with pytest.raises(PrimaryResponseRejected):
        planner(model).plan({"instruction": "Read current directory"})

    assert len(model.calls) == 1


def test_second_invalid_plan_stops_instead_of_open_ended_model_loop():
    model = ScriptedModel([turn(text="not a plan"), turn(text="still not a plan")])
    with pytest.raises(ValueError, match="invalid"):
        planner(model).plan({"instruction": "Read current directory"})
    assert len(model.calls) == 2


def test_transport_exception_is_not_automatically_replanned_or_retried():
    model = ScriptedModel([ConnectionError("transport unavailable")])
    with pytest.raises(ConnectionError):
        planner(model).plan({"instruction": "Read current directory"})
    assert len(model.calls) == 1


def test_patch_context_binds_parent_and_frozen_nodes():
    value = read_plan()
    value["revision"] = 2
    model = ScriptedModel([turn(value)])
    result = planner(model).plan(
        {
            "instruction": "Read refreshed directory",
            "parent_revision": 1,
            "frozen_nodes": ["already_created"],
            "replan_reason": "new_observation",
            "evidence": [{"kind": "verified", "node_id": "already_created"}],
        }
    )
    assert result.plan.revision == 2
    assert "already_created" in model.calls[0][1]["content"]
    assert "new_observation" in model.calls[0][1]["content"]


def test_planner_tool_validator_rejects_other_capabilities():
    module = importlib.import_module("decagent.business.plans.planner")
    with pytest.raises(ValueError):
        module.validate_plan_tool("tickets_prepare_close", {})
    assert module.validate_plan_tool("decagent_submit_plan", read_plan())["schema_version"] == "1"


def test_real_primary_adapter_preserves_bounded_nested_plan(monkeypatch):
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from decagent.business.plans.planner import plan_tool_definitions, validate_plan_tool
    from decagent.models import primary
    from evals.deterministic.test_business_plan_compiler import branching_plan

    value = branching_plan()

    class Client:
        def bind_tools(self, definitions):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "nested-plan",
                        "name": "decagent_submit_plan",
                        "args": value,
                    }
                ],
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                response_metadata={"model_name": "fixture-model"},
            )

    monkeypatch.setattr(primary, "ChatOpenAI", lambda **kwargs: Client())
    settings = SimpleNamespace(
        main_model="fixture-model",
        main_base_url="https://example.test/v1",
        main_api_key="test-local-key",
        jev_api_key="",
        github_token="",
    )
    model = primary.PrimaryModel.from_settings(
        settings,
        tool_definitions=plan_tool_definitions(),
        validator=validate_plan_tool,
    )
    result = model.respond([{"role": "user", "content": "Inspect fixture plan"}])
    assert result.tools[0].params["nodes"] == value["nodes"]


def test_planning_transport_receives_repair_purpose_and_parent_revision():
    value = read_plan()
    value["revision"] = 2

    class AccountedModel(ScriptedModel):
        def __init__(self):
            super().__init__([turn(text="invalid"), turn(value)])
            self.metadata = []

        def respond_plan(self, messages, **metadata):
            self.metadata.append(metadata)
            return self.respond(messages)

    model = AccountedModel()
    planner(model).plan(
        {
            "instruction": "Read current directory",
            "parent_revision": 1,
            "replan_reason": "new_observation",
        }
    )
    assert [item["purpose"] for item in model.metadata] == ["replan", "format_repair"]
    assert [item["exception_reason"] for item in model.metadata] == [
        "new_observation",
        "format_repair",
    ]
    assert all(item["plan_revision"] == 2 for item in model.metadata)

"""Prompt compression must preserve every registered contract and count full input."""

import importlib
import json

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from evals.deterministic.test_business_conditional_planner import ScriptedModel, turn
from evals.deterministic.test_business_plan_compiler import read_plan


def test_catalog_factoring_is_lossless_deterministic_and_reduces_repeated_schemas():
    api = importlib.import_module("decagent.business.plans.prompt_catalog")
    original = builtin_manifest()
    packed = api.pack_catalog(original)
    assert packed == api.pack_catalog(original)
    assert api.expand_catalog(packed) == original
    assert len(json.dumps(packed)) < len(json.dumps(original)) * 0.7
    assert api.expand_catalog(api.pack_catalog({"$catalog_ref": "fragment_0"})) == {
        "$catalog_ref": "fragment_0"
    }


def test_full_planner_input_guard_counts_system_tools_and_format_repair_before_dispatch():
    from decagent.business.plans.planner import ConditionalPlanner

    model = ScriptedModel([turn(read_plan())])
    with pytest.raises(ValueError, match="request|input|context"):
        ConditionalPlanner(
            model, PluginSpec.from_mapping(builtin_manifest()), max_request_tokens=100
        ).plan({"instruction": "Read current directory"})
    assert model.calls == []


def test_default_planner_prefix_is_factored_without_removing_registered_capabilities():
    from decagent.business.plans.planner import ConditionalPlanner

    model = ScriptedModel([turn(read_plan())])
    planner = ConditionalPlanner(model, PluginSpec.from_mapping(builtin_manifest()))
    assert len(planner.instructions().encode("utf-8")) < 22000
    assert planner.plan({"instruction": "Read current directory"}).plan.revision == 1
    assert len(model.calls) == 1

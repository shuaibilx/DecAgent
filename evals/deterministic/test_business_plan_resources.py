"""Installed plan resources remain independently usable outside a source checkout."""

import json
from importlib.resources import files

from decagent.business.contracts import PluginSpec
from decagent.business.plans import plan_schema
from decagent.business.plans.compiler import compile_plan
from decagent.business.tickets import builtin_manifest


def test_packaged_examples_compile_with_the_installed_schema():
    document = json.loads(
        files("decagent.business.plans").joinpath("examples.json").read_text(encoding="utf-8")
    )
    assert plan_schema()["$schema"].endswith("2020-12/schema")
    assert document["format"] == "decagent.plan-examples.v1"
    assert set(document["examples"]) == {"directory", "create_with_category"}
    spec = PluginSpec.from_mapping(builtin_manifest())
    for example in document["examples"].values():
        result = compile_plan(example["plan"], spec, input_schema=example["input_schema"])
        assert result.plan.digest == example["plan"]["content_digest"]

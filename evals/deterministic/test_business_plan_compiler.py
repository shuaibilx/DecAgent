"""Compile-time validation rejects plans before any business effect."""

import importlib
from copy import deepcopy

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.tickets import builtin_manifest
from evals.deterministic.test_business_plan_contracts import literal, node, reference


def context():
    spec = PluginSpec.from_mapping(builtin_manifest())
    return spec, QuestionRegistry.from_spec(spec)


def read_plan():
    spec, registry = context()
    value = {
        "schema_version": "1",
        "plan_id": "directory",
        "revision": 1,
        "registry_fingerprint": registry.fingerprint,
        "entry": "directory",
        "goal": {
            "description": "Read current directory",
            "evidence": [
                {
                    "id": "observed",
                    "kind": "tool_result",
                    "description": "Verified directory",
                    "source": reference(
                        "node_output", node_id="directory", path=["categories"], value_type="array"
                    ),
                }
            ],
        },
        "nodes": [
            node(
                "directory",
                "read_tool",
                tool={"plugin_id": spec.id, "command": "directory"},
                inputs={},
                next=["done"],
            ),
            node("done", "complete", depends_on=["directory"], evidence=["observed"]),
        ],
    }
    return value


def compile_value(value):
    api = importlib.import_module("decagent.business.plans.compiler")
    spec, _ = context()
    return api.compile_plan(
        value,
        spec,
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "ticket_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )


def test_registered_read_is_compiled_without_running_any_tool():
    compiled = compile_value(read_plan())
    assert compiled.order == ("directory", "done")
    assert compiled.successors("directory") == ("done",)
    assert compiled.node("directory")["tool"]["command"] == "directory"


@pytest.mark.parametrize(
    "mutation",
    ["unknown_tool", "wrong_plugin", "write_as_read", "extra_input", "wrong_fingerprint"],
)
def test_compiler_rejects_unknown_capabilities_and_changed_contract(mutation):
    value = read_plan()
    first = value["nodes"][0]
    if mutation == "unknown_tool":
        first["tool"]["command"] = "shell"
    elif mutation == "wrong_plugin":
        first["tool"]["plugin_id"] = "other"
    elif mutation == "write_as_read":
        first["tool"]["command"] = "close"
    elif mutation == "extra_input":
        first["inputs"]["employee_id"] = literal("another-employee")
    else:
        value["registry_fingerprint"] = "f" * 64
    with pytest.raises(ValueError):
        compile_value(value)


@pytest.mark.parametrize("mutation", ["duplicate", "dangling", "cycle", "unreachable"])
def test_compiler_rejects_invalid_graphs(mutation):
    value = read_plan()
    if mutation == "duplicate":
        value["nodes"].append(deepcopy(value["nodes"][0]))
    elif mutation == "dangling":
        value["nodes"][0]["next"] = ["absent"]
    elif mutation == "cycle":
        value["nodes"][0]["next"] = ["directory"]
    else:
        value["nodes"].append(
            node(
                "orphan",
                "ask_user",
                prompt="unused",
                answer={"value_type": "string", "nullable": False},
                next=["done"],
            )
        )
    with pytest.raises(ValueError):
        compile_value(value)


@pytest.mark.parametrize(
    "mutation", ["future", "false_provenance", "unknown_path", "wrong_type", "unknown_input"]
)
def test_compiler_checks_real_producer_type_and_reference_path(mutation):
    value = read_plan()
    first = value["nodes"][0]
    if mutation == "future":
        first["inputs"]["ticket_id"] = reference("node_output", node_id="done", path=["id"])
        first["tool"]["command"] = "get"
    elif mutation == "false_provenance":
        value["goal"]["evidence"][0]["source"]["provenance"] = "write_receipt"
        value["goal"]["evidence"][0]["kind"] = "write_receipt"
    elif mutation == "unknown_path":
        value["goal"]["evidence"][0]["source"]["path"] = ["imaginary"]
    elif mutation == "wrong_type":
        value["goal"]["evidence"][0]["source"]["value_type"] = "string"
    else:
        first["tool"]["command"] = "get"
        first["inputs"] = {"ticket_id": reference("task_input", path=["invented"])}
    with pytest.raises(ValueError):
        compile_value(value)


def branching_plan():
    value = read_plan()
    branch = node(
        "choose",
        "branch",
        cases=[
            {
                "condition": {
                    "op": "exists",
                    "value": reference("task_input", path=["ticket_id"], nullable=True),
                },
                "next": ["read_ticket"],
            }
        ],
        on_no_match={"node_id": "directory"},
    )
    get = node(
        "read_ticket",
        "read_tool",
        tool={"plugin_id": context()[0].id, "command": "get"},
        inputs={"ticket_id": reference("task_input", path=["ticket_id"])},
        depends_on=["choose"],
        next=["merge"],
    )
    value["entry"] = "choose"
    value["nodes"][0]["next"] = ["merge"]
    value["nodes"] = [
        branch,
        get,
        *value["nodes"][:1],
        node(
            "merge",
            "join",
            mode="all_active",
            depends_on=["directory", "read_ticket"],
            next=["done"],
        ),
        value["nodes"][-1],
    ]
    # The directory is not available on the read-ticket branch.
    value["goal"]["evidence"][0]["source"]["nullable"] = True
    value["goal"]["evidence"][0]["condition"] = {
        "op": "exists",
        "value": reference(
            "node_output",
            node_id="directory",
            path=["categories"],
            value_type="array",
            nullable=True,
        ),
    }
    value["nodes"][-1]["depends_on"] = ["merge"]
    return value


def test_all_active_join_accepts_optional_branch_data_and_does_not_require_both():
    compiled = compile_value(branching_plan())
    assert compiled.node("merge")["mode"] == "all_active"
    assert set(compiled.predecessors("merge")) == {"read_ticket", "directory"}


def test_required_cross_branch_reference_is_rejected():
    value = branching_plan()
    value["goal"]["evidence"][0]["source"]["nullable"] = False
    value["goal"]["evidence"][0].pop("condition")
    with pytest.raises(ValueError, match="branch"):
        compile_value(value)


def test_deep_branching_exceeds_four_levels():
    value = read_plan()
    value["entry"] = "branch_0"
    branches = []
    for index in range(5):
        target = "branch_" + str(index + 1) if index < 4 else "directory"
        branches.append(
            node(
                "branch_" + str(index),
                "branch",
                cases=[
                    {
                        "condition": {"op": "eq", "left": literal("a"), "right": literal("a")},
                        "next": [target],
                    }
                ],
                on_no_match={"stop": "no_match"},
            )
        )
    value["nodes"] = [*branches, *value["nodes"]]
    with pytest.raises(ValueError, match="depth"):
        compile_value(value)


def test_primary_cannot_add_inline_question_criteria():
    value = read_plan()
    definition = context()[1].get("work_blockage")
    decide = node(
        "severity",
        "decide",
        question={
            "question_id": definition["question_id"],
            "version": definition["version"],
            "type": "score",
            "criteria": ["safe", "not safe"],
        },
        policy={"policy_id": "calibrated", "version": "1"},
        inputs={"instruction": reference("task_input", path=["instruction"])},
        next=["directory"],
        on_no_match={"stop": "no_match"},
    )
    value["entry"] = "severity"
    value["nodes"].insert(0, decide)
    with pytest.raises(ValueError):
        compile_value(value)


def test_parallel_ready_nodes_are_not_mistaken_for_exclusive_branches():
    value = read_plan()
    spec, _ = context()
    value["nodes"][0]["next"] = ["left", "right"]
    value["nodes"] = [
        value["nodes"][0],
        *[
            node(
                name,
                "read_tool",
                tool={"plugin_id": spec.id, "command": "list"},
                inputs={},
                depends_on=["directory"],
                next=["done"],
            )
            for name in ("left", "right")
        ],
        node("done", "complete", depends_on=["left", "right"], evidence=["observed"]),
    ]
    value["goal"]["evidence"][0]["source"] = reference(
        "node_output", node_id="left", path=["tickets"], value_type="array"
    )
    compiled = compile_value(value)
    assert set(compiled.predecessors("done")) == {"left", "right"}

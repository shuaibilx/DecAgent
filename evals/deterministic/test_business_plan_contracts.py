"""Finite plans are typed data; future outputs remain explicit references."""

import copy
import hashlib
import importlib
import json
from dataclasses import FrozenInstanceError

import pytest
from jsonschema import Draft202012Validator


def contracts():
    return importlib.import_module("decagent.business.plans")


def literal(value, value_type="string", nullable=False):
    return {
        "source": "literal",
        "value": value,
        "value_type": value_type,
        "nullable": nullable,
        "provenance": "plan_literal",
    }


def reference(source, *, node_id=None, value_type="string", nullable=False, path=None):
    binding = {
        "source": source,
        "path": [] if path is None else path,
        "value_type": value_type,
        "nullable": nullable,
        "provenance": {
            "task_input": "user_input",
            "node_output": "tool_result",
            "accepted_decision": "accepted_decision",
            "verified_memory": "verified_memory",
        }[source],
    }
    if node_id is not None:
        binding["node_id"] = node_id
    if source == "verified_memory":
        binding["memory_kind"] = "confirmed_preference"
        binding["subject"] = "response_language"
    return binding


def node(node_id, kind, **fields):
    return {
        "id": node_id,
        "type": kind,
        "depends_on": [],
        "on_missing": {"stop": "missing_input"},
        "on_error": {"stop": "error"},
        **fields,
    }


def straight_plan():
    receipt = reference("node_output", node_id="create", path=["receipt", "id"])
    receipt["provenance"] = "write_receipt"
    return {
        "schema_version": "1",
        "plan_id": "create-ticket",
        "revision": 1,
        "registry_fingerprint": "a" * 64,
        "goal": {
            "description": "Create a ticket and verify its receipt",
            "evidence": [
                {
                    "id": "created",
                    "kind": "write_receipt",
                    "description": "A verified creation receipt",
                    "source": receipt,
                }
            ],
        },
        "entry": "lookup",
        "nodes": [
            node(
                "lookup",
                "read_tool",
                tool={"plugin_id": "service-tickets", "command": "lookup"},
                inputs={"query": reference("task_input", path=["query"])},
                next=["create"],
            ),
            node(
                "create",
                "governed_write",
                command={"plugin_id": "service-tickets", "command": "create"},
                inputs={"title": reference("node_output", node_id="lookup", path=["title"])},
                depends_on=["lookup"],
                next=["done"],
            ),
            node("done", "complete", depends_on=["create"], evidence=["created"]),
        ],
    }


def branch_plan():
    value = straight_plan()
    value["entry"] = "route"
    value["nodes"] = [
        node(
            "route",
            "decide",
            question={"question_id": "route_ticket", "version": "1", "type": "choice"},
            policy={"policy_id": "ticket_route", "version": "1"},
            inputs={
                "request": reference("task_input"),
                "history": reference("verified_memory", value_type="object"),
            },
            next=["branch"],
            on_no_match={"node_id": "clarify"},
        ),
        node(
            "branch",
            "branch",
            depends_on=["route"],
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": reference("accepted_decision", node_id="route", path=["choice"]),
                        "right": literal("create"),
                    },
                    "next": ["lookup"],
                },
                {
                    "condition": {
                        "op": "exists",
                        "value": reference("task_input", nullable=True, path=["ticket_id"]),
                    },
                    "next": ["clarify"],
                },
            ],
            on_no_match={"node_id": "clarify"},
        ),
        node(
            "clarify",
            "ask_user",
            prompt="What ticket title should be used?",
            answer={"value_type": "string", "nullable": False},
            next=["normalize"],
        ),
        node(
            "normalize",
            "transform",
            transform={"transform_id": "normalize_title", "version": "1"},
            inputs={"title": reference("node_output", node_id="clarify")},
            next=["lookup"],
        ),
        *value["nodes"][:-1],
        node(
            "joined",
            "join",
            depends_on=["create", "normalize"],
            mode="all_active",
            next=["done"],
        ),
        value["nodes"][-1],
    ]
    value["nodes"][3]["inputs"]["title"]["provenance"] = "user_answer"
    value["nodes"][5]["next"] = ["joined"]
    value["nodes"][-1]["depends_on"] = ["joined"]
    return value


def test_straight_plan_round_trips_and_derives_a_canonical_digest():
    api = contracts()
    original = straight_plan()
    plan = api.Plan.from_mapping(original)
    expected = hashlib.sha256(
        json.dumps(
            original, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    assert plan.digest == expected
    assert plan.to_mapping() == {**original, "content_digest": expected}
    assert plan.plan_id == "create-ticket"
    assert plan.schema_version == "1"
    assert plan.revision == 1
    assert plan.registry_fingerprint == "a" * 64
    assert plan.entry == "lookup"
    assert plan.nodes == original["nodes"]
    assert plan.goal == original["goal"]
    assert api.Plan.from_mapping(plan.to_mapping()).digest == expected


def test_branch_plan_accepts_each_registered_node_shape_and_future_references():
    value = branch_plan()
    plan = contracts().Plan.from_mapping(value)
    assert {item["type"] for item in plan.nodes} == {
        "read_tool",
        "transform",
        "decide",
        "branch",
        "join",
        "ask_user",
        "governed_write",
        "complete",
    }
    assert plan.goal["evidence"][0]["source"]["node_id"] == "create"
    assert plan.goal["evidence"][0]["source"]["value_type"] == "string"


def test_contract_is_immutable_and_returns_independent_json_snapshots():
    api = contracts()
    value = straight_plan()
    plan = api.Plan.from_mapping(value)
    before = plan.digest
    value["nodes"][0]["tool"]["command"] = "other"
    snapshot = plan.to_mapping()
    snapshot["nodes"][0]["tool"]["command"] = "changed"
    plan.nodes[0]["inputs"].clear()
    plan.goal["description"] = "changed"
    assert plan.digest == before
    assert plan.nodes[0]["tool"]["command"] == "lookup"
    assert plan.goal["description"] == "Create a ticket and verify its receipt"
    with pytest.raises(FrozenInstanceError):
        plan._source = "{}"


def test_digest_binds_every_plan_content_field_and_rejects_supplied_mismatch():
    api = contracts()
    value = straight_plan()
    before = api.Plan.from_mapping(value).digest
    value["revision"] = 2
    assert api.Plan.from_mapping(value).digest != before
    value["content_digest"] = before
    with pytest.raises(ValueError, match="content_digest"):
        api.Plan.from_mapping(value)


def test_schema_resource_is_standalone_closed_and_independent():
    api = contracts()
    schema = api.plan_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(straight_plan())
    validator.validate(branch_plan())
    schema["properties"].clear()
    assert api.plan_schema()["properties"]["schema_version"]["const"] == "1"
    assert api.MAX_NODES == 32
    assert api.MAX_BRANCH_DEPTH == 4
    assert api.MAX_REVISION == 4


@pytest.mark.parametrize("field", ["schema_version", "plan_id", "revision", "entry", "goal"])
def test_missing_required_plan_fields_are_rejected(field):
    value = straight_plan()
    del value[field]
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("schema_version", "2"),
        ("plan_id", ""),
        ("revision", 0),
        ("revision", 5),
        ("revision", True),
        ("registry_fingerprint", "not-a-sha256"),
        ("entry", "__import__('os')"),
        ("nodes", []),
    ],
)
def test_invalid_plan_scalars_and_empty_nodes_are_rejected(field, invalid):
    value = straight_plan()
    value[field] = invalid
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "path, field",
    [
        ([], "actor"),
        ([], "grants"),
        ([], "authorization"),
        ([], "extra"),
        (["goal"], "status"),
        (["goal", "evidence", 0], "verified"),
        (["nodes", 0], "code"),
        (["nodes", 0, "tool"], "module"),
        (["nodes", 0, "inputs", "query"], "expression"),
        (["nodes", 0, "on_error"], "retry_code"),
    ],
)
def test_unknown_and_executable_fields_are_rejected_at_every_contract_level(path, field):
    value = straight_plan()
    target = value
    for part in path:
        target = target[part]
    target[field] = "import os"
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("kind", ["python", "shell", "eval", "loop", "subplan"])
def test_unknown_or_executable_node_types_are_rejected(kind):
    value = straight_plan()
    value["nodes"][0]["type"] = kind
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "name", ["actor", "actor_id", "grants", "auth", "authorization", "permissions"]
)
def test_nodes_cannot_bind_reserved_authority_arguments(name):
    value = straight_plan()
    value["nodes"][1]["inputs"][name] = literal("forged")
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "binding",
    [
        literal(3),
        literal(True, "integer"),
        literal(None),
        literal("3", "number"),
        literal([], "object"),
        literal({}, "array"),
        literal(1, "boolean"),
        literal(float("nan"), "number"),
        literal(float("inf"), "number"),
        literal({1: "non-json-key"}, "object"),
        literal(("tuple",), "array"),
        literal(object(), "object"),
    ],
)
def test_literal_bindings_reject_type_confusion_and_non_json_values(binding):
    value = straight_plan()
    value["nodes"][0]["inputs"]["query"] = binding
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "binding",
    [
        literal(None, nullable=True),
        literal(3, "integer"),
        literal(3.5, "number"),
        literal(True, "boolean"),
        literal({"nested": [1, None, "text"]}, "object"),
        literal(["a", "b"], "array"),
    ],
)
def test_literals_accept_only_values_matching_their_declared_type(binding):
    value = straight_plan()
    value["nodes"][0]["inputs"]["query"] = binding
    contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "expression"},
        {"value_type": "uuid"},
        {"nullable": "false"},
        {"path": ["__class__"]},
        {"path": ["ticket.id"]},
        {"path": ["*"]},
        {"path": [".."]},
        {"path": [-1]},
        {"path": [True]},
        {"path": ["id"] * 9},
        {"provenance": "authorization"},
        {"node_id": "fake-node"},
    ],
)
def test_reference_bindings_reject_invalid_paths_and_inappropriate_source_fields(changes):
    value = straight_plan()
    value["nodes"][0]["inputs"]["query"].update(changes)
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("remove", ["value_type", "nullable", "provenance", "path", "node_id"])
def test_future_output_references_require_type_provenance_and_a_named_source(remove):
    value = straight_plan()
    del value["nodes"][1]["inputs"]["title"][remove]
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_field_paths_support_array_indices_without_executable_expressions():
    value = straight_plan()
    value["nodes"][1]["inputs"]["title"]["path"] = ["tickets", 0, "id"]
    contracts().Plan.from_mapping(value)


def test_graph_registration_and_reference_resolution_are_left_to_the_compiler():
    value = straight_plan()
    value["nodes"][0]["tool"]["command"] = "not_registered_yet"
    value["nodes"][1]["inputs"]["title"]["node_id"] = "future_output"
    value["nodes"][1]["depends_on"] = ["future_output"]
    contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("field", ["on_missing", "on_error", "next", "depends_on"])
def test_executable_nodes_require_explicit_dependencies_successors_and_failure_exits(field):
    value = straight_plan()
    del value["nodes"][0][field]
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("kind", ["choice", "noul", "score"])
def test_decision_definitions_are_versioned_trusted_references(kind):
    value = branch_plan()
    value["nodes"][0]["question"]["type"] = kind
    contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "field, invalid",
    [
        ("question", {"question_id": "route", "type": "choice"}),
        ("question", {"question_id": "route", "version": "1", "type": "free_text"}),
        (
            "question",
            {"question_id": "route", "version": "1", "type": "choice", "instructions": "do it"},
        ),
        ("policy", {"policy_id": "route", "threshold": 0.2}),
    ],
)
def test_decisions_reject_unversioned_or_model_authored_definitions(field, invalid):
    value = branch_plan()
    value["nodes"][0][field] = invalid
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def condition():
    return {"op": "eq", "left": literal("open"), "right": literal("open")}


@pytest.mark.parametrize(
    "rule",
    [
        condition(),
        {"op": "in", "value": literal("open"), "collection": literal(["open"], "array")},
        {"op": "exists", "value": reference("task_input", path=["id"], nullable=True)},
        {"op": "all", "conditions": [condition(), condition()]},
        {"op": "any", "conditions": [condition(), condition()]},
        *[
            {"op": op, "left": literal(2, "integer"), "right": literal(1.5, "number")}
            for op in ["lt", "lte", "gt", "gte"]
        ],
    ],
)
def test_branch_condition_operations_are_closed_typed_data(rule):
    value = branch_plan()
    value["nodes"][1]["cases"][0]["condition"] = rule
    contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "rule",
    [
        {"op": "eval", "expression": "state.id"},
        {"op": "regex", "value": literal("a"), "pattern": ".*"},
        {"op": "all", "conditions": []},
        {"op": "eq", "left": literal(1, "integer"), "right": literal("1")},
        {"op": "lt", "left": literal("1"), "right": literal("2")},
        {"op": "in", "value": literal("a"), "collection": literal("a")},
        {"op": "exists", "value": literal("a")},
        {**condition(), "expression": "True"},
    ],
)
def test_malformed_executable_and_type_incompatible_conditions_are_rejected(rule):
    value = branch_plan()
    value["nodes"][1]["cases"][0]["condition"] = rule
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_branch_and_decision_require_explicit_no_match_exits():
    for index in [0, 1]:
        value = branch_plan()
        del value["nodes"][index]["on_no_match"]
        with pytest.raises(ValueError):
            contracts().Plan.from_mapping(value)


def test_write_completion_evidence_cannot_be_a_literal_or_accepted_decision():
    for source in [literal("fake-id"), reference("accepted_decision", node_id="route")]:
        value = straight_plan()
        value["goal"]["evidence"][0]["source"] = source
        with pytest.raises(ValueError):
            contracts().Plan.from_mapping(value)


@pytest.mark.parametrize(
    "kind, source",
    [
        ("tool_result", reference("node_output", node_id="read")),
        ("accepted_decision", reference("accepted_decision", node_id="decide")),
        ("verified_memory", reference("verified_memory")),
        ("user_input", reference("task_input")),
    ],
)
def test_goal_evidence_preserves_its_distinct_source_kind(kind, source):
    value = straight_plan()
    value["goal"]["evidence"][0].update(kind=kind, source=source)
    contracts().Plan.from_mapping(value)


def test_verified_memory_bindings_declare_a_stable_slot_not_an_employee_record_id():
    value = branch_plan()
    binding = value["nodes"][0]["inputs"]["history"]
    assert binding["memory_kind"] == "confirmed_preference"
    assert binding["subject"] == "response_language"
    assert "memory_id" not in binding
    contracts().Plan.from_mapping(value)
    binding["memory_id"] = "employee-specific-record"
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_transformed_output_cannot_independently_claim_verified_completion():
    value = straight_plan()
    value["goal"]["evidence"][0]["kind"] = "tool_result"
    value["goal"]["evidence"][0]["source"]["provenance"] = "transform_result"
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_flat_plans_are_bounded_to_32_expanded_nodes():
    api = contracts()
    value = straight_plan()
    value["nodes"] = [
        node(f"done-{index}", "complete", evidence=["created"]) for index in range(32)
    ]
    api.Plan.from_mapping(value)
    value["nodes"].append(node("overflow", "complete", evidence=["created"]))
    with pytest.raises(ValueError):
        api.Plan.from_mapping(value)


def test_nested_branch_conditions_are_bounded_to_depth_four():
    api = contracts()
    value = branch_plan()
    rule = condition()
    for _ in range(3):
        rule = {"op": "all", "conditions": [rule]}
    value["nodes"][1]["cases"][0]["condition"] = rule
    api.Plan.from_mapping(value)
    value["nodes"][1]["cases"][0]["condition"] = {"op": "any", "conditions": [rule]}
    with pytest.raises(ValueError, match="depth"):
        api.Plan.from_mapping(value)


def test_plan_json_byte_size_is_bounded_and_deep_data_is_rejected():
    api = contracts()
    value = straight_plan()
    value["nodes"][0]["inputs"]["query"] = literal("x" * api.MAX_PLAN_BYTES)
    with pytest.raises(ValueError, match="size"):
        api.Plan.from_mapping(value)
    nested = []
    for _ in range(40):
        nested = [nested]
    value["nodes"][0]["inputs"]["query"] = literal(nested, "array")
    with pytest.raises(ValueError, match="depth"):
        api.Plan.from_mapping(value)


def test_cyclic_python_data_is_rejected_as_non_json():
    value = straight_plan()
    recursive = []
    recursive.append(recursive)
    value["nodes"][0]["inputs"]["query"] = literal(recursive, "array")
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_mapping_input_must_be_a_json_object():
    for value in [None, [], "{}", 1]:
        with pytest.raises(ValueError):
            contracts().Plan.from_mapping(value)


def test_digest_is_order_independent_but_preserves_array_order():
    api = contracts()
    value = straight_plan()
    reverse_keys = dict(reversed(list(copy.deepcopy(value).items())))
    assert api.Plan.from_mapping(value).digest == api.Plan.from_mapping(reverse_keys).digest
    reverse_keys["nodes"].reverse()
    assert api.Plan.from_mapping(value).digest != api.Plan.from_mapping(reverse_keys).digest


@pytest.mark.parametrize("op", [[], {}, 1, None])
def test_malformed_condition_operator_types_fail_with_a_contract_error(op):
    value = branch_plan()
    value["nodes"][1]["cases"][0]["condition"]["op"] = op
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("source", ["literal", "task_input", "node_output"])
def test_null_only_declarations_must_allow_null(source):
    value = straight_plan()
    binding = (
        literal(None, "null")
        if source == "literal"
        else reference(
            source, value_type="null", node_id="lookup" if source == "node_output" else None
        )
    )
    value["nodes"][0]["inputs"]["query"] = binding
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


@pytest.mark.parametrize("field", ["plan_id", "entry", "registry_fingerprint"])
def test_identifiers_and_fingerprints_do_not_accept_trailing_newlines(field):
    value = straight_plan()
    value[field] += "\n"
    with pytest.raises(ValueError):
        contracts().Plan.from_mapping(value)


def test_standalone_schema_rejects_incompatible_equality_declarations():
    value = branch_plan()
    value["nodes"][1]["cases"][0]["condition"] = {
        "op": "eq",
        "left": literal(1, "integer"),
        "right": literal("1"),
    }
    assert not Draft202012Validator(contracts().plan_schema()).is_valid(value)


def test_derived_digest_counts_toward_the_serialized_plan_size_limit():
    api = contracts()
    value = straight_plan()
    value["nodes"][0]["inputs"]["query"] = literal("")
    overhead = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    value["nodes"][0]["inputs"]["query"]["value"] = "x" * (api.MAX_PLAN_BYTES - overhead)
    source = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert len(source.encode("utf-8")) == api.MAX_PLAN_BYTES
    with pytest.raises(ValueError, match="size"):
        api.Plan(source)
    with pytest.raises(ValueError, match="size"):
        api.Plan.from_mapping(value)


def test_serialized_plan_at_the_byte_limit_round_trips():
    api = contracts()
    value = api.Plan.from_mapping(straight_plan()).to_mapping()
    value["nodes"][0]["inputs"]["query"] = literal("")
    overhead = len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    value["nodes"][0]["inputs"]["query"]["value"] = "x" * (api.MAX_PLAN_BYTES - overhead)
    del value["content_digest"]
    plan = api.Plan.from_mapping(value)
    assert (
        len(json.dumps(plan.to_mapping(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
        == api.MAX_PLAN_BYTES
    )
    assert api.Plan.from_mapping(plan.to_mapping()).digest == plan.digest

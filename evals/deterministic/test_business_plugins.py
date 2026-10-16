"""The installed plugin contract is the only capability source."""

import copy

import pytest


def manifest():
    return {
        "id": "test-plugin",
        "version": "1.0.0",
        "level": "basic",
        "entrypoint": {"module": "decagent.business.tickets.cli"},
        "commands": {
            "create": {
                "description": "Create a record",
                "effect": "write",
                "confirmation": "auto",
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "details": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string", "enum": ["a", "b"]},
                                },
                                "active": {"type": "boolean"},
                            },
                            "required": ["tags"],
                        }
                    },
                    "required": ["details"],
                },
                "result_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
    }


def test_nested_schema_is_shared_with_tools_and_rejects_unknown_arguments():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    spec = PluginSpec.from_mapping(source)
    spec.validate("create", {"details": {"tags": ["a"], "active": True}})
    assert (
        spec.tool_schemas()[0]["function"]["parameters"]
        == source["commands"]["create"]["input_schema"]
    )
    for params in (
        {"details": {"tags": ["c"]}},
        {"details": {"tags": [], "extra": True}},
        {"details": {"tags": []}, "unknown": 1},
    ):
        with pytest.raises(ValueError):
            spec.validate("create", params)
    with pytest.raises(ValueError, match="command"):
        spec.validate("shell", {})
    frozen = spec.digest
    source["version"] = "2.0.0"
    assert spec.version == "1.0.0" and spec.digest == frozen


@pytest.mark.parametrize("remove", ["version", "entrypoint", "commands"])
def test_incomplete_manifest_rejected(remove):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    del source[remove]
    with pytest.raises(ValueError):
        PluginSpec.from_mapping(source)


def test_enhanced_requires_versioned_workflow_and_closed_input_objects():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["level"] = "enhanced"
    with pytest.raises(ValueError, match="workflow"):
        PluginSpec.from_mapping(source)
    source = manifest()
    del source["commands"]["create"]["input_schema"]["properties"]["details"][
        "additionalProperties"
    ]
    with pytest.raises(ValueError, match="additionalProperties"):
        PluginSpec.from_mapping(source)


def test_changed_contract_has_different_digest():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    changed = copy.deepcopy(source)
    changed["commands"]["create"]["confirmation"] = "confirm"
    assert PluginSpec.from_mapping(source).digest != PluginSpec.from_mapping(changed).digest


@pytest.mark.parametrize("schema", [None, "not a schema", []])
def test_invalid_input_schema_is_a_contract_error(schema):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"] = schema
    with pytest.raises(ValueError):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize("field", ["level", "effect", "confirmation"])
def test_non_string_contract_tags_are_rejected(field):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    if field == "level":
        source[field] = []
    else:
        source["commands"]["create"][field] = []
    with pytest.raises(ValueError):
        PluginSpec.from_mapping(source)


def test_union_object_input_must_be_closed():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = {
        "type": ["object", "null"],
        "properties": {"tag": {"type": "string"}},
    }
    with pytest.raises(ValueError, match="additionalProperties"):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize(
    "nested",
    [
        {"properties": {"label": {"type": "string"}}},
        {"patternProperties": {"^label$": {"type": "string"}}},
        {"anyOf": [{"properties": {"label": {"type": "string"}}}, {"type": "string"}]},
        {"type": "array", "items": {"properties": {"label": {"type": "string"}}}},
        {
            "type": "object",
            "additionalProperties": False,
            "dependentSchemas": {"label": {"properties": {"related": {"type": "string"}}}},
        },
    ],
)
def test_implicit_object_and_applicator_schemas_require_unknown_field_rejection(nested):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = nested
    with pytest.raises(ValueError, match="additionalProperties"):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize(
    "keyword,ref",
    [
        ("$ref", "https://example.invalid/schema"),
        ("$dynamicRef", "https://example.invalid/schema#target"),
        ("$ref", "#/$defs/absent"),
        ("$dynamicRef", "#/$defs/absent"),
        ("$ref", "#absent_anchor"),
        ("$dynamicRef", "#absent_anchor"),
        ("$ref", "#/description"),
    ],
)
def test_external_or_unresolvable_references_are_registration_errors(keyword, ref):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    schema = source["commands"]["create"]["input_schema"]
    schema["description"] = "Not a schema resource"
    schema["properties"]["details"] = {keyword: ref}
    with pytest.raises(ValueError, match="reference"):
        PluginSpec.from_mapping(source)


def test_valid_local_pointer_and_dynamic_anchor_keep_strict_nested_validation():
    from decagent.business.contracts import PluginSpec

    for reference in ({"$ref": "#/$defs/details"}, {"$dynamicRef": "#details"}):
        source = manifest()
        schema = source["commands"]["create"]["input_schema"]
        details = schema["properties"]["details"]
        details["$dynamicAnchor"] = "details"
        schema["$defs"] = {"details": details}
        schema["properties"]["details"] = reference
        spec = PluginSpec.from_mapping(source)
        spec.validate("create", {"details": {"tags": ["a"]}})
        with pytest.raises(ValueError, match="extra"):
            spec.validate("create", {"details": {"tags": ["a"], "extra": True}})


def test_runtime_reference_error_is_a_specific_value_error():
    from decagent.business.contracts import validate_schema

    with pytest.raises(ValueError, match="reference.*absent"):
        validate_schema({"$dynamicRef": "#absent"}, {})


@pytest.mark.parametrize(
    "nested",
    [
        {
            "properties": {"label": {"type": "string"}},
            "additionalProperties": False,
            "not": {"type": "array"},
        },
        {
            "patternProperties": {"^label$": {"type": "string"}},
            "additionalProperties": False,
            "not": {"type": "array"},
        },
    ],
)
def test_closed_implicit_object_schemas_accept_known_fields_and_reject_unknown(nested):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = nested
    spec = PluginSpec.from_mapping(source)
    spec.validate("create", {"details": {"label": "known"}})
    with pytest.raises(ValueError, match="unknown"):
        spec.validate("create", {"details": {"label": "known", "unknown": True}})


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "states",
        "transitions",
        "branches",
        "questions",
        "parameter_sources",
        "completion",
    ],
)
def test_enhanced_workflow_rejects_invalid_field_shapes(field):
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    source = builtin_manifest()
    source["workflow"][field] = "invalid"
    with pytest.raises(ValueError, match="workflow"):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("states", ["open", "open"]),
        ("transitions", {"shell": {"from": ["open"], "to": "open"}}),
        ("transitions", {"assign": {"from": ["missing"], "to": "assigned"}}),
        ("transitions", {"assign": {"from": "open", "to": "assigned"}}),
        ("transitions", {"assign": {"from": ["open"], "to": "missing"}}),
        ("branches", [{"condition": "", "route": "request_user"}]),
        ("branches", [{"condition": "fact missing", "route": []}]),
        ("parameter_sources", {"invented_argument": "user"}),
        ("parameter_sources", {"title": []}),
        ("completion", {"write": []}),
    ],
)
def test_enhanced_workflow_rejects_invalid_lifecycle_and_provenance(field, value):
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    source = builtin_manifest()
    source["workflow"][field] = value
    with pytest.raises(ValueError, match="workflow"):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize(
    "question",
    [
        {"id": "operation", "type": "choice", "candidates": ["shell"], "no_match": True},
        {"id": "operation", "type": "choice", "candidates": ["create"], "no_match": False},
        {"id": "operation", "type": "choice", "candidates": "create", "no_match": True},
        {"id": "operation", "type": "choice", "candidates": ["create", "create"], "no_match": True},
        {"id": "operation", "type": "choice", "candidates": [], "no_match": True},
        {"id": "explicit", "type": "noul", "proposition": ""},
        {"id": "explicit", "type": "noul", "proposition": []},
        {"id": "impact", "type": "score", "scale": [{"score": 0, "context": "none"}]},
        {
            "id": "impact",
            "type": "score",
            "scale": [{"score": 1, "context": "some"}, {"score": 0, "context": "none"}],
        },
        {
            "id": "impact",
            "type": "score",
            "scale": [{"score": 0, "context": "none"}, {"score": 1, "context": ""}],
        },
        {
            "id": "impact",
            "type": "score",
            "scale": [{"score": 0, "context": "same"}, {"score": 1, "context": "same"}],
        },
        {
            "id": "impact",
            "type": "score",
            "scale": [{"score": True, "context": "none"}, {"score": 1, "context": "some"}],
        },
        {
            "id": "impact",
            "type": "score",
            "scale": [{"score": i, "context": str(i)} for i in range(11)],
        },
    ],
)
def test_enhanced_workflow_questions_are_consumable_typed_contracts(question):
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    source = builtin_manifest()
    source["workflow"]["questions"] = [question]
    with pytest.raises(ValueError, match="workflow"):
        PluginSpec.from_mapping(source)


def test_enhanced_workflow_question_ids_are_unique():
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    source = builtin_manifest()
    source["workflow"]["questions"].append(copy.deepcopy(source["workflow"]["questions"][0]))
    with pytest.raises(ValueError, match="workflow"):
        PluginSpec.from_mapping(source)


def test_builtin_workflow_remains_unchanged_and_basic_needs_no_workflow():
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    source = builtin_manifest()
    questions = copy.deepcopy(source["workflow"]["questions"])
    spec = PluginSpec.from_mapping(source)
    assert spec.manifest["workflow"]["questions"] == questions
    assert PluginSpec.from_mapping(manifest()).level == "basic"


def test_read_only_enhanced_plugin_without_lifecycle_is_admitted_and_consumable():
    from decagent.business.contracts import PluginSpec
    from decagent.business.questions import build_business_request, legal_commands

    source = manifest()
    command = source["commands"].pop("create")
    command["effect"] = "read"
    command["input_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    source["commands"]["lookup"] = command
    source["level"] = "enhanced"
    source["skill"] = "test-plugin:SKILL.md"
    source["workflow"] = {
        "version": "1.0.0",
        "branches": [{"condition": "Explicit lookup request", "route": "validate_and_execute"}],
        "questions": [
            {"id": "operation", "type": "choice", "candidates": ["lookup"], "no_match": True}
        ],
        "parameter_sources": {"query": "User supplied search text"},
        "completion": {"read": "Validated query result"},
    }
    spec = PluginSpec.from_mapping(source)
    assert legal_commands(spec) == ("lookup",)
    request = build_business_request(spec, "task", "scope", "Lookup VPN", {})
    assert request.questions[0].id == "operation"
    spec.validate("lookup", {"query": "VPN"})
    # An explicitly empty transition catalog is also a valid stateless enhancement.
    source["workflow"]["transitions"] = {}
    assert PluginSpec.from_mapping(source).manifest["workflow"]["transitions"] == {}


@pytest.mark.parametrize("nested", [{}, True])
def test_unconstrained_nested_object_input_is_rejected_at_registration(nested):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = nested
    with pytest.raises(ValueError, match="object|additionalProperties"):
        PluginSpec.from_mapping(source)


def test_unsupported_recursive_ref_is_rejected_for_draft_202012():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    schema = source["commands"]["create"]["input_schema"]
    schema["$defs"] = {"closed": schema["properties"]["details"]}
    schema["properties"]["details"] = {"$recursiveRef": "#/$defs/closed"}
    with pytest.raises(ValueError, match="recursiveRef"):
        PluginSpec.from_mapping(source)


@pytest.mark.parametrize(
    "nested,good,bad",
    [
        ({"type": "string", "allOf": [{"minLength": 2}]}, "ok", "x"),
        ({"allOf": [{"type": "string"}, {"minLength": 2}]}, "ok", "x"),
        ({"anyOf": [{"type": "string"}, {"type": "number"}]}, "ok", {}),
    ],
)
def test_scalar_applicator_schemas_keep_valid_constraints(nested, good, bad):
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = nested
    spec = PluginSpec.from_mapping(source)
    spec.validate("create", {"details": good})
    with pytest.raises(ValueError):
        spec.validate("create", {"details": bad})


def test_local_reference_under_applicator_subresource_keeps_its_base_uri():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    source["commands"]["create"]["input_schema"]["properties"]["details"] = {
        "allOf": [
            {
                "$id": "urn:decagent:test-details",
                "$defs": {"text": {"type": "string", "minLength": 2}},
                "$ref": "#/$defs/text",
            }
        ]
    }
    spec = PluginSpec.from_mapping(source)
    spec.validate("create", {"details": "ok"})
    with pytest.raises(ValueError):
        spec.validate("create", {"details": {"unknown": True}})


def test_implicit_array_path_cannot_accept_undeclared_nested_object_fields():
    from decagent.business.contracts import PluginSpec

    source = manifest()
    # Excluding a direct object still admits arrays, including [{"unknown": true}].
    source["commands"]["create"]["input_schema"]["properties"]["details"] = {
        "not": {"type": "object"}
    }
    with pytest.raises(ValueError, match="array|object"):
        PluginSpec.from_mapping(source)

"""Resolve typed data references and registered pure transforms at the last moment."""

import json
from dataclasses import dataclass
from functools import lru_cache

from jsonschema import Draft202012Validator

from decagent.business.contracts import validate_schema
from decagent.business.mcp_contract import FORBIDDEN_INPUT_KEYS
from decagent.business.memory_slots import memory_slot_key
from decagent.business.questions import priority_for_score
from decagent.decisions.contracts import canonical_json

from .contracts import plan_schema
from .evaluation_catalog import EvaluationCatalog

ALL_TYPES = ["string", "integer", "number", "boolean", "object", "array", "null"]
ANY_VALUE = {"type": ALL_TYPES}
_MISSING = object()


class BindingMissing(ValueError):
    """The declared value has not been produced and cannot be invented."""


class BindingInvalid(ValueError):
    """A reference is not current, verified or type-compatible."""


@dataclass
class BindingContext:
    task_input: dict
    node_outputs: dict
    accepted_decisions: dict
    memories: dict
    employee_id: str
    scope_revision: int
    account_revision: int | None = None
    workflow_id: str | None = None

    def __post_init__(self):
        if self.workflow_id is not None and (
            not isinstance(self.workflow_id, str)
            or not self.workflow_id.strip()
            or len(self.workflow_id) > 64
        ):
            raise BindingInvalid("Registered workflow identity is invalid")
        if not self.employee_id or type(self.scope_revision) is not int or self.scope_revision < 1:
            raise BindingInvalid("Binding context requires current employee and scope")
        if self.account_revision is not None and (
            type(self.account_revision) is not int or self.account_revision < 1
        ):
            raise BindingInvalid("Current employee account revision is invalid")
        if any(
            type(value) is not dict
            for value in (
                self.task_input,
                self.node_outputs,
                self.accepted_decisions,
                self.memories,
            )
        ):
            raise BindingInvalid("Binding sources must be structured records")


@dataclass(frozen=True)
class ResolvedBinding:
    value: object
    provenance: dict


@lru_cache(maxsize=2)
def _validator(name):
    schema = plan_schema()
    return Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": schema["$defs"]})


def _validate(name, value):
    error = next(_validator(name).iter_errors(value), None)
    if error is not None:
        raise BindingInvalid("Invalid typed " + name + " structure") from None


def _copy(value):
    try:
        return json.loads(canonical_json(value))
    except (ValueError, TypeError, OverflowError) as exc:
        raise BindingInvalid("Bound value must be finite JSON data") from exc


def _lookup(value, path):
    for part in path:
        if type(part) is str and type(value) is dict:
            value = value.get(part, _MISSING)
        elif type(part) is int and type(value) is list and 0 <= part < len(value):
            value = value[part]
        else:
            return _MISSING
        if value is _MISSING:
            return value
    return value


def _current(record, context):
    if (
        type(record) is not dict
        or record.get("employee_id") != context.employee_id
        or record.get("scope_revision") != context.scope_revision
        or (
            context.account_revision is not None
            and record.get("account_revision") != context.account_revision
        )
        or record.get("stale", False) is not False
    ):
        raise BindingInvalid("Source is not current for this employee and scope")


def _matches(value, kind):
    if kind == "number":
        return type(value) in {int, float}
    return (
        type(value)
        is {
            "string": str,
            "integer": int,
            "boolean": bool,
            "object": dict,
            "array": list,
            "null": type(None),
        }[kind]
    )


def resolve_binding(binding, context):
    _validate("binding", binding)
    source = binding["source"]
    provenance = {"source": source, "provenance": binding["provenance"]}
    if source == "literal":
        value = binding["value"]
    elif source == "task_input":
        value = _lookup(context.task_input, binding["path"])
    else:
        key = (
            memory_slot_key(binding["memory_kind"], binding["subject"])
            if source == "verified_memory"
            else binding["node_id"]
        )
        collection = {
            "node_output": context.node_outputs,
            "accepted_decision": context.accepted_decisions,
            "verified_memory": context.memories,
        }[source]
        record = collection.get(key)
        if record is None:
            value = _MISSING
        else:
            _current(record, context)
            if source == "accepted_decision":
                if record.get("accepted") is not True:
                    raise BindingInvalid("Decision has not been accepted")
                data = record.get("value", _MISSING)
            else:
                if record.get("verified") is not True:
                    raise BindingInvalid("Source is not verified")
                if source == "verified_memory":
                    if (
                        record.get("active") is not True
                        or record.get("expired") is not False
                        or record.get("kind") != binding["memory_kind"]
                        or record.get("subject") != binding["subject"]
                    ):
                        raise BindingInvalid("Memory slot is mismatched, inactive or expired")
                else:
                    kind = record.get("kind")
                    allowed = {
                        "tool_result": {"read_tool", "governed_write"},
                        "write_receipt": {"governed_write"},
                        "transform_result": {"transform"},
                        "user_answer": {"ask_user"},
                    }.get(binding["provenance"], set())
                    if kind not in allowed:
                        raise BindingInvalid("Producer provenance is not verified")
                    if binding["provenance"] == "write_receipt" and (
                        not binding["path"] or binding["path"][0] != "receipt"
                    ):
                        raise BindingInvalid("Receipt must reference actual receipt data")
                data = record.get("data", _MISSING)
            value = _lookup(data, binding["path"])
            provenance.update(
                {
                    "employee_id": context.employee_id,
                    "scope_revision": context.scope_revision,
                    **(
                        {"account_revision": context.account_revision}
                        if context.account_revision is not None
                        else {}
                    ),
                    **{
                        name: record[name] for name in ("revision", "evidence_id") if name in record
                    },
                    **(
                        {"memory_id": record["id"]}
                        if source == "verified_memory" and record.get("id")
                        else {}
                    ),
                    **(
                        {"content_digest": record["content_digest"]}
                        if source == "verified_memory" and record.get("content_digest")
                        else {}
                    ),
                }
            )
        provenance["node_id" if source != "verified_memory" else "memory_slot"] = key
    provenance["path"] = list(binding.get("path", []))
    if value is _MISSING:
        if binding["nullable"]:
            value = None
        else:
            raise BindingMissing("Required value is missing")
    if value is None and binding["nullable"]:
        return ResolvedBinding(None, provenance)
    if not _matches(value, binding["value_type"]):
        raise BindingInvalid("Bound value type differs from declaration")
    return ResolvedBinding(_copy(value), provenance)


def resolve_inputs(node, context, spec=None):
    values, trace = {}, {}
    for name, binding in node.get("inputs", {}).items():
        key = "".join(char for char in name.lower() if char.isalnum())
        if key in FORBIDDEN_INPUT_KEYS and type(spec) is not EvaluationCatalog:
            raise BindingInvalid("Program-owned identity cannot be supplied by a plan")
        resolved = resolve_binding(binding, context)
        values[name], trace[name] = resolved.value, resolved.provenance
    if node["type"] in {"read_tool", "governed_write"}:
        ref = node["tool" if node["type"] == "read_tool" else "command"]
        if spec is None or ref["plugin_id"] != spec.id:
            raise BindingInvalid("Tool does not belong to the registered business scope")
        try:
            spec.validate(ref["command"], values)
        except (KeyError, ValueError) as exc:
            raise BindingInvalid("Resolved tool inputs failed the registered MCP schema") from exc
    return values, trace


def _coalesce(values):
    value = values["left"] if values["left"] is not None else values["right"]
    if value is None:
        raise BindingMissing("Both branch values are missing")
    return value


def _normalize(values):
    value = values["title"].strip()
    if not value:
        raise BindingMissing("Title is missing")
    return value


TRANSFORM_CONTRACTS = {
    "identity": {
        "version": "1",
        "input_schema": {
            "type": "object",
            "properties": {"value": ANY_VALUE},
            "required": ["value"],
            "additionalProperties": False,
        },
        "output_schema": ANY_VALUE,
    },
    "coalesce": {
        "version": "1",
        "input_schema": {
            "type": "object",
            "properties": {"left": ANY_VALUE, "right": ANY_VALUE},
            "required": ["left", "right"],
            "additionalProperties": False,
        },
        "output_schema": ANY_VALUE,
    },
    "normalize_title": {
        "version": "1",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
        "output_schema": {"type": "string", "minLength": 1},
    },
    "priority_for_score": {
        "version": "1",
        "input_schema": {
            "type": "object",
            "properties": {"score": {"type": "number", "minimum": 0, "maximum": 2}},
            "required": ["score"],
            "additionalProperties": False,
        },
        "output_schema": {"type": "string", "enum": ["normal", "high", "urgent"]},
    },
}
_TRANSFORM_FUNCTIONS = {
    "identity": lambda values: values["value"],
    "coalesce": _coalesce,
    "normalize_title": _normalize,
    "priority_for_score": lambda values: priority_for_score(values["score"]),
}


def run_transform(name, version, inputs):
    definition = TRANSFORM_CONTRACTS.get(name)
    if definition is None or version != definition["version"]:
        raise BindingInvalid("Unregistered pure transform")
    try:
        validate_schema(definition["input_schema"], inputs)
    except ValueError as exc:
        raise BindingInvalid("Pure transform input failed its registered schema") from exc
    value = _TRANSFORM_FUNCTIONS[name](_copy(inputs))
    validate_schema(definition["output_schema"], value)
    return _copy(value)


def _equal(left, right):
    if type(left) in {int, float} and type(right) in {int, float}:
        return left == right
    return type(left) is type(right) and canonical_json(left) == canonical_json(right)


def _condition(condition, context):
    operation = condition["op"]
    if operation in {"all", "any"}:
        values = (_condition(child, context) for child in condition["conditions"])
        return all(values) if operation == "all" else any(values)
    if operation == "exists":
        try:
            return resolve_binding(condition["value"], context).value is not None
        except BindingMissing:
            return False
    if operation == "in":
        left = resolve_binding(condition["value"], context).value
        collection = resolve_binding(condition["collection"], context).value
        if type(collection) is not list:
            raise BindingInvalid("Membership requires an actual array")
        return any(_equal(left, right) for right in collection)
    left = resolve_binding(condition["left"], context).value
    right = resolve_binding(condition["right"], context).value
    if operation == "eq":
        return _equal(left, right)
    if type(left) not in {int, float} or type(right) not in {int, float}:
        raise BindingInvalid("Numeric condition requires numeric values")
    return {
        "lt": lambda: left < right,
        "lte": lambda: left <= right,
        "gt": lambda: left > right,
        "gte": lambda: left >= right,
    }[operation]()


def evaluate_condition(condition, context):
    _validate("condition", condition)
    return _condition(condition, context)

"""Versioned, immutable JSON plans interpreted only by trusted static code.

This layer validates contract shape, finite JSON size and declared value types.
The compiler owns graph topology/depth, capability registration, branch-safe
dependencies and provenance claims. Runtime resolves references and preserves
existing MCP authorization; a plan never carries actor, grants or credentials.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

SCHEMA_VERSION = "1"
MAX_NODES = 32
MAX_BRANCH_DEPTH = 4
MAX_REVISION = 4
MAX_PLAN_BYTES = 128_000
MAX_JSON_DEPTH = 32
MAX_CONDITION_DEPTH = 4


def plan_schema() -> dict[str, Any]:
    """Return an independent Draft 2020-12 schema snapshot, without external refs."""
    return json.loads(files(__package__).joinpath("plan-schema.json").read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _json_check(value: Any, depth: int = 0, active: frozenset[int] = frozenset()) -> None:
    """Reject Python coercions, cycles, non-finite numbers and unbounded nesting."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Plan JSON depth exceeds the bounded input depth")
    kind = type(value)
    if kind in {dict, list}:
        if id(value) in active:
            raise ValueError("Plan requires acyclic serializable JSON data")
        active = active | {id(value)}
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("Plan JSON objects require string keys")
            children = value.values()
        else:
            children = value
        for child in children:
            _json_check(child, depth + 1, active)
    elif kind is float:
        if not math.isfinite(value):
            raise ValueError("Plan JSON numbers must be finite")
    elif kind not in {str, int, bool, type(None)}:
        raise ValueError("Plan requires serializable JSON data only")


def _condition_check(condition: Any, depth: int = 1) -> None:
    """Bound logical nesting and reject comparisons of incompatible declarations."""
    if depth > MAX_CONDITION_DEPTH:
        raise ValueError("Condition depth exceeds the bounded branch condition depth")
    if not isinstance(condition, dict):
        return  # Schema validation reports malformed shapes.
    operation = condition.get("op")
    if not isinstance(operation, str):
        return
    if operation in {"all", "any"}:
        children = condition.get("conditions")
        if isinstance(children, list):
            for child in children:
                _condition_check(child, depth + 1)
    if operation == "eq":
        left, right = condition.get("left"), condition.get("right")
        if not isinstance(left, dict) or not isinstance(right, dict):
            return
        left_type, right_type = left.get("value_type"), right.get("value_type")
        if (
            isinstance(left_type, str)
            and isinstance(right_type, str)
            and left_type != right_type
            and {left_type, right_type} != {"integer", "number"}
        ):
            raise ValueError("Equality condition requires compatible declared value types")


def _conditions_check(value: dict[str, Any]) -> None:
    nodes = value.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("type") == "branch":
                cases = node.get("cases")
                if isinstance(cases, list):
                    for case in cases:
                        if isinstance(case, dict) and "condition" in case:
                            _condition_check(case["condition"])
    goal = value.get("goal")
    if isinstance(goal, dict) and isinstance(goal.get("evidence"), list):
        for evidence in goal["evidence"]:
            if isinstance(evidence, dict) and "condition" in evidence:
                _condition_check(evidence["condition"])


def _validated_source(value: Any) -> str:
    if type(value) is not dict:
        raise ValueError("Plan must be a JSON object")
    _json_check(value)
    try:
        source = _canonical(value)
        size = len(source.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise ValueError("Plan requires serializable JSON data only") from exc
    if size > MAX_PLAN_BYTES:
        raise ValueError("Plan JSON size exceeds the bounded input size")
    _conditions_check(value)
    try:
        Draft202012Validator(plan_schema()).validate(value)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"{path}: {exc.message[:256]}") from exc
    content = {key: item for key, item in value.items() if key != "content_digest"}
    digest = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    if "content_digest" in value and value["content_digest"] != digest:
        raise ValueError("content_digest does not match the canonical plan content")
    normalized = _canonical({**content, "content_digest": digest})
    if len(normalized.encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("Plan JSON size exceeds the bounded input size")
    return normalized


@dataclass(frozen=True)
class Plan:
    """A validated immutable snapshot with copy-returning JSON views.

    The digest covers every field except content_digest itself. A supplied digest
    must match; omitting it derives the value. Node outputs and accepted decisions
    remain symbolic references until execution, even when their IDs are not yet
    resolvable. Constructing this wrapper does not compile or execute the plan.
    """

    _source: str

    def __post_init__(self) -> None:
        try:
            value = json.loads(self._source)
        except (TypeError, ValueError) as exc:
            raise ValueError("Plan source must be serialized JSON") from exc
        object.__setattr__(self, "_source", _validated_source(value))

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> "Plan":
        """Validate and snapshot a JSON object; retain no caller-owned containers."""
        return cls(_validated_source(mapping))

    def to_mapping(self) -> dict[str, Any]:
        """Return a fresh JSON object including the verified derived digest."""
        return json.loads(self._source)

    @property
    def digest(self) -> str:
        return self.to_mapping()["content_digest"]

    @property
    def schema_version(self) -> str:
        return self.to_mapping()["schema_version"]

    @property
    def plan_id(self) -> str:
        return self.to_mapping()["plan_id"]

    @property
    def revision(self) -> int:
        return self.to_mapping()["revision"]

    @property
    def registry_fingerprint(self) -> str:
        return self.to_mapping()["registry_fingerprint"]

    @property
    def entry(self) -> str:
        return self.to_mapping()["entry"]

    @property
    def goal(self) -> dict[str, Any]:
        return self.to_mapping()["goal"]

    @property
    def nodes(self) -> list[dict[str, Any]]:
        return self.to_mapping()["nodes"]

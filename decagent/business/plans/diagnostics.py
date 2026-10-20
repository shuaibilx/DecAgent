"""Translate known plan validation failures without serializing business responses."""

from jsonschema.exceptions import ValidationError

from decagent.models.validation_feedback import (
    COMPILE_CODES,
    PLAN_FIELDS,
    SCHEMA_CODES,
    ValidationIssue,
    safe_path,
)

from .compiler import PlanCompileError


def plan_validation_issues(error, candidate=None):
    if type(error) is PlanCompileError and error.code in COMPILE_CODES:
        path = ()
        if type(candidate) is dict and type(candidate.get("nodes")) is list:
            for index, node in enumerate(candidate["nodes"][:32]):
                if type(node) is dict and node.get("id") == error.node_id:
                    path = ("nodes", index)
                    break
        return (ValidationIssue(error.code, path),)
    current = error
    for _ in range(4):
        if isinstance(current, ValidationError):
            code = "schema_" + str(current.validator)
            if code not in SCHEMA_CODES:
                break
            path = safe_path(current.absolute_path)
            if current.validator == "required" and type(current.instance) is dict:
                missing = [name for name in current.validator_value
                           if type(name) is str and name in PLAN_FIELDS
                           and name not in current.instance]
                if missing:
                    return tuple(ValidationIssue(code, safe_path((*path, name)))
                                 for name in missing[:8])
            return (ValidationIssue(code, path),)
        current = getattr(current, "__cause__", None)
        if current is None:
            break
    return (ValidationIssue("invalid_plan"),)

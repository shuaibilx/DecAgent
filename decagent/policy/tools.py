"""Small, strictly typed contracts; unknown fields and coercions are rejected."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class Field:
    kind: type
    default: Any = None
    required: bool = False
    minimum: int = 0
    maximum: int = 1_048_576


SCHEMAS: Mapping[str, Mapping[str, Field]] = {
    "read_file": {
        "path": Field(str, required=True, minimum=1, maximum=4096),
        "max_bytes": Field(int, 65536, minimum=1, maximum=1_048_576),
    },
    "search": {
        "query": Field(str, required=True, minimum=1, maximum=4096),
        "paths": Field(tuple, (".",), minimum=1, maximum=100),
        "max_results": Field(int, 100, minimum=1, maximum=1000),
    },
    "write_file": {
        "path": Field(str, required=True, minimum=1, maximum=4096),
        "content": Field(str, required=True),
    },
    "edit_file": {
        "path": Field(str, required=True, minimum=1, maximum=4096),
        "old": Field(str, required=True, minimum=1),
        "new": Field(str, required=True),
        "expected_matches": Field(int, 1, minimum=1, maximum=1000),
    },
    "diff": {"paths": Field(tuple, (".",), minimum=1, maximum=100)},
    "run_command": {
        "purpose": Field(str, "command", minimum=1, maximum=200),
        "argv": Field(tuple, required=True, minimum=1, maximum=100),
        "cwd": Field(str, ".", minimum=1, maximum=4096),
        "timeout_seconds": Field(int, 120, minimum=1, maximum=600),
        "max_output_bytes": Field(int, 65536, minimum=1, maximum=1_048_576),
    },
    "request_user": {
        "question": Field(str, required=True, minimum=1, maximum=8192),
    },
    "publish_draft": {
        "title": Field(str, required=True, minimum=1, maximum=256),
        "body": Field(str, required=True, maximum=65536),
        "branch": Field(str, "", maximum=256),
    },
}

READ_TOOLS = frozenset({"read_file", "search", "diff", "request_user"})


def validate_tool(tool: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return detached immutable parameters; never accept extra model arguments."""
    if type(tool) is not str or tool not in SCHEMAS:
        raise ValueError("Unsupported tool")
    if not isinstance(params, Mapping) or any(type(key) is not str for key in params):
        raise ValueError("Tool parameters must be an object with string keys")
    schema = SCHEMAS[tool]
    if set(params) - set(schema):
        raise ValueError("Unknown tool parameter")
    normalized: dict[str, Any] = {}
    for name, rule in schema.items():
        if name not in params and rule.required:
            raise ValueError(f"Missing required parameter: {name}")
        value = params.get(name, rule.default)
        if rule.kind is tuple:
            if type(value) not in (list, tuple):
                raise ValueError(f"{name} must be an array")
            item_limit = 16384 if tool == "run_command" and name == "argv" else 4096
            if any(type(item) is not str or not item or len(item) > item_limit for item in value):
                raise ValueError(f"{name} must contain bounded nonempty strings")
            value = tuple(value)
            size = len(value)
        elif type(value) is not rule.kind:
            raise ValueError(f"Incorrect parameter type: {name}")
        else:
            size = len(value) if rule.kind is str else value
        if not rule.minimum <= size <= rule.maximum:
            raise ValueError(f"Parameter outside limits: {name}")
        if rule.kind is str and name not in {"content", "old", "new", "body"}:
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"Control characters in parameter: {name}")
        normalized[name] = value
    branch = normalized.get("branch")
    if tool == "publish_draft" and branch and not branch.startswith("decagent/"):
        raise ValueError("Publishing requires a DecAgent task branch")
    return MappingProxyType(normalized)

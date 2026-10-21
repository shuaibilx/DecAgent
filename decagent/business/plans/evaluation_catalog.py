"""Closed-lifetime catalog for tools owned by an isolated benchmark runner.

This capability is intentionally separate from PluginSpec and PluginRegistry.
It cannot be enabled by HTTP input, a serialized manifest, or a production
service configuration. The evaluator creates one active scope around one
fresh upstream environment and closes it with that environment.
"""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from jsonschema import Draft202012Validator

from decagent.business.contracts import canonical, validate_schema
from decagent.decisions.contracts import digest

OPEN_RESULT_PATH = "x-decagent-open-result-path"
_ANY_JSON = {"type": ["string", "integer", "number", "boolean", "object", "array", "null"]}
_NEVER_IN_AGENT_INPUT = frozenset(
    {"endpoint", "endpoint_ref", "url", "base_url", "headers", "authorization", "token"}
)
_UNSET = object()
# The pinned CSM image's `find_user` handler was audited in its bytecode and
# executes a parameterized SELECT only. This exact MCP tool schema digest keeps
# the read classification scoped to that image and schema version.
_REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS = {
    (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm"
        "@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189",
        "find_user",
    ): "7515a3367bce8c157fa4c8738ea55b24e6983c0fd9787415abeaad8a252242d9",
    (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm"
        "@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189",
        "find_product",
    ): "fcef63ecbf449b3a7507fb660d6b548b207774b06c9e65e6d52b6d584f189a4f",
    (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-itsm"
        "@sha256:a234ae3fb7cee196ba25e6b9957969dea829919b6e8271dddae128f065aaf39f",
        "list_users",
    ): "af0f958626854fbcf94636b7a41ee809b901f90de7a5beed2ffeaee35d9e5be7",
}


def _reviewed_enterprise_read_tool(
    benchmark,
    benchmark_version,
    name,
    input_schema,
    output_schema,
    annotations,
    source_digest,
):
    """Apply narrow read-only overrides to immutable, audited tool contracts."""
    if (
        benchmark != "enterpriseops-gym"
        or name != "retrieve_knowledge"
    ):
        return (
            benchmark == "enterpriseops-gym"
            and "readOnlyHint" not in annotations
            and _REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS.get(
                (benchmark_version, name)
            )
            == source_digest
        )
    if (
        "readOnlyHint" in annotations
        or not isinstance(input_schema, dict)
        or not isinstance(output_schema, dict)
        or output_schema.get("type") != "array"
    ):
        return False
    input_fields = input_schema.get("properties", {})
    state = input_fields.get("state", {})
    item = output_schema.get("items", {})
    fields = item.get("properties", {}) if isinstance(item, dict) else {}
    output_state = fields.get("state", {})
    return (
        input_fields.get("search_text", {}).get("type") == "string"
        and "published" in state.get("enum", [])
        and input_fields.get("limit", {}).get("type") == "integer"
        and "limit" not in input_schema.get("required", [])
        and input_fields.get("product_id", {}).get("type") == "integer"
        and {"internal", "external"}
        <= set(input_fields.get("visibility", {}).get("enum", []))
        and item.get("type") == "object"
        and fields.get("knowledge_id", {}).get("type") == "integer"
        and fields.get("title", {}).get("type") == "string"
        and "published" in output_state.get("enum", [])
        and fields.get("visibility", {}).get("enum")
        and fields.get("body", {}).get("type") == "string"
    )


class EvaluationScope:
    """One single-use active benchmark task lifetime."""

    def __init__(self, benchmark: str, version: str, case_id: str):
        # τ-Bench telecom task IDs contain the full scenario and can reach 213
        # characters in the frozen source. Keep a bound while preserving IDs.
        for name, value, limit in (
            ("benchmark", benchmark, 160),
            ("version", version, 160),
            ("case_id", case_id, 256),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"Evaluation {name} is required and bounded")
        self.benchmark, self.version, self.case_id = benchmark, version, case_id
        self.scope_id = str(uuid4())
        self._state = "new"

    def __enter__(self):
        if self._state != "new":
            raise RuntimeError("Evaluation scope cannot be reused")
        self._state = "active"
        return self

    def __exit__(self, *_):
        self._state = "closed"

    def assert_active(self):
        if self._state != "active":
            raise ValueError("Evaluation scope is not active")


@dataclass(frozen=True)
class EvaluationCommand:
    """Original upstream tool contract plus a deterministic output view."""

    name: str
    _source: str

    @property
    def _contract(self):
        return json.loads(self._source)

    @property
    def description(self):
        return self._contract["description"]

    @property
    def effect(self):
        return self._contract["effect"]

    @property
    def confirmation(self):
        # Confirmation belongs to the benchmark/user simulator. This field is
        # only present to satisfy the shared interpreter's tool descriptor.
        return "auto"

    @property
    def input_schema(self):
        return self._contract["input_schema"]

    @property
    def result_schema(self):
        return self._contract["result_schema"]

    @property
    def upstream_schema(self):
        return self._contract["upstream_schema"]

    @property
    def upstream_output_schema(self):
        return self._contract["upstream_output_schema"]


class EvaluationCatalog:
    """A read-only projection of a fixed upstream catalog while scope is active."""

    id = "evaluation-tools"
    level = "basic"

    def __init__(self, scope, benchmark_version, result_contract_version, raw_tools):
        if not isinstance(scope, EvaluationScope):
            raise TypeError("Trusted active EvaluationScope required")
        scope.assert_active()
        if not all(
            isinstance(item, str) and item.strip()
            for item in (benchmark_version, result_contract_version)
        ):
            raise ValueError("Benchmark and result contract versions are required")
        if len(raw_tools) > 256:
            raise ValueError("Upstream tool catalog exceeds the bounded size")

        public_tools = []
        commands = {}
        for source in raw_tools:
            original, name, description, input_schema, output_schema, annotations = (
                self._normalize_tool(source)
            )
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or name in commands:
                raise ValueError("Upstream tool names must be unique bounded plan identifiers")
            self._check_local_schema(input_schema)
            if output_schema is not None:
                self._check_local_schema(output_schema)
            effect = (
                "read"
                if annotations.get("readOnlyHint") is True
                or _reviewed_enterprise_read_tool(
                    scope.benchmark,
                    benchmark_version,
                    name,
                    input_schema,
                    output_schema,
                    annotations,
                    digest(original),
                )
                else "write"
            )
            contract = {
                "description": description[:2048],
                "effect": effect,
                "input_schema": input_schema,
                "result_schema": {
                    "type": "object",
                    "properties": {
                        "raw": deepcopy(_ANY_JSON),
                        "value": {OPEN_RESULT_PATH: True},
                        "is_error": {"type": "boolean"},
                    },
                    "required": ["raw", "value", "is_error"],
                    "additionalProperties": False,
                },
                "upstream_schema": original,
                "upstream_output_schema": output_schema,
            }
            commands[name] = EvaluationCommand(name, canonical(contract))
            public_tools.append(original)

        if not commands:
            raise ValueError("An isolated evaluation catalog requires at least one tool")
        self._scope = scope
        self.benchmark = scope.benchmark
        self.benchmark_version = benchmark_version
        self.case_id = scope.case_id
        self.result_contract_version = result_contract_version
        self._commands = commands
        self._raw_tools = canonical(public_tools)
        self._manifest = canonical(
            {
                "id": self.id,
                "version": "1.0.0",
                "level": "basic",
                "benchmark": self.benchmark,
                "benchmark_version": benchmark_version,
                "result_contract_version": result_contract_version,
                "commands": {
                    name: {
                        "description": command.description,
                        "effect": command.effect,
                        "confirmation": command.confirmation,
                        "input_schema": command.input_schema,
                        "result_schema": command.result_schema,
                    }
                    for name, command in commands.items()
                },
            }
        )
        self._digest = digest(
            {
                "benchmark": self.benchmark,
                "benchmark_version": benchmark_version,
                "result_contract_version": result_contract_version,
                "tool_catalog": public_tools,
            }
        )

    @classmethod
    def from_mcp_tools(cls, scope, tools, *, benchmark_version: str, result_contract_version: str):
        """Capture MCP Tool records or the original τ-Bench OpenAI tool records."""
        if not isinstance(tools, (list, tuple)):
            raise TypeError("Original upstream tool list required")
        return cls(scope, benchmark_version, result_contract_version, tools)

    @staticmethod
    def _normalize_tool(source):
        if not isinstance(source, dict):
            raise ValueError("Upstream tool must be a JSON object")
        if "function" in source:
            function = source.get("function")
            if not isinstance(function, dict):
                raise ValueError("OpenAI function tool requires a function object")
            name = function.get("name")
            description = function.get("description") or name
            input_schema = deepcopy(function.get("parameters"))
            output_schema = None
            annotations = {}
            original = deepcopy(source)
        else:
            name = source.get("name")
            description = source.get("description") or name
            input_schema = deepcopy(source.get("inputSchema"))
            output_schema = deepcopy(source.get("outputSchema"))
            annotations = deepcopy(source.get("annotations") or {})
            original = deepcopy(source)
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not isinstance(input_schema, dict)
            or input_schema.get("type") != "object"
            or not isinstance(annotations, dict)
        ):
            raise ValueError("Upstream tool name, description and object schema are required")
        if name in _NEVER_IN_AGENT_INPUT:
            raise ValueError("Upstream tool name conflicts with a protected transport field")
        canonical(original)
        return original, name, description, input_schema, output_schema, annotations

    @staticmethod
    def _check_local_schema(schema):
        if not isinstance(schema, dict):
            raise ValueError("Upstream tool schema must be an object")
        canonical(schema)

        def walk(value):
            if isinstance(value, dict):
                ref = value.get("$ref")
                if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
                    raise ValueError("Upstream schemas may contain local references only")
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
        Draft202012Validator.check_schema(schema)

    @property
    def scope_id(self):
        self.assert_active()
        return self._scope.scope_id

    @property
    def manifest(self):
        self.assert_active()
        return json.loads(self._manifest)

    @property
    def digest(self):
        return self._digest

    @property
    def registry_fingerprint(self):
        from .templates import QuestionRegistry

        return QuestionRegistry.from_spec(self).fingerprint

    @property
    def commands(self):
        self.assert_active()
        return dict(self._commands)

    @property
    def raw_tools(self):
        self.assert_active()
        return json.loads(self._raw_tools)

    def assert_active(self):
        self._scope.assert_active()

    def validate(self, command, params):
        self.assert_active()
        if command not in self._commands or type(params) is not dict:
            raise ValueError("Unknown evaluation tool or invalid arguments")
        canonical(params)
        validate_schema(self._commands[command].input_schema, params)

    def observe(self, command, raw):
        """Preserve the exact transport record and derive only a typed JSON view."""
        self.assert_active()
        if command not in self._commands:
            raise ValueError("Unknown evaluation tool")
        is_error = False
        value = _UNSET
        if hasattr(raw, "content") and hasattr(raw, "is_error"):
            is_error = raw.is_error is True
            raw = {"content": raw.content, "is_error": is_error}
            canonical(raw)
            try:
                value = json.loads(raw["content"])
            except (TypeError, ValueError):
                value = raw["content"]
        else:
            canonical(raw)
        if isinstance(raw, dict) and "content" in raw and value is _UNSET:
            is_error = raw.get("isError") is True
            if raw.get("structuredContent") is not None:
                value = deepcopy(raw["structuredContent"])
            else:
                content = raw["content"]
                text = (
                    [item["text"] for item in content if item.get("type") == "text"]
                    if isinstance(content, list)
                    else []
                )
                if len(text) == 1:
                    try:
                        value = json.loads(text[0])
                    except (TypeError, ValueError):
                        value = text[0]
                elif text:
                    value = text
                else:
                    value = deepcopy(content)
        if value is _UNSET:
            value = deepcopy(raw)
        command_spec = self._commands[command]
        output_schema = command_spec.upstream_output_schema
        if output_schema is not None and not is_error:
            validate_schema(output_schema, value)
        return {"raw": deepcopy(raw), "value": value, "is_error": is_error}

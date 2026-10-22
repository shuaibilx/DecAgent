"""The installed service-ticket MCP contract; no endpoint or identity comes from tools."""

import hashlib
import json
from copy import deepcopy
from importlib.resources import files

READ_COMMANDS = frozenset({"directory", "get", "list", "receipt"})
WRITE_COMMANDS = frozenset(
    {"create", "assign", "start", "close", "reopen", "comment", "resolve", "set_priority"}
)
TRUSTED_CONNECTION = {
    "service_id": "service-tickets",
    "transport": "streamable-http",
    "protocol_version": "2026-07-28",
    "endpoint_ref": "DECAGENT_TICKETS_MCP_URL",
}
COMPOSE_INTERNAL_MCP_ENDPOINT = "http://business-mcp:8001/mcp"
RECOVERY_CONTRACT = {
    "version": "2.0.0",
    "prepare_prefix": "tickets_prepare_",
    "commit_tool": "business_commit_operation",
    "get_operation_tool": "business_get_operation",
    "request_key": "request_id",
    "operation_key": "operation_id",
    "receipt_command": "receipt",
    "resource_command": "get",
    "immutable": True,
}
FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "requester_id",
        "employee_id",
        "actor_id",
        "account_id",
        "owner_id",
        "task_id",
        "role",
        "principal",
        "authorization",
        "auth",
        "headers",
        "token",
        "access_token",
        "credential",
        "credential_ref",
        "session_id",
        "grant",
        "task_grant",
        "approved",
        "approval",
        "endpoint",
        "endpoint_ref",
        "url",
        "service_id",
        "transport",
        "protocol_version",
    }
)
FORBIDDEN_INPUT_KEYS = frozenset(
    "".join(char for char in field.lower() if char.isalnum()) for field in FORBIDDEN_INPUT_FIELDS
)
OPERATION_STATES = (
    "ready",
    "awaiting_approval",
    "running",
    "unknown",
    "succeeded",
    "failed",
    "rejected",
    "expired",
    "cancelled",
)


def verified_skill_text(manifest):
    """Load only the packaged service-ticket Skill and fence its exact content."""
    if manifest.get("skill") != "decagent.business.tickets:SKILL.md":
        raise ValueError("MCP Skill must be the installed service-ticket resource")
    summary = manifest.get("skill_summary")
    digest = manifest.get("skill_digest")
    if not isinstance(summary, str) or not summary or "\n" in summary:
        raise ValueError("MCP Skill summary is required")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("MCP Skill digest is required")
    raw = files("decagent.business.tickets").joinpath("SKILL.md").read_bytes()
    if len(raw) > 65536:
        raise ValueError("MCP Skill exceeds its bounded input size")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("MCP Skill digest changed; register a reviewed contract")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("MCP Skill must be UTF-8") from exc
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError("MCP Skill requires YAML front matter")
    front_matter = text.split("\n---\n", 1)[0]
    if "description: " + summary not in front_matter.split("\n"):
        raise ValueError("MCP Skill summary does not match the packaged description")
    return text


def check_mcp_manifest(manifest):
    """Admit only the fixed service and its twelve domain operations."""
    untrusted = {"endpoint", "url", "base_url", "server_url", "mcp_url", "headers", "auth", "token"}
    if untrusted & manifest.keys():
        raise ValueError("MCP endpoint and authentication belong to trusted connection context")
    if manifest.get("connection") != TRUSTED_CONNECTION or "entrypoint" in manifest:
        raise ValueError("MCP connection requires the fixed trusted service and endpoint reference")
    if manifest["id"] != "service-tickets" or manifest["version"] != "2.0.0":
        raise ValueError("MCP connection requires service-tickets version 2.0.0")
    commands = manifest["commands"]
    if set(commands) != READ_COMMANDS | WRITE_COMMANDS:
        raise ValueError("MCP business capability whitelist must contain exactly twelve commands")
    if manifest.get("recovery") != RECOVERY_CONTRACT:
        raise ValueError("MCP writes require the complete immutable operation recovery contract")
    if manifest.get("receipts") != {"command": "receipt", "key": "operation_id"}:
        raise ValueError("MCP writes require the declared receipt recovery command and key")
    verified_skill_text(manifest)

    def check_input(node):
        if isinstance(node, dict):
            if node.get("patternProperties"):
                raise ValueError("MCP business input forbids pattern-based identity parameters")
            forbidden = {
                field
                for field in node.get("properties", {})
                if "".join(char for char in field.lower() if char.isalnum()) in FORBIDDEN_INPUT_KEYS
            }
            if forbidden:
                raise ValueError(
                    "MCP identity, auth and transport parameters are trusted context: "
                    + ", ".join(sorted(forbidden))
                )
            for child in node.values():
                check_input(child)
        elif isinstance(node, list):
            for child in node:
                check_input(child)

    for name, command in commands.items():
        effect = "read" if name in READ_COMMANDS else "write"
        if command["effect"] != effect:
            raise ValueError(f"MCP capability {name} requires effect={effect}")
        check_input(command["input_schema"])


def _object(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def _identifier():
    return {"type": "string", "minLength": 1, "maxLength": 200}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def _embedded(schema):
    """Keep local references local when a standalone command schema is nested."""
    result = deepcopy(schema)

    def references(node):
        if isinstance(node, dict):
            return (
                "$ref" in node
                or "$dynamicRef" in node
                or any(references(value) for value in node.values())
            )
        return isinstance(node, list) and any(references(value) for value in node)

    if references(result) and "$id" not in result:
        source = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        result["$id"] = "urn:decagent:mcp-schema:" + hashlib.sha256(source.encode()).hexdigest()
    return result


def envelope_schema(data_schema):
    """Describe the same strict {ok,data}/{ok,error} boundary as the shared codec."""
    return {
        "type": "object",
        "oneOf": [
            _object({"ok": {"const": True}, "data": _embedded(data_schema)}),
            _object(
                {
                    "ok": {"const": False},
                    "error": _object(
                        {
                            "code": {"type": "string", "minLength": 1},
                            "message": {"type": "string", "minLength": 1},
                        }
                    ),
                }
            ),
        ],
    }


def operation_schema(spec, command_name=None):
    """A safe immutable-operation view with results from the command result contracts."""
    definitions = {}
    branches = []
    for name, command in spec.commands.items():
        if name not in WRITE_COMMANDS or (command_name is not None and name != command_name):
            continue
        definitions[name + "_input"] = _embedded(command.input_schema)
        definitions[name + "_result"] = _embedded(command.result_schema)
        result_ref = {"$ref": "#/$defs/" + name + "_result"}
        properties = {
            "operation_id": _identifier(),
            "employee_id": {"type": "string", "format": "uuid"},
            "request_id": _identifier(),
            "command": {"const": name},
            "params": {"$ref": "#/$defs/" + name + "_input"},
            "input_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "contract_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "expected_revision": _nullable({"type": "integer", "minimum": 1}),
            "source": {"type": "string", "minLength": 1, "maxLength": 64},
            "state": {"type": "string", "enum": list(OPERATION_STATES)},
            "approval_url": _nullable(
                {"type": "string", "pattern": "^/#operation=[A-Za-z0-9_-]{1,200}$"}
            ),
            "approval_expires_at": _nullable({"type": "string", "format": "date-time"}),
            "approved_at": _nullable({"type": "string", "format": "date-time"}),
            "result": _nullable(result_ref),
            "error_code": _nullable({"type": "string", "pattern": "^[a-z_]{1,64}$"}),
        }
        branch = _object(
            properties, [key for key in properties if key not in {"approved_at", "error_code"}]
        )
        branch["allOf"] = [
            {
                "if": {"properties": {"state": {"const": "succeeded"}}},
                "then": {"properties": {"result": result_ref}},
                "else": {"properties": {"result": {"type": "null"}}},
            },
            {
                "if": {"properties": {"state": {"const": "awaiting_approval"}}},
                "then": {"properties": {"approval_url": {"type": "string"}}},
                "else": {"properties": {"approval_url": {"type": "null"}}},
            },
        ]
        branches.append(branch)
    if not branches:
        raise ValueError("Operation result requires a declared write capability")
    return {"type": "object", "$defs": definitions, "oneOf": branches}


def wire_tools(spec, *, allowed_tools=None):
    """Generate MCP tool dictionaries from the registered business schemas."""
    if spec.connection is None:
        raise ValueError("Historical CLI contract has no MCP wire tools")
    result = []
    for name, command in spec.commands.items():
        if name in READ_COMMANDS:
            tool_name = "tickets_" + name
            arguments = command.input_schema
            output = command.result_schema
            description = command.description
        else:
            tool_name = "tickets_prepare_" + name
            arguments = _object(
                {"request_id": _identifier(), "params": _embedded(command.input_schema)}
            )
            output = operation_schema(spec, name)
            description = "Prepare an immutable operation: " + command.description
        result.append(
            {
                "name": tool_name,
                "description": description,
                "inputSchema": arguments,
                "outputSchema": envelope_schema(output),
            }
        )
    arguments = _object({"operation_id": _identifier()})
    output = envelope_schema(operation_schema(spec))
    for name, description in (
        (
            "business_commit_operation",
            "Commit an existing immutable operation after server authorization and any approval.",
        ),
        (
            "business_get_operation",
            "Read the current state and verified result of an existing immutable operation.",
        ),
    ):
        result.append(
            {
                "name": name,
                "description": description,
                "inputSchema": deepcopy(arguments),
                "outputSchema": deepcopy(output),
            }
        )
    if allowed_tools is None:
        return result
    allowed = set(allowed_tools)
    if allowed - {tool["name"] for tool in result}:
        raise ValueError("Permission filtering contains an unknown MCP tool")
    return [tool for tool in result if tool["name"] in allowed]


def compare_wire_catalog(spec, catalog, *, allowed_tools=None):
    """Accept a permission subset; fence added tools, schemas and executable descriptions.

    Advisory MCP annotations and display metadata never authorize inputs or operations.
    The caller may pass the expected principal's allowed tool names for a stricter fence.
    """
    expected = {tool["name"]: tool for tool in wire_tools(spec, allowed_tools=allowed_tools)}
    if not isinstance(catalog, (list, tuple)):
        raise ValueError("MCP tool catalog must be a list")
    observed = set()
    for tool in catalog:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("Invalid MCP tool catalog entry")
        name = tool["name"]
        if name in observed or name not in expected:
            raise ValueError("MCP tool catalog contains a duplicate or unpermitted tool")
        observed.add(name)
        if any(
            tool.get(key) != expected[name][key]
            for key in ("inputSchema", "outputSchema", "description")
        ):
            raise ValueError(f"MCP tool catalog schema or description drift: {name}")
    if allowed_tools is not None and observed != set(expected):
        raise ValueError("MCP tool catalog is missing an authorized tool")
    return observed

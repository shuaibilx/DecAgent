"""Versioned, frozen JSON capability contracts for trusted installed plugins."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import CannotDetermineSpecification, Unresolvable
from referencing.jsonschema import DRAFT202012

from decagent.business.mcp_contract import (
    check_mcp_manifest,
    compare_wire_catalog,
    verified_skill_text,
    wire_tools,
)

FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _is_datetime(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})",
        value,
    ):
        return False
    return datetime.fromisoformat(value.upper()).utcoffset() is not None


@FORMAT_CHECKER.checks("uri", raises=ValueError)
def _is_uri(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*", value):
        return False
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return False
    return bool(urlsplit(value).scheme)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _input_closure_check(root, root_resolver, object_keywords):
    """Prove unknown object fields are rejected at each new parameter value.

    Scalar constraints inherit their parent's type through applicators. New property and
    array values reset that context; annotations and unused definitions never grant input.
    """
    universe = {"object", "array", "string", "number", "integer", "boolean", "null"}

    def literal_type(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, dict):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, str):
            return "string"
        return "integer" if isinstance(value, int) else "number"

    def references(node, resolver):
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                target = resolver.lookup(node[keyword])
                yield DRAFT202012.create_resource(target.contents), target.resolver

    def possible(resource, resolver, seen=frozenset()):
        node = resource.contents
        if node is False:
            return set()
        if node is True or id(node) in seen:
            return universe.copy()
        seen = seen | {id(node)}
        types = node.get("type")
        allowed = set(types if isinstance(types, list) else [types]) if types else universe.copy()
        if "number" in allowed:
            allowed.add("integer")
        if "const" in node:
            allowed &= {literal_type(node["const"])}
        if "enum" in node:
            allowed &= {literal_type(value) for value in node["enum"]}
        for target, target_resolver in references(node, resolver):
            allowed &= possible(target, target_resolver, seen)
        for child in node.get("allOf", []):
            child = DRAFT202012.create_resource(child)
            allowed &= possible(child, resolver.in_subresource(child), seen)
        for keyword in ("anyOf", "oneOf"):
            if keyword in node:
                branches = set()
                for child in node[keyword]:
                    child = DRAFT202012.create_resource(child)
                    branches |= possible(child, resolver.in_subresource(child), seen)
                allowed &= branches
        excluded = node.get("not")
        if excluded is True:
            return set()
        if isinstance(excluded, dict) and set(excluded) == {"type"}:
            types = excluded["type"]
            allowed -= set(types if isinstance(types, list) else [types])
        return allowed

    def object_closed(resource, resolver, seen=frozenset()):
        node = resource.contents
        if "object" not in possible(resource, resolver):
            return True
        if node is True or id(node) in seen:
            return False
        if node.get("additionalProperties") is False or "const" in node or "enum" in node:
            return True
        seen = seen | {id(node)}
        if any(
            object_closed(target, target_resolver, seen)
            for target, target_resolver in references(node, resolver)
        ):
            return True
        if any(
            object_closed(child, resolver.in_subresource(child), seen)
            for child in (DRAFT202012.create_resource(item) for item in node.get("allOf", []))
        ):
            return True
        for keyword in ("anyOf", "oneOf"):
            if keyword in node and all(
                object_closed(child, resolver.in_subresource(child), seen)
                for child in (DRAFT202012.create_resource(item) for item in node[keyword])
            ):
                return True
        return False

    visited = set()

    def check(resource, resolver, context=None, inherited_closed=False):
        node = resource.contents
        allowed = possible(resource, resolver) & (universe if context is None else context)
        key = (id(node), frozenset(allowed), inherited_closed)
        if key in visited:
            return
        visited.add(key)
        guarded = inherited_closed or object_closed(resource, resolver)
        if "object" in allowed:
            if not guarded:
                raise ValueError(
                    "Input schema allows unconstrained object fields; require "
                    "additionalProperties=false or a closed referenced/applicator schema"
                )
            if (
                isinstance(node, dict)
                and object_keywords & node.keys()
                and node.get("additionalProperties") is not False
            ):
                raise ValueError("Input objects require additionalProperties=false")
        if not isinstance(node, dict):
            return
        for target, target_resolver in references(node, resolver):
            check(target, target_resolver, allowed, guarded)
        # These subschemas constrain the same value, so keep its effective type and closure.
        for keyword in ("allOf", "anyOf", "oneOf"):
            for child in node.get(keyword, []):
                child = DRAFT202012.create_resource(child)
                check(child, resolver.in_subresource(child), allowed, guarded)
        for keyword in ("then", "else"):
            if keyword in node:
                child = DRAFT202012.create_resource(node[keyword])
                check(child, resolver.in_subresource(child), allowed, guarded)
        for child in node.get("dependentSchemas", {}).values():
            child = DRAFT202012.create_resource(child)
            check(child, resolver.in_subresource(child), allowed, guarded)
        # Each property/item is a new parameter value and cannot inherit container closure.
        for keyword in ("properties", "patternProperties"):
            for child in node.get(keyword, {}).values():
                child = DRAFT202012.create_resource(child)
                check(child, resolver.in_subresource(child))
        for keyword in ("additionalProperties", "unevaluatedProperties"):
            if keyword in node:
                child = DRAFT202012.create_resource(node[keyword])
                check(child, resolver.in_subresource(child))
        # Omitting type or excluding another type still admits arrays. Their items
        # need the same closure proof as explicitly declared array parameters.
        if "array" in allowed:
            for child in node.get("prefixItems", []):
                child = DRAFT202012.create_resource(child)
                check(child, resolver.in_subresource(child))
            child = DRAFT202012.create_resource(node.get("items", True))
            check(child, resolver.in_subresource(child))

    check(root, root_resolver)


def _schema_check(schema, *, closed=False):
    if not isinstance(schema, dict):
        raise ValueError("JSON Schema must be an object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Invalid JSON Schema: " + exc.message) from exc

    object_keywords = {
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "dependentSchemas",
        "propertyNames",
        "unevaluatedProperties",
    }
    root = DRAFT202012.create_resource(schema)
    root_resolver = Registry().resolver_with_root(root)
    visited = set()

    def walk(resource, resolver):
        node = resource.contents
        if not isinstance(node, dict) or id(node) in visited:
            return
        visited.add(id(node))
        if node.get("$schema", "https://json-schema.org/draft/2020-12/schema") not in {
            "https://json-schema.org/draft/2020-12/schema",
            "https://json-schema.org/draft/2020-12/schema#",
        }:
            raise ValueError("JSON Schema must use supported Draft 2020-12")
        if "$recursiveRef" in node:
            raise ValueError("$recursiveRef is unsupported in Draft 2020-12; use $dynamicRef")
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node:
                ref = node[keyword]
                if not isinstance(ref, str) or not ref.startswith("#"):
                    raise ValueError(f"External JSON Schema reference {keyword} is not allowed")
                try:
                    resolved = resolver.lookup(ref)
                    target = resolved.contents
                    if not isinstance(target, (dict, bool)):
                        raise ValueError(f"JSON Schema reference {ref} does not point to a schema")
                    Draft202012Validator.check_schema(target)
                    walk(DRAFT202012.create_resource(target), resolved.resolver)
                except (Unresolvable, SchemaError, TypeError, LookupError) as exc:
                    raise ValueError(f"Invalid JSON Schema reference {ref}: {exc}") from exc
        for subresource in resource.subresources():
            walk(subresource, resolver.in_subresource(subresource))

    try:
        walk(root, root_resolver)
        if closed:
            _input_closure_check(root, root_resolver, object_keywords)
    except CannotDetermineSpecification as exc:
        raise ValueError("Unsupported JSON Schema specification") from exc


def validate_schema(schema, value):
    try:
        Draft202012Validator(schema, format_checker=FORMAT_CHECKER, registry=Registry()).validate(
            value
        )
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise ValueError(f"{path}: {exc.message}") from exc
    except Unresolvable as exc:
        raise ValueError(f"Invalid JSON Schema reference: {exc}") from exc


def _workflow_check(workflow, commands):
    required = {
        "version",
        "branches",
        "questions",
        "parameter_sources",
        "completion",
    }
    if not isinstance(workflow, dict) or not required <= workflow.keys():
        raise ValueError("Enhanced plugin requires complete versioned workflow")

    def nonempty(value):
        return isinstance(value, str) and bool(value.strip())

    def names(value):
        return (
            isinstance(value, list)
            and bool(value)
            and all(nonempty(item) for item in value)
            and len(value) == len(set(value))
        )

    version = workflow["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?", version
    ):
        raise ValueError("workflow.version must be a numeric or semantic version string")
    states = workflow.get("states", [])
    if "states" in workflow and not names(states):
        raise ValueError("workflow.states requires unique nonempty state names")
    transitions = workflow.get("transitions", {})
    if not isinstance(transitions, dict):
        raise ValueError("workflow.transitions requires a command-to-transition object")
    for command, transition in transitions.items():
        if (
            command not in commands
            or not isinstance(transition, dict)
            or set(transition) != {"from", "to"}
            or not names(transition["from"])
            or not set(transition["from"]) <= set(states)
            or not isinstance(transition["to"], str)
            or transition["to"] not in states
        ):
            raise ValueError(f"workflow.transitions.{command} requires legal commands and states")
    branches = workflow["branches"]
    routes = {"request_user", "main_planner", "validate_and_execute", "finish"} | commands.keys()
    if (
        not isinstance(branches, list)
        or not branches
        or any(
            not isinstance(branch, dict)
            or set(branch) != {"condition", "route"}
            or not nonempty(branch["condition"])
            or not isinstance(branch["route"], str)
            or branch["route"] not in routes
            for branch in branches
        )
    ):
        raise ValueError("workflow.branches requires conditions and declared execution routes")
    parameters = {
        name
        for command in commands.values()
        for name in command["input_schema"].get("properties", {})
    }
    sources = workflow["parameter_sources"]
    if (
        not isinstance(sources, dict)
        or not sources
        or any(name not in parameters or not nonempty(source) for name, source in sources.items())
    ):
        raise ValueError("workflow.parameter_sources requires declared parameters and provenance")
    completion = workflow["completion"]
    if (
        not isinstance(completion, dict)
        or not completion
        or any(
            not nonempty(name) or not nonempty(condition) for name, condition in completion.items()
        )
    ):
        raise ValueError("workflow.completion requires named completion conditions")
    questions = workflow["questions"]
    if not isinstance(questions, list) or not 1 <= len(questions) <= 64:
        raise ValueError("workflow.questions requires 1 to 64 typed question definitions")
    identifiers = set()
    for question in questions:
        if (
            not isinstance(question, dict)
            or not isinstance(question.get("id"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", question["id"])
            or question["id"] in identifiers
        ):
            raise ValueError("workflow.questions requires unique valid question IDs")
        identifiers.add(question["id"])
        kind = question.get("type")
        if kind == "choice":
            candidates = question.get("candidates")
            if (
                not names(candidates)
                or len(candidates) > 253
                or not set(candidates) <= commands.keys()
                or question.get("no_match") is not True
            ):
                raise ValueError("workflow choice requires command candidates and no_match=true")
        elif kind == "noul":
            if not nonempty(question.get("proposition")):
                raise ValueError("workflow noul requires a nonempty proposition")
        elif kind == "score":
            scale = question.get("scale")
            if not isinstance(scale, list) or not 2 <= len(scale) <= 10:
                raise ValueError("workflow score requires 2 to 10 ordered contextual levels")
            contexts = []
            for index, level in enumerate(scale):
                if (
                    not isinstance(level, dict)
                    or set(level) != {"score", "context"}
                    or type(level["score"]) is not int
                    or level["score"] != index
                    or not nonempty(level["context"])
                ):
                    raise ValueError(
                        "workflow score levels require ascending integer scores and contexts"
                    )
                contexts.append(level["context"].strip())
            if len(contexts) != len(set(contexts)):
                raise ValueError("workflow score contexts must be distinct")
        else:
            raise ValueError("workflow questions support only choice, noul or score")


@dataclass(frozen=True)
class CommandSpec:
    name: str
    _source: str

    @property
    def description(self):
        return json.loads(self._source).get("description", self.name)

    @property
    def effect(self):
        return json.loads(self._source)["effect"]

    @property
    def confirmation(self):
        return json.loads(self._source)["confirmation"]

    @property
    def input_schema(self):
        return json.loads(self._source)["input_schema"]

    @property
    def result_schema(self):
        return json.loads(self._source)["result_schema"]


@dataclass(frozen=True)
class PluginSpec:
    _source: str

    @classmethod
    def from_mapping(cls, manifest):
        if not isinstance(manifest, dict):
            raise ValueError("Plugin manifest must be an object")
        required = {"id", "version", "level", "commands"}
        missing = required - manifest.keys()
        if missing:
            raise ValueError("Missing plugin fields: " + ", ".join(sorted(missing)))
        if not isinstance(manifest["id"], str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", manifest["id"]
        ):
            raise ValueError("Invalid plugin id")
        if not isinstance(manifest["version"], str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?", manifest["version"]
        ):
            raise ValueError("Plugin version must be a semantic version")
        if not isinstance(manifest["level"], str) or manifest["level"] not in {"basic", "enhanced"}:
            raise ValueError("Plugin level must be basic or enhanced")
        mcp = "connection" in manifest or (
            manifest["id"] == "service-tickets" and manifest["version"] == "2.0.0"
        )
        if not mcp and "entrypoint" not in manifest:
            raise ValueError("Missing plugin fields: entrypoint or MCP connection")
        entry = manifest.get("entrypoint", {}) if not mcp else None
        if mcp:
            pass  # Fixed MCP fields are checked after the common command/workflow contract.
        elif not isinstance(entry, dict):
            raise ValueError("A fixed installed entrypoint is required")
        elif set(entry) == {"module"}:
            if not isinstance(entry["module"], str) or not re.fullmatch(
                r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", entry["module"]
            ):
                raise ValueError("Invalid installed Python module")
        elif set(entry) == {"argv"}:
            argv = entry["argv"]
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
                or not Path(argv[0]).is_absolute()
            ):
                raise ValueError("Entrypoint argv requires a fixed absolute executable")
        else:
            raise ValueError("Entrypoint requires exactly module or argv")
        commands = manifest["commands"]
        if not isinstance(commands, dict) or not commands:
            raise ValueError("Command whitelist is required")
        for name, command in commands.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError("Invalid command name")
            if (
                not isinstance(command, dict)
                or not {"input_schema", "result_schema", "effect", "confirmation"} <= command.keys()
            ):
                raise ValueError(f"Command {name} has incomplete execution contract")
            if not isinstance(command["effect"], str) or command["effect"] not in {"read", "write"}:
                raise ValueError(f"Command {name} has invalid effect")
            if not isinstance(command["confirmation"], str) or command["confirmation"] not in {
                "auto",
                "confirm",
                "initial_assignment",
            }:
                raise ValueError(f"Command {name} has invalid confirmation policy")
            _schema_check(command["input_schema"], closed=True)
            if command["input_schema"].get("type") != "object":
                raise ValueError("Command input_schema must declare object")
            _schema_check(command["result_schema"])
        env = manifest.get("environment", [])
        if not isinstance(env, list) or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
            or any(
                word in name
                for word in (
                    "OPENAI",
                    "OPENROUTER",
                    "GITHUB",
                    "OWNER",
                    "QWEN",
                    "JEV",
                    "MODEL",
                    "PYTHON",
                    "PATH",
                )
            )
            for name in env
        ):
            raise ValueError("Invalid or prohibited plugin environment variable")
        limits = manifest.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - {
            "timeout_seconds",
            "max_output_bytes",
            "max_processes",
        }:
            raise ValueError("Invalid execution limits")
        for name, default, minimum, maximum in (
            ("timeout_seconds", 15, 0.05, 120),
            ("max_output_bytes", 262144, 128, 1048576),
            ("max_processes", 2, 1, 8),
        ):
            value = limits.get(name, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not (minimum <= value <= maximum)
                or (name != "timeout_seconds" and not isinstance(value, int))
            ):
                raise ValueError(f"Invalid {name}")
        if manifest["level"] == "enhanced":
            _workflow_check(manifest.get("workflow"), commands)
            if not isinstance(manifest.get("skill"), str) or not manifest["skill"]:
                raise ValueError("Enhanced plugin requires a packaged skill reference")
        if mcp:
            check_mcp_manifest(manifest)
        try:
            return cls(canonical(manifest))
        except (TypeError, ValueError) as exc:
            raise ValueError("Manifest must contain finite JSON values") from exc

    @property
    def manifest(self):
        return json.loads(self._source)

    @property
    def id(self):
        return self.manifest["id"]

    @property
    def version(self):
        return self.manifest["version"]

    @property
    def level(self):
        return self.manifest["level"]

    @property
    def connection(self):
        """Trusted configuration reference; historical CLI contracts return None."""
        return self.manifest.get("connection")

    @property
    def skill_digest(self):
        return self.manifest.get("skill_digest")

    def skill_text(self):
        if self.connection is None:
            raise ValueError("Historical CLI Skill uses the legacy packaged loader")
        return verified_skill_text(self.manifest)

    @property
    def digest(self):
        return hashlib.sha256(self._source.encode("utf-8")).hexdigest()

    @property
    def commands(self):
        return {
            name: CommandSpec(name, canonical(command))
            for name, command in self.manifest["commands"].items()
        }

    def validate(self, command, params):
        if command not in self.commands:
            raise ValueError(f"Unknown plugin command: {command}")
        canonical(params)
        validate_schema(self.commands[command].input_schema, params)

    def tool_schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": command.name,
                    "description": command.description,
                    "parameters": command.input_schema,
                },
            }
            for command in self.commands.values()
        ]

    def wire_tools(self, *, allowed_tools=None):
        return wire_tools(self, allowed_tools=allowed_tools)

    def _wire_tool(self, name):
        for tool in self.wire_tools():
            if tool["name"] == name:
                return tool
        raise ValueError(f"Unknown MCP tool: {name}")

    def validate_wire(self, name, arguments):
        canonical(arguments)
        validate_schema(self._wire_tool(name)["inputSchema"], arguments)

    def validate_wire_result(self, name, response):
        canonical(response)
        validate_schema(self._wire_tool(name)["outputSchema"], response)

    def compare_wire_catalog(self, catalog, *, allowed_tools=None):
        return compare_wire_catalog(self, catalog, allowed_tools=allowed_tools)

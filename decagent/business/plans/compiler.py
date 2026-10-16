"""Compile finite data plans against current registered capabilities and schemas."""

from dataclasses import dataclass
from types import MappingProxyType

from decagent.business.contracts import validate_schema
from decagent.business.mcp_contract import FORBIDDEN_INPUT_KEYS

from .bindings import TRANSFORM_CONTRACTS
from .contracts import MAX_BRANCH_DEPTH, Plan
from .schema_tools import schema_at_path, schema_types, schema_variants, type_compatible
from .templates import QuestionRegistry


class PlanCompileError(ValueError):
    def __init__(self, code, message, node_id=None):
        self.code, self.node_id = code, node_id
        super().__init__(f"{node_id or 'plan'}: {message}")


@dataclass(frozen=True)
class CompiledPlan:
    plan: Plan
    order: tuple
    edges: object
    parents: object
    dominators: object

    def node(self, node_id):
        return next(node for node in self.plan.nodes if node["id"] == node_id)

    def successors(self, node_id):
        return self.edges[node_id]

    def predecessors(self, node_id):
        return self.parents[node_id]


def _bindings(value):
    if isinstance(value, dict):
        if "source" in value and "value_type" in value:
            yield value
        else:
            for child in value.values():
                yield from _bindings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _bindings(child)


def _targets(node, *, exceptional=True):
    output = list(node.get("next", []))
    for case in node.get("cases", []):
        output.extend(case["next"])
    keys = ("on_missing", "on_error", "on_no_match") if exceptional else ("on_no_match",)
    for key in keys:
        target = node.get(key, {}).get("node_id")
        if target is not None:
            output.append(target)
    return tuple(dict.fromkeys(output))


def _graph(plan):
    nodes = {node["id"]: node for node in plan.nodes}
    if len(nodes) != len(plan.nodes):
        raise PlanCompileError("duplicate_node", "Duplicate node ID")
    if plan.entry not in nodes:
        raise PlanCompileError("entry", "Unknown entry node")
    edges = {name: _targets(node) for name, node in nodes.items()}
    parents = {name: [] for name in nodes}
    for name, targets in edges.items():
        for target in targets:
            if target not in nodes:
                raise PlanCompileError("dangling_edge", "Unknown successor " + target, name)
            parents[target].append(name)
        if any(dep not in nodes or dep == name for dep in nodes[name]["depends_on"]):
            raise PlanCompileError("dependency", "Unknown or self dependency", name)
    visited, active, order = set(), set(), []

    def visit(name):
        if name in active:
            raise PlanCompileError("cycle", "Plan has an unbounded cycle", name)
        if name in visited:
            return
        active.add(name)
        for target in edges[name]:
            visit(target)
        active.remove(name)
        visited.add(name)
        order.append(name)

    visit(plan.entry)
    if visited != set(nodes):
        raise PlanCompileError("unreachable", "Plan contains unreachable nodes")
    order.reverse()
    ancestors, dominators = {}, {}
    for name in order:
        predecessors = parents[name]
        ancestors[name] = set(predecessors)
        for parent in predecessors:
            ancestors[name].update(ancestors[parent])
        dominators[name] = {name}
        if predecessors:
            dominators[name].update(set.intersection(*(dominators[p] for p in predecessors)))
        if set(nodes[name]["depends_on"]) - ancestors[name]:
            raise PlanCompileError("dependency", "Dependency does not precede the node", name)
    if nodes[plan.entry]["depends_on"]:
        raise PlanCompileError("dependency", "Entry cannot wait for later nodes", plan.entry)
    # Successful shared joins close branch scopes; exceptions don't fabricate extra depth.
    normal = {name: _targets(node, exceptional=False) for name, node in nodes.items()}
    postdominators = {}
    for name in reversed(order):
        targets = normal[name]
        postdominators[name] = {name}
        if targets:
            postdominators[name].update(set.intersection(*(postdominators[t] for t in targets)))
    closes = {}
    position = {name: index for index, name in enumerate(order)}
    for name, node in nodes.items():
        if node["type"] == "branch":
            candidates = [
                target
                for target in postdominators[name]
                if target != name and nodes[target]["type"] == "join"
            ]
            if candidates:
                closes[name] = min(candidates, key=position.__getitem__)
    scopes = {}
    for name in order:
        current = set().union(*(scopes[p] for p in parents[name]))
        current.difference_update(branch for branch, join in closes.items() if join == name)
        if nodes[name]["type"] == "branch":
            current.add(name)
        if len(current) > MAX_BRANCH_DEPTH:
            raise PlanCompileError("branch_depth", "Branch depth exceeds four", name)
        scopes[name] = current
    available = _available_dependencies(nodes, order, dominators)
    return nodes, edges, parents, order, ancestors, dominators, available


def _available_dependencies(nodes, order, dominators):
    # Normal next arrays activate every target; branch cases activate one route.
    paths = {name: set() for name in nodes}
    paths[order[0]].add(frozenset())
    for name in order:
        node = nodes[name]
        routes = []
        if node["type"] == "branch":
            for index, case in enumerate(node["cases"]):
                routes.extend((target, (name, str(index))) for target in case["next"])
        else:
            alternative = any(
                node.get(key, {}).get("node_id")
                for key in ("on_missing", "on_error", "on_no_match")
            )
            predicate = (name, "success") if alternative else None
            routes.extend((target, predicate) for target in node.get("next", []))
        for key in ("on_missing", "on_error", "on_no_match"):
            target = node.get(key, {}).get("node_id")
            if target:
                routes.append((target, (name, key)))
        for target, predicate in routes:
            for term in paths[name]:
                paths[target].add(frozenset(term | ({predicate} if predicate else set())))
            if len(paths[target]) > 4096:
                raise PlanCompileError("path_limit", "Control-flow analysis limit exceeded", target)
    dependencies = {}
    available = {name: set(dominators[name]) for name in nodes}
    for name in order:
        dependencies[name] = set(nodes[name]["depends_on"])
        for parent in nodes[name]["depends_on"]:
            dependencies[name].update(dependencies[parent])
        for producer in dependencies[name]:
            if all(any(source <= term for source in paths[producer]) for term in paths[name]):
                available[name].add(producer)
    return available


def _producer_schema(node, spec, questions, transforms):
    kind = node["type"]
    if kind in {"read_tool", "governed_write"}:
        reference = node["tool" if kind == "read_tool" else "command"]
        if reference["plugin_id"] != spec.id or reference["command"] not in spec.commands:
            raise PlanCompileError("capability", "Capability is not registered", node["id"])
        command = spec.commands[reference["command"]]
        effect = "read" if kind == "read_tool" else "write"
        if command.effect != effect:
            raise PlanCompileError("effect", "Capability effect disagrees with node", node["id"])
        return command.result_schema
    if kind == "decide":
        definition = questions.validate_reference(node["question"])
        return {
            "type": "object",
            "properties": {
                name: {"type": value} for name, value in definition["output_fields"].items()
            },
            "additionalProperties": False,
        }
    if kind == "ask_user":
        declaration = node["answer"]
        return {
            "type": [declaration["value_type"], "null"]
            if declaration["nullable"]
            else declaration["value_type"]
        }
    if kind == "transform":
        reference = node["transform"]
        definition = transforms.get(reference["transform_id"])
        if not definition or definition["version"] != reference["version"]:
            raise PlanCompileError("transform", "Transform is not registered", node["id"])
        return definition["output_schema"]
    return {"type": "null"}


def _reference(binding, consumer, nodes, schemas, input_schema, ancestors, dominators):
    source = binding["source"]
    if source == "literal":
        return
    if source == "verified_memory":
        return  # ACL, freshness and source evidence are checked at runtime, not inferred here.
    if source == "task_input":
        root = input_schema
    else:
        producer = binding["node_id"]
        if producer not in nodes or producer not in ancestors[consumer]:
            raise PlanCompileError("future_reference", "Reference must precede consumer", consumer)
        kind = nodes[producer]["type"]
        provenance = binding["provenance"]
        if source == "accepted_decision":
            valid = kind == "decide" and provenance == "accepted_decision"
        else:
            valid = (
                (kind == "read_tool" and provenance == "tool_result")
                or (kind == "governed_write" and provenance in {"tool_result", "write_receipt"})
                or (kind == "transform" and provenance == "transform_result")
                or (kind == "ask_user" and provenance == "user_answer")
            )
            if provenance == "write_receipt":
                valid = valid and bool(binding["path"]) and binding["path"][0] == "receipt"
        if not valid:
            raise PlanCompileError(
                "provenance", "Reference has false producer provenance", consumer
            )
        if producer not in dominators[consumer] and not binding["nullable"]:
            raise PlanCompileError(
                "branch_reference", "Required reference crosses a branch", consumer
            )
        root = schemas[producer]
    leaf = schema_at_path(root, root, binding["path"])
    if not type_compatible(binding["value_type"], schema_types(root, leaf)):
        raise PlanCompileError("reference_type", "Reference type disagrees with producer", consumer)


def _inputs(node, schema):
    inputs = node.get("inputs", {})
    variants = schema_variants(schema, schema)
    properties, required = {}, set()
    for item in variants:
        properties.update(item.get("properties", {}))
        required.update(item.get("required", []))
    if required - set(inputs):
        raise PlanCompileError(
            "missing_parameter", "Required parameter binding missing", node["id"]
        )
    if set(inputs) - set(properties):
        raise PlanCompileError("unknown_parameter", "Unregistered parameter binding", node["id"])
    for name, binding in inputs.items():
        key = "".join(char for char in name.lower() if char.isalnum())
        if key in FORBIDDEN_INPUT_KEYS:
            raise PlanCompileError("authority", "Program-owned identity field", node["id"])
        if not type_compatible(binding["value_type"], schema_types(schema, properties[name])):
            raise PlanCompileError(
                "input_type", "Parameter declaration disagrees with schema", node["id"]
            )
        if binding["source"] == "literal":
            validate_schema(
                {"$defs": schema.get("$defs", {}), **properties[name]}, binding["value"]
            )


def default_input_schema():
    return {
        "type": "object",
        "properties": {"instruction": {"type": "string"}},
        "additionalProperties": False,
    }


def compile_plan(value, spec, *, input_schema=None, transforms=None):
    """Validate topology, versions, registered effects, field types and provenance."""
    plan = value if isinstance(value, Plan) else Plan.from_mapping(value)
    questions = QuestionRegistry.from_spec(spec)
    if plan.registry_fingerprint != questions.fingerprint:
        raise PlanCompileError("registry_changed", "Registry fingerprint changed")
    input_schema = input_schema if input_schema is not None else default_input_schema()
    transforms = TRANSFORM_CONTRACTS if transforms is None else transforms
    nodes, edges, parents, order, ancestors, dominators, available = _graph(plan)
    schemas = {
        name: _producer_schema(node, spec, questions, transforms) for name, node in nodes.items()
    }
    evidence = {item["id"]: item for item in plan.goal["evidence"]}
    if len(evidence) != len(plan.goal["evidence"]):
        raise PlanCompileError("evidence", "Duplicate goal evidence ID")
    completed = False
    for name in order:
        node = nodes[name]
        kind = node["type"]
        if kind in {"read_tool", "governed_write"}:
            command = node["tool" if kind == "read_tool" else "command"]["command"]
            _inputs(node, spec.commands[command].input_schema)
        elif kind == "decide":
            definition = questions.validate_reference(node["question"])
            if set(node["inputs"]) - set(definition["state_fields"]):
                raise PlanCompileError("question_input", "Unknown question state fields", name)
            if "instruction" not in node["inputs"]:
                raise PlanCompileError("question_input", "User instruction binding required", name)
            if node["inputs"]["instruction"]["source"] not in {"task_input", "node_output"}:
                raise PlanCompileError("question_input", "Instruction must have real source", name)
            if definition["candidate_source"]:
                directory = node["inputs"].get("directory")
                if not directory or directory["source"] != "node_output":
                    raise PlanCompileError(
                        "candidate_source", "Directory must be a real read", name
                    )
                producer = nodes.get(directory["node_id"], {})
                if (
                    producer.get("type") != "read_tool"
                    or producer.get("tool", {}).get("command") != "directory"
                ):
                    raise PlanCompileError(
                        "candidate_source", "Unregistered candidate source", name
                    )
        elif kind == "transform":
            definition = transforms[node["transform"]["transform_id"]]
            _inputs(node, definition["input_schema"])
        for binding in _bindings({key: item for key, item in node.items() if key != "inputs"}):
            _reference(binding, name, nodes, schemas, input_schema, ancestors, available)
        for binding in node.get("inputs", {}).values():
            _reference(binding, name, nodes, schemas, input_schema, ancestors, available)
        if kind == "complete":
            completed = True
            if set(node["evidence"]) - set(evidence):
                raise PlanCompileError("evidence", "Unknown goal evidence ID", name)
            actual = False
            for evidence_id in node["evidence"]:
                item = evidence[evidence_id]
                for binding in _bindings(item):
                    _reference(binding, name, nodes, schemas, input_schema, ancestors, available)
                actual = actual or item["kind"] in {"tool_result", "write_receipt"}
            if not actual:
                raise PlanCompileError("completion", "Goal lacks actual business evidence", name)
    if not completed:
        raise PlanCompileError("completion", "No reachable completion node")
    return CompiledPlan(
        plan,
        tuple(order),
        MappingProxyType({key: tuple(value) for key, value in edges.items()}),
        MappingProxyType({key: tuple(value) for key, value in parents.items()}),
        MappingProxyType({key: frozenset(value) for key, value in dominators.items()}),
    )

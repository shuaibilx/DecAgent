"""Three owner-authored workflow drafts; building never validates quality or publishes.

Policy pins are supplied by the owner from independently calibrated policy versions.
All business values are bound anew by the normal interpreter and MCP governance.
"""

from copy import deepcopy

from decagent.business.plans.templates import QuestionRegistry
from decagent.business.workflow_registry import validate_definition

TEMPLATE_QUESTIONS = {
    "create-ticket": ("category",),
    "directory-assign": ("explicit_assignee", "person"),
    "review-priority": ("work_blockage",),
}


def _ref(source, path=(), *, node_id=None, kind="string", provenance=None):
    value = {
        "source": source,
        "path": list(path),
        "value_type": kind,
        "nullable": False,
        "provenance": provenance
        or {
            "task_input": "user_input",
            "node_output": "tool_result",
            "accepted_decision": "accepted_decision",
        }[source],
    }
    if node_id is not None:
        value["node_id"] = node_id
    return value


def _node(name, kind, *, depends=(), **fields):
    return {
        "id": name,
        "type": kind,
        "depends_on": list(depends),
        "on_missing": {"stop": "missing_input"},
        "on_error": {"stop": "error"},
        **fields,
    }


def _evidence(name, source, *, receipt=False, condition=None):
    result = {
        "id": name,
        "kind": "write_receipt" if receipt else "tool_result",
        "description": "Verified current business result",
        "source": source,
    }
    if condition is not None:
        result["condition"] = condition
    return result


def build_workflow_template(
    template_id, *, spec, scope_id, scope_revision, policy_dependencies, version="1"
):
    """Return a compiled registry definition, without registering or granting authority.

    ``policy_dependencies`` maps the template's question IDs to exact owner policy
    pins (policy_id, policy_version, definition_digest). Publication independently
    checks that every pinned policy is published and calibrated for this scope.
    """
    if template_id not in TEMPLATE_QUESTIONS:
        raise ValueError("Unknown business workflow template")
    if not isinstance(policy_dependencies, dict) or set(policy_dependencies) != set(
        TEMPLATE_QUESTIONS[template_id]
    ):
        raise ValueError("Template requires exact question policy dependencies")
    registry = QuestionRegistry.from_spec(spec)

    def read(name, command, inputs, *, depends=(), targets=()):
        return _node(
            name,
            "read_tool",
            tool={"plugin_id": spec.id, "command": command},
            inputs=inputs,
            depends=depends,
            next=list(targets),
        )

    def write(name, command, inputs, *, depends=(), targets=()):
        return _node(
            name,
            "governed_write",
            command={"plugin_id": spec.id, "command": command},
            inputs=inputs,
            depends=depends,
            next=list(targets),
        )

    def decide(name, question_id, inputs, *, depends=(), targets=()):
        question = registry.get(question_id)
        pin = policy_dependencies[question_id]
        return _node(
            name,
            "decide",
            depends=depends,
            question={key: question[key] for key in ("question_id", "version", "type")},
            policy={"policy_id": pin["policy_id"], "version": pin["policy_version"]},
            inputs={"instruction": _ref("task_input", ["instruction"]), **inputs},
            next=list(targets),
            on_no_match={"stop": "no_match"},
        )

    def complete(name, evidence, depends):
        return _node(name, "complete", evidence=evidence, depends=depends)

    def receipt(name):
        return _ref(
            "node_output",
            ["receipt", "operation_id"],
            node_id=name,
            provenance="write_receipt",
        )

    def selected(name, kind="string"):
        return _ref("accepted_decision", ["selected_value"], node_id=name, kind=kind)

    ticket = _ref("node_output", ["ticket"], node_id="get-ticket", kind="object")
    ticket_id = _ref("node_output", ["ticket", "id"], node_id="get-ticket")
    revision = _ref("node_output", ["ticket", "revision"], node_id="get-ticket", kind="integer")
    directory = _ref("node_output", node_id="directory", kind="object")
    fields = {"instruction": {"type": "string", "minLength": 1, "maxLength": 10000}}
    roles = ["employee", "handler", "supervisor"]
    if template_id == "create-ticket":
        fields.update(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 300},
                "description": {"type": "string", "maxLength": 10000},
            }
        )
        evidence = [_evidence("created", receipt("create"), receipt=True)]
        nodes = [
            read("directory", "directory", {}, targets=["select-category"]),
            decide(
                "select-category",
                "category",
                {"directory": directory},
                depends=["directory"],
                targets=["create"],
            ),
            write(
                "create",
                "create",
                {
                    "title": _ref("task_input", ["title"]),
                    "description": _ref("task_input", ["description"]),
                    "category_id": selected("select-category"),
                },
                depends=["select-category"],
                targets=["done"],
            ),
            complete("done", ["created"], ["create"]),
        ]
    elif template_id == "directory-assign":
        fields["ticket_id"] = {"type": "string", "format": "uuid"}
        roles = ["handler", "supervisor"]
        evidence = [_evidence("assigned", receipt("assign"), receipt=True)]
        nodes = [
            read("directory", "directory", {}, targets=["get-ticket"]),
            read(
                "get-ticket",
                "get",
                {"ticket_id": _ref("task_input", ["ticket_id"])},
                depends=["directory"],
                targets=["explicit-assignee"],
            ),
            decide(
                "explicit-assignee",
                "explicit_assignee",
                {
                    "directory": directory,
                    "ticket": ticket,
                },
                depends=["get-ticket"],
                targets=["has-assignee"],
            ),
            _node(
                "has-assignee",
                "branch",
                depends=["explicit-assignee"],
                cases=[
                    {
                        "condition": {
                            "op": "eq",
                            "left": selected("explicit-assignee", "boolean"),
                            "right": {
                                "source": "literal",
                                "value": True,
                                "value_type": "boolean",
                                "nullable": False,
                                "provenance": "plan_literal",
                            },
                        },
                        "next": ["select-person"],
                    }
                ],
                on_no_match={"stop": "missing_input"},
            ),
            decide(
                "select-person",
                "person",
                {"directory": directory, "ticket": ticket},
                depends=["has-assignee"],
                targets=["assign"],
            ),
            write(
                "assign",
                "assign",
                {
                    "ticket_id": ticket_id,
                    "person_id": selected("select-person"),
                    "expected_revision": revision,
                },
                depends=["select-person"],
                targets=["done"],
            ),
            complete("done", ["assigned"], ["assign"]),
        ]
    else:
        fields["ticket_id"] = {"type": "string", "format": "uuid"}
        suggestion = _ref("node_output", node_id="suggest-priority", provenance="transform_result")
        unchanged = {
            "op": "eq",
            "left": _ref("node_output", ["ticket", "priority"], node_id="get-ticket"),
            "right": suggestion,
        }
        evidence = [
            _evidence("reviewed", ticket, condition=unchanged),
            _evidence("updated", receipt("set-priority"), receipt=True),
        ]
        nodes = [
            read(
                "get-ticket",
                "get",
                {"ticket_id": _ref("task_input", ["ticket_id"])},
                targets=["work-blockage"],
            ),
            decide(
                "work-blockage",
                "work_blockage",
                {"ticket": ticket},
                depends=["get-ticket"],
                targets=["suggest-priority"],
            ),
            _node(
                "suggest-priority",
                "transform",
                depends=["work-blockage"],
                transform={"transform_id": "priority_for_score", "version": "1"},
                inputs={"score": selected("work-blockage", "number")},
                next=["priority-changed"],
            ),
            _node(
                "priority-changed",
                "branch",
                depends=["suggest-priority"],
                cases=[{"condition": unchanged, "next": ["unchanged"]}],
                on_no_match={"node_id": "set-priority"},
            ),
            complete("unchanged", ["reviewed"], ["priority-changed"]),
            write(
                "set-priority",
                "set_priority",
                {
                    "ticket_id": ticket_id,
                    "priority": suggestion,
                    "expected_revision": revision,
                },
                depends=["priority-changed"],
                targets=["done"],
            ),
            complete("done", ["updated"], ["set-priority"]),
        ]
    definition = {
        "workflow_id": template_id,
        "version": version,
        "goal_id": template_id,
        "scope_id": scope_id,
        "scope_revision": scope_revision,
        "plan": {
            "schema_version": "1",
            "plan_id": template_id,
            "revision": 1,
            "registry_fingerprint": registry.fingerprint,
            "entry": nodes[0]["id"],
            "goal": {"description": template_id, "evidence": evidence},
            "nodes": nodes,
        },
        "input_schema": {
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
        "applicability": {"roles": roles, "input_equals": {}},
        "policy_dependencies": [
            deepcopy(policy_dependencies[q]) for q in TEMPLATE_QUESTIONS[template_id]
        ],
    }
    return validate_definition(definition, spec)

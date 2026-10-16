"""Narrow deterministic proofs for registered conditional workflow goals."""

import re

from decagent.business.actions import literal_request
from decagent.business.questions import priority_for_score

from .bindings import evaluate_condition, resolve_binding


def satisfied_without_write(compiled, complete_node, context, instruction):
    """Prove an already satisfied conditional priority goal; never grant a write.

    The runtime supplies workflow_id from the server-pinned task authorization.
    Unregistered model plans and direct literal write requests keep the normal
    requirement to prove every requested command actually executed.
    """
    if (
        context.workflow_id != "review-priority"
        or complete_node != "unchanged"
        or literal_request(instruction) is not None
        or not re.search(r"按需|如果需要|必要时|\bif\s+(?:needed|necessary)\b", instruction, re.I)
        or context.task_input.get("instruction") != instruction
    ):
        return set()
    try:
        transform = compiled.node("suggest-priority")
        score = transform["inputs"]["score"]
        judge = compiled.node("work-blockage")
        branch = compiled.node("priority-changed")
        evidence = next(e for e in compiled.plan.goal["evidence"] if e["id"] == "reviewed")
        condition = evidence["condition"]
        left, right = condition["left"], condition["right"]
        if (
            transform["type"] != "transform"
            or transform["transform"] != {"transform_id": "priority_for_score", "version": "1"}
            or score["source"] != "accepted_decision"
            or score["node_id"] != "work-blockage"
            or score["path"] != ["selected_value"]
            or judge["type"] != "decide"
            or judge["question"]["question_id"] != "work_blockage"
            or judge["question"]["type"] != "score"
            or compiled.node("get-ticket")["tool"]["command"] != "get"
            or condition["op"] != "eq"
            or left["source"] != "node_output"
            or left["node_id"] != "get-ticket"
            or left["path"] != ["ticket", "priority"]
            or right["source"] != "node_output"
            or right["node_id"] != "suggest-priority"
            or right["path"] != []
            or branch["type"] != "branch"
            or branch["cases"] != [{"condition": condition, "next": ["unchanged"]}]
            or branch["on_no_match"] != {"node_id": "set-priority"}
            or compiled.node(complete_node)["evidence"] != ["reviewed"]
            or evidence["kind"] != "tool_result"
            or evidence["source"]["source"] != "node_output"
            or evidence["source"]["node_id"] != "get-ticket"
            or evidence["source"]["path"] != ["ticket"]
        ):
            return set()
        expected = priority_for_score(resolve_binding(score, context).value)
        ticket = resolve_binding(evidence["source"], context).value
        original = context.node_outputs["get-ticket"]
        if (
            ticket["id"] != context.task_input["ticket_id"]
            or original["inputs"] != {"ticket_id": ticket["id"]}
            or ticket["priority"] != expected
            or resolve_binding(right, context).value != expected
            or not evaluate_condition(condition, context)
        ):
            return set()
        return {"set_priority"}
    except (KeyError, TypeError, ValueError, StopIteration):
        return set()

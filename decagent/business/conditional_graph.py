"""A fixed business-3 graph around the finite, durable plan interpreter.

Plan nodes are data, never dynamically generated LangGraph code. Interrupts run
in a separate node so replay cannot repeat preparation or tool dispatch.
"""

from copy import deepcopy
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from decagent.business.plans.runtime import NodePending
from decagent.storage.budget import BudgetExceeded

GRAPH_VERSION = "business-3"
MAX_TRANSITIONS = 1024
RECURSION_LIMIT = 2 * MAX_TRANSITIONS + 16
PHASES = frozenset({"prepare", "execute", "finalize"})


class ConditionalState(TypedDict):
    data: dict


def build_graph(services, checkpointer):
    task_id = str(services.task["id"])

    def checked(state):
        data = deepcopy(state.get("data"))
        if not isinstance(data, dict) or data.get("task_id") != task_id:
            raise ValueError("Checkpoint belongs to another business task")
        if data.get("graph_version") != GRAPH_VERSION:
            raise ValueError("Checkpoint business graph version is incompatible")
        return data

    def budget(data, error, target):
        return {
            "data": {
                **data,
                "status": "waiting",
                "wait_target": target,
                "pending": {"kind": "budget", "reason": str(error), "code": error.code},
            }
        }

    def prepare(state):
        data = checked(state)
        try:
            services.guard()
            services.prepare(deepcopy(data))
        except BudgetExceeded as exc:
            return budget(data, exc, "prepare")
        except NodePending as pending:
            return {
                "data": {
                    **data,
                    "status": "waiting",
                    "wait_target": "prepare",
                    "pending": pending.request,
                }
            }
        return {"data": {**data, "phase": "prepare", "status": "advanced"}}

    def advance(state):
        data = checked(state)
        try:
            services.guard()
            result = services.step(deepcopy(data))
        except BudgetExceeded as exc:
            return budget(data, exc, "advance")
        if not isinstance(result, dict) or result.get("status") not in {
            "advanced",
            "waiting",
            "complete",
            "stopped",
        }:
            raise ValueError("Conditional runtime returned an unsupported result")
        update = {**data, **result, "phase": "execute"}
        if result["status"] == "waiting":
            request = result.get("request")
            if not isinstance(request, dict) or not isinstance(request.get("kind"), str):
                raise ValueError("Conditional wait requires a durable request")
            update.update(pending=request, wait_target="advance")
        elif result["status"] == "stopped":
            update.update(
                phase="finalize",
                completion={
                    "valid": False,
                    "summary": result.get("reason", "Plan stopped"),
                },
            )
        elif result["status"] == "complete":
            update["phase"] = "finalize"
        return {"data": update}

    def wait(state):
        data = checked(state)
        request = data["pending"]
        reply = interrupt(deepcopy(request))
        if not isinstance(reply, dict) or (
            (request.get("approval_id") and reply.get("approval_id") != request["approval_id"])
            or (not request.get("approval_id") and reply.get("kind") != request["kind"])
        ):
            raise ValueError("Resume must match the current durable request")
        # This method must verify durable authenticated facts, never grant authority
        # from the checkpoint or raw resume payload. It must be replay-safe.
        services.resume(deepcopy(data), deepcopy(reply))
        return {"data": {**data, "pending": None, "status": "advanced"}}

    def after_step(state):
        status = checked(state)["status"]
        return "wait" if status == "waiting" else "advance" if status == "advanced" else END

    builder = StateGraph(ConditionalState)
    builder.add_node("prepare", prepare)
    builder.add_node("advance", advance)
    builder.add_node("wait", wait)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", after_step)
    builder.add_conditional_edges("advance", after_step)
    builder.add_conditional_edges("wait", lambda state: checked(state)["wait_target"])
    return builder.compile(checkpointer=checkpointer).with_config(
        {
            "recursion_limit": RECURSION_LIMIT,
        }
    )

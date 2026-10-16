"""Bounded business phases with serial routing and checkpointed human waits.

The graph stores plain data. Durable services retain authority, approval provenance,
lease fencing and receipt verification; checkpoint values never grant permissions.
"""

import json
from copy import deepcopy
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from decagent.storage.budget import BudgetExceeded

from .errors import BusinessReconciliationRequired

GRAPH_VERSION = "business-2"
MAX_STEPS = 128
PHASES = frozenset(
    {"prepare", "decide", "plan", "validate", "human", "execute", "verify", "finalize"}
)
DESTINATIONS = (*sorted(PHASES), "human_wait", "budget_wait", "reconciliation_wait", END)


class BusinessState(TypedDict):
    data: dict


BusinessCommand = Command[
    Literal[
        "prepare",
        "decide",
        "plan",
        "validate",
        "human",
        "execute",
        "verify",
        "finalize",
        "human_wait",
        "budget_wait",
        "reconciliation_wait",
        "__end__",
    ]
]


def build_graph(services, checkpointer):
    """Compile a single-path graph against the provided durable checkpointer."""
    task_id = str(services.task["id"])

    def data_from(state):
        data = deepcopy(state.get("data"))
        if not isinstance(data, dict) or data.get("task_id") != task_id:
            raise ValueError("Checkpoint belongs to another business task")
        if data.get("graph_version") != GRAPH_VERSION:
            raise ValueError("Checkpoint business graph version is incompatible")
        steps = data.get("steps", 0)
        if type(steps) is not int or not 0 <= steps <= MAX_STEPS:
            raise ValueError("Checkpoint business step count is invalid")
        return data

    def boundary(data):
        return Command(
            update={
                "data": {
                    **data,
                    "phase": "finalize",
                    "boundary": "step_limit",
                    "route": END,
                    "completion": {"valid": False, "summary": "Business phase limit reached"},
                }
            },
            goto=END,
        )

    def jump(data, target):
        if data.get("steps", 0) >= MAX_STEPS:
            unknown = (
                data.get("reconciliation")
                or (data.get("execution") or {}).get("status") == "unknown"
            )
            if unknown:
                payload = data.get("reconciliation") or {
                    "kind": "reconciliation",
                    "action_id": data.get("action_id"),
                    "reason": "Phase limit reached before the external outcome was verified",
                }
                data = {
                    **data,
                    "boundary": "step_limit",
                    "bounded_reconciliation": True,
                    "completion": {"valid": False, "summary": "Business phase limit reached"},
                    "reconciliation": payload,
                }
                if target == "budget_wait":
                    pass  # A revised budget must still be checked before receipt reconciliation.
                elif (
                    target == "verify"
                    and isinstance(data.get("reconciliation_reply"), dict)
                    and data["reconciliation_reply"].get("kind") == "reconciliation"
                ):
                    pass  # Only explicit receipt reconciliation may re-enter verify at the bound.
                else:
                    target = "reconciliation_wait"
                    data = {**data, "pending": payload, "reconciliation_reply": None}
            elif target != END:
                return boundary(data)
        return Command(update={"data": {**data, "route": target}}, goto=target)

    def checked_update(data, partial):
        if not isinstance(partial, dict):
            raise ValueError("Business phase update must be an object")
        route = partial.get("route")
        if route == "end":
            route = END
        if not isinstance(route, str) or route not in PHASES | {END}:
            raise ValueError("Business phase returned an unsupported route")
        # Services cannot replace task identity or reset the lifetime phase bound.
        merged = {**data, **deepcopy(partial), "steps": data["steps"]}
        data_from({"data": merged})
        if data.get("bounded_reconciliation") and data["phase"] == "verify":
            # Preserve the checked receipt, but prohibit autonomous work beyond the phase bound.
            return boundary(merged)
        return jump(merged, route)

    def wait_for_budget(data, error, target):
        return jump(
            {
                **data,
                "pending": {"kind": "budget", "reason": str(error)},
                "wait_target": target,
            },
            "budget_wait",
        )

    def wait_for_reconciliation(data, error):
        payload = {**deepcopy(error.payload), "kind": "reconciliation"}
        json.dumps(payload, allow_nan=False)
        return jump(
            {**data, "reconciliation": payload, "reconciliation_reply": None, "pending": payload},
            "reconciliation_wait",
        )

    def phase_node(phase):
        def run(state) -> BusinessCommand:
            data = data_from(state)
            bounded_reconciliation = (
                phase == "verify"
                and data.get("bounded_reconciliation")
                and isinstance(data.get("reconciliation_reply"), dict)
                and data["reconciliation_reply"].get("kind") == "reconciliation"
            )
            if data.get("steps", 0) >= MAX_STEPS and not bounded_reconciliation:
                return boundary(data)
            data = {
                **data,
                "phase": phase,
                "steps": data.get("steps", 0) + (0 if bounded_reconciliation else 1),
            }
            try:
                if phase in {"verify", "finalize"}:
                    getattr(services, "reconciliation_guard", services.guard)()
                else:
                    services.guard()
                if phase == "human":
                    request = services.human_request(deepcopy(data))
                    if (
                        not isinstance(request, dict)
                        or request.get("kind") not in {"action", "clarification"}
                        or not isinstance(request.get("approval_id"), str)
                        or not request["approval_id"]
                    ):
                        raise ValueError("Human request must reference a durable business approval")
                    json.dumps(request, allow_nan=False)
                    # This update is checkpointed before the separate interrupt node runs.
                    return jump({**data, "human_request": deepcopy(request)}, "human_wait")
                return checked_update(data, services.run_phase(phase, deepcopy(data)))
            except BudgetExceeded as error:
                return wait_for_budget(data, error, phase)
            except BusinessReconciliationRequired as error:
                return wait_for_reconciliation(data, error)

        return run

    def human_wait(state) -> BusinessCommand:
        data = data_from(state)
        try:
            services.guard()
            request = data["human_request"]
            reply = interrupt(request)
            if not isinstance(reply, dict) or reply.get("approval_id") != request["approval_id"]:
                raise ValueError("Resume must reference the current durable business approval")
            services.guard()
            return checked_update(data, services.human_reply(deepcopy(data), deepcopy(reply)))
        except BudgetExceeded as error:
            return wait_for_budget(data, error, "human_wait")
        except BusinessReconciliationRequired as error:
            return wait_for_reconciliation(data, error)

    def budget_wait(state) -> BusinessCommand:
        data = data_from(state)
        reply = interrupt(data["pending"])
        if not isinstance(reply, dict) or reply.get("kind") != "budget":
            raise ValueError("Budget resume must reference the durable budget request")
        try:
            services.guard()
        except BudgetExceeded as error:
            return wait_for_budget(data, error, data["wait_target"])
        return jump({**data, "budget_reply": deepcopy(reply), "pending": None}, data["wait_target"])

    def reconciliation_wait(state) -> BusinessCommand:
        data = data_from(state)
        reply = interrupt(data["reconciliation"])
        if not isinstance(reply, dict) or reply.get("kind") != "reconciliation":
            raise ValueError("Reconciliation resume must reference the durable request")
        updated = {**data, "reconciliation_reply": deepcopy(reply), "pending": None}
        try:
            getattr(services, "reconciliation_guard", services.guard)()
        except BudgetExceeded as error:
            return wait_for_budget(updated, error, "verify")
        # The verifier reads durable receipts and resources before any possible retry.
        return jump(updated, "verify")

    builder = StateGraph(BusinessState)
    for phase in sorted(PHASES):
        builder.add_node(phase, phase_node(phase), destinations=DESTINATIONS)
    builder.add_node("human_wait", human_wait, destinations=DESTINATIONS)
    builder.add_node("budget_wait", budget_wait, destinations=DESTINATIONS)
    builder.add_node("reconciliation_wait", reconciliation_wait, destinations=DESTINATIONS)
    builder.add_edge(START, "prepare")
    # Commands own all further routing; adding static edges would run both branches.
    return builder.compile(checkpointer=checkpointer).with_config(
        {"recursion_limit": MAX_STEPS + 8}
    )

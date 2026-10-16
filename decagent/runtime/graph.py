"""Serial, checkpointed tool loop with a separate replay-safe approval node.

Services provide trusted current snapshots, budgeted model calls, fenced broker operations,
and independent verification. Graph state is plain JSON and never grants permissions itself.
"""

from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from decagent.policy import Action, Outcome, govern
from decagent.policy.canonical import digest, primitive
from decagent.policy.models import ApprovalStatus, CodeTrustRequest, TrustKind
from decagent.storage.budget import BudgetExceeded

from .operations import ReproductionUnavailable, WaitingRequired
from .progress import (
    accept_feedback,
    begin_replan,
    observe_progress,
    progress_signals,
    route_progress,
)

GRAPH_VERSION = "1"


class TaskState(TypedDict, total=False):
    task_id: str
    graph_version: str
    phase: str
    messages: list[dict]
    iterations: int
    proposal_revision: int
    queue: list[dict]
    action: dict
    action_id: str
    digest: str
    execution_id: str
    approval_id: str | None
    route: str
    observation: dict
    verification: dict
    decision_id: str | None
    pending: dict
    wait_target: str
    progress: dict
    intervention_decision: dict


def build_graph(services, checkpointer):
    def fence():
        return services.database.assert_lease(
            services.task_id,
            services.lease_owner,
            services.generation,
        )

    def prepare(state):
        fence()
        services.database.check_version(GRAPH_VERSION)
        if state.get("graph_version", GRAPH_VERSION) != GRAPH_VERSION:
            raise ValueError("Checkpoint graph version is incompatible")
        if state["task_id"] != services.task_id:
            raise ValueError("Checkpoint belongs to a different task")
        try:
            if hasattr(services, "prepare_phase"):
                services.prepare_phase()
        except WaitingRequired as waiting:
            return {"pending": waiting.payload, "route": "wait", "wait_target": "prepare"}
        return {
            "graph_version": GRAPH_VERSION,
            "phase": state.get("phase", "prepare"),
            "route": "model",
        }

    def model(state):
        fence()
        maximum = services.database.get_task(services.task_id)["budget"].get("max_calls", 30)
        if state.get("iterations", 0) >= maximum:
            return {"route": "budget"}
        try:
            turn = services.invoke_model(state["messages"])
        except BudgetExceeded:
            return {"route": "budget"}
        except WaitingRequired as waiting:
            return {"pending": waiting.payload, "route": "wait", "wait_target": "model"}
        return {
            "messages": [*state["messages"], turn.assistant_message()],
            "iterations": state.get("iterations", 0) + 1,
            "queue": [
                {"id": t.id, "tool": t.name, "params": primitive(t.params)} for t in turn.tools
            ],
            "proposal_revision": 0,
            "phase": "repair",
            "route": "propose" if turn.tools else "verify",
        }

    def propose(state):
        fence()
        call = state["queue"][0]
        action = Action(call["tool"], call["params"])
        context = services.current_context()
        if hasattr(services, "assess_action"):
            decision, decision_id = services.assess_action(action, context)
        else:
            decision, decision_id = govern(action, context), None
        if decision.digest is None:
            return {
                "action": {"tool": call["tool"], "params": call["params"], "call_id": call["id"]},
                "observation": {"status": "denied", "reason": decision.reason},
                "route": "observe",
            }
        normalized = primitive(decision.action)
        normalized["call_id"] = call["id"]
        logical_key = (
            f"turn:{state['iterations']}:call:{call['id']}:"
            f"revision:{state.get('proposal_revision', 0)}"
        )
        record = services.database.plan_action(
            services.task_id,
            logical_key,
            decision.digest,
            primitive(decision.action),
        )
        execution_id = str(
            uuid5(NAMESPACE_URL, f"{services.task_id}:{logical_key}:{decision.digest}")
        )
        route = "execute" if decision.outcome == Outcome.ALLOW else "approval"
        if decision.outcome == Outcome.DENY:
            route = "observe"
        return {
            "action": normalized,
            "action_id": str(record["id"]),
            "digest": decision.digest,
            "execution_id": execution_id,
            "approval_id": None,
            "route": route,
            "observation": {"status": "denied", "reason": decision.reason},
            "decision_id": decision_id,
        }

    def approval(state):
        # Only idempotent proposal upsert precedes interrupt. Never execute or invoke a model here.
        action = Action(state["action"]["tool"], state["action"]["params"])
        context = services.current_context()
        current = govern(action, context)
        if current.outcome == Outcome.DENY:
            return {
                "observation": {"status": "denied", "reason": current.reason},
                "route": "observe",
            }
        if current.digest != state["digest"]:
            return {"route": "propose", "proposal_revision": state.get("proposal_revision", 0) + 1}
        kind = "code_trust" if current.outcome == Outcome.CODE_TRUST_REQUIRED else "action"
        approval_digest = state["digest"]
        trust_request = None
        if kind == "code_trust":
            trust_request = CodeTrustRequest(
                context.repository, TrustKind.EXACT_COMMIT, context.snapshot.head_sha
            )
            approval_digest = trust_request.digest(context)
        record = services.database.ensure_approval(
            services.task_id,
            state["action_id"],
            approval_digest,
            kind,
        )
        if trust_request is not None:
            with services.database.connect() as conn:
                conn.execute(
                    "UPDATE app.approvals SET reply_payload=%s WHERE id=%s AND status='pending'",
                    (Jsonb({"code_trust_request": primitive(trust_request)}), record["id"]),
                )
        response = interrupt(
            {
                "approval_id": str(record["id"]),
                "digest": approval_digest,
                "kind": kind,
                "action": state["action"],
                "reason": current.reason,
            }
        )
        if not isinstance(response, dict) or response.get("approval_id") != str(record["id"]):
            raise ValueError("Resume must reference the current durable approval")
        approval_record = services.approvals.read(record["id"])
        if kind == "code_trust":
            if approval_record.status == ApprovalStatus.REJECTED:
                return {
                    "observation": {"status": "denied", "reason": "Owner rejected code trust"},
                    "route": "observe",
                }
            # The owner gateway separately issues a versioned trust scope; always re-govern.
            return {"route": "propose", "proposal_revision": state.get("proposal_revision", 0) + 1}
        if approval_record.edited_action is not None:
            updated = [
                {"id": state["action"]["call_id"], **primitive(approval_record.edited_action)},
                *state["queue"][1:],
            ]
            return {
                "queue": updated,
                "route": "propose",
                "proposal_revision": state.get("proposal_revision", 0) + 1,
            }
        checked = govern(action, services.current_context(), approval=approval_record)
        if checked.outcome == Outcome.ALLOW:
            return {"approval_id": str(record["id"]), "route": "execute"}
        if checked.outcome == Outcome.DENY:
            return {
                "observation": {"status": "denied", "reason": checked.reason},
                "route": "observe",
            }
        return {"route": "approval"}

    def execute(state):
        fence()
        with services.database.connect() as conn:
            record = conn.execute(
                "SELECT * FROM app.actions WHERE id=%s AND task_id=%s",
                (state["action_id"], services.task_id),
            ).fetchone()
        if not record or record["digest"] != state["digest"]:
            raise ValueError("Action record differs from checkpoint")
        if record["status"] in {"succeeded", "failed"} and record["receipt"]:
            return {"observation": record["receipt"], "route": "observe"}
        if record["status"] in {"running", "unknown"}:
            return {"route": "uncertain"}
        action = Action(state["action"]["tool"], state["action"]["params"])
        approved = (
            services.approvals.read(state["approval_id"]) if state.get("approval_id") else None
        )
        context = services.current_context()
        checked = (
            services.check_action(
                action, context, approval=approved, decision_id=state.get("decision_id")
            )
            if hasattr(services, "check_action")
            else govern(action, context, approval=approved)
        )
        if checked.outcome != Outcome.ALLOW or checked.digest != state["digest"]:
            return {"route": "propose", "proposal_revision": state.get("proposal_revision", 0) + 1}
        try:
            if hasattr(services, "before_execute"):
                services.before_execute(checked.action)
        except WaitingRequired as waiting:
            return {"pending": waiting.payload, "route": "wait", "wait_target": "execute"}
        except ReproductionUnavailable as error:
            return {
                "observation": {"status": "denied", "reason": str(error)},
                "route": "observe",
            }
        with services.database.connect() as conn:
            services.database.assert_lease(
                services.task_id, services.lease_owner, services.generation, conn
            )
            if approved:
                services.approvals.claim(
                    approved.id, checked.digest, state["execution_id"], connection=conn
                )
            conn.execute(
                "UPDATE app.actions SET status='running',receipt=%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "execution_id": state["execution_id"],
                            "status": "unknown",
                            "phase": "dispatch",
                        }
                    ),
                    record["id"],
                ),
            )
        result = services.execute(checked.action, record, state["execution_id"])
        with services.database.connect() as conn:
            services.database.assert_lease(
                services.task_id,
                services.lease_owner,
                services.generation,
                conn,
            )
            status = result.get("status", "unknown")
            if status not in {"succeeded", "failed", "unknown"}:
                status = "unknown"
            conn.execute(
                "UPDATE app.actions SET status=%s,receipt=%s WHERE id=%s",
                (
                    status,
                    Jsonb(services.database.redactor.sanitize(result, surface="evidence")),
                    record["id"],
                ),
            )
        return {"observation": result, "route": "observe" if status != "unknown" else "uncertain"}

    def observe(state):
        safe = services.database.redactor.sanitize(state["observation"], surface="model")
        import json

        result = {
            "role": "tool",
            "tool_call_id": state["action"]["call_id"],
            "content": json.dumps(safe, ensure_ascii=False),
        }
        services.database.event_once(
            services.task_id,
            f"tool:{state['iterations']}:{state['action']['call_id']}",
            "tool_result",
            {
                "call_id": state["action"]["call_id"],
                "result": safe,
            },
        )
        remaining = state["queue"][1:]
        progress = observe_progress(
            state.get("progress"),
            state["action"],
            safe,
            step_id=f"tool:{state['iterations']}:{state['action']['call_id']}",
        )
        services.database.event_once(
            services.task_id,
            "progress:" + progress["steps"][-1]["id"],
            "progress.observed",
            progress["steps"][-1],
        )
        if (
            state["action"]["tool"] == "request_user"
            and safe.get("status") == "succeeded"
            and isinstance(safe.get("owner_response"), str)
            and safe["owner_response"].strip()
        ):
            feedback_id = digest(
                "decagent.owner-tool-feedback.v1",
                {
                    "task_id": services.task_id,
                    "action_id": state["action_id"],
                    "answer": safe["owner_response"],
                },
            )
            progress = accept_feedback(progress, feedback_id)
            services.database.event_once(
                services.task_id,
                "help-feedback:" + feedback_id,
                "intervention.feedback",
                {
                    "feedback_id": feedback_id,
                    "answer": safe["owner_response"],
                    "source": "request_user",
                },
            )
        fence()
        return {
            "messages": [*state["messages"], result],
            "queue": remaining,
            "proposal_revision": 0,
            "progress": progress,
            "route": "propose" if remaining else "progress",
        }

    def progress(state):
        fence()
        observed = state.get("progress", {})
        signals = progress_signals(observed)
        if signals["needs_check"] and hasattr(services, "assess_progress"):
            decision = services.assess_progress(observed)
        else:
            decision = route_progress(observed)
        fence()
        route = decision["route"]
        next_node = {
            "continue": "model",
            "replan": "replan",
            "clarify": "help",
            "escalate": "help",
            "reconcile": "uncertain",
        }[route]
        if signals["needs_check"]:
            observed = {**observed, "checked_total": observed["total"]}
        return {"progress": observed, "intervention_decision": decision, "route": next_node}

    def replan(state):
        fence()
        observed = begin_replan(state["progress"])
        import json

        instruction = (
            "Runtime strategy adjustment (does not grant owner authorization): the current "
            "approach repeated old evidence. State a different testable hypothesis and choose "
            "a different useful next step. Respect every denied action and existing scope; "
            "do not evade safeguards. Recent observed attempts: "
            + json.dumps(observed["steps"][-6:], ensure_ascii=False)
        )
        services.database.event_once(
            services.task_id,
            f"replan:{observed['total']}:{observed['replans']}",
            "intervention.replan",
            {
                "round": observed["replans"],
                "observed_steps": observed["total"],
                "instruction": instruction,
            },
        )
        return {
            "progress": observed,
            "queue": [],
            "messages": [*state["messages"], {"role": "user", "content": instruction}],
            "route": "model",
        }

    def help_owner(state):
        fence()
        if not hasattr(services, "request_help"):
            raise ValueError("Human intervention requires a durable feedback service")
        try:
            feedback = services.request_help(state["progress"], state["intervention_decision"])
        except WaitingRequired as waiting:
            fence()
            return {"pending": waiting.payload, "route": "wait", "wait_target": "help"}
        fence()
        observed = accept_feedback(state["progress"], feedback["feedback_id"])
        messages = state["messages"]
        if observed != state["progress"]:
            messages = [*messages, {"role": "user", "content": feedback["answer"]}]
        return {"progress": observed, "messages": messages, "queue": [], "route": "model"}

    def uncertain(state):
        interrupt(
            {
                "kind": "reconciliation",
                "action_id": state["action_id"],
                "reason": "External result is unknown; reconcile receipt before continuing",
            }
        )
        return {"route": "execute"}

    def budget(state):
        interrupt({"kind": "budget", "reason": "Maximum model iterations reached"})
        return {"route": "model"}  # Model node rereads the revised owner-authorized durable budget.

    def verify(state):
        fence()
        try:
            verification = services.verify_and_finalize()
        except WaitingRequired as waiting:
            return {"pending": waiting.payload, "route": "wait", "wait_target": "verify"}
        # A model completion never constitutes repair success.
        return {"phase": "finalize", "verification": verification, "route": "end"}

    def wait(state):
        interrupt(state["pending"])
        # The destination reloads the durable owner reply; resume payload grants no authority.
        return {"route": state["wait_target"]}

    builder = StateGraph(TaskState)
    nodes = {
        "prepare": prepare,
        "model": model,
        "propose": propose,
        "approval": approval,
        "execute": execute,
        "observe": observe,
        "uncertain": uncertain,
        "budget": budget,
        "verify": verify,
        "wait": wait,
        "progress": progress,
        "replan": replan,
        "help": help_owner,
    }
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", lambda state: state["route"], ["model", "wait"])
    for name in (
        "model",
        "propose",
        "approval",
        "execute",
        "observe",
        "uncertain",
        "progress",
        "replan",
        "help",
    ):
        builder.add_conditional_edges(name, lambda state: state["route"], list(nodes))
    builder.add_conditional_edges(
        "verify", lambda state: state["route"], {"end": END, "wait": "wait"}
    )
    builder.add_conditional_edges("wait", lambda state: state["route"], list(nodes))
    builder.add_conditional_edges("budget", lambda state: state["route"], ["model"])
    return builder.compile(checkpointer=checkpointer)

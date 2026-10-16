"""Durable progress judgments and evidence-bound human help, independent of tool permission."""

from psycopg.types.json import Jsonb

from decagent.decisions import ChoiceAnswer, DecisionContext, build_request
from decagent.decisions.durable import durable_decide
from decagent.policy.canonical import digest

from .context import authorization_version
from .operations import WaitingRequired, ask_confirmation
from .progress import ROUTES, progress_signals, route_progress


def help_packet(goal, progress, decision):
    attempts = progress.get("steps", [])[-8:]
    latest = attempts[-1] if attempts else {}
    clarify = decision["route"] == "clarify"
    needed = (
        "请补充正确的业务行为，最好给出一组输入及期望输出。"
        if clarify
        else "请提供尚未尝试的定位线索，例如相关模块、最小复现或一组输入及期望输出。"
    )
    if clarify:
        summary = "当前证据不足以确定业务目标。"
    elif decision["signals"]["stalled"]:
        summary = f"已发出 {progress.get('replans', 0)} 次策略调整提示，仍出现重复观察。"
    else:
        summary = "当前尝试提供的线索仍不足以继续，需要具体协助。"
    question = (
        summary
        + " 最近操作："
        + latest.get("tool", "unknown")
        + " "
        + latest.get("target", "")[:240]
        + "；结果："
        + latest.get("excerpt", "")[:300]
        + "。"
        + needed
    )
    return {
        "summary": summary,
        "question": question,
        "needed_input": needed,
        "subject": {
            "type": "adaptive_intervention",
            "goal": goal[:8192],
            "route": decision["route"],
            "basis": decision["basis"],
            "attempts": attempts,
            "signals": decision["signals"],
            "progress_hash": digest("decagent.progress.v1", progress),
        },
    }


class InterventionCoordinator:
    def __init__(self, database, engine, *, dataset_version="uncalibrated"):
        self.database, self.engine = database, engine
        self.dataset_version = dataset_version

    def assess(self, progress, context, *, goal):
        signals = progress_signals(progress)
        if not signals["needs_check"]:
            return route_progress(progress)
        state = self.database.redactor.sanitize(
            {
                "goal": goal[:8192],
                "attempts": progress["steps"][-8:],
                "progress_signals": signals,
            },
            surface="model",
        )
        request = build_request(
            ("task_intervention",),
            state,
            DecisionContext(
                context.task_id,
                context.project_id,
                context.snapshot.tree_digest,
                digest("decagent.progress-evidence.v1", state),
                context.policy_version,
                authorization_version(context),
                self.dataset_version,
            ),
        )
        judged, record_id = durable_decide(self.database, self.engine, request)
        answer = judged.evaluation.answers.get("task_intervention")
        suggestion = answer.choice if isinstance(answer, ChoiceAnswer) else None
        calibrated = judged.routes.get("task_intervention") in ROUTES
        result = {
            **route_progress(
                progress,
                suggestion=suggestion,
                calibrated=calibrated,
                error=judged.evaluation.error,
            ),
            "decision_id": record_id,
            "strategy": judged.evaluation.strategy,
            "actual_model": judged.evaluation.actual_model,
            "calibrated": calibrated,
        }
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE app.decision_records SET result=result || %s WHERE id=%s AND task_id=%s",
                (Jsonb({"intervention_result": result}), record_id, context.task_id),
            )
        self.database.event_once(
            context.task_id,
            "progress-check:"
            + digest("decagent.intervention.v1", {"record_id": record_id, "result": result}),
            "intervention.checked",
            result,
        )
        return result

    def request_help(self, service, progress, decision):
        packet = help_packet(service.goal, progress, decision)
        packet = self.database.redactor.sanitize(packet, surface="evidence")
        key = packet["subject"]["progress_hash"]
        self.database.event_once(
            service.task_id, "help-request:" + key, "intervention.requested", packet
        )
        try:
            answer = ask_confirmation(service, packet["subject"], packet["question"])
        except WaitingRequired as waiting:
            raise WaitingRequired(
                {**waiting.payload, "reason": packet["summary"], "help": packet}
            ) from waiting
        feedback_id = digest(
            "decagent.intervention-feedback.v1",
            {
                "subject": packet["subject"],
                "answer": answer,
                "context": authorization_version(service.current_context()),
            },
        )
        safe_answer = self.database.redactor.redact_text(answer)
        self.database.event_once(
            service.task_id,
            "help-feedback:" + feedback_id,
            "intervention.feedback",
            {"feedback_id": feedback_id, "answer": safe_answer},
        )
        return {"feedback_id": feedback_id, "answer": safe_answer}

"""Durable wrappers for pure transitions; caller supplies authenticated owner context."""

from contextlib import nullcontext

from psycopg.types.json import Jsonb

from decagent.policy import (
    Action,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    GovernanceDecision,
    Outcome,
    Provenance,
    Source,
    consume_approval,
    reply_to_approval,
)
from decagent.policy.canonical import primitive
from decagent.storage.safe_record import exact_safe_record


def _hydrate(row):
    saved = (row["reply_payload"] or {}).get("policy_record", {})
    if row["status"] != "pending" and not saved:
        raise ValueError("Approval reply has no authenticated operative record; review required")
    provenance = saved.get("provenance")
    if provenance:
        provenance = Provenance(
            Source(provenance["source"]),
            provenance["owner_id"],
            provenance["session_id"],
            provenance["authenticated"],
        )
    edited = saved.get("edited_action")
    edited_action = Action(edited["tool"], edited["params"]) if edited else None
    decision = saved.get("edited_decision")
    edited_decision = (
        GovernanceDecision(
            Outcome(decision["outcome"]),
            decision["reason"],
            decision.get("digest"),
            edited_action,
            decision["hard_denial"],
        )
        if decision
        else None
    )
    return Approval(
        str(row["id"]),
        ApprovalKind(row["kind"]),
        row["digest"],
        ApprovalStatus(saved.get("status", "pending")),
        saved.get("response_id"),
        saved.get("response_fingerprint"),
        provenance,
        saved.get("confirmation"),
        saved.get("execution_id"),
        edited_action,
        edited_decision,
    )


class DurableApprovals:
    def __init__(self, database):
        self.database = database

    def reply(self, approval_id, reply, *, action=None, context=None):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError("Approval not found")
            if context is None or str(row["task_id"]) != context.task_id:
                raise ValueError("Authenticated current task context is required")
            result = reply_to_approval(_hydrate(row), reply, action=action, context=context)
            if not result.replayed:
                safe = exact_safe_record(primitive(result.approval), self.database.redactor)
                conn.execute(
                    "UPDATE app.approvals SET status='replied',reply=%s,reply_payload=%s,"
                    "version=version+1,replied_at=now() WHERE id=%s",
                    (reply.decision.value, Jsonb({"policy_record": safe}), approval_id),
                )
            return result

    def claim(self, approval_id, current_digest, execution_id, connection=None):
        scope = self.database.connect() if connection is None else nullcontext(connection)
        with scope as conn:
            row = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (approval_id,)
            ).fetchone()
            if not row:
                raise KeyError("Approval not found")
            result = consume_approval(_hydrate(row), current_digest, execution_id)
            if not result.replayed:
                conn.execute(
                    "UPDATE app.approvals SET status='consumed',reply_payload=%s WHERE id=%s",
                    (Jsonb({"policy_record": primitive(result.approval)}), approval_id),
                )
            return result

    def read(self, approval_id):
        with self.database.connect() as conn:
            row = conn.execute("SELECT * FROM app.approvals WHERE id=%s", (approval_id,)).fetchone()
            if not row:
                raise KeyError("Approval not found")
            return _hydrate(row)

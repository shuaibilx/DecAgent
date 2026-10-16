"""Replay-safe controller operations share the graph's exact approval and broker boundary."""

from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from decagent.policy import ApprovalKind, Outcome, confirmation_digest, govern
from decagent.policy.canonical import primitive
from decagent.policy.models import ApprovalStatus, CodeTrustRequest, TrustKind
from decagent.tools.repair.broker_backend import GovernedAction
from decagent.tools.repair.models import ExecutionResult


class ReproductionUnavailable(ValueError):
    """A correctable evidence gap; source effects must wait for a failing baseline."""


class OperationJournal:
    def __init__(self, services):
        self.services = services
        self.phase = "prepare"
        self.index = 0

    def begin(self, phase):
        self.phase, self.index = phase, 0

    def authorize(self, action, intent):
        service = self.services
        context = service.current_context()
        checked = govern(
            action, context, verification_intent=intent if hasattr(intent, "checker_hash") else None
        )
        if action.tool in {"read_file", "search", "diff"}:
            if checked.outcome != Outcome.ALLOW:
                raise PermissionError(checked.reason)
            return GovernedAction("", checked.digest)
        prefix = f"controller:{self.phase}:{self.index}:"
        self.index += 1
        snapshot = primitive(checked.action or action)
        if intent:
            snapshot["execution_kind"] = intent.kind
        with service.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM app.actions WHERE task_id=%s AND (starts_with(logical_key,%s) "
                "OR (%s AND digest=%s AND status IN ('succeeded','failed'))) "
                "ORDER BY created_at DESC",
                (service.task_id, prefix, hasattr(intent, "checker_hash"), checked.digest),
            ).fetchall()
        prior = next(
            (
                r
                for r in rows
                if r["snapshot"] == snapshot
                and (
                    r["status"] in {"succeeded", "failed", "running", "unknown"}
                    or r["digest"] == checked.digest
                )
            ),
            None,
        )
        if prior:
            if prior["snapshot"] != snapshot:
                raise ValueError("Controller operation changed during replay")
            cached = prior.get("receipt") or {}
            if prior["status"] in {"succeeded", "failed"}:
                output = cached.get("output", "")
                if cached.get("output_ref"):
                    from decagent.storage.evidence import EvidenceRef

                    output = service.evidence.read(EvidenceRef(**cached["output_ref"]))
                result = ExecutionResult(
                    cached["execution_id"],
                    cached.get("state", "completed"),
                    cached.get("exit_code"),
                    output,
                    cached.get("truncated", False),
                )
                return GovernedAction(
                    str(prior["id"]), prior["digest"], result.execution_id, result
                )
            if prior["status"] in {"running", "unknown"}:
                raise WaitingRequired(
                    {
                        "kind": "reconciliation",
                        "action_id": str(prior["id"]),
                        "reason": "Controller command result is unknown; reconcile before resuming",
                    }
                )
        if checked.outcome == Outcome.DENY:
            raise PermissionError(checked.reason)
        key = prefix + checked.digest
        record = prior or service.database.plan_action(
            service.task_id, key, checked.digest, snapshot
        )
        if record["digest"] != checked.digest:
            raise ValueError("Current scope changed; controller approval is stale")
        execution_id = str(uuid5(NAMESPACE_URL, f"{service.task_id}:{key}:{checked.digest}"))
        approval = None
        if checked.outcome != Outcome.ALLOW:
            kind = "code_trust" if checked.outcome == Outcome.CODE_TRUST_REQUIRED else "action"
            approval_digest = checked.digest
            subject = None
            if kind == "code_trust":
                subject = CodeTrustRequest(
                    context.repository, TrustKind.EXACT_COMMIT, context.snapshot.head_sha
                )
                approval_digest = subject.digest(context)
            pending = service.database.ensure_approval(
                service.task_id, record["id"], approval_digest, kind
            )
            if subject:
                with service.database.connect() as conn:
                    conn.execute(
                        "UPDATE app.approvals SET reply_payload=%s WHERE id=%s "
                        "AND status='pending'",
                        (Jsonb({"code_trust_request": primitive(subject)}), pending["id"]),
                    )
            if pending["status"] == "pending":
                raise WaitingRequired(
                    {
                        "kind": kind,
                        "approval_id": str(pending["id"]),
                        "digest": approval_digest,
                        "action": snapshot,
                        "reason": checked.reason,
                    }
                )
            approval = service.approvals.read(pending["id"])
            if approval.status == ApprovalStatus.REJECTED:
                raise PermissionError("Owner rejected the controller operation")
            if subject:
                context = service.current_context()
                checked = govern(
                    action,
                    context,
                    verification_intent=intent if hasattr(intent, "checker_hash") else None,
                )
                raise ValueError("Code trust update requires the gateway and a fresh operation")
            else:
                checked = govern(
                    action,
                    service.current_context(),
                    approval=approval,
                    verification_intent=intent if hasattr(intent, "checker_hash") else None,
                )
        if checked.outcome != Outcome.ALLOW:
            raise PermissionError(checked.reason)
        with service.database.connect() as conn:
            service.fence(conn)
            if approval:
                service.approvals.claim(approval.id, checked.digest, execution_id, connection=conn)
            conn.execute(
                "UPDATE app.actions SET status='running',receipt=%s WHERE id=%s",
                (Jsonb({"execution_id": execution_id, "status": "unknown"}), record["id"]),
            )
        return GovernedAction(str(record["id"]), checked.digest, execution_id)

    def complete(self, grant, result):
        service = self.services
        reference = service.evidence.write(result.output, "executor:" + result.execution_id)
        service.database.register_evidence(reference)
        status = (
            ("succeeded" if result.exit_code == 0 else "failed")
            if (result.state == "completed")
            else "unknown"
        )
        receipt = {
            "execution_id": result.execution_id,
            "state": result.state,
            "exit_code": result.exit_code,
            "truncated": result.truncated,
            "output_ref": asdict(reference),
            "status": status,
        }
        with service.database.connect() as conn:
            service.fence(conn)
            conn.execute(
                "UPDATE app.actions SET status=%s,receipt=%s WHERE id=%s AND task_id=%s",
                (status, Jsonb(receipt), grant.action_id, service.task_id),
            )


class WaitingRequired(Exception):
    def __init__(self, payload):
        super().__init__(payload["reason"])
        self.payload = payload


def ask_confirmation(service, subject, question):
    context = service.current_context()
    expected = confirmation_digest(ApprovalKind.CLARIFICATION, subject, context)
    pending = service.database.ensure_approval(service.task_id, None, expected, "clarification")
    with service.database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET reply_payload=%s WHERE id=%s AND status='pending'",
            (Jsonb({"subject": subject}), pending["id"]),
        )
    service.database.event_once(
        service.task_id,
        "question:" + expected,
        "task.question",
        {
            "question_id": str(pending["id"]),
            "question": question,
            "digest": expected,
            "subject": subject,
        },
    )
    if pending["status"] == "pending":
        raise WaitingRequired(
            {
                "kind": "clarification",
                "approval_id": str(pending["id"]),
                "question_id": str(pending["id"]),
                "digest": expected,
                "question": question,
                "reason": "Owner requirements or evidence relation needed",
            }
        )
    approved = service.approvals.read(pending["id"])
    if approved.status != ApprovalStatus.CONFIRMED or not approved.provenance.trusted_owner:
        raise PermissionError("Owner did not confirm the requirement")
    if approved.digest != expected:
        raise ValueError("Clarification material changed")
    return approved.confirmation

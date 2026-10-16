"""Pure approval transitions. No process-local store claims durable once-only use.

Persist the returned record under SELECT FOR UPDATE or a versioned CAS. Store the
execution claim and outbox entry in that same transaction. Broker idempotency and
external reconciliation still apply; these functions do not guarantee exactly-once
external effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .canonical import digest
from .governance import action_digest, govern
from .models import (
    Action,
    Approval,
    ApprovalKind,
    ApprovalReply,
    ApprovalStatus,
    ApprovalTransition,
    PolicyContext,
    ReplyDecision,
)

CONFIRMATION_KINDS = frozenset(
    {
        ApprovalKind.ACCEPTANCE,
        ApprovalKind.MEMORY_CONFLICT,
        ApprovalKind.CLARIFICATION,
    }
)


def reply_to_approval(
    approval: Approval,
    reply: ApprovalReply,
    *,
    action: Action | None = None,
    context: PolicyContext | None = None,
) -> ApprovalTransition:
    """An edit consumes the proposal and returns a newly governed proposal."""
    if not reply.provenance.trusted_owner:
        raise ValueError("Approval replies require authenticated owner provenance")
    if not reply.response_id or reply.expected_digest != approval.digest:
        raise ValueError("Approval reply digest is stale or response identity is missing")
    if context is not None:
        if reply.provenance.owner_id != context.authorization.provenance.owner_id:
            raise ValueError("Approval reply owner does not match task owner")
    fingerprint = digest("decagent.approval-reply.v1", reply)
    if approval.response_id is not None:
        if approval.response_id == reply.response_id:
            if approval.response_fingerprint != fingerprint:
                raise ValueError("Duplicate approval response conflict")
            return ApprovalTransition(
                approval, True, approval.edited_action, approval.edited_decision
            )
        raise ValueError("Approval has already been answered")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError("Approval has already been answered")
    if reply.decision not in set(ReplyDecision):
        raise ValueError("Unknown approval reply decision")
    edited_action = None
    decision = None
    confirmation = None
    if reply.decision == ReplyDecision.EDIT:
        if (
            approval.kind != ApprovalKind.ACTION
            or action is None
            or context is None
            or reply.edited_params is None
            or reply.confirmation is not None
        ):
            raise ValueError("Editing requires an action, context and replacement parameters")
        if action_digest(action, context) != approval.digest:
            raise ValueError("Action context digest changed before edit")
        edited_action = Action(action.tool, reply.edited_params)
        decision = govern(edited_action, context)
        status = ApprovalStatus.EDITED
    elif reply.edited_params is not None:
        raise ValueError("Only edit replies accept replacement parameters")
    elif reply.decision == ReplyDecision.CONFIRM:
        if approval.kind not in CONFIRMATION_KINDS:
            raise ValueError("This approval kind does not accept confirmation")
        if type(reply.confirmation) is not str or not reply.confirmation.strip():
            raise ValueError("Owner confirmation text is required")
        if len(reply.confirmation) > 65536:
            raise ValueError("Owner confirmation exceeds limits")
        confirmation = reply.confirmation
        status = ApprovalStatus.CONFIRMED
    elif reply.decision == ReplyDecision.APPROVE:
        if approval.kind not in {ApprovalKind.ACTION, ApprovalKind.CODE_TRUST}:
            raise ValueError("This approval kind requires explicit confirmation")
        if reply.confirmation is not None:
            raise ValueError("Approve replies do not create confirmation content")
        status = ApprovalStatus.APPROVED
    else:
        if reply.confirmation is not None:
            raise ValueError("Reject replies do not create confirmation content")
        status = ApprovalStatus.REJECTED
    record = replace(
        approval,
        status=status,
        response_id=reply.response_id,
        response_fingerprint=fingerprint,
        provenance=reply.provenance,
        confirmation=confirmation,
        edited_action=edited_action,
        edited_decision=decision,
    )
    return ApprovalTransition(record, False, edited_action, decision)


def consume_approval(
    approval: Approval,
    expected_digest: str,
    execution_id: str,
) -> ApprovalTransition:
    """Claim one approved action/trust decision for one durable operation id."""
    if approval.digest != expected_digest:
        raise ValueError("Approval execution digest is stale")
    if (
        approval.status != ApprovalStatus.APPROVED
        or approval.kind not in {ApprovalKind.ACTION, ApprovalKind.CODE_TRUST}
        or approval.provenance is None
        or not approval.provenance.trusted_owner
    ):
        raise ValueError("Execution requires an approved authenticated owner record")
    if type(execution_id) is not str or not execution_id:
        raise ValueError("Execution identity is required")
    if approval.execution_id is not None:
        if approval.execution_id == execution_id:
            return ApprovalTransition(approval, replayed=True)
        raise ValueError("Approval already consumed by a different execution")
    return ApprovalTransition(replace(approval, execution_id=execution_id))


def confirmation_digest(
    kind: ApprovalKind,
    subject: Mapping[str, Any],
    context: PolicyContext,
) -> str:
    """Bind an acceptance, design/memory, or clarification subject to its versions."""
    if kind not in CONFIRMATION_KINDS or not isinstance(subject, Mapping) or not subject:
        raise ValueError("Confirmation requires an explicit subject and confirmation kind")
    return digest(
        "decagent.confirmation.v1",
        {
            "kind": kind,
            "subject": subject,
            "task_id": context.task_id,
            "project_id": context.project_id,
            "repository": context.repository,
            "base_sha": context.base_sha or context.snapshot.head_sha,
            "snapshot": context.snapshot,
            "authorization": context.authorization,
            "policy_version": context.policy_version,
        },
    )


def valid_confirmation(
    approval: Approval,
    kind: ApprovalKind,
    expected_digest: str,
    owner_id: str,
) -> bool:
    """Check a trusted database record; never accept a model-supplied Approval DTO."""
    return (
        kind in CONFIRMATION_KINDS
        and approval.kind == kind
        and approval.status == ApprovalStatus.CONFIRMED
        and approval.digest == expected_digest
        and bool(approval.confirmation)
        and approval.provenance is not None
        and approval.provenance.trusted_owner
        and approval.provenance.owner_id == owner_id
        and bool(approval.response_id)
        and bool(approval.response_fingerprint)
    )

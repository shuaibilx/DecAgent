"""Explicit code trust; repository provenance is supplied by the control plane."""

from __future__ import annotations

import re

from .canonical import digest
from .models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    CodeOrigin,
    CodeTrust,
    CodeTrustRequest,
    PolicyContext,
    TrustKind,
)


def valid_sha(value: str) -> bool:
    return type(value) is str and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None


def trust_covers(context: PolicyContext) -> bool:
    trust, snapshot = context.code_trust, context.snapshot
    if (
        trust is None
        or trust.active is not True
        or not trust.provenance.trusted_owner
        or trust.repository != context.repository
        or not valid_sha(trust.sha)
    ):
        return False
    if trust.provenance.owner_id != context.authorization.provenance.owner_id:
        return False
    if trust.kind == TrustKind.EXACT_COMMIT:
        return snapshot.head_sha == trust.sha
    return (
        trust.kind == TrustKind.OWN_BRANCH
        and snapshot.origin == CodeOrigin.OWNER
        and snapshot.has_external_commits is False
        and bool(trust.branch)
        and snapshot.branch == trust.branch
    )


def code_trust_digest(request: CodeTrustRequest, context: PolicyContext) -> str:
    if not valid_sha(request.sha) or request.kind not in set(TrustKind):
        raise ValueError("Invalid code trust request")
    if request.kind == TrustKind.OWN_BRANCH and not request.branch:
        raise ValueError("Owner branch trust requires an exact branch")
    if request.kind == TrustKind.EXACT_COMMIT and request.branch is not None:
        raise ValueError("Exact commit trust does not accept a branch scope")
    return digest(
        "decagent.code-trust.v1",
        {
            "request": request,
            "task_id": context.task_id,
            "project_id": context.project_id,
            "repository": context.repository,
            "authorization": context.authorization,
            "policy_version": context.policy_version,
            "snapshot": context.snapshot,
            "base_sha": context.base_sha or context.snapshot.head_sha,
        },
    )


def issue_code_trust(
    request: CodeTrustRequest,
    context: PolicyContext,
    approval: Approval,
    *,
    trust_id: str,
    version: str,
) -> CodeTrust:
    """Called under the same transaction that consumes the code-trust approval."""
    if approval.digest != code_trust_digest(request, context):
        raise ValueError("Code trust approval digest is stale")
    if (
        approval.kind != ApprovalKind.CODE_TRUST
        or approval.status != ApprovalStatus.APPROVED
        or approval.execution_id is not None
        or approval.provenance is None
        or not approval.provenance.trusted_owner
    ):
        raise ValueError("An unused approved owner code-trust record is required")
    if (
        context.authorization.active is not True
        or not context.authorization.provenance.trusted_owner
        or request.repository != context.repository
        or approval.provenance.owner_id != context.authorization.provenance.owner_id
        or (
            context.authorization.task_id,
            context.authorization.project_id,
            context.authorization.repository,
        )
        != (context.task_id, context.project_id, context.repository)
    ):
        raise ValueError("Code trust scope or owner does not match")
    if request.sha != context.snapshot.head_sha:
        raise ValueError("Code trust must bind the current exact code SHA")
    if request.kind == TrustKind.OWN_BRANCH:
        if (
            context.snapshot.origin != CodeOrigin.OWNER
            or context.snapshot.has_external_commits is not False
            or context.snapshot.branch != request.branch
        ):
            raise ValueError("External code cannot acquire owner branch trust")
    if not trust_id or not version:
        raise ValueError("Code trust identity and version are required")
    return CodeTrust(
        trust_id,
        version,
        request.repository,
        request.kind,
        request.sha,
        approval.provenance,
        request.branch,
    )

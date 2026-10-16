"""Immutable policy inputs supplied by the trusted control service.

These records are not authentication mechanisms. The HTTP/worker boundary must load
authorization, snapshot provenance and approvals from authenticated durable records,
never deserialize them from model arguments or repository text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) in (tuple, list):
        return tuple(_freeze(item) for item in value)
    return value


class Source(StrEnum):
    OWNER = "owner"
    REPOSITORY = "repository"
    TOOL = "tool"
    MEMORY = "memory"
    MODEL = "model"


@dataclass(frozen=True)
class Provenance:
    source: Source
    owner_id: str = ""
    session_id: str = ""
    authenticated: bool = False

    @property
    def trusted_owner(self) -> bool:
        return (
            self.source == Source.OWNER
            and self.authenticated is True
            and bool(self.owner_id)
            and bool(self.session_id)
        )


@dataclass(frozen=True)
class OwnerAuthorization:
    id: str
    version: str
    task_id: str
    project_id: str
    repository: str
    provenance: Provenance
    publish_draft: bool = False
    active: bool = True


class CodeOrigin(StrEnum):
    OWNER = "owner"
    EXTERNAL = "external"


@dataclass(frozen=True)
class CodeSnapshot:
    head_sha: str
    tree_digest: str
    branch: str
    origin: CodeOrigin
    has_external_commits: bool = False


class TrustKind(StrEnum):
    EXACT_COMMIT = "exact_commit"
    OWN_BRANCH = "own_branch"


@dataclass(frozen=True)
class CodeTrust:
    id: str
    version: str
    repository: str
    kind: TrustKind
    sha: str
    provenance: Provenance
    branch: str | None = None
    active: bool = True


@dataclass(frozen=True)
class ExecutionBoundary:
    """Fresh broker attestations, not flags accepted from tool calls."""

    task_isolated: bool = False
    network_verified: bool = False
    credentials_isolated: bool = False
    resource_limits_verified: bool = False

    @property
    def verified(self) -> bool:
        return all(
            value is True
            for value in (
                self.task_isolated,
                self.network_verified,
                self.credentials_isolated,
                self.resource_limits_verified,
            )
        )


@dataclass(frozen=True)
class PolicyContext:
    task_id: str
    project_id: str
    repository: str
    workspace_root: Path
    snapshot: CodeSnapshot
    authorization: OwnerAuthorization
    policy_version: str
    code_trust: CodeTrust | None = None
    execution_boundary: ExecutionBoundary = field(default_factory=ExecutionBoundary)
    base_sha: str = ""
    default_branch: str = "main"


@dataclass(frozen=True)
class Action:
    tool: str
    params: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", _freeze(self.params))


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"
    CODE_TRUST_REQUIRED = "code_trust_required"


@dataclass(frozen=True)
class GovernanceDecision:
    outcome: Outcome
    reason: str
    digest: str | None = None
    action: Action | None = None
    hard_denial: bool = False


@dataclass(frozen=True)
class SemanticPermission:
    """Internal capability issued from a calibrated durable decision, never a model DTO."""

    decision_id: str
    action_digest: str
    task_id: str
    code_version: str
    authorization_version: str
    policy_version: str


class ApprovalKind(StrEnum):
    ACTION = "action"
    CODE_TRUST = "code_trust"
    ACCEPTANCE = "acceptance"
    MEMORY_CONFLICT = "memory_conflict"
    CLARIFICATION = "clarification"


class ReplyDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"
    CONFIRM = "confirm"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class Approval:
    id: str
    kind: ApprovalKind
    digest: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    response_id: str | None = None
    response_fingerprint: str | None = None
    provenance: Provenance | None = None
    confirmation: str | None = None
    execution_id: str | None = None
    edited_action: Action | None = None
    edited_decision: GovernanceDecision | None = None


@dataclass(frozen=True)
class ApprovalReply:
    response_id: str
    expected_digest: str
    decision: ReplyDecision
    provenance: Provenance
    edited_params: Mapping[str, Any] | None = None
    confirmation: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edited_params", _freeze(self.edited_params))


@dataclass(frozen=True)
class ApprovalTransition:
    approval: Approval
    replayed: bool = False
    edited_action: Action | None = None
    decision: GovernanceDecision | None = None


@dataclass(frozen=True)
class CodeTrustRequest:
    repository: str
    kind: TrustKind
    sha: str
    branch: str | None = None

    def digest(self, context: PolicyContext) -> str:
        from .trust import code_trust_digest

        return code_trust_digest(self, context)

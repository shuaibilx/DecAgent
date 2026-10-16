"""One entry point for all tool proposals, including resumed and memory actions.

Budget reservation must precede this entry point. Decisions carry no capability to
execute: the caller rechecks the snapshot and claims execution durably immediately
before asking the broker to perform a permitted action.
"""

from __future__ import annotations

import re
from types import MappingProxyType

from .canonical import digest
from .models import (
    Action,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    GovernanceDecision,
    Outcome,
    PolicyContext,
    SemanticPermission,
)
from .paths import normalize_relative_path, safe_workspace_path, sensitive_path
from .tools import READ_TOOLS, validate_tool
from .trust import trust_covers, valid_sha


def normalized_action(action: Action, context: PolicyContext) -> Action:
    params = dict(validate_tool(action.tool, action.params))
    if "path" in params:
        params["path"] = normalize_relative_path(params["path"])
    if "paths" in params:
        params["paths"] = tuple(normalize_relative_path(path) for path in params["paths"])
    if "cwd" in params:
        params["cwd"] = normalize_relative_path(params["cwd"])
    if action.tool == "publish_draft":
        params["branch"] = params["branch"] or f"decagent/{context.task_id}"
    return Action(action.tool, MappingProxyType(params))


def action_digest(action: Action, context: PolicyContext) -> str:
    """Bind tool, arguments, repository/task, tree, authorization and policy.

    Lexically forbidden proposals can still be shown to the owner with an exact
    digest; hashing is not permission. Unknown or malformed schemas cannot be hashed.
    """
    params = dict(validate_tool(action.tool, action.params))
    # Do not normalize forbidden paths into innocent-looking ones.
    for name in ("path", "cwd"):
        if name in params:
            try:
                params[name] = normalize_relative_path(params[name])
            except ValueError:
                pass
    if "paths" in params:
        normalized = []
        for path in params["paths"]:
            try:
                normalized.append(normalize_relative_path(path))
            except ValueError:
                normalized.append(path)
        params["paths"] = tuple(normalized)
    if action.tool == "publish_draft":
        params["branch"] = params["branch"] or f"decagent/{context.task_id}"
    return digest(
        "decagent.action.v1",
        {
            "tool": action.tool,
            "params": params,
            "task_id": context.task_id,
            "project_id": context.project_id,
            "repository": context.repository,
            "base_sha": context.base_sha or context.snapshot.head_sha,
            "snapshot": context.snapshot,
            "authorization": context.authorization,
            "code_trust": context.code_trust,
            "policy_version": context.policy_version,
            "execution_boundary": context.execution_boundary,
            "default_branch": context.default_branch,
            "workspace_root": str(context.workspace_root.absolute()),
        },
    )


def _context_error(context: PolicyContext) -> str | None:
    auth = context.authorization
    if not auth.provenance.trusted_owner or auth.active is not True:
        return "Task authorization requires an active authenticated owner record"
    if (auth.task_id, auth.project_id, auth.repository) != (
        context.task_id,
        context.project_id,
        context.repository,
    ):
        return "Authorization scope does not match this task and repository"
    if not all(
        (
            auth.id,
            auth.version,
            context.task_id,
            context.project_id,
            context.repository,
            context.policy_version,
        )
    ):
        return "Authorization identity and versions are required"
    if any(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None
        for value in (context.task_id, context.project_id)
    ):
        return "Task and project identities must be opaque identifiers"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", context.repository) is None:
        return "Repository must be a registered owner/name identity"
    if (
        not valid_sha(context.snapshot.head_sha)
        or not valid_sha(context.snapshot.tree_digest)
        or (context.base_sha and not valid_sha(context.base_sha))
    ):
        return "Current code snapshot identifiers are invalid"
    return None


def _check_command(action: Action, context: PolicyContext, verification_intent=None) -> str | None:
    argv = action.params["argv"]
    executable = argv[0]
    # No shell, arbitrary executable paths, Docker API or raw Git mutations.
    # These names are a capability boundary, not a proof of command semantics.
    if executable not in {"python", "python3", "pip", "pytest", "ruff", "uv"}:
        return "Executable is outside the supported Python runner tools"
    reserved_checker = "decagent_check" in argv or any(
        arg.startswith("-") and "mdecagent_check" in arg for arg in argv[1:]
    )
    if reserved_checker:
        if verification_intent is None:
            return "The fixed verifier is reserved for trusted validation intents"
        from decagent.tools.repair.session import validate_validation_intent

        try:
            validate_validation_intent(action, verification_intent)
        except (ValueError, TypeError, KeyError):
            return "Frozen verification intent does not match this action"
    elif verification_intent is not None:
        return "Verification intent requires the fixed verifier command"
    if executable == "uv" and (len(argv) < 2 or argv[1] not in {"sync", "pip", "lock"}):
        return "uv supports dependency preparation only; external tool execution is forbidden"
    if any(
        arg.casefold() in {"docker", "podman", "sh", "bash", "powershell", "cmd.exe"}
        for arg in argv[1:]
    ):
        return "Nested shell or container management command is forbidden"
    if executable in {"python", "python3"} and any(
        arg == "-c" or arg.startswith("-c") for arg in argv[1:]
    ):
        return "Inline code is outside the approved repository code scope"
    for arg in argv[1:]:
        if any(ord(char) < 32 or ord(char) == 127 for char in arg):
            return "Control characters in command arguments"
        candidate = arg.split("=", 1)[-1] if arg.startswith("-") and "=" in arg else arg
        if candidate.startswith(("https://", "http://")):
            continue  # The broker proxy validates every actual resolved destination.
        if candidate.startswith(("/", "\\")) or ":" in candidate:
            return "Absolute or device paths in command arguments are forbidden"
        if ".." in candidate.replace("\\", "/").split("/") or sensitive_path(candidate):
            return "Command arguments reference an excluded path"
    if not context.execution_boundary.verified:
        return "Verified task, network, credential and resource isolation is required"
    return None


def govern(
    action: Action,
    context: PolicyContext,
    *,
    approval: Approval | None = None,
    semantic_advice: str | None = None,
    semantic_permission: SemanticPermission | None = None,
    publication_proof=None,
    verification_intent=None,
) -> GovernanceDecision:
    """Hard checks always precede trust, semantic advice and exact approval."""
    try:
        validated = normalized_action(action, context)
        bound_digest = action_digest(validated, context)
        reason = _context_error(context)
        if reason:
            return GovernanceDecision(Outcome.DENY, reason, bound_digest, validated, True)
        paths = []
        if "path" in validated.params:
            paths.append(validated.params["path"])
        paths.extend(validated.params.get("paths", ()))
        if "cwd" in validated.params:
            paths.append(validated.params["cwd"])
        for path in paths:
            safe_workspace_path(context.workspace_root, path)
        if validated.tool == "publish_draft":
            if context.authorization.publish_draft is not True:
                reason = "Draft publication was not authorized for this task"
            elif validated.params["branch"] != f"decagent/{context.task_id}":
                reason = "Draft publication must use this task's dedicated branch"
            else:
                reason = None
            if reason:
                return GovernanceDecision(Outcome.DENY, reason, bound_digest, validated, True)
        if validated.tool == "run_command":
            reason = _check_command(validated, context, verification_intent)
            if reason:
                return GovernanceDecision(Outcome.DENY, reason, bound_digest, validated, True)
            if not trust_covers(context):
                return GovernanceDecision(
                    Outcome.CODE_TRUST_REQUIRED,
                    "Current code requires explicit owner trust",
                    bound_digest,
                    validated,
                )
    except (ValueError, OSError, RuntimeError) as error:
        # Reasons are deliberately small and never echo arguments or filesystem data.
        reason = (
            str(error) if isinstance(error, ValueError) else "Workspace path cannot be verified"
        )
        return GovernanceDecision(Outcome.DENY, reason, hard_denial=True)
    if semantic_advice not in (None, "allow", "deny", "review"):
        return GovernanceDecision(
            Outcome.REVIEW, "Invalid semantic advice", bound_digest, validated
        )
    if semantic_advice == "deny":
        return GovernanceDecision(
            Outcome.DENY, "Semantic assessment denied the action", bound_digest, validated
        )
    if approval is not None and approval.kind == ApprovalKind.ACTION:
        if approval.digest == bound_digest:
            if approval.status in {ApprovalStatus.REJECTED, ApprovalStatus.EDITED}:
                return GovernanceDecision(
                    Outcome.DENY,
                    "Owner rejected or replaced this proposal",
                    bound_digest,
                    validated,
                )
            if approval.execution_id is not None:
                return GovernanceDecision(
                    Outcome.DENY, "Approval already claimed for execution", bound_digest, validated
                )
            if (
                approval.status == ApprovalStatus.APPROVED
                and approval.provenance is not None
                and approval.provenance.trusted_owner
                and approval.provenance.owner_id == context.authorization.provenance.owner_id
            ):
                return GovernanceDecision(
                    Outcome.ALLOW, "Exact owner action approval", bound_digest, validated
                )
    if validated.tool in READ_TOOLS and semantic_advice != "review":
        return GovernanceDecision(Outcome.ALLOW, "Scoped static read", bound_digest, validated)
    if validated.tool == "publish_draft" and publication_proof is not None:
        from decagent.integrations.github import PublicationBlocked, VerificationGate

        if type(publication_proof) is VerificationGate and all(
            str(row.get("task_id")) == context.task_id for row in publication_proof.records
        ):
            try:
                VerificationGate.check(
                    publication_proof.records,
                    context.base_sha or context.snapshot.head_sha,
                    context.snapshot.tree_digest,
                )
            except PublicationBlocked:
                pass
            else:
                return GovernanceDecision(
                    Outcome.ALLOW, "Task-authorized verified draft", bound_digest, validated
                )
    if type(semantic_permission) is SemanticPermission and (
        bool(semantic_permission.decision_id)
        and semantic_permission.action_digest == bound_digest
        and semantic_permission.task_id == context.task_id
        and semantic_permission.code_version == context.snapshot.tree_digest
        and semantic_permission.authorization_version == context.authorization.version
        and semantic_permission.policy_version == context.policy_version
    ):
        return GovernanceDecision(
            Outcome.ALLOW, "Current calibrated decision", bound_digest, validated
        )
    return GovernanceDecision(
        Outcome.REVIEW, "Conservative policy requires owner review", bound_digest, validated
    )

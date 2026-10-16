"""Approval transitions are pure; persistence must apply them under a row lock."""

from dataclasses import replace

import pytest
from test_policy_boundaries import OWNER, SHA, context

from decagent.policy import (
    Action,
    Approval,
    ApprovalKind,
    ApprovalReply,
    ApprovalStatus,
    Outcome,
    Provenance,
    ReplyDecision,
    Source,
    action_digest,
    consume_approval,
    govern,
    issue_code_trust,
    reply_to_approval,
)


def action():
    return Action("write_file", {"path": "src/a.py", "content": "print(1)"})


def pending(tmp_path, kind=ApprovalKind.ACTION):
    return Approval("approval-1", kind, action_digest(action(), context(tmp_path)))


def reply(decision=ReplyDecision.APPROVE, **kwargs):
    return ApprovalReply("reply-1", "", decision, OWNER, **kwargs)


def approve(tmp_path):
    record = pending(tmp_path)
    return reply_to_approval(record, replace(reply(), expected_digest=record.digest)).approval


def test_digest_is_canonical_and_covers_relevant_versions(tmp_path):
    ctx = context(tmp_path)
    digest = action_digest(action(), ctx)
    reordered = Action("write_file", {"content": "print(1)", "path": "src/a.py"})
    assert action_digest(reordered, ctx) == digest
    variants = [
        replace(ctx, policy_version="policy-2"),
        replace(ctx, authorization=replace(ctx.authorization, version="v2")),
        replace(ctx, snapshot=replace(ctx.snapshot, head_sha="c" * 40)),
        replace(ctx, snapshot=replace(ctx.snapshot, tree_digest="c" * 64)),
        replace(ctx, base_sha="d" * 40),
        replace(ctx, code_trust=replace(ctx.code_trust, version="v2")),
        replace(ctx, repository="owner/other"),
        replace(ctx, task_id="task-2"),
    ]
    for variant in variants:
        assert action_digest(action(), variant) != digest
    assert (
        action_digest(Action("write_file", {"path": "src/a.py", "content": "print(2)"}), ctx)
        != digest
    )


def test_normalized_default_has_same_digest(tmp_path):
    ctx = context(tmp_path)
    assert action_digest(Action("read_file", {"path": "a.py"}), ctx) == action_digest(
        Action("read_file", {"path": "a.py", "max_bytes": 65536}), ctx
    )


def test_approved_action_allows_only_exact_digest(tmp_path):
    approval = approve(tmp_path)
    assert govern(action(), context(tmp_path), approval=approval).outcome == Outcome.ALLOW
    changed = Action("write_file", {"path": "src/a.py", "content": "print(2)"})
    assert govern(changed, context(tmp_path), approval=approval).outcome == Outcome.REVIEW


def test_reject_prevents_execution_and_cannot_be_changed(tmp_path):
    record = pending(tmp_path)
    denied = reply_to_approval(
        record, replace(reply(ReplyDecision.REJECT), expected_digest=record.digest)
    ).approval
    assert govern(action(), context(tmp_path), approval=denied).outcome == Outcome.DENY
    with pytest.raises(ValueError, match="already"):
        reply_to_approval(
            denied, replace(reply(), response_id="reply-2", expected_digest=record.digest)
        )


def test_duplicate_reply_is_idempotent_but_conflicting_reply_is_rejected(tmp_path):
    record = pending(tmp_path)
    response = replace(reply(), expected_digest=record.digest)
    first = reply_to_approval(record, response)
    second = reply_to_approval(first.approval, response)
    assert second.replayed
    assert first.approval == second.approval
    with pytest.raises(ValueError, match="conflict"):
        reply_to_approval(first.approval, replace(response, decision=ReplyDecision.REJECT))


def test_stale_and_untrusted_replies_rejected(tmp_path):
    record = pending(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        reply_to_approval(record, reply())
    for source in (Source.MODEL, Source.REPOSITORY, Source.TOOL, Source.MEMORY):
        forged = replace(
            reply(),
            expected_digest=record.digest,
            provenance=Provenance(source, "owner-1", "session-1", True),
        )
        with pytest.raises(ValueError, match="owner"):
            reply_to_approval(record, forged)


def test_edit_never_approves_old_action_and_requires_regovernance(tmp_path):
    record = pending(tmp_path)
    response = replace(
        reply(ReplyDecision.EDIT),
        expected_digest=record.digest,
        edited_params={"path": "../secret", "content": "x"},
    )
    result = reply_to_approval(record, response, action=action(), context=context(tmp_path))
    assert result.approval.status == ApprovalStatus.EDITED
    assert result.edited_action is not None
    assert result.decision.outcome == Outcome.DENY
    assert result.decision.hard_denial
    assert govern(action(), context(tmp_path), approval=result.approval).outcome == Outcome.DENY


def test_safe_edit_creates_new_digest_and_new_review(tmp_path):
    record = pending(tmp_path)
    response = replace(
        reply(ReplyDecision.EDIT),
        expected_digest=record.digest,
        edited_params={"path": "src/b.py", "content": "x"},
    )
    result = reply_to_approval(record, response, action=action(), context=context(tmp_path))
    assert result.decision.outcome == Outcome.REVIEW
    assert result.decision.digest != record.digest


def test_confirmation_is_recorded_only_for_confirmation_kinds(tmp_path):
    record = pending(tmp_path, ApprovalKind.ACCEPTANCE)
    response = replace(
        reply(ReplyDecision.CONFIRM),
        expected_digest=record.digest,
        confirmation="The previous test is obsolete for this requirement.",
    )
    confirmed = reply_to_approval(record, response).approval
    assert confirmed.status == ApprovalStatus.CONFIRMED
    assert confirmed.confirmation == response.confirmation
    assert govern(action(), context(tmp_path), approval=confirmed).outcome == Outcome.REVIEW
    with pytest.raises(ValueError, match="kind"):
        reply_to_approval(pending(tmp_path), response)


def test_approval_consumption_requires_once_only_execution_claim(tmp_path):
    record = approve(tmp_path)
    first = consume_approval(record, record.digest, "execution-1")
    second = consume_approval(first.approval, record.digest, "execution-1")
    assert second.replayed
    assert first.approval == second.approval
    with pytest.raises(ValueError, match="consumed"):
        consume_approval(first.approval, record.digest, "execution-2")
    with pytest.raises(ValueError, match="digest"):
        consume_approval(record, "wrong", "execution-1")
    assert govern(action(), context(tmp_path), approval=first.approval).outcome == Outcome.DENY


def test_hard_denial_survives_approval(tmp_path):
    dangerous = Action("write_file", {"path": "../secret", "content": "x"})
    ctx = context(tmp_path)
    record = Approval("a", ApprovalKind.ACTION, action_digest(dangerous, ctx))
    approved = reply_to_approval(record, replace(reply(), expected_digest=record.digest)).approval
    assert govern(dangerous, ctx, approval=approved, semantic_advice="allow").hard_denial


def test_code_trust_issued_only_from_exact_code_trust_confirmation(tmp_path):
    from decagent.policy import CodeTrustRequest, TrustKind

    ctx = context(tmp_path)
    request = CodeTrustRequest("owner/repo", TrustKind.EXACT_COMMIT, SHA)
    record = Approval("trust-approval", ApprovalKind.CODE_TRUST, request.digest(ctx))
    approved = reply_to_approval(record, replace(reply(), expected_digest=record.digest)).approval
    trust = issue_code_trust(request, ctx, approved, trust_id="new-trust", version="v2")
    assert trust.sha == SHA
    assert trust.provenance == OWNER
    changed = replace(request, sha="f" * 40)
    with pytest.raises(ValueError, match="digest"):
        issue_code_trust(changed, ctx, approved, trust_id="t", version="v3")


def test_reply_edit_parameters_cannot_mutate_in_persisted_result(tmp_path):
    record = pending(tmp_path)
    params = {"path": "src/b.py", "content": "x"}
    response = replace(
        reply(ReplyDecision.EDIT), expected_digest=record.digest, edited_params=params
    )
    result = reply_to_approval(record, response, action=action(), context=context(tmp_path))
    params["path"] = "../secret"
    assert result.edited_action.params["path"] == "src/b.py"
    assert reply_to_approval(result.approval, response).replayed


def test_workspace_target_change_invalidates_approval(tmp_path):
    ctx = context(tmp_path)
    assert action_digest(action(), replace(ctx, workspace_root=tmp_path.parent)) != action_digest(
        action(), ctx
    )


def test_untrusted_code_trust_approval_cannot_issue_trust(tmp_path):
    from decagent.policy import CodeTrustRequest, TrustKind

    ctx = context(tmp_path)
    request = CodeTrustRequest("owner/repo", TrustKind.EXACT_COMMIT, SHA)
    record = Approval(
        "a",
        ApprovalKind.CODE_TRUST,
        request.digest(ctx),
        ApprovalStatus.APPROVED,
        provenance=Provenance(Source.MEMORY, "owner-1", "session-1", True),
    )
    with pytest.raises(ValueError, match="owner"):
        issue_code_trust(request, ctx, record, trust_id="t", version="v1")


def test_code_trust_cannot_cross_task_authorization_scope(tmp_path):
    from decagent.policy import CodeTrustRequest, TrustKind

    ctx = context(tmp_path)
    ctx = replace(ctx, authorization=replace(ctx.authorization, repository="other/repo"))
    request = CodeTrustRequest("owner/repo", TrustKind.EXACT_COMMIT, SHA)
    record = Approval("a", ApprovalKind.CODE_TRUST, request.digest(ctx))
    approved = reply_to_approval(record, replace(reply(), expected_digest=record.digest)).approval
    with pytest.raises(ValueError, match="scope"):
        issue_code_trust(request, ctx, approved, trust_id="t", version="v1")


@pytest.mark.parametrize(
    "kind", [ApprovalKind.ACCEPTANCE, ApprovalKind.MEMORY_CONFLICT, ApprovalKind.CLARIFICATION]
)
def test_confirmation_binds_subject_versions_and_owner(tmp_path, kind):
    from decagent.policy import confirmation_digest, valid_confirmation

    ctx = context(tmp_path)
    subject = {"requirement_id": "R1", "evidence_sha256": "f" * 64}
    bound = confirmation_digest(kind, subject, ctx)
    record = Approval("a", kind, bound)
    confirmed = reply_to_approval(
        record,
        replace(
            reply(ReplyDecision.CONFIRM),
            expected_digest=bound,
            confirmation="Required behavior is X.",
        ),
    ).approval
    assert valid_confirmation(confirmed, kind, bound, "owner-1")
    assert not valid_confirmation(confirmed, kind, bound, "other-owner")
    assert not valid_confirmation(confirmed, kind, "stale", "owner-1")
    assert confirmation_digest(kind, subject, replace(ctx, policy_version="v2")) != bound
    forged = replace(confirmed, provenance=Provenance(Source.REPOSITORY, "owner-1", "s", True))
    assert not valid_confirmation(forged, kind, bound, "owner-1")

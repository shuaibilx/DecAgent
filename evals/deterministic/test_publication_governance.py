from dataclasses import replace

from test_policy_boundaries import context

from decagent.integrations.github import VerificationGate
from decagent.policy import Action, Outcome, govern


def test_auto_draft_requires_current_task_verification_proof(tmp_path):
    ctx = context(tmp_path)
    ctx = replace(ctx, base_sha="a" * 40, snapshot=replace(ctx.snapshot, tree_digest="b" * 64))
    action = Action("publish_draft", {"title": "Fix", "body": "Evidence"})
    rows = tuple(
        {
            "kind": kind,
            "task_id": ctx.task_id,
            "base_sha": ctx.base_sha,
            "tree_hash": ctx.snapshot.tree_digest,
            "checker_hash": "c" * 64,
            "status": "failed" if kind == "baseline" else "passed",
            "environment": {"related_failure": kind == "baseline"},
            "evidence_hash": "d" * 64,
            "test_count": 1,
        }
        for kind in ("baseline", "independent", "regression")
    )
    proof = VerificationGate.check(rows, ctx.base_sha, ctx.snapshot.tree_digest)
    assert govern(action, ctx).outcome == Outcome.REVIEW
    assert govern(action, ctx, publication_proof=proof).outcome == Outcome.ALLOW
    other = VerificationGate(tuple({**r, "task_id": "other"} for r in rows))
    assert govern(action, ctx, publication_proof=other).outcome == Outcome.REVIEW
    stale = replace(ctx, snapshot=replace(ctx.snapshot, tree_digest="e" * 64))
    assert govern(action, stale, publication_proof=proof).outcome == Outcome.REVIEW

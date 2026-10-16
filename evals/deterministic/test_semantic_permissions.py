from dataclasses import replace

from test_policy_boundaries import context

from decagent.policy import Action, Outcome, action_digest, govern


def test_only_typed_current_permission_can_enable_calibrated_action(tmp_path):
    from decagent.policy.models import SemanticPermission

    ctx = context(tmp_path)
    action = Action("write_file", {"path": "fixed.py", "content": "fixed"})
    assert govern(action, ctx, semantic_advice="allow").outcome == Outcome.REVIEW
    permit = SemanticPermission(
        "decision-id",
        action_digest(action, ctx),
        ctx.task_id,
        ctx.snapshot.tree_digest,
        ctx.authorization.version,
        ctx.policy_version,
    )
    assert govern(action, ctx, semantic_permission=permit).outcome == Outcome.ALLOW
    stale = replace(permit, code_version="c" * 64)
    assert govern(action, ctx, semantic_permission=stale).outcome == Outcome.REVIEW
    forbidden = Action("write_file", {"path": "../outside", "content": "fixed"})
    assert govern(forbidden, ctx, semantic_permission=permit).outcome == Outcome.DENY


def test_permission_does_not_replace_code_trust(tmp_path):
    from decagent.policy.models import SemanticPermission

    ctx = replace(context(tmp_path), code_trust=None)
    action = Action("run_command", {"argv": ("python", "main.py")})
    permit = SemanticPermission(
        "decision-id",
        action_digest(action, ctx),
        ctx.task_id,
        ctx.snapshot.tree_digest,
        ctx.authorization.version,
        ctx.policy_version,
    )
    assert govern(action, ctx, semantic_permission=permit).outcome == Outcome.CODE_TRUST_REQUIRED

"""Governance rejects dangerous inputs before consulting a semantic judge."""

from dataclasses import replace
from pathlib import Path

import pytest

from decagent.policy import (
    Action,
    CodeOrigin,
    CodeSnapshot,
    CodeTrust,
    ExecutionBoundary,
    Outcome,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
    TrustKind,
    govern,
    validate_tool,
)

SHA = "a" * 40
TREE = "b" * 64
OWNER = Provenance(Source.OWNER, "owner-1", "session-1", authenticated=True)


def context(tmp_path: Path) -> PolicyContext:
    return PolicyContext(
        task_id="task-1",
        project_id="project-1",
        repository="owner/repo",
        workspace_root=tmp_path,
        snapshot=CodeSnapshot(SHA, TREE, "feature", CodeOrigin.OWNER),
        authorization=OwnerAuthorization(
            "auth-1",
            "v1",
            "task-1",
            "project-1",
            "owner/repo",
            OWNER,
            publish_draft=True,
        ),
        policy_version="policy-1",
        code_trust=CodeTrust(
            "trust-1",
            "v1",
            "owner/repo",
            TrustKind.OWN_BRANCH,
            SHA,
            OWNER,
            branch="feature",
        ),
        execution_boundary=ExecutionBoundary(True, True, True, True),
    )


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("read_file", {"path": "a.py", "max_bytes": True}),
        ("read_file", {"path": "a.py", "unexpected": 1}),
        ("read_file", {"path": 2}),
        ("read_file", {"path": "a.py", "max_bytes": 0}),
        ("search", {"query": "x", "paths": "src"}),
        ("search", {"query": "x", "max_results": 10001}),
        ("write_file", {"path": "a.py", "content": 1}),
        ("edit_file", {"path": "a.py", "old": "", "new": "x"}),
        ("run_command", {"argv": "pytest"}),
        ("run_command", {"argv": ["pytest"], "env": {"TOKEN": "secret"}}),
        ("run_command", {"argv": ["pytest"], "timeout_seconds": 601}),
        ("publish_draft", {"title": "t", "body": "b", "branch": "main"}),
        ("unknown_tool", {}),
    ],
)
def test_strict_tool_parameters(tool, params):
    with pytest.raises(ValueError):
        validate_tool(tool, params)


def test_schema_normalizes_defaults_and_is_independent_of_input_mutation():
    params = {"argv": ["pytest", "-q"]}
    validated = validate_tool("run_command", params)
    params["argv"].append("--disable-warnings")
    assert validated["argv"] == ("pytest", "-q")
    assert validated["cwd"] == "."
    assert validated["timeout_seconds"] == 120


@pytest.mark.parametrize(
    "path",
    [
        "../other/a.py",
        "/etc/passwd",
        "C:/Users/a",
        "\\\\host\\share",
        "a/../b",
        "a\x00b",
        ".env",
        ".env.production",
        ".git/config",
        ".ssh/id_rsa",
        "keys/private.pem",
        "CON",
        "a:stream",
        "src/.aws/credentials",
    ],
)
def test_forbidden_paths_are_hard_denied_even_when_judge_allows(tmp_path, path):
    decision = govern(
        Action("read_file", {"path": path}), context(tmp_path), semantic_advice="allow"
    )
    assert decision.outcome == Outcome.DENY
    assert decision.hard_denial


def test_symlink_escape_and_secret_alias_rejected(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    secret = tmp_path / ".env"
    secret.write_text("secret")
    try:
        (tmp_path / "escape").symlink_to(outside)
        (tmp_path / "alias").symlink_to(secret)
    except OSError:
        pytest.skip("Platform does not permit symbolic links")
    for path in ("escape", "alias"):
        result = govern(Action("read_file", {"path": path}), context(tmp_path))
        assert result.outcome == Outcome.DENY
        assert result.hard_denial


def test_safe_static_read_of_untrusted_external_code(tmp_path):
    ctx = replace(
        context(tmp_path),
        code_trust=None,
        snapshot=CodeSnapshot(SHA, TREE, "pr/1", CodeOrigin.EXTERNAL),
    )
    assert govern(Action("read_file", {"path": "setup.py"}), ctx).outcome == Outcome.ALLOW
    assert (
        govern(Action("run_command", {"argv": ["pip", "install", "."]}), ctx).outcome
        == Outcome.CODE_TRUST_REQUIRED
    )


@pytest.mark.parametrize(
    "field,value",
    [("repository", "other/repo"), ("task_id", "task-2"), ("project_id", "project-2")],
)
def test_authorization_cannot_cross_scope(tmp_path, field, value):
    ctx = replace(context(tmp_path), **{field: value})
    result = govern(Action("read_file", {"path": "src/a.py"}), ctx)
    assert result.outcome == Outcome.DENY
    assert result.hard_denial


@pytest.mark.parametrize("source", [Source.REPOSITORY, Source.TOOL, Source.MEMORY, Source.MODEL])
def test_untrusted_sources_cannot_authorize_actions(tmp_path, source):
    ctx = context(tmp_path)
    forged = Provenance(source, "owner-1", "session-1", authenticated=True)
    ctx = replace(ctx, authorization=replace(ctx.authorization, provenance=forged))
    result = govern(Action("read_file", {"path": "src/a.py"}), ctx)
    assert result.outcome == Outcome.DENY


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "ps"],
        ["git", "push", "--force", "origin", "feature"],
        ["git", "push", "origin", "HEAD:main"],
        ["git", "merge", "feature"],
        ["git", "-c", "core.hooksPath=/tmp", "status"],
        ["sh", "-c", "pytest"],
        ["python", "-c", "print('new inline code')"],
        ["/usr/bin/python", "a.py"],
    ],
)
def test_forbidden_commands_are_hard_denied(tmp_path, argv):
    result = govern(
        Action("run_command", {"argv": argv}), context(tmp_path), semantic_advice="allow"
    )
    assert result.outcome == Outcome.DENY
    assert result.hard_denial


def test_command_boundary_must_be_verified(tmp_path):
    ctx = replace(context(tmp_path), execution_boundary=ExecutionBoundary())
    result = govern(Action("run_command", {"argv": ["pytest"]}), ctx)
    assert result.outcome == Outcome.DENY
    assert result.hard_denial


def test_conservative_mutation_requires_exact_approval_even_with_judge_allow(tmp_path):
    ctx = context(tmp_path)
    for action in (
        Action("write_file", {"path": "src/a.py", "content": "x"}),
        Action("run_command", {"argv": ["pytest", "-q"]}),
        Action("publish_draft", {"title": "Fix", "body": "Evidence"}),
    ):
        assert govern(action, ctx, semantic_advice="allow").outcome == Outcome.REVIEW


def test_external_new_commit_invalidates_exact_code_trust(tmp_path):
    ctx = context(tmp_path)
    trust = CodeTrust("trust-1", "v1", "owner/repo", TrustKind.EXACT_COMMIT, SHA, OWNER)
    ctx = replace(
        ctx, code_trust=trust, snapshot=CodeSnapshot(SHA, TREE, "pr/1", CodeOrigin.EXTERNAL)
    )
    assert govern(Action("run_command", {"argv": ["pytest"]}), ctx).outcome == Outcome.REVIEW
    ctx = replace(ctx, snapshot=replace(ctx.snapshot, head_sha="c" * 40))
    result = govern(Action("run_command", {"argv": ["pytest"]}), ctx)
    assert result.outcome == Outcome.CODE_TRUST_REQUIRED


def test_own_branch_scope_does_not_cover_external_commits_or_other_branches(tmp_path):
    ctx = context(tmp_path)
    for snapshot in (
        replace(ctx.snapshot, branch="another"),
        replace(ctx.snapshot, has_external_commits=True),
        replace(ctx.snapshot, origin=CodeOrigin.EXTERNAL),
    ):
        result = govern(
            Action("run_command", {"argv": ["pytest"]}), replace(ctx, snapshot=snapshot)
        )
        assert result.outcome == Outcome.CODE_TRUST_REQUIRED


def test_publish_permission_and_branch_enforced(tmp_path):
    ctx = context(tmp_path)
    denied = replace(ctx, authorization=replace(ctx.authorization, publish_draft=False))
    assert govern(Action("publish_draft", {"title": "Fix", "body": "b"}), denied).hard_denial
    result = govern(
        Action("publish_draft", {"title": "Fix", "body": "b", "branch": "decagent/task-2"}), ctx
    )
    assert result.hard_denial


def test_resolved_escape_rejected_without_symlink_privileges(tmp_path, monkeypatch):
    original = Path.resolve

    def resolved(path, strict=False):
        if path.name == "escape":
            return tmp_path.parent / "outside"
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolved)
    result = govern(Action("read_file", {"path": "escape"}), context(tmp_path))
    assert result.hard_denial


def test_resolved_secret_alias_rejected_without_symlink_privileges(tmp_path, monkeypatch):
    original = Path.resolve

    def resolved(path, strict=False):
        if path.name == "alias":
            return tmp_path / ".env"
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolved)
    assert govern(Action("read_file", {"path": "alias"}), context(tmp_path)).hard_denial


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "docker", "ps"],
        ["uv", "tool", "run", "remote-code"],
        ["uv", "run", "sh", "-c", "pytest"],
        ["python", "-m", "http.server", "--directory", ".env"],
    ],
)
def test_nested_and_external_executables_are_denied(tmp_path, argv):
    assert govern(Action("run_command", {"argv": argv}), context(tmp_path)).hard_denial


def test_schema_argv_control_characters_are_rejected(tmp_path):
    assert govern(
        Action("run_command", {"argv": ["pytest", "x\nsecret"]}), context(tmp_path)
    ).hard_denial


def test_task_identifier_cannot_inject_publish_branch(tmp_path):
    ctx = context(tmp_path)
    task_id = "../../main"
    ctx = replace(ctx, task_id=task_id, authorization=replace(ctx.authorization, task_id=task_id))
    assert govern(Action("publish_draft", {"title": "x", "body": "y"}), ctx).hard_denial


def test_model_params_cannot_mutate_after_action_construction():
    params = {"argv": ["pytest"]}
    action = Action("run_command", params)
    params["argv"].append("danger")
    assert action.params["argv"] == ("pytest",)
    with pytest.raises(TypeError):
        action.params["argv"] = ("docker",)


def test_approval_from_another_owner_does_not_allow_action(tmp_path):
    from decagent.policy import Approval, ApprovalKind, ApprovalStatus, action_digest

    ctx = context(tmp_path)
    action = Action("write_file", {"path": "a.py", "content": "x"})
    approval = Approval(
        "a",
        ApprovalKind.ACTION,
        action_digest(action, ctx),
        ApprovalStatus.APPROVED,
        provenance=replace(OWNER, owner_id="other-owner"),
    )
    assert govern(action, ctx, approval=approval).outcome == Outcome.REVIEW

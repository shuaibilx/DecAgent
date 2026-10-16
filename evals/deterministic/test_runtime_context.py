"""Owner branch trust needs authenticated GitHub ownership and exact branch binding."""

from types import SimpleNamespace

import pytest

from decagent.policy import CodeOrigin
from decagent.policy.trust import trust_covers
from decagent.runtime.context import current_context_for


@pytest.mark.parametrize(
    "verified,source,kind,covered",
    [
        (True, "github_authenticated_diagnostic", "own_branch", True),
        (False, "github_authenticated_diagnostic", "own_branch", False),
        (True, "model", "own_branch", False),
        (True, "github_authenticated_diagnostic", "exact_commit", True),
    ],
)
def test_owner_branch_hydration(tmp_path, verified, source, kind, covered):
    project = {
        "id": "project",
        "repository": "owner/repo",
        "base_branch": "main",
        "trust": {
            "code_origin": "owner",
            "ownership_verified": verified,
            "source": source,
            "authenticated_owner_login": "owner",
            "repository_owner_login": "owner",
            "repository_id": 123,
        },
    }
    task = {
        "id": "task",
        "project_id": "project",
        "base_sha": "a" * 40,
        "authorization_snapshot": {
            "id": "auth",
            "version": "1",
            "trust_scope": {"kind": kind, "branch": "main"},
            "provenance": {
                "source": "owner",
                "owner_id": "local-owner",
                "session_id": "session",
                "authenticated": True,
            },
        },
    }
    database = SimpleNamespace(get_task=lambda _: task, get_project=lambda _: project)
    context = current_context_for(
        database,
        SimpleNamespace(data_dir=tmp_path),
        "task",
        snapshot={"tree_digest": "b" * 64, "boundary_verified": True},
    )
    assert trust_covers(context) is covered
    assert (context.snapshot.origin == CodeOrigin.OWNER) is (
        verified and source == "github_authenticated_diagnostic"
    )

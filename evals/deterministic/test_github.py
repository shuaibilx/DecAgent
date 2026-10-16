"""GitHub contracts run against an injected HTTP transport, never a real account."""

import httpx
import pytest

from decagent.integrations.github import (
    FileChange,
    GitHub,
    GitHubError,
    PublicationBlocked,
    VerificationGate,
    task_branch,
)


def test_repository_scope_and_permission_diagnostic():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/repos/owner/project":
            return httpx.Response(
                200, json={"id": 17, "full_name": "owner/project", "default_branch": "main"}
            )
        return httpx.Response(200, json=[])

    api = GitHub(
        "owner/project",
        "private-test-secret",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    diagnostic = api.diagnose()
    assert diagnostic.repository_id == 17
    assert diagnostic.contents_readable and diagnostic.pull_requests_readable
    assert diagnostic.write_permissions_verified is False
    with pytest.raises(ValueError):
        api.get("https://api.github.com/repos/other/project")
    with pytest.raises(ValueError):
        api.get("../other/project")
    assert len(requests) == 3
    assert all(r.headers["x-github-api-version"] == "2026-03-10" for r in requests)


def test_api_errors_do_not_leak_token_and_unsafe_redirect_is_rejected():
    api = GitHub(
        "owner/project",
        "private-test-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={"message": "private-test-secret"})
            )
        ),
    )
    with pytest.raises(GitHubError, match="permissions") as raised:
        api.get("")
    assert "private-test-secret" not in str(raised.value)
    api = GitHub(
        "owner/project",
        "private-test-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(302, headers={"location": "https://evil.example/archive"})
            )
        ),
    )
    with pytest.raises(GitHubError, match="redirect"):
        api.archive("a" * 40)


def valid_records(tree="b" * 64):
    common = {
        "base_sha": "a" * 40,
        "tree_hash": tree,
        "checker_hash": "c" * 64,
        "test_count": 2,
        "evidence_hash": "d" * 64,
        "environment": {"related_failure": True},
        "command": ["pytest"],
    }
    return [
        {**common, "kind": "baseline", "status": "failed"},
        {**common, "kind": "independent", "status": "passed"},
        {**common, "kind": "regression", "status": "passed"},
    ]


@pytest.mark.parametrize(
    "mutation",
    ["baseline", "independent", "regression", "zero", "tree", "checker", "evidence", "relation"],
)
def test_delivery_requires_frozen_independent_and_regression_evidence(mutation):
    records = valid_records()
    if mutation in {"baseline", "independent", "regression"}:
        records = [r for r in records if r["kind"] != mutation]
    else:
        target = records[1]
        field, value = {
            "zero": ("test_count", 0),
            "tree": ("tree_hash", "e" * 64),
            "checker": ("checker_hash", "e" * 64),
            "evidence": ("evidence_hash", None),
            "relation": ("environment", {"related_failure": False}),
        }[mutation]
        (records[0] if mutation == "relation" else target)[field] = value
    with pytest.raises(PublicationBlocked):
        VerificationGate.check(records, "a" * 40, "b" * 64)


def test_gate_scope_branch_and_content_rules():
    gate = VerificationGate.check(valid_records(), "a" * 40, "b" * 64)
    assert len(gate.records) == 3
    assert task_branch("9bbcfb39-5685-42d9-ae2e-1d83eb138091").startswith("decagent/")
    for bad in ["main", "../branch", "", "A" * 40]:
        with pytest.raises(ValueError):
            task_branch(bad)
    for path in ["../secret", ".env", ".git/config", "C:/temp/file", "key.pem"]:
        with pytest.raises(ValueError):
            FileChange(path, b"anything")
    with pytest.raises(ValueError):
        FileChange("module.py", b"password=should-never-be-published")
    assert FileChange("src/module.py", b"return 1").path == "src/module.py"


def test_reconcile_uses_head_base_marker_and_does_not_reopen_closed():
    task_id = "9bbcfb39-5685-42d9-ae2e-1d83eb138091"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "number": 4,
                    "state": "closed",
                    "body": f"<!-- decagent-task:{task_id} -->",
                    "head": {"ref": task_branch(task_id), "repo": {"full_name": "owner/project"}},
                    "base": {"ref": "main", "repo": {"full_name": "owner/project"}},
                    "html_url": "https://github.com/owner/project/pull/4",
                    "draft": True,
                },
            ],
        )

    api = GitHub(
        "owner/project", "secret", client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    found = api.find_pull(task_id, "main")
    assert found["state"] == "closed"
    assert requests[0].url.params["state"] == "all"
    assert requests[0].url.params["head"] == "owner:" + task_branch(task_id)
    assert all(r.method == "GET" for r in requests)


def test_archive_is_fixed_sha_and_redirect_does_not_forward_credentials():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://codeload.github.com/owner/project/legacy.tar.gz/"
                    + "a" * 40
                },
            )
        return httpx.Response(200, content=b"archive-bytes")

    api = GitHub(
        "owner/project",
        "private-secret",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert api.archive("a" * 40) == b"archive-bytes"
    assert "authorization" in requests[0].headers
    assert "authorization" not in requests[1].headers
    with pytest.raises(ValueError):
        api.archive("main")

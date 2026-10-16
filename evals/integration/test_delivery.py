"""Real PostgreSQL receipts, injected GitHub HTTP responses, no external publication."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from decagent.executor.archive import make_archive
from decagent.integrations.delivery import DeliveryService, Publication
from decagent.integrations.github import GitHub, GitHubError, PublicationBlocked, task_branch
from decagent.storage.evidence import EvidenceStore

pytestmark = pytest.mark.integration
BASE, TREE, HEAD, MERGE = "a" * 40, "b" * 40, "c" * 40, "d" * 40


class Server:
    def __init__(self):
        self.calls = []
        self.branch = None
        self.pr = None
        self.lose = None
        self.base = BASE
        self.task_id = None
        self.commit_message = None
        self.blobs = {}

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/repos/owner/project").lstrip("/")
        payload = json.loads(request.content) if request.content else None
        if path == "":
            result = {"id": 12, "full_name": "owner/project", "default_branch": "main"}
        elif path == "contents":
            result = []
        elif path.startswith("tarball/"):
            return httpx.Response(200, content=make_archive({"repository/pkg.py": b"bad\n"}))
        elif path == "git/ref/heads/main":
            result = {"object": {"sha": self.base}}
        elif path.startswith("git/ref/heads/decagent/"):
            if not self.branch:
                return httpx.Response(404)
            result = {"object": {"sha": self.branch}}
        elif path == "git/commits/" + BASE:
            result = {"tree": {"sha": TREE}}
        elif path == "git/commits/" + HEAD:
            result = {
                "tree": {"sha": TREE},
                "parents": [{"sha": BASE}],
                "message": self.commit_message,
            }
        elif path == "git/blobs":
            result = {"sha": "e" * 40}
        elif path == "git/trees":
            self.blobs = {r["path"]: r["sha"] for r in payload["tree"]}
            result = {"sha": TREE}
        elif path == "git/commits":
            self.commit_message = payload["message"]
            result = {"sha": HEAD}
        elif path == "git/refs":
            assert payload["ref"] == "refs/heads/" + task_branch(self.task_id)
            assert "force" not in payload
            self.branch = payload["sha"]
            result = {"object": {"sha": self.branch}}
        elif path == "pulls" and request.method == "GET":
            result = [self.pr] if self.pr else []
        elif path == "pulls" and request.method == "POST":
            assert payload["draft"] is True
            self.pr = {
                "number": 1,
                "html_url": "https://github.com/owner/project/pull/1",
                "state": "open",
                "merged": False,
                "draft": True,
                "body": payload["body"],
                "head": {
                    "sha": HEAD,
                    "ref": task_branch(self.task_id),
                    "repo": {"full_name": "owner/project"},
                },
                "base": {"ref": "main", "repo": {"full_name": "owner/project"}},
            }
            result = self.pr
        elif path == "pulls/1":
            result = self.pr
        elif path.startswith("contents/"):
            file = path.removeprefix("contents/")
            result = {"type": "file", "path": file, "sha": self.blobs[file]}
        else:
            raise AssertionError((request.method, path, payload))
        if path == self.lose and request.method == "POST":
            self.lose = None
            raise httpx.ReadTimeout("Simulated response lost after mutation", request=request)
        return httpx.Response(201 if request.method == "POST" else 200, json=result)


@pytest.fixture
def scenario(database, tmp_path):
    project = database.add_project("owner/project")
    task = database.create_task(project["id"], BASE, {})
    task_id = str(task["id"])
    authorization = {
        "task_id": task_id,
        "project_id": str(project["id"]),
        "repository": "owner/project",
        "publish_draft": True,
        "provenance": {
            "source": "owner",
            "authenticated": True,
            "owner_id": "owner",
            "session_id": "owner-session",
        },
    }
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET authorization_snapshot=%s WHERE id=%s",
            (Jsonb(authorization), task_id),
        )
    lease = database.claim(task_id, "delivery-test")
    evidence = EvidenceStore(tmp_path / "evidence")
    plan = Publication.from_snapshots(
        task_id,
        BASE,
        {"pkg.py": b"bad\n"},
        {"pkg.py": b"fixed\n"},
        issue="Actual bug",
        behavior="Return correct value",
        test_changes="Independent checker retained",
        limitations="Mock GitHub transport",
    )
    for kind, status in [
        ("baseline", "failed"),
        ("independent", "passed"),
        ("regression", "passed"),
    ]:
        reference = evidence.write("verified fixture " + kind, "fixture:" + kind)
        database.register_evidence(reference)
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO app.verification_runs(id,task_id,kind,base_sha,tree_hash,"
                "checker_hash,command,environment,test_count,status,evidence_hash) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,2,%s,%s)",
                (
                    uuid4(),
                    task_id,
                    kind,
                    BASE,
                    plan.tree_hash,
                    "f" * 64,
                    Jsonb(["pytest"]),
                    Jsonb({"related_failure": True}),
                    status,
                    reference.sha256,
                ),
            )
    server = Server()
    server.task_id = task_id
    github = GitHub(
        "owner/project",
        "private-test-secret",
        client=httpx.Client(transport=httpx.MockTransport(server)),
    )
    service = DeliveryService(
        database,
        evidence,
        github,
        author_name="Configured Test Author",
        author_email="test@example.invalid",
        lease_owner="delivery-test",
        lease_generation=lease["lease_generation"],
    )
    return database, server, service, plan


def test_delivery_is_draft_only_and_verified_content_is_bound(scenario):
    _, server, service, plan = scenario
    saved = service.publish(plan)
    assert saved["status"] == "draft"
    assert saved["head_sha"] == HEAD and saved["base_sha"] == BASE
    assert "baseline: failed" in server.pr["body"]
    assert "independent: passed" in server.pr["body"]
    assert service.publish(plan)["id"] == saved["id"]
    assert sum(method == "POST" and path.endswith("/pulls") for method, path in server.calls) == 1
    forged = replace(plan, changes=(replace(plan.changes[0], content=b"unverified\n"),))
    with pytest.raises(PublicationBlocked, match="differs"):
        service.publish(forged)
    assert all(method != "PATCH" for method, _ in server.calls)


@pytest.mark.parametrize("lost_stage", ["git/refs", "pulls"])
def test_lost_response_restart_does_not_duplicate_publication(scenario, lost_stage):
    database, server, service, plan = scenario
    server.lose = lost_stage
    with pytest.raises(GitHubError) as raised:
        service.publish(plan)
    assert raised.value.unknown is True
    assert service._read(plan.task_id)["status"] == "unknown"
    restarted = DeliveryService(
        database,
        service.evidence,
        service.github,
        author_name="Configured Test Author",
        author_email="test@example.invalid",
        lease_owner=service.lease_owner,
        lease_generation=service.lease_generation,
    )
    saved = restarted.publish(plan)
    assert saved["status"] == "draft"
    assert (
        sum(method == "POST" and path.endswith("/" + lost_stage) for method, path in server.calls)
        == 1
    )


def test_unknown_unreachable_commit_stops_and_closed_pr_is_never_reopened(scenario):
    _, server, service, plan = scenario
    server.lose = "git/commits"
    with pytest.raises(GitHubError):
        service.publish(plan)
    with pytest.raises(GitHubError, match="remains unknown"):
        service.publish(plan)
    assert (
        sum(method == "POST" and path.endswith("/git/commits") for method, path in server.calls)
        == 1
    )


def test_base_conflict_missing_permission_and_authorization_block(scenario):
    database, server, service, plan = scenario
    server.base = "9" * 40
    with pytest.raises(PublicationBlocked, match="Base branch advanced"):
        service.publish(plan)
    assert all(method == "GET" for method, _ in server.calls)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET authorization_snapshot='{}' WHERE id=%s", (plan.task_id,)
        )
    with pytest.raises(PublicationBlocked, match="authorization"):
        service.publish(plan)


def test_squash_merge_records_actual_content_and_repeated_poll_is_stable(scenario):
    _, server, service, plan = scenario
    service.publish(plan)
    server.pr.update(state="closed", merged=False)
    closed, reference = service.observe(plan.task_id)
    assert closed["status"] == "closed_unmerged" and reference is None
    assert service.publish(plan)["status"] == "closed_unmerged"
    server.pr.update(merged=True, merge_commit_sha=MERGE)
    merged, reference = service.observe(plan.task_id)
    assert merged["merge_sha"] == MERGE != HEAD
    assert merged["merged_content"]["blobs"] == {"pkg.py": "e" * 40}
    assert service.observe(plan.task_id)[1] == reference
    assert hashlib.sha256(service.evidence.read(reference).encode()).hexdigest() == reference.sha256


def test_owner_reconciliation_reads_existing_draft_without_running_lease(scenario):
    database, server, service, plan = scenario
    server.lose = "pulls"
    with pytest.raises(GitHubError):
        service.publish(plan)
    task = database.get_task(plan.task_id)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='recovering',lease_until=NULL WHERE id=%s", (plan.task_id,)
        )
    posts = sum(method == "POST" for method, _ in server.calls)
    owner_observer = DeliveryService(
        database,
        service.evidence,
        service.github,
        author_name="Owner observer",
        author_email="test@example.invalid",
    )
    row = owner_observer.reconcile_observed(
        plan.task_id,
        expected_generation=task["lease_generation"],
        expected_lease_owner=task["lease_owner"],
    )
    assert row["status"] == "draft" and row["pr_number"] == 1
    assert sum(method == "POST" for method, _ in server.calls) == posts
    with pytest.raises(PublicationBlocked, match="generation"):
        owner_observer.reconcile_observed(
            plan.task_id, expected_generation=999, expected_lease_owner=task["lease_owner"]
        )

"""Durable draft delivery; immutable content, task authorization and evidence are mandatory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.executor.archive import file_mode, read_archive, snapshot_hash
from decagent.integrations.github import (
    FileChange,
    GitHubError,
    PublicationBlocked,
    VerificationGate,
    require_sha,
    task_branch,
)
from decagent.storage.evidence import EvidenceRef


@dataclass(frozen=True)
class Publication:
    task_id: str
    base_sha: str
    tree_hash: str
    changes: tuple[FileChange, ...]
    issue: str
    behavior: str
    test_changes: str
    limitations: str

    @classmethod
    def from_snapshots(
        cls,
        task_id,
        base_sha,
        baseline,
        current,
        *,
        issue,
        behavior,
        test_changes="No test changes",
        limitations="None reported",
    ):
        changes = tuple(
            FileChange(path, current.get(path), executable=file_mode(current, path) == 0o755)
            for path in sorted(set(baseline) | set(current))
            if baseline.get(path) != current.get(path)
            or file_mode(baseline, path) != file_mode(current, path)
        )
        return cls(
            task_id,
            require_sha(base_sha),
            snapshot_hash(current),
            changes,
            issue,
            behavior,
            test_changes,
            limitations,
        )


class DeliveryService:
    def __init__(
        self,
        database,
        evidence,
        github,
        *,
        author_name,
        author_email,
        lease_owner=None,
        lease_generation=None,
    ):
        if (
            not author_name
            or "\n" in author_name
            or "\r" in author_name
            or "@" not in author_email
            or any(c.isspace() for c in author_email)
        ):
            raise ValueError("Configure an explicit commit author name and email")
        self.database = database
        self.evidence = evidence
        self.github = github
        self.author_name, self.author_email = author_name, author_email
        self.lease_owner, self.lease_generation = lease_owner, lease_generation

    def download(self, sha):
        # No host extraction. Broker repeats validation before constructing a task volume.
        return read_archive(self.github.archive(require_sha(sha)), strip_root=True)

    def _fence(self, task_id, conn=None):
        if not self.lease_owner or self.lease_generation is None:
            raise PublicationBlocked("Publication requires a valid worker lease")
        self.database.assert_lease(task_id, self.lease_owner, self.lease_generation, conn=conn)

    def _authorize(self, plan):
        task = self.database.get_task(plan.task_id)
        project = self.database.get_project(task["project_id"])
        auth = task["authorization_snapshot"]
        owner = auth.get("provenance", {})
        if (
            auth.get("publish_draft") is not True
            or auth.get("active", True) is not True
            or owner.get("source") != "owner"
            or owner.get("authenticated") is not True
            or not owner.get("owner_id")
            or not owner.get("session_id")
            or auth.get("task_id") != plan.task_id
            or auth.get("project_id") != str(project["id"])
            or auth.get("repository") != project["repository"]
        ):
            raise PublicationBlocked("Authenticated task draft-publication authorization missing")
        if project["repository"].casefold() != self.github.repository.casefold():
            raise PublicationBlocked("GitHub client does not match task repository")
        if task["base_sha"] != plan.base_sha:
            raise PublicationBlocked("Publication base differs from task base")
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM app.verification_runs WHERE task_id=%s ORDER BY created_at,id",
                (plan.task_id,),
            ).fetchall()
        gate = VerificationGate.check(rows, plan.base_sha, plan.tree_hash)
        for record in gate.records:
            with self.database.connect() as conn:
                ref = conn.execute(
                    "SELECT sha256,size,source FROM app.evidence WHERE sha256=%s",
                    (record["evidence_hash"],),
                ).fetchone()
            if not ref:
                raise PublicationBlocked("Verification evidence reference missing")
            self.evidence.read(EvidenceRef(**ref))
        return task, project, gate

    def _read(self, task_id):
        with self.database.connect() as conn:
            return conn.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s", (task_id,)
            ).fetchone()

    def _save(self, task_id, status, intent, *, head=None, pull=None):
        with self.database.connect() as conn:
            self._fence(task_id, conn)
            row = conn.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not row:
                raise PublicationBlocked("Delivery intent is missing")
            return conn.execute(
                "UPDATE app.deliveries SET status=%s,intent=%s,head_sha=%s,"
                "pr_url=%s,pr_number=%s,updated_at=now() WHERE task_id=%s RETURNING *",
                (
                    status,
                    Jsonb(intent),
                    head or row["head_sha"],
                    pull["html_url"] if pull else row["pr_url"],
                    pull["number"] if pull else row["pr_number"],
                    task_id,
                ),
            ).fetchone()

    def _body(self, plan, gate, head):
        rows = []
        for record in gate.records:
            command = (
                " ".join(record["command"])
                if isinstance(record["command"], list)
                else str(record["command"])
            )
            rows.append(
                f"- {record['kind']}: {record['status']}; {record['test_count']} cases; "
                f"`{command}`; evidence `{record['evidence_hash']}`"
            )
        body = (
            f"<!-- decagent-task:{plan.task_id} -->\n\n{plan.issue}\n\n"
            f"Behavior after this change: {plan.behavior}\n\n"
            f"Base: `{plan.base_sha}`\nHead: `{head}`\n"
            f"Workspace SHA256: `{plan.tree_hash}`\n\n"
            + "\n".join(rows)
            + f"\n\nTest changes: {plan.test_changes}\n\nRemaining limitations: {plan.limitations}"
        )
        safe = self.github.redactor.redact_text(body)
        if f"<!-- decagent-task:{plan.task_id} -->" not in safe:
            raise PublicationBlocked("Delivery identity was lost during sanitization")
        return safe

    def _validate_pull(self, data, task_id, base, head):
        if (
            data.get("head", {}).get("sha") != head
            or data.get("head", {}).get("ref") != task_branch(task_id)
            or data.get("base", {}).get("ref") != base
            or f"<!-- decagent-task:{task_id} -->" not in (data.get("body") or "")
            or type(data.get("number")) is not int
            or data.get("html_url")
            != f"https://github.com/{self.github.repository}/pull/{data['number']}"
        ):
            raise GitHubError("Pull request differs from durable publication intent")
        for side in ("head", "base"):
            if data[side].get("repo", {}).get("full_name", "").casefold() != (
                self.github.repository.casefold()
            ):
                raise GitHubError("Pull request belongs to another repository")

    def publish(self, plan: Publication):
        self._fence(plan.task_id)
        task, project, gate = self._authorize(plan)
        if not plan.changes:
            raise PublicationBlocked("No verified source changes to publish")
        paths = [change.path for change in plan.changes]
        if len(set(paths)) != len(paths):
            raise PublicationBlocked("Duplicate publication paths")
        current = self.download(plan.base_sha)
        for change in plan.changes:
            if change.content is None:
                if change.path not in current:
                    raise PublicationBlocked("Deleted file is absent from the fixed base")
                del current[change.path]
                current.modes.pop(change.path, None)
            else:
                current[change.path] = change.content
                current.modes[change.path] = 0o755 if change.executable else 0o644
        if snapshot_hash(current) != plan.tree_hash:
            raise PublicationBlocked("Published content differs from verified workspace")
        change_hash = hashlib.sha256(
            json.dumps(
                [
                    [
                        change.path,
                        hashlib.sha256(change.content).hexdigest()
                        if change.content is not None
                        else None,
                        change.executable,
                    ]
                    for change in plan.changes
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        diagnostic = self.github.diagnose()
        if task_branch(plan.task_id) in {project["base_branch"], diagnostic.default_branch}:
            raise PublicationBlocked("Default/base branch publication is forbidden")
        existing = self._read(plan.task_id)
        if existing:
            intent = existing["intent"]
            if (
                existing["base_sha"] != plan.base_sha
                or existing["tree_hash"] != plan.tree_hash
                or intent.get("change_hash") != change_hash
            ):
                raise PublicationBlocked("Content changed after delivery started; reverify")
            if existing["status"] in {"draft", "merged", "closed_unmerged"}:
                return existing
        else:
            intent = {
                "change_hash": change_hash,
                "phase": "planned",
                "paths": paths,
                "base_branch": project["base_branch"],
                "blobs": {},
                "author": {
                    "name": self.author_name,
                    "email": self.author_email,
                    "date": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            }
            with self.database.connect() as conn:
                self._fence(plan.task_id, conn)
                conn.execute(
                    "INSERT INTO app.deliveries"
                    "(id,task_id,branch,head_sha,tree_hash,status,base_sha,intent) "
                    "VALUES(%s,%s,%s,%s,%s,'planned',%s,%s)",
                    (
                        uuid4(),
                        plan.task_id,
                        task_branch(plan.task_id),
                        plan.base_sha,
                        plan.tree_hash,
                        plan.base_sha,
                        Jsonb(intent),
                    ),
                )
        # A base movement changes the fixed environment; do not silently rebase or push.
        if self.github.ref(project["base_branch"]) != plan.base_sha:
            raise PublicationBlocked("Base branch advanced; prepare and verify the new base")
        try:
            if intent.get("phase") == "commit_pending" and not intent.get("commit_sha"):
                return self.reconcile(plan.task_id)
            if not intent.get("tree_sha"):
                base_tree = self.github.commit(plan.base_sha)["tree"]["sha"]
                entries = []
                for change in plan.changes:
                    if change.content is not None:
                        text = change.content.decode("utf-8", errors="replace")
                        if self.github.redactor.redact_text(text) != text:
                            raise PublicationBlocked("Configured secret detected in source change")
                        if change.path not in intent["blobs"]:
                            intent["phase"] = "blob_pending"
                            self._save(plan.task_id, "publishing", intent)
                            intent["blobs"][change.path] = self.github.blob(change.content)
                            self._save(plan.task_id, "publishing", intent)
                        sha = intent["blobs"][change.path]
                    else:
                        sha = None
                    entries.append(
                        {
                            "path": change.path,
                            "mode": "100755" if change.executable else "100644",
                            "type": "blob",
                            "sha": sha,
                        }
                    )
                intent["phase"] = "tree_pending"
                self._save(plan.task_id, "publishing", intent)
                intent["tree_sha"] = self.github.tree(base_tree, entries)
                self._save(plan.task_id, "publishing", intent)
            if not intent.get("commit_sha"):
                intent["phase"] = "commit_pending"
                self._save(plan.task_id, "publishing", intent)
                intent["commit_sha"] = self.github.create_commit(
                    f"Fix: {self.github.redactor.redact_text(plan.behavior)[:160]}\n\n"
                    f"DecAgent-Task: {plan.task_id}",
                    intent["tree_sha"],
                    plan.base_sha,
                    intent["author"],
                )
                intent["phase"] = "ref_pending"
                self._save(plan.task_id, "publishing", intent, head=intent["commit_sha"])
            head = intent["commit_sha"]
            branch_head = self.github.ref(task_branch(plan.task_id))
            if branch_head is None:
                self._save(plan.task_id, "publishing", intent, head=head)
                self.github.create_ref(plan.task_id, head, default_branch=diagnostic.default_branch)
            elif branch_head != head:
                raise PublicationBlocked("Task branch changed; no force update is allowed")
            intent["phase"] = "pr_pending"
            self._save(plan.task_id, "publishing", intent, head=head)
            pull = self.github.find_pull(plan.task_id, project["base_branch"])
            if pull is None:
                self._fence(plan.task_id)
                pull = self.github.create_pull(
                    plan.task_id,
                    project["base_branch"],
                    "Fix: " + plan.behavior[:220],
                    self._body(plan, gate, head),
                )
            self._validate_pull(pull, plan.task_id, project["base_branch"], head)
            if pull["state"] == "closed":
                return self._save(plan.task_id, "closed_unmerged", intent, head=head, pull=pull)
            if pull.get("draft") is not True:
                raise GitHubError("Existing PR is not a draft; owner review required")
            intent["phase"] = "draft"
            return self._save(plan.task_id, "draft", intent, head=head, pull=pull)
        except GitHubError as exc:
            self._save(plan.task_id, "unknown" if exc.unknown else "blocked", intent)
            raise

    def reconcile(self, task_id):
        self._fence(task_id)
        row = self._read(task_id)
        if not row:
            raise PublicationBlocked("No durable delivery to reconcile")
        status, intent, head, pull = self._inspect_delivery(task_id, row)
        return self._save(task_id, status, intent, head=head, pull=pull)

    def _inspect_delivery(self, task_id, row):
        intent = dict(row["intent"])
        head = self.github.ref(task_branch(task_id))
        if head is None:
            raise GitHubError("No task branch found; external result remains unknown", unknown=True)
        commit = self.github.commit(head)
        if (
            commit["tree"]["sha"] != intent.get("tree_sha")
            or [r["sha"] for r in commit["parents"]] != [row["base_sha"]]
            or f"DecAgent-Task: {task_id}" not in commit["message"]
        ):
            raise GitHubError("Existing commit does not match task delivery", unknown=True)
        if intent.get("commit_sha") and intent["commit_sha"] != head:
            raise GitHubError("Task branch head changed", unknown=True)
        intent["commit_sha"] = head
        pull = self.github.find_pull(task_id, intent["base_branch"])
        if not pull:
            intent["phase"] = "pr_pending"
            return "publishing", intent, head, None
        self._validate_pull(pull, task_id, intent["base_branch"], head)
        if pull["state"] != "closed" and pull.get("draft") is not True:
            raise GitHubError("Existing PR is not a draft; owner review required")
        status = "closed_unmerged" if pull["state"] == "closed" else "draft"
        intent["phase"] = status
        return status, intent, head, pull

    def reconcile_observed(self, task_id, *, expected_generation, expected_lease_owner):
        """Authenticated API recovery uses GitHub GET only and a local generation CAS."""
        task = self.database.get_task(task_id)
        project = self.database.get_project(task["project_id"])
        if project["repository"].casefold() != self.github.repository.casefold():
            raise PublicationBlocked("Observer repository differs from task scope")
        if (
            task["state"] != "recovering"
            or task["lease_generation"] != expected_generation
            or task["lease_owner"] != expected_lease_owner
        ):
            raise PublicationBlocked("Task generation or recovery state changed")
        row = self._read(task_id)
        if not row:
            raise PublicationBlocked("No durable delivery to reconcile")
        status, intent, head, pull = self._inspect_delivery(task_id, row)
        if pull is None:
            raise GitHubError("Draft result remains unknown; no matching PR observed", unknown=True)
        with self.database.connect() as conn:
            current = conn.execute(
                "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if (
                current["state"] != "recovering"
                or current["lease_generation"] != expected_generation
                or current["lease_owner"] != expected_lease_owner
            ):
                raise PublicationBlocked("Task generation changed during observation")
            saved = conn.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if saved["intent"] != row["intent"] or saved["head_sha"] != row["head_sha"]:
                raise PublicationBlocked("Delivery changed during observation")
            return conn.execute(
                "UPDATE app.deliveries SET status=%s,intent=%s,head_sha=%s,pr_url=%s,"
                "pr_number=%s,updated_at=now() WHERE task_id=%s RETURNING *",
                (status, Jsonb(intent), head, pull["html_url"], pull["number"], task_id),
            ).fetchone()

    def observe(self, task_id):
        """Read-only GitHub polling is allowed after the worker lease is released."""
        row = self._read(task_id)
        if not row or not row["pr_number"]:
            raise PublicationBlocked("No confirmed PR to observe")
        task = self.database.get_task(task_id)
        project = self.database.get_project(task["project_id"])
        if project["repository"].casefold() != self.github.repository.casefold():
            raise PublicationBlocked("Observer repository differs from task scope")
        pull = self.github.pull(row["pr_number"])
        self._validate_pull(pull, task_id, row["intent"]["base_branch"], row["head_sha"])
        if pull.get("merged") is True:
            merge_sha = require_sha(pull.get("merge_commit_sha"))
            blobs = self.github.source_blobs(merge_sha, row["intent"]["paths"])
            reference = self.evidence.write(
                json.dumps(
                    {
                        "repository": project["repository"],
                        "task_id": task_id,
                        "pr_url": row["pr_url"],
                        "merge_sha": merge_sha,
                        "blobs": blobs,
                    },
                    sort_keys=True,
                ),
                "github:verified-merge",
            )
            self.database.register_evidence(reference)
            content = {"blobs": blobs, "evidence_hash": reference.sha256}
            with self.database.connect() as conn:
                saved = conn.execute(
                    "UPDATE app.deliveries SET status='merged',merge_sha=%s,"
                    "merged_content=%s,updated_at=now() WHERE task_id=%s RETURNING *",
                    (merge_sha, Jsonb(content), task_id),
                ).fetchone()
            return saved, reference
        if pull.get("state") == "closed":
            with self.database.connect() as conn:
                row = conn.execute(
                    "UPDATE app.deliveries SET status='closed_unmerged',"
                    "updated_at=now() WHERE task_id=%s RETURNING *",
                    (task_id,),
                ).fetchone()
        return row, None

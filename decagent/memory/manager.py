"""Transactional memory lifecycle with separate evidence, approval and merge gates."""

import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.decisions import DecisionContext, DecisionEngine, RuleStrategy, build_request
from decagent.decisions.durable import durable_decide
from decagent.policy import ApprovalKind
from decagent.policy.approvals import confirmation_digest, valid_confirmation
from decagent.storage.approval_service import DurableApprovals
from decagent.storage.evidence import EvidenceRef

from .contracts import (
    CandidateInput,
    MemoryRecord,
    RetrievalRequest,
    RetrievalResult,
    VerifiedMerge,
)
from .model_calls import run_model

INJECTION = re.compile(
    r"(?:ignore|bypass|override|disable).{0,40}(?:approval|permission|policy|instruction)|"
    r"(?:read|reveal|print|exfiltrate).{0,30}(?:secret|api.?key|credential)|"
    r"(?:忽略|绕过|关闭).{0,20}(?:审批|授权|权限)|(?:读取|泄露|输出).{0,15}(?:密钥|秘密)",
    re.I | re.S,
)


class MemoryManager:
    def __init__(
        self,
        database,
        evidence_store,
        decisions=None,
        *,
        dataset_version="uncalibrated",
        policy_version="1",
    ):
        self.database, self.evidence_store = database, evidence_store
        self.decisions = decisions or DecisionEngine(RuleStrategy())
        self.dataset_version, self.policy_version = dataset_version, policy_version

    @staticmethod
    def candidate_subject(candidate: CandidateInput):
        return {
            "memory": candidate.content.strip(),
            "category": candidate.category,
            "fingerprint": candidate.fingerprint,
            "applicability": candidate.applicability.to_dict(),
        }

    def review_subject(self, candidate_id: str, operation: str):
        record = self.get(candidate_id)
        return {
            "memory_id": record.id,
            "operation": operation,
            "revision": record.revision,
            "content": record.content,
            "conflicts": list(record.conflicts),
        }

    def correction_subject(self, candidate_id: str, replacement: CandidateInput):
        record = self.get(candidate_id)
        return {
            "memory_id": record.id,
            "operation": "correct",
            "revision": record.revision,
            "replacement_fingerprint": replacement.fingerprint,
            "replacement_content": replacement.content,
        }

    def _safe(self, content):
        if self.database.redactor.redact_text(content) != content or "[REDACTED]" in content:
            raise ValueError("sensitive candidate content is blocked")
        if INJECTION.search(content):
            raise ValueError("Memory instruction injection is blocked")

    def _evidence(self, ref, conn):
        row = conn.execute("SELECT * FROM app.evidence WHERE sha256=%s", (ref.sha256,)).fetchone()
        if not row or row["size"] != ref.size:
            raise ValueError("Candidate evidence must be registered with immutable metadata")
        return self.evidence_store.read(ref)

    def _verification(self, task_id, ids, evidence_hash, category, conn):
        if not ids:
            if category == "design":
                return
            raise ValueError("Facts and causes require verification records")
        rows = conn.execute(
            "SELECT * FROM app.verification_runs WHERE task_id=%s AND id=ANY(%s)",
            (task_id, list(ids)),
        ).fetchall()
        if len(rows) != len(ids):
            raise ValueError("Missing or cross-task verification evidence")
        supported = [
            row
            for row in rows
            if row["kind"] == "independent"
            and row["status"] == "passed"
            and row["test_count"] > 0
            and row["evidence_hash"] == evidence_hash
        ]
        if category in {"fact", "cause"} and not supported:
            raise ValueError("Candidate lacks passed independent verification evidence")
        if category == "cause" and not any(
            row["kind"] == "baseline" and row["status"] == "failed" for row in rows
        ):
            raise ValueError("Cause requires reproducible failed baseline verification")
        for row in rows:
            if row["evidence_hash"]:
                evidence = conn.execute(
                    "SELECT * FROM app.evidence WHERE sha256=%s", (row["evidence_hash"],)
                ).fetchone()
                if not evidence:
                    raise ValueError("Verification evidence is unavailable")
                self.evidence_store.read(
                    EvidenceRef(evidence["sha256"], evidence["size"], evidence["source"])
                )

    @staticmethod
    def _context_check(context, project_id, task_id=None):
        if (
            context is None
            or not context.authorization.provenance.trusted_owner
            or (
                not context.authorization.active
                or context.project_id != str(project_id)
                or context.authorization.project_id != str(project_id)
                or context.authorization.task_id != context.task_id
                or (task_id is not None and context.task_id != str(task_id))
            )
        ):
            raise ValueError("Authenticated owner context in this project is required")

    def _confirmation(self, confirmation_id, subject, context, task_id):
        if not confirmation_id or context is None:
            raise ValueError("Explicit owner confirmation is required")
        self._context_check(context, context.project_id, task_id)
        try:
            approval = DurableApprovals(self.database).read(confirmation_id)
            expected = confirmation_digest(ApprovalKind.MEMORY_CONFLICT, subject, context)
            with self.database.connect() as conn:
                row = conn.execute(
                    "SELECT task_id FROM app.approvals WHERE id=%s", (confirmation_id,)
                ).fetchone()
            if (
                not row
                or str(row["task_id"]) != str(task_id)
                or not valid_confirmation(
                    approval,
                    ApprovalKind.MEMORY_CONFLICT,
                    expected,
                    context.authorization.provenance.owner_id,
                )
            ):
                raise ValueError("Invalid confirmation")
        except (KeyError, ValueError) as exc:
            raise ValueError("Missing, stale or unrelated owner confirmation") from exc
        return {
            "id": str(confirmation_id),
            "digest": expected,
            "owner_id": context.authorization.provenance.owner_id,
            "subject": subject,
        }

    def _saved_confirmation(self, metadata):
        saved = metadata.get("_confirmation")
        if not saved:
            return False
        try:
            approval = DurableApprovals(self.database).read(saved["id"])
            return valid_confirmation(
                approval, ApprovalKind.MEMORY_CONFLICT, saved["digest"], saved["owner_id"]
            )
        except (KeyError, ValueError):
            return False

    def _decision(self, question, state, *, task_id, project_id, version, evidence, conn=None):
        request = build_request(
            [question],
            self.database.redactor.sanitize(state, surface="model"),
            DecisionContext(
                str(task_id),
                str(project_id),
                version,
                evidence,
                self.policy_version,
                "1",
                self.dataset_version,
            ),
        )
        result, _ = durable_decide(self.database, self.decisions, request)
        return result

    @staticmethod
    def _record(row, version=None):
        metadata = row["applicability"]
        return MemoryRecord(
            str(row["id"]),
            str(row["project_id"]),
            str(row["source_task_id"]),
            row["category"],
            version["content"] if version else row["content"],
            row["status"],
            metadata.get("_revision", 1),
            row["evidence_hash"],
            metadata,
            row["user_confirmed"],
            tuple(metadata.get("_conflicts", [])),
            str(version["id"]) if version else None,
            version["version"] if version else None,
            version["merge_sha"] if version else None,
        )

    @staticmethod
    def _version(conn, candidate_id):
        return conn.execute(
            "SELECT * FROM app.memory_versions WHERE candidate_id=%s ORDER BY version DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()

    @staticmethod
    def _lock_project(conn, project_id):
        if not conn.execute(
            "SELECT id FROM app.projects WHERE id=%s FOR UPDATE", (project_id,)
        ).fetchone():
            raise KeyError("Project not found")

    @staticmethod
    def _audit(conn, task_id, kind, payload):
        task = conn.execute(
            "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s RETURNING project_id,event_seq",
            (task_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) VALUES(%s,%s,%s,%s,%s)",
            (task_id, task["project_id"], task["event_seq"], kind, Jsonb(payload)),
        )

    def submit(self, candidate: CandidateInput, *, confirmation_id=None, context=None):
        if not isinstance(candidate, CandidateInput):
            raise ValueError("Extraction must produce typed candidates without truth flags")
        self._safe(candidate.content)
        self._safe(candidate.applicability.key)
        for tag in candidate.applicability.tags:
            self._safe(tag)
        confirmation = None
        if candidate.category == "design" or confirmation_id:
            if not confirmation_id:
                raise ValueError("Explicit owner confirmation is required for design memory")
            self._context_check(context, candidate.project_id, candidate.source_task_id)
            confirmation = self._confirmation(
                confirmation_id,
                self.candidate_subject(candidate),
                context,
                candidate.source_task_id,
            )
        with self.database.connect() as conn:
            self._lock_project(conn, candidate.project_id)
            project = conn.execute(
                "SELECT repository FROM app.projects WHERE id=%s", (candidate.project_id,)
            ).fetchone()
            if candidate.source_pr and not re.fullmatch(
                "https://github.com/" + re.escape(project["repository"]) + r"/pull/[1-9][0-9]*",
                candidate.source_pr,
            ):
                raise ValueError("Candidate source PR is outside the project repository")
            task = conn.execute(
                "SELECT * FROM app.tasks WHERE id=%s AND project_id=%s",
                (candidate.source_task_id, candidate.project_id),
            ).fetchone()
            if not task:
                raise ValueError("Candidate source task belongs to another project")
            evidence_text = self._evidence(candidate.evidence, conn)
            self._verification(
                candidate.source_task_id,
                candidate.verification_ids,
                candidate.evidence.sha256,
                candidate.category,
                conn,
            )
            existing = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE project_id=%s AND fingerprint=%s",
                (candidate.project_id, candidate.fingerprint),
            ).fetchone()
            if existing:
                return self._record(existing, self._version(conn, existing["id"]))
            conflicts = conn.execute(
                "SELECT id FROM app.memory_candidates WHERE project_id=%s "
                "AND applicability->>'key'=%s AND content<>%s AND status "
                "NOT IN ('rejected','revoked','superseded') ORDER BY id",
                (candidate.project_id, candidate.applicability.key, candidate.content.strip()),
            ).fetchall()
            conflict_ids = [str(row["id"]) for row in conflicts]
            result = self._decision(
                "memory_write",
                {
                    "candidate": {
                        "category": candidate.category,
                        "content": candidate.content,
                        "applicability": candidate.applicability.to_dict(),
                    },
                    "source_evidence": {
                        "sha256": candidate.evidence.sha256,
                        "excerpt": evidence_text[:2048],
                        "verification_ids": list(candidate.verification_ids),
                    },
                    "confirmation_refs": [confirmation_id] if confirmation else [],
                    "conflicts": conflict_ids,
                },
                task_id=candidate.source_task_id,
                project_id=candidate.project_id,
                version=task["base_sha"],
                evidence=candidate.evidence.sha256,
                conn=conn,
            )
            route = result.routes["memory_write"]
            status = "pending_review"
            if route == "discard":
                status = "rejected"
            elif route == "retain" and not conflict_ids and candidate.category != "design":
                status = "pending_merge" if candidate.source_pr else "task_local"
            metadata = {
                **candidate.applicability.to_dict(),
                "_revision": 1,
                "_source_path": candidate.source_path,
                "_verification_ids": list(candidate.verification_ids),
                "_write_route": route,
                "_conflicts": conflict_ids,
                "_confirmation": confirmation,
                "_history": [{"revision": 1, "status": status, "reason": result.reason}],
            }
            row = conn.execute(
                "INSERT INTO app.memory_candidates(id,project_id,source_task_id,"
                "fingerprint,category,content,source_pr,source_blob,evidence_hash,"
                "user_confirmed,applicability,status) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    candidate.project_id,
                    candidate.source_task_id,
                    candidate.fingerprint,
                    candidate.category,
                    candidate.content.strip(),
                    candidate.source_pr,
                    candidate.source_blob,
                    candidate.evidence.sha256,
                    confirmation is not None,
                    Jsonb(metadata),
                    status,
                ),
            ).fetchone()
            self._audit(
                conn,
                candidate.source_task_id,
                "memory.candidate",
                {
                    "candidate_id": str(row["id"]),
                    "status": status,
                    "conflicts": conflict_ids,
                    "evidence_hash": candidate.evidence.sha256,
                },
            )
            return self._record(row)

    def extract(self, task_id, call, *, context=None, confirmation_ids=None):
        task = self.database.get_task(task_id)
        events = self.database.events(task_id)
        payload = self.database.redactor.sanitize(
            {
                "task_id": str(task_id),
                "project_id": str(task["project_id"]),
                "current_version": task["base_sha"],
                "observations": [
                    {"seq": row["seq"], "kind": row["kind"], "payload": row["payload"]}
                    for row in events[-30:]
                    if row["kind"] not in {"authorization", "approval.pending"}
                ],
                "contract": (
                    "Return evidence-bearing candidates; truth and authority flags are invalid"
                ),
            },
            surface="model",
        )
        output = run_model(self.database, task_id, call, payload, "extract")
        if not isinstance(output, (tuple, list)) or len(output) > 20:
            raise ValueError("Extractor must return a bounded typed candidate sequence")
        for item in output:
            if (
                not isinstance(item, CandidateInput)
                or item.project_id != str(task["project_id"])
                or (item.source_task_id != str(task_id))
            ):
                raise ValueError("Extractor cannot change project or source task")
        confirmation_ids = confirmation_ids or {}
        return tuple(
            self.submit(
                item, confirmation_id=confirmation_ids.get(item.fingerprint), context=context
            )
            for item in output
        )

    def get(self, candidate_id, project_id=None):
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE id=%s", (candidate_id,)
            ).fetchone()
            if not row or (project_id is not None and str(row["project_id"]) != str(project_id)):
                raise KeyError("Memory not found in this project")
            return self._record(row, self._version(conn, candidate_id))

    def task_candidates(self, task_id):
        self.database.get_task(task_id)
        with self.database.connect() as conn:
            return tuple(
                self._record(row, self._version(conn, row["id"]))
                for row in conn.execute(
                    "SELECT * FROM app.memory_candidates WHERE source_task_id=%s "
                    "AND status IN ('task_local','pending_review','pending_merge','active') "
                    "ORDER BY created_at,id",
                    (task_id,),
                ).fetchall()
            )

    def _transition(self, conn, row, status, reason, *, extra=None, owner=None):
        metadata = dict(row["applicability"])
        revision = metadata.get("_revision", 1) + 1
        metadata.update(extra or {})
        metadata["_revision"] = revision
        metadata["_history"] = [
            *metadata.get("_history", []),
            {"revision": revision, "status": status, "reason": reason, "owner": owner},
        ]
        updated = conn.execute(
            "UPDATE app.memory_candidates SET status=%s,applicability=%s WHERE id=%s RETURNING *",
            (status, Jsonb(metadata), row["id"]),
        ).fetchone()
        self._audit(
            conn,
            row["source_task_id"],
            "memory.transition",
            {
                "candidate_id": str(row["id"]),
                "revision": revision,
                "status": status,
                "reason": reason,
                "owner": owner,
            },
        )
        return updated

    def _activation(self, conn, row, *, confirmed_by=None, replaces=None):
        metadata = row["applicability"]
        self._safe(row["content"])
        evidence = conn.execute(
            "SELECT * FROM app.evidence WHERE sha256=%s", (row["evidence_hash"],)
        ).fetchone()
        self._evidence(EvidenceRef(evidence["sha256"], evidence["size"], evidence["source"]), conn)
        self._verification(
            row["source_task_id"],
            metadata.get("_verification_ids", []),
            row["evidence_hash"],
            row["category"],
            conn,
        )
        if row["category"] == "design" and not self._saved_confirmation(metadata):
            raise ValueError("Design confirmation is no longer valid")
        if row["source_blob"]:
            merged = metadata.get("_merge")
            if (
                not merged
                or merged["blob"] != row["source_blob"]
                or (merged["merged_sha"] not in metadata["versions"])
            ):
                raise ValueError("Branch knowledge requires confirmed merged applicable blob")
        elif not confirmed_by:
            raise ValueError("Project constraints require explicit owner review")
        conflicts = [
            identity
            for identity in metadata.get("_conflicts", [])
            if conn.execute(
                "SELECT id FROM app.memory_candidates WHERE id=%s AND project_id=%s "
                "AND status NOT IN ('rejected','revoked','superseded')",
                (identity, row["project_id"]),
            ).fetchone()
        ]
        if conflicts:
            raise ValueError("Unresolved memory conflicts require review and correction")
        latest = self._version(conn, row["id"])
        if row["status"] == "active" and latest:
            return self._record(row, latest)
        if row["status"] in {"revoked", "rejected", "superseded"}:
            raise ValueError("Terminal candidate cannot be reactivated")
        version = conn.execute(
            "INSERT INTO app.memory_versions(id,candidate_id,project_id,version,"
            "content,confirmed_by,merge_sha,source_blob,expires_at,replaces) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                row["id"],
                row["project_id"],
                latest["version"] + 1 if latest else 1,
                row["content"],
                confirmed_by,
                metadata.get("_merge", {}).get("merged_sha"),
                row["source_blob"],
                metadata.get("expires_at"),
                replaces,
            ),
        ).fetchone()
        row = self._transition(
            conn,
            row,
            "active",
            "owner_review" if confirmed_by else "calibrated_retain_verified_merge",
            owner=confirmed_by,
        )
        return self._record(row, version)

    def observe_merge(self, observed: VerifiedMerge):
        if not isinstance(observed, VerifiedMerge):
            raise ValueError("Merge observer must supply a typed verified receipt")
        with self.database.connect() as conn:
            self._lock_project(conn, observed.project_id)
            task = conn.execute(
                "SELECT id FROM app.tasks WHERE id=%s AND project_id=%s",
                (observed.source_task_id, observed.project_id),
            ).fetchone()
            delivery = conn.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s", (observed.source_task_id,)
            ).fetchone()
            if (
                not task
                or not delivery
                or delivery["status"] != "merged"
                or (
                    delivery["pr_url"] != observed.source_pr
                    or delivery["merge_sha"] != observed.merged_sha
                    or (delivery["merged_content"] or {}).get("blobs") != dict(observed.blobs)
                    or (delivery["merged_content"] or {}).get("evidence_hash")
                    != observed.evidence.sha256
                )
            ):
                raise ValueError("Receipt is not bound to a confirmed merged delivery")
            self._evidence(observed.evidence, conn)
            rows = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE project_id=%s "
                "AND source_task_id=%s AND source_pr=%s ORDER BY id FOR UPDATE",
                (observed.project_id, observed.source_task_id, observed.source_pr),
            ).fetchall()
            result = []
            for row in rows:
                if row["status"] in {"active", "revoked", "rejected", "superseded"}:
                    result.append(self._record(row, self._version(conn, row["id"])))
                    continue
                metadata = row["applicability"]
                blob = observed.blobs.get(metadata.get("_source_path"))
                merge = {
                    "merged_sha": observed.merged_sha,
                    "blob": blob,
                    "evidence_hash": observed.evidence.sha256,
                }
                compatible = blob == row["source_blob"]
                if metadata.get("_merge") != merge:
                    bindings = {"_merge": merge}
                    if compatible:
                        bindings.update(
                            {
                                "_source_versions": metadata.get(
                                    "_source_versions", metadata["versions"]
                                ),
                                "versions": [observed.merged_sha],
                            }
                        )
                    row = self._transition(
                        conn, row, "pending_review", "merge_observed", extra=bindings
                    )
                if (
                    compatible
                    and metadata.get("_write_route") == "retain"
                    and not (metadata.get("_conflicts") or row["category"] == "design")
                ):
                    supporting = conn.execute(
                        "SELECT * FROM app.evidence WHERE sha256=%s", (row["evidence_hash"],)
                    ).fetchone()
                    current_decision = self._decision(
                        "memory_write",
                        {
                            "candidate": {
                                "category": row["category"],
                                "content": row["content"],
                                "actual_merge_sha": observed.merged_sha,
                            },
                            "source_evidence": {
                                "sha256": row["evidence_hash"],
                                "excerpt": self.evidence_store.read(
                                    EvidenceRef(
                                        supporting["sha256"],
                                        supporting["size"],
                                        supporting["source"],
                                    )
                                )[:2048],
                                "merge_evidence_hash": observed.evidence.sha256,
                            },
                            "confirmation_refs": [],
                            "conflicts": [],
                        },
                        task_id=row["source_task_id"],
                        project_id=row["project_id"],
                        version=observed.merged_sha,
                        evidence=row["evidence_hash"],
                        conn=conn,
                    )
                    if current_decision.routes["memory_write"] != "retain":
                        result.append(self._record(row, self._version(conn, row["id"])))
                        continue
                    try:
                        result.append(self._activation(conn, row))
                        continue
                    except ValueError:
                        row = self._transition(
                            conn, row, "pending_review", "activation_evidence_invalid"
                        )
                elif not compatible:
                    row = self._transition(
                        conn, row, "pending_review", "merged_content_requires_reverification"
                    )
                result.append(self._record(row, self._version(conn, row["id"])))
            return tuple(result)

    def review(self, candidate_id, operation, *, confirmation_id, context, expected_revision):
        if operation not in {"activate", "reject"}:
            raise ValueError("Unsupported memory review operation")
        record = self.get(candidate_id)
        self._context_check(context, record.project_id)
        confirmation = self._confirmation(
            confirmation_id, self.review_subject(candidate_id, operation), context, context.task_id
        )
        with self.database.connect() as conn:
            self._lock_project(conn, record.project_id)
            row = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE id=%s FOR UPDATE", (candidate_id,)
            ).fetchone()
            if row["applicability"].get("_revision", 1) != expected_revision or (
                expected_revision != record.revision
            ):
                raise ValueError("Memory revision changed; review current sources")
            if operation == "activate":
                metadata = {**row["applicability"], "_review_confirmation": confirmation}
                row = conn.execute(
                    "UPDATE app.memory_candidates SET applicability=%s WHERE id=%s RETURNING *",
                    (Jsonb(metadata), candidate_id),
                ).fetchone()
                return self._activation(conn, row, confirmed_by=confirmation["owner_id"])
            if row["status"] in {"active", "revoked", "superseded"}:
                raise ValueError("An active memory must be revoked instead")
            row = self._transition(
                conn,
                row,
                "rejected",
                "owner_rejected",
                extra={"_review_confirmation": confirmation},
                owner=confirmation["owner_id"],
            )
            return self._record(row)

    def revoke(self, candidate_id, *, expected_revision, context):
        record = self.get(candidate_id)
        self._context_check(context, record.project_id)
        with self.database.connect() as conn:
            self._lock_project(conn, record.project_id)
            row = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE id=%s FOR UPDATE", (candidate_id,)
            ).fetchone()
            if row["applicability"].get("_revision", 1) != expected_revision:
                raise ValueError("Memory revision changed")
            if row["status"] == "revoked":
                return self._record(row, self._version(conn, candidate_id))
            conn.execute(
                "UPDATE app.memory_versions SET revoked_at=COALESCE(revoked_at,now()) "
                "WHERE candidate_id=%s",
                (candidate_id,),
            )
            row = self._transition(
                conn,
                row,
                "revoked",
                "owner_revoked",
                owner=context.authorization.provenance.owner_id,
            )
            return self._record(row, self._version(conn, candidate_id))

    def correct(
        self,
        candidate_id,
        replacement: CandidateInput,
        *,
        confirmation_id,
        context,
        expected_revision,
        candidate_confirmation_id=None,
    ):
        original = self.get(candidate_id)
        self._context_check(context, original.project_id)
        if original.revision != expected_revision or original.status != "active":
            raise ValueError("Memory revision changed or original is already superseded")
        if (
            not isinstance(replacement, CandidateInput)
            or replacement.project_id != original.project_id
        ):
            raise ValueError("Correction must remain in the same project")
        if replacement.applicability.key != original.applicability["key"] or (
            replacement.content.strip() == original.content
        ):
            raise ValueError("Correction requires changed content for the same conflict key")
        confirmation = self._confirmation(
            confirmation_id,
            self.correction_subject(candidate_id, replacement),
            context,
            context.task_id,
        )
        saved = self.submit(
            replacement,
            confirmation_id=candidate_confirmation_id,
            context=context if candidate_confirmation_id else None,
        )
        # Only reconstruct an observation from a durable delivery already verified by the observer.
        with self.database.connect() as conn:
            delivery = conn.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s AND status='merged'",
                (replacement.source_task_id,),
            ).fetchone()
            if delivery:
                details = delivery["merged_content"] or {}
                evidence = conn.execute(
                    "SELECT * FROM app.evidence WHERE sha256=%s", (details.get("evidence_hash"),)
                ).fetchone()
            else:
                evidence = None
        if evidence:
            self.observe_merge(
                VerifiedMerge(
                    replacement.project_id,
                    replacement.source_task_id,
                    delivery["pr_url"],
                    delivery["merge_sha"],
                    details.get("blobs", {}),
                    EvidenceRef(evidence["sha256"], evidence["size"], evidence["source"]),
                )
            )
        with self.database.connect() as conn:
            self._lock_project(conn, original.project_id)
            old = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE id=%s FOR UPDATE", (candidate_id,)
            ).fetchone()
            new = conn.execute(
                "SELECT * FROM app.memory_candidates WHERE id=%s FOR UPDATE", (saved.id,)
            ).fetchone()
            if (
                old["applicability"].get("_revision", 1) != expected_revision
                or old["status"] != "active"
            ):
                raise ValueError("Memory revision changed or original is already superseded")
            metadata = dict(new["applicability"])
            remaining = [
                identity for identity in metadata.get("_conflicts", []) if identity != candidate_id
            ]
            if remaining:
                raise ValueError("Correction has additional unresolved conflicts")
            metadata.update(
                {
                    "_conflicts": [],
                    "_review_confirmation": confirmation,
                    "_replaces_candidate": candidate_id,
                }
            )
            new = conn.execute(
                "UPDATE app.memory_candidates SET applicability=%s WHERE id=%s RETURNING *",
                (Jsonb(metadata), new["id"]),
            ).fetchone()
            old_version = self._version(conn, candidate_id)
            activated = self._activation(
                conn, new, confirmed_by=confirmation["owner_id"], replaces=old_version["id"]
            )
            conn.execute(
                "UPDATE app.memory_versions SET revoked_at=COALESCE(revoked_at,now()) "
                "WHERE candidate_id=%s",
                (candidate_id,),
            )
            self._transition(
                conn,
                old,
                "superseded",
                "owner_corrected",
                extra={
                    "_replacement_candidate": saved.id,
                    "_correction_confirmation": confirmation,
                },
                owner=confirmation["owner_id"],
            )
            return activated

    def retrieve(self, request: RetrievalRequest):
        if not isinstance(request, RetrievalRequest):
            raise ValueError("Retrieval requires a typed scoped request")
        task = self.database.get_task(request.task_id, request.project_id)
        self._safe(request.task_summary)
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT c.*,v.id AS version_id FROM app.memory_candidates c JOIN "
                "app.memory_versions v ON v.candidate_id=c.id AND v.project_id=c.project_id "
                "WHERE c.project_id=%s AND c.status='active' AND v.revoked_at IS NULL "
                "AND (v.expires_at IS NULL OR v.expires_at>now()) AND v.valid_from<=now() "
                "AND c.applicability->'versions' ? %s AND "
                "(c.applicability->'tags') @> %s::jsonb AND "
                "(%s='' OR to_tsvector('simple',v.content) @@ plainto_tsquery('simple',%s) "
                "OR c.id::text=ANY(%s)) ORDER BY v.valid_from DESC,c.id LIMIT %s",
                (
                    request.project_id,
                    request.current_version,
                    Jsonb(list(request.tags)),
                    request.query,
                    request.query,
                    list(request.owner_selected),
                    request.limit,
                ),
            ).fetchall()
        selected, excluded, degraded = [], {}, False
        reason = "no_applicable_memory"
        if rows:
            gating = self._decision(
                "memory_read_needed",
                {
                    "task_summary": request.task_summary,
                    "current_version": request.current_version,
                    "search_terms": request.query,
                },
                task_id=request.task_id,
                project_id=request.project_id,
                version=request.current_version,
                evidence=task["base_sha"],
            )
            reason = gating.reason
            gate = gating.routes["memory_read_needed"]
            degraded = gate == "review"
            for row in rows:
                identity = str(row["id"])
                if INJECTION.search(row["content"]):
                    excluded[identity] = "instruction_injection"
                    continue
                if self.database.redactor.redact_text(row["content"]) != row["content"]:
                    excluded[identity] = "sensitive_content"
                    continue
                explicit = identity in request.owner_selected
                if gate != "retrieve" and not explicit:
                    excluded[identity] = "read_gate_" + gate
                    continue
                if not explicit:
                    relevance = self._decision(
                        "memory_relevant",
                        {
                            "task_summary": request.task_summary,
                            "current_version": request.current_version,
                            "candidate_summary": {
                                "id": identity,
                                "content": row["content"],
                                "evidence_hash": row["evidence_hash"],
                            },
                        },
                        task_id=request.task_id,
                        project_id=request.project_id,
                        version=request.current_version,
                        evidence=row["evidence_hash"],
                    )
                    if relevance.routes["memory_relevant"] != "retrieve":
                        excluded[identity] = "relevance_" + relevance.routes["memory_relevant"]
                        degraded |= relevance.routes["memory_relevant"] == "review"
                        continue
                # Re-read under lock so revocation completed during semantic decisions takes effect.
                with self.database.connect() as conn:
                    current = conn.execute(
                        "SELECT c.* FROM app.memory_candidates c JOIN "
                        "app.memory_versions v ON v.candidate_id=c.id "
                        "WHERE c.id=%s AND c.project_id=%s AND c.status='active' "
                        "AND v.id=%s AND v.revoked_at IS NULL "
                        "AND (v.expires_at IS NULL OR v.expires_at>now()) "
                        "AND c.applicability->'versions' ? %s FOR SHARE OF c,v",
                        (row["id"], request.project_id, row["version_id"], request.current_version),
                    ).fetchone()
                    if current:
                        try:
                            evidence = conn.execute(
                                "SELECT * FROM app.evidence WHERE sha256=%s",
                                (row["evidence_hash"],),
                            ).fetchone()
                            self._evidence(
                                EvidenceRef(
                                    evidence["sha256"], evidence["size"], evidence["source"]
                                ),
                                conn,
                            )
                            selected.append(self._record(current, self._version(conn, row["id"])))
                        except (ValueError, OSError):
                            excluded[identity] = "evidence_integrity_failed"
                            degraded = True
                    else:
                        excluded[identity] = "revoked_or_expired_during_decision"
        result = RetrievalResult(
            tuple(selected), tuple(item.id for item in selected), excluded, degraded, reason
        )
        self.database.event(
            request.task_id,
            "memory.retrieval",
            {
                "current_version": request.current_version,
                "selected": list(result.selected),
                "excluded": excluded,
                "degraded": degraded,
                "reason": reason,
                "authority": "untrusted_data",
            },
        )
        return result

"""Observed verification facts become reviewable candidates, never inferred design decisions."""

import hashlib

from decagent.executor.archive import snapshot_hash
from decagent.memory import Applicability, CandidateInput, VerifiedMerge
from decagent.storage.evidence import EvidenceRef
from decagent.tools.repair.changes import is_test


def git_blob_sha(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def capture_verified_candidates(database, manager, task_id, baseline, files, *, source_pr=None):
    task = database.get_task(task_id)
    with database.connect() as conn:
        run = conn.execute(
            "SELECT * FROM app.verification_runs WHERE task_id=%s AND kind='independent' "
            "AND status='passed' AND test_count>0 ORDER BY created_at DESC,id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not run:
            return []
        if run["tree_hash"] != snapshot_hash(files):
            raise ValueError("Memory candidate differs from the independently verified tree")
        evidence = conn.execute(
            "SELECT * FROM app.evidence WHERE sha256=%s", (run["evidence_hash"],)
        ).fetchone()
    if not evidence:
        raise ValueError("Verified memory source evidence is missing")
    reference = EvidenceRef(evidence["sha256"], evidence["size"], evidence["source"])
    manager.evidence_store.read(reference)
    cases = run["environment"]["cases"]
    candidates = []
    for path, content in sorted(files.items()):
        if not path.endswith(".py") or is_test(path) or baseline.get(path) == content:
            continue
        if len(candidates) >= 8:
            break
        description = (
            f"Source `{path}` at blob `{git_blob_sha(content)}` passed frozen independent "
            "checks: "
            + ", ".join(cases)[:2048]
            + ". This records tested scope, not a causal claim."
        )
        candidate = CandidateInput(
            str(task["project_id"]),
            str(task_id),
            "fact",
            description,
            reference,
            Applicability(
                "verified-source:" + path, tags=("python-repair",), versions=(task["base_sha"],)
            ),
            source_pr=source_pr,
            source_path=path,
            source_blob=git_blob_sha(content),
            verification_ids=(str(run["id"]),),
        )
        try:
            memory = manager.submit(candidate)
        except ValueError as error:
            database.event_once(
                task_id,
                "memory-blocked:" + candidate.fingerprint,
                "memory.candidate_blocked",
                {"source_path": path, "reason": str(error)},
            )
            continue
        candidates.append({"id": memory.id, "status": memory.status})
    return candidates


def observe_task_merge(database, evidence, github, manager, task_id, *, author_name, author_email):
    # DeliveryService first fetches actual GitHub content and persists its verified receipt.
    from decagent.integrations.delivery import DeliveryService

    delivery, reference = DeliveryService(
        database, evidence, github, author_name=author_name, author_email=author_email
    ).observe(task_id)
    if reference is None:
        return delivery
    task = database.get_task(task_id)
    manager.observe_merge(
        VerifiedMerge(
            str(task["project_id"]),
            str(task_id),
            delivery["pr_url"],
            delivery["merge_sha"],
            delivery["merged_content"]["blobs"],
            reference,
        )
    )
    return delivery

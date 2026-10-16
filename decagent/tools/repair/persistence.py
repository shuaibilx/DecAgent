"""Persist publishable facts in the shared verification_runs schema, under the task lease."""

import hashlib
import json
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from .models import ReproductionBundle, VerificationRecord


def verification_rows(record: VerificationRecord, bundle: ReproductionBundle) -> tuple[dict, ...]:
    rows = []
    for kind, evidence, tree, checker in (
        ("baseline", bundle.baseline, bundle.baseline_tree, bundle.checker_hash),
        ("independent", record.independent, record.tree_hash, record.checker_hash),
        ("regression", record.regression, record.tree_hash, bundle.regression_hash),
    ):
        report = evidence.report
        collection_hash = hashlib.sha256(json.dumps(report.collected).encode()).hexdigest()
        if evidence.classification == "environment_error":
            status = "environment_error"
        elif kind == "baseline" and evidence.classification == "related_failure":
            status = "failed"
        elif evidence.classification == "passed" and report.passed:
            status = "passed"
        else:
            status = "unverified"
        if kind == "regression" and "regression_scope_changed_or_empty" in record.reasons:
            status = "unverified"
        if "workspace_changed_during_verification" in record.reasons and kind != "baseline":
            status = "unverified"
        environment = {
            "runner_digest": bundle.runner_digest,
            "python_version": bundle.python_version,
            "toolchain": bundle.toolchain,
            "dependencies": list(bundle.dependencies),
            "repair_sha": record.repair_sha,
            "related_failure": (
                kind == "baseline" and evidence.classification == "related_failure"
            ),
            "collection_hash": collection_hash,
            "cases": list(report.collected),
            "failed_cases": list(report.failed),
            "skip_count": len(report.skipped),
            "xfail_count": len(report.xfailed),
            "error_count": len(report.errors),
            "exit_code": report.exit_code,
            "execution_id": evidence.execution_id,
            "bundle_hash": record.bundle_ref.sha256,
            "runtime": dict(evidence.runtime_environment),
            "reasons": list(record.reasons),
            "acceptance_approval": bundle.acceptance_approval,
        }
        rows.append(
            {
                "kind": kind,
                "task_id": record.task_id,
                "base_sha": record.base_sha,
                "tree_hash": tree,
                "checker_hash": checker,
                "command": list(evidence.command),
                "environment": environment,
                "test_count": len(report.collected),
                "status": status,
                "evidence": evidence.log_ref,
            }
        )
    return tuple(rows)


class DatabaseVerificationSink:
    def __init__(self, database, store, *, owner: str, lease_generation: int):
        self.database, self.store = database, store
        self.owner, self.lease_generation = owner, lease_generation

    def __call__(self, record: VerificationRecord):
        bundle = self.store.load(record.bundle_ref)
        rows = verification_rows(record, bundle)
        # All proof/log refs are hash-checked before they are registered as database evidence.
        for reference in [record.bundle_ref, *(row["evidence"] for row in rows)]:
            self.store.evidence.read(reference)
            self.database.register_evidence(reference)
        with self.database.connect() as connection:
            self.database.assert_lease(
                record.task_id,
                self.owner,
                self.lease_generation,
                connection,
            )
            for row in rows:
                key = json.dumps(
                    {
                        "task": row["task_id"],
                        "kind": row["kind"],
                        "tree": row["tree_hash"],
                        "checker": row["checker_hash"],
                        "evidence": asdict(row["evidence"]),
                    },
                    sort_keys=True,
                )
                identifier = uuid5(NAMESPACE_URL, "decagent.verification.v1:" + key)
                connection.execute(
                    "INSERT INTO app.verification_runs"
                    "(id,task_id,kind,base_sha,tree_hash,checker_hash,command,environment,"
                    "test_count,status,evidence_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(id) DO NOTHING",
                    (
                        identifier,
                        row["task_id"],
                        row["kind"],
                        row["base_sha"],
                        row["tree_hash"],
                        row["checker_hash"],
                        Jsonb(row["command"]),
                        Jsonb(row["environment"]),
                        row["test_count"],
                        row["status"],
                        row["evidence"].sha256,
                    ),
                )

"""Trusted, empty-by-default policy publication and versioned task settings.

Report provenance is attested by an authenticated owner importing a referenced,
content-addressed report. Schema checks cannot independently establish that its
labels are real or reviewed. The evaluation producer and owner must establish
that fact before import; model output and employee parameters are never report
authorities. No report, dataset or model is downloaded or evaluated here.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from decagent.business.access import AccessError, principal
from decagent.business.decision_policy import NORMALIZATION_VERSION, PolicyContext, ThresholdPolicy
from decagent.business.employee_auth import EmployeeIdentity
from decagent.decisions.contracts import MODEL_ID, canonical_json, digest, probability
from decagent.gateway.auth import OwnerIdentity

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CONTEXT = set(PolicyContext.__dataclass_fields__)


def _fields(value, names):
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise ValueError("Unexpected validation report fields")


def _text(value, limit=512):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def validate_report(report, expected_sha256):
    """Validate an owner-attested report and preserve failed grid rows truthfully."""
    _fields(report, {"format", "report_id", "reference", "binding", "dataset", "thresholds"})
    if not _hash(expected_sha256) or digest(report) != expected_sha256:
        raise ValueError("Validation report digest does not match imported content")
    if (
        report["format"] != "decagent.decision-validation.v1"
        or not _text(report["report_id"], 200)
        or not _text(report["reference"])
    ):
        raise ValueError("Validation report identity and reference are required")
    binding = report["binding"]
    _fields(
        binding,
        {
            "question_id",
            "question_version",
            "primitive",
            "actual_model",
            "context",
            "min_candidates",
            "max_candidates",
        },
    )
    _fields(binding["context"], _CONTEXT)
    context = PolicyContext(**binding["context"])
    if (
        not all(_text(value) for value in vars(context).values())
        or context.normalization_version != NORMALIZATION_VERSION
        or not _text(binding["question_id"], 200)
        or not _text(binding["question_version"], 200)
        or binding["primitive"] not in ("choice", "noul", "score")
        or not isinstance(binding["actual_model"], str)
        or not MODEL_ID.fullmatch(binding["actual_model"])
        or type(binding["min_candidates"]) is not int
        or type(binding["max_candidates"]) is not int
        or not 2 <= binding["min_candidates"] <= binding["max_candidates"] <= 256
    ):
        raise ValueError("Validation report binding must pin supported, bounded versions")
    dataset = report["dataset"]
    _fields(
        dataset,
        {
            "version",
            "split",
            "label_source",
            "synthetic",
            "human_reviewed",
            "reference",
            "sha256",
            "review_reference",
            "review_sha256",
        },
    )
    if (
        dataset["version"] != context.dataset_version
        or dataset["split"] != "validation"
        or dataset["label_source"] != "human-verified"
        or dataset["synthetic"] is not False
        or dataset["human_reviewed"] is not True
        or not _text(dataset["reference"])
        or not _text(dataset["review_reference"])
        or not _hash(dataset["sha256"])
        or not _hash(dataset["review_sha256"])
    ):
        raise ValueError(
            "Production validation requires referenced, human-verified nonsynthetic data"
        )
    rows = report["thresholds"]
    if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 32:
        raise ValueError("Validation must record an explicit bounded threshold grid")
    seen = set()
    for row in rows:
        _fields(row, {"threshold", "n_auto", "n_correct"})
        threshold = probability(row["threshold"])
        if (
            threshold in seen
            or type(row["n_auto"]) is not int
            or type(row["n_correct"]) is not int
            or not 0 <= row["n_correct"] <= row["n_auto"]
        ):
            raise ValueError("Threshold metrics must be unique nonnegative integer counts")
        seen.add(threshold)
    encoded = canonical_json(report)
    if len(encoded.encode()) > 262144:
        raise ValueError("Validation report exceeds its bounded metadata size")
    return json.loads(encoded)


def _qualified(report, threshold):
    return any(
        row["threshold"] == threshold
        and row["n_auto"] >= 100
        and row["n_correct"] * 100 >= row["n_auto"] * 95
        for row in report["thresholds"]
    )


def _definition(policy):
    return {
        **vars(policy),
        "context": dict(vars(policy.context)),
        "valid_from": policy.valid_from.isoformat(),
        "valid_until": policy.valid_until.isoformat(),
    }


def _policy(row, threshold=None):
    value = dict(row["definition"])
    value["context"] = PolicyContext(**value["context"])
    value["valid_from"] = datetime.fromisoformat(value["valid_from"])
    value["valid_until"] = datetime.fromisoformat(value["valid_until"])
    value["status"] = row["status"]
    value["threshold"] = row["minimum_threshold"] if threshold is None else threshold
    return ThresholdPolicy(**value)


@dataclass(frozen=True)
class PolicyResolution:
    policy: ThresholdPolicy | None
    reason: str
    minimum_threshold: float | None = None
    task_threshold: float | None = None
    override_revision: int | None = None
    effective_revision: int | None = None
    allowed_thresholds: tuple[float, ...] = ()

    @property
    def primary_fallback(self):
        return False

    @property
    def grants_write_authority(self):
        return False

    def to_dict(self):
        return {
            **vars(self),
            "policy": _definition(self.policy) if self.policy else None,
            "allowed_thresholds": list(self.allowed_thresholds),
            "primary_fallback": False,
            "grants_write_authority": False,
        }


class DecisionPolicyStore:
    def __init__(self, database):
        self.database = database

    def _owner(self, conn, identity):
        if (
            not isinstance(identity, OwnerIdentity)
            or identity.owner_id != "owner"
            or type(identity.bearer) is not bool
        ):
            raise AccessError("forbidden", "Authenticated owner administration required")
        if identity.bearer:
            value = identity.session_id.removeprefix("bearer:")
            row = conn.execute(
                "SELECT singleton FROM app.owner_credentials WHERE token_hash=%s FOR SHARE",
                (value,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT token_hash FROM app.owner_sessions WHERE token_hash=%s "
                "AND revoked_at IS NULL AND expires_at>now() FOR SHARE",
                (identity.session_id,),
            ).fetchone()
        if not row:
            raise AccessError("unauthorized", "Owner credential unavailable")
        return "owner"

    def _task(self, conn, identity, task_id, *, browser=False):
        if not isinstance(identity, EmployeeIdentity):
            raise AccessError("unauthorized", "Original employee authentication required")
        if browser and identity.source != "employee_session":
            raise AccessError("forbidden", "Original employee browser session required")
        if identity.source == "employee_session":
            row = conn.execute(
                "SELECT employee_id FROM business.employee_sessions WHERE token_hash=%s "
                "AND employee_id=%s AND account_revision=%s AND revoked_at IS NULL "
                "AND expires_at>now() FOR SHARE",
                (identity.token_hash, identity.employee_id, identity.account_revision),
            ).fetchone()
        elif identity.source == "mcp_token":
            row = conn.execute(
                "SELECT employee_id FROM business.mcp_credentials WHERE token_hash=%s "
                "AND employee_id=%s AND account_revision=%s AND id=%s "
                "AND revoked_at IS NULL AND expires_at>now() FOR SHARE",
                (
                    identity.token_hash,
                    identity.employee_id,
                    identity.account_revision,
                    identity.credential_ref,
                ),
            ).fetchone()
        else:
            row = None
        if not row:
            raise AccessError("unauthorized", "Employee credential unavailable")
        actor = principal(conn, identity.employee_id, lock=True)
        if actor["credential_revision"] != identity.account_revision:
            raise AccessError("unauthorized", "Employee credential revision changed")
        task = conn.execute(
            "SELECT t.*,s.manifest_digest,s.enabled AS scope_enabled "
            "FROM app.tasks t JOIN app.business_scopes s ON s.id=t.business_scope_id "
            "WHERE t.id=%s AND t.kind='business' AND t.employee_id=%s "
            "FOR UPDATE OF t FOR SHARE OF s",
            (task_id, actor["id"]),
        ).fetchone()
        if not task:
            raise AccessError("not_found", "Original employee task unavailable")
        if not task["scope_enabled"]:
            raise AccessError("contract_changed", "Business scope is disabled")
        return task

    def _row(self, conn, policy_id, policy_version, *, lock="SHARE"):
        return conn.execute(
            "SELECT * FROM app.decision_policies WHERE policy_id=%s "
            "AND policy_version=%s FOR " + lock,
            (policy_id, policy_version),
        ).fetchone()

    def _report(self, conn, report_id):
        row = conn.execute(
            "SELECT * FROM app.decision_validation_reports WHERE report_id=%s FOR SHARE",
            (report_id,),
        ).fetchone()
        if not row:
            raise ValueError("Referenced validation report is unavailable")
        return validate_report(row["report"], row["report_digest"])

    def _matches_report(self, policy, report):
        binding = report["binding"]
        if (
            any(
                getattr(policy, key) != binding[key]
                for key in ("question_id", "question_version", "primitive", "actual_model")
            )
            or dict(vars(policy.context)) != binding["context"]
            or policy.min_candidates < binding["min_candidates"]
            or policy.max_candidates > binding["max_candidates"]
        ):
            raise ValueError("Policy binding exceeds the evaluated report binding")
        if not _qualified(report, policy.threshold):
            raise ValueError("Policy threshold has no qualified validation row")

    def _event(
        self,
        conn,
        kind,
        actor_kind,
        actor_id,
        payload,
        *,
        policy_id=None,
        policy_version=None,
        task=None,
    ):
        safe = self.database.redactor.sanitize(payload, surface="ui")
        conn.execute(
            "INSERT INTO app.decision_policy_events"
            "(policy_id,policy_version,task_id,kind,actor_kind,actor_id,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                policy_id,
                policy_version,
                task["id"] if task else None,
                kind,
                actor_kind,
                str(actor_id),
                Jsonb(safe),
            ),
        )
        if task:
            row = conn.execute(
                "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s "
                "RETURNING project_id,event_seq",
                (task["id"],),
            ).fetchone()
            conn.execute(
                "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
                "VALUES(%s,%s,%s,%s,%s)",
                (task["id"], row["project_id"], row["event_seq"], kind, Jsonb(safe)),
            )

    def register_validation(self, owner, report, expected_sha256):
        with self.database.connect() as conn:
            actor = self._owner(conn, owner)
            record = validate_report(report, expected_sha256)
            row = conn.execute(
                "INSERT INTO app.decision_validation_reports"
                "(report_id,report_digest,report,registered_by) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(report_id) DO NOTHING RETURNING *",
                (record["report_id"], expected_sha256, Jsonb(record), actor),
            ).fetchone()
            if row:
                self._event(
                    conn,
                    "decision.validation_registered",
                    "owner",
                    actor,
                    {"report_id": record["report_id"], "report_digest": expected_sha256},
                )
                return row
            row = conn.execute(
                "SELECT * FROM app.decision_validation_reports WHERE report_id=%s",
                (record["report_id"],),
            ).fetchone()
            if row["report_digest"] != expected_sha256:
                raise ValueError(
                    "Validation report identity already has different immutable content"
                )
            return row

    def register(self, owner, policy):
        with self.database.connect() as conn:
            actor = self._owner(conn, owner)
            if not isinstance(policy, ThresholdPolicy) or policy.status != "draft":
                raise ValueError("Only a draft ThresholdPolicy can be registered")
            self._matches_report(policy, self._report(conn, policy.validation_record))
            definition = _definition(policy)
            encoded = canonical_json(definition)
            if (
                not _text(policy.policy_id, 200)
                or not _text(policy.policy_version, 200)
                or len(encoded.encode()) > 65536
            ):
                raise ValueError("Policy definition exceeds its bounded format")
            definition_digest = digest(definition)
            row = conn.execute(
                "INSERT INTO app.decision_policies"
                "(policy_id,policy_version,definition,definition_digest,validation_record,"
                "minimum_threshold,registered_by) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(policy_id,policy_version) DO NOTHING RETURNING *",
                (
                    policy.policy_id,
                    policy.policy_version,
                    Jsonb(definition),
                    definition_digest,
                    policy.validation_record,
                    policy.threshold,
                    actor,
                ),
            ).fetchone()
            if row:
                self._event(
                    conn,
                    "decision.policy_registered",
                    "owner",
                    actor,
                    {
                        "policy_id": policy.policy_id,
                        "policy_version": policy.policy_version,
                        "definition_digest": definition_digest,
                        "validation_record": policy.validation_record,
                    },
                    policy_id=policy.policy_id,
                    policy_version=policy.policy_version,
                )
                return _policy(row)
            row = self._row(conn, policy.policy_id, policy.policy_version)
            if row["definition_digest"] != definition_digest:
                raise ValueError("Policy version already has different immutable content")
            return _policy(row)

    def publish(self, owner, policy_id, policy_version):
        with self.database.connect() as conn:
            actor = self._owner(conn, owner)
            row = self._row(conn, policy_id, policy_version, lock="UPDATE")
            if not row or row["status"] == "revoked":
                raise ValueError("Policy version is unavailable or permanently revoked")
            policy = _policy(row)
            self._matches_report(policy, self._report(conn, policy.validation_record))
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if policy.valid_until <= now:
                raise ValueError("Expired policy cannot be published")
            if row["status"] != "published":
                row = conn.execute(
                    "UPDATE app.decision_policies SET status='published',"
                    "published_at=clock_timestamp() "
                    "WHERE policy_id=%s AND policy_version=%s RETURNING *",
                    (policy_id, policy_version),
                ).fetchone()
                self._event(
                    conn,
                    "decision.policy_published",
                    "owner",
                    actor,
                    {
                        "policy_id": policy_id,
                        "policy_version": policy_version,
                        "minimum_threshold": row["minimum_threshold"],
                        "validation_record": row["validation_record"],
                    },
                    policy_id=policy_id,
                    policy_version=policy_version,
                )
            return _policy(row)

    def revoke(self, owner, policy_id, policy_version, reason):
        if not _text(reason):
            raise ValueError("A bounded revocation reason is required")
        with self.database.connect() as conn:
            actor = self._owner(conn, owner)
            row = self._row(conn, policy_id, policy_version, lock="UPDATE")
            if not row:
                raise ValueError("Policy version unavailable")
            if row["status"] != "revoked":
                row = conn.execute(
                    "UPDATE app.decision_policies SET status='revoked',"
                    "revoked_at=clock_timestamp() "
                    "WHERE policy_id=%s AND policy_version=%s RETURNING *",
                    (policy_id, policy_version),
                ).fetchone()
                self._event(
                    conn,
                    "decision.policy_revoked",
                    "owner",
                    actor,
                    {"policy_id": policy_id, "policy_version": policy_version, "reason": reason},
                    policy_id=policy_id,
                    policy_version=policy_version,
                )
            return _policy(row)

    def _available(self, conn, row):
        if not row or row["status"] == "draft":
            return "policy_unavailable"
        if row["status"] == "revoked":
            return "policy_revoked"
        policy = _policy(row)
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if now < policy.valid_from:
            return "policy_not_yet_valid"
        if now >= policy.valid_until:
            return "policy_expired"
        self._matches_report(policy, self._report(conn, policy.validation_record))
        return "policy_available"

    def set_minimum(self, owner, policy_id, policy_version, threshold):
        threshold = probability(threshold)
        with self.database.connect() as conn:
            actor = self._owner(conn, owner)
            row = self._row(conn, policy_id, policy_version, lock="UPDATE")
            if self._available(conn, row) != "policy_available":
                raise ValueError("Published current policy required")
            if threshold < row["minimum_threshold"]:
                raise ValueError("Administrator minimum cannot be lowered on an existing version")
            if not _qualified(self._report(conn, row["validation_record"]), threshold):
                raise ValueError("Minimum must select a qualified evaluated threshold")
            if threshold != row["minimum_threshold"]:
                previous = row["minimum_threshold"]
                row = conn.execute(
                    "UPDATE app.decision_policies SET minimum_threshold=%s "
                    "WHERE policy_id=%s AND policy_version=%s RETURNING *",
                    (threshold, policy_id, policy_version),
                ).fetchone()
                self._event(
                    conn,
                    "decision.minimum_changed",
                    "owner",
                    actor,
                    {
                        "policy_id": policy_id,
                        "policy_version": policy_version,
                        "previous_minimum": previous,
                        "minimum_threshold": threshold,
                    },
                    policy_id=policy_id,
                    policy_version=policy_version,
                )
            return _policy(row)

    def set_task_threshold(self, employee, task_id, policy_id, policy_version, threshold):
        threshold = probability(threshold)
        with self.database.connect() as conn:
            task = self._task(conn, employee, task_id, browser=True)
            row = self._row(conn, policy_id, policy_version)
            if self._available(conn, row) != "policy_available":
                raise ValueError("Published current policy required")
            if row["definition"]["context"]["plugin_digest"] != task["manifest_digest"]:
                raise ValueError("Policy binding does not match the task's business contract")
            if threshold < row["minimum_threshold"]:
                raise ValueError("Task threshold cannot lower administrator minimum")
            if not _qualified(self._report(conn, row["validation_record"]), threshold):
                raise ValueError("Task threshold must select a qualified evaluated threshold")
            previous = conn.execute(
                "SELECT * FROM app.task_decision_thresholds "
                "WHERE task_id=%s AND policy_id=%s AND policy_version=%s",
                (task_id, policy_id, policy_version),
            ).fetchone()
            if previous and previous["threshold"] == threshold:
                return previous
            updated = conn.execute(
                "INSERT INTO app.task_decision_thresholds"
                "(task_id,policy_id,policy_version,threshold,revision,changed_by) "
                "VALUES(%s,%s,%s,%s,1,%s) ON CONFLICT(task_id,policy_id,policy_version) "
                "DO UPDATE SET threshold=EXCLUDED.threshold,"
                "revision=app.task_decision_thresholds.revision+1,"
                "changed_by=EXCLUDED.changed_by,updated_at=clock_timestamp() RETURNING *",
                (task_id, policy_id, policy_version, threshold, employee.employee_id),
            ).fetchone()
            self._event(
                conn,
                "decision.threshold_changed",
                "employee",
                employee.employee_id,
                {
                    "policy_id": policy_id,
                    "policy_version": policy_version,
                    "previous_threshold": previous["threshold"] if previous else None,
                    "task_threshold": threshold,
                    "minimum_threshold": row["minimum_threshold"],
                    "override_revision": updated["revision"],
                },
                policy_id=policy_id,
                policy_version=policy_version,
                task=task,
            )
            return updated

    def resolve(self, employee, task_id, policy_id, policy_version):
        """Pin explicit policy versions; gate_decision still checks question/model/snapshots."""
        with self.database.connect() as conn:
            task = self._task(conn, employee, task_id)
            row = self._row(conn, policy_id, policy_version)
            reason = self._available(conn, row)
            if not row:
                return PolicyResolution(None, reason)
            override = conn.execute(
                "SELECT * FROM app.task_decision_thresholds "
                "WHERE task_id=%s AND policy_id=%s AND policy_version=%s",
                (task_id, policy_id, policy_version),
            ).fetchone()
            threshold = max(row["minimum_threshold"], override["threshold"] if override else 0)
            if row["definition"]["context"]["plugin_digest"] != task["manifest_digest"]:
                reason = "policy_context_mismatch"
            policy = _policy(row, threshold) if reason == "policy_available" else None
            allowed = (
                tuple(
                    sorted(
                        item["threshold"]
                        for item in self._report(conn, row["validation_record"])["thresholds"]
                        if item["threshold"] >= row["minimum_threshold"]
                        and _qualified({"thresholds": [item]}, item["threshold"])
                    )
                )
                if policy
                else ()
            )
            payload = {
                "policy_id": policy_id,
                "policy_version": policy_version,
                "reason": reason,
                "minimum_threshold": row["minimum_threshold"],
                "effective_threshold": threshold if policy else None,
                "task_threshold": override["threshold"] if override else None,
                "override_revision": override["revision"] if override else None,
                "allowed_thresholds": list(allowed),
                "validation_record": row["validation_record"],
                "policy": _definition(policy) if policy else None,
            }
            effective_digest = digest(payload)
            previous = conn.execute(
                "SELECT * FROM app.task_decision_policy_bindings "
                "WHERE task_id=%s AND policy_id=%s AND policy_version=%s",
                (task_id, policy_id, policy_version),
            ).fetchone()
            revision = previous["effective_revision"] if previous else 0
            if not previous or previous["effective_digest"] != effective_digest:
                revision += 1
                conn.execute(
                    "INSERT INTO app.task_decision_policy_bindings"
                    "(task_id,policy_id,policy_version,effective_revision,"
                    "effective_digest,configuration) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id,policy_id,policy_version) "
                    "DO UPDATE SET effective_revision=EXCLUDED.effective_revision,"
                    "effective_digest=EXCLUDED.effective_digest,"
                    "configuration=EXCLUDED.configuration,updated_at=clock_timestamp()",
                    (
                        task_id,
                        policy_id,
                        policy_version,
                        revision,
                        effective_digest,
                        Jsonb(payload),
                    ),
                )
                self._event(
                    conn,
                    "decision.policy_effective" if policy else "decision.policy_unavailable",
                    "employee",
                    employee.employee_id,
                    {**payload, "effective_revision": revision},
                    policy_id=policy_id,
                    policy_version=policy_version,
                    task=task,
                )
            return PolicyResolution(
                policy,
                reason,
                row["minimum_threshold"],
                override["threshold"] if override else None,
                override["revision"] if override else None,
                revision,
                allowed,
            )

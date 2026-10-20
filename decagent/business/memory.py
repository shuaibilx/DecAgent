"""Evidence-bound employee memories; never a source of execution authority.

The store returns only current, permission-filtered candidates. Semantic selection
can narrow those candidates later; it cannot publish rules or verify their sources.
"""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.contracts import PluginSpec
from decagent.business.mcp_contract import WRITE_COMMANDS
from decagent.business.memory_slots import PREFERENCE_SUBJECTS, MemorySlotAmbiguous, memory_slot_key
from decagent.business.operations import OperationService
from decagent.business.policy_store import DecisionPolicyStore
from decagent.business.tickets.store import _json
from decagent.decisions.contracts import digest
from decagent.storage.safe_record import exact_safe_record

_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _text(value, limit=512):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


@dataclass(frozen=True)
class MemoryContext:
    scope_id: str
    scope_revision: int
    account_revision: int
    contract_fingerprint: str
    question_fingerprint: str
    plan_fingerprint: str
    workspace_id: str

    def __post_init__(self):
        if (
            not isinstance(self.workspace_id, str)
            or str(UUID(self.workspace_id)) != self.workspace_id
            or not isinstance(self.scope_id, str)
            or str(UUID(self.scope_id)) != self.scope_id
            or any(
                type(value) is not int or value < 1
                for value in (self.scope_revision, self.account_revision)
            )
            or any(
                not isinstance(value, str) or not _HASH.fullmatch(value)
                for value in (
                    self.contract_fingerprint,
                    self.question_fingerprint,
                    self.plan_fingerprint,
                )
            )
        ):
            raise ValueError(
                "Business memory requires a trusted workspace, scope and version context"
            )


class BusinessMemoryStore:
    def __init__(self, database, *, clock=None):
        self.database = database
        self.operations = OperationService(database)
        self.policies = DecisionPolicyStore(database)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _scope(self, conn, context):
        if not isinstance(context, MemoryContext):
            raise ValueError("A trusted current memory context is required")
        workspace = conn.execute(
            "SELECT id FROM business.workspaces WHERE id=%s AND active FOR SHARE",
            (context.workspace_id,),
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (context.scope_id,)
        ).fetchone()
        if (
            not workspace
            or not row
            or not row["enabled"]
            or row["revision"] != context.scope_revision
            or row["manifest_digest"] != context.contract_fingerprint
            or PluginSpec.from_mapping(row["manifest"]).digest != context.contract_fingerprint
        ):
            raise AccessError("contract_changed", "Business memory scope or contract changed")
        return row

    def _checked(self, conn, identity, context, *, browser=False):
        actor = self.operations._identity(conn, identity, lock=True, browser_only=browser)
        self._scope(conn, context)
        if str(actor["workspace_id"]) != context.workspace_id:
            raise AccessError("workspace_mismatch", "Business memory workspace changed")
        if actor["credential_revision"] != context.account_revision:
            raise AccessError("version_changed", "Business memory account revision changed")
        return actor

    def context(self, identity, scope_id, *, question_fingerprint, plan_fingerprint):
        """Called by trusted runtime code with current registry/plan fingerprints."""
        with self.database.connect() as conn:
            actor = self.operations._identity(conn, identity, lock=True)
            scope = conn.execute(
                "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (scope_id,)
            ).fetchone()
            if not scope:
                raise AccessError("not_found", "Business memory scope unavailable")
            result = MemoryContext(
                str(scope["id"]),
                scope["revision"],
                actor["credential_revision"],
                scope["manifest_digest"],
                question_fingerprint,
                plan_fingerprint,
                str(actor["workspace_id"]),
            )
            self._scope(conn, result)
            return result

    @staticmethod
    def _reference(reference):
        if not isinstance(reference, dict):
            raise ValueError("A real business evidence reference is required")
        kind = reference.get("kind")
        fields = (
            {"kind", "task_id", "seq"}
            if kind == "task_event"
            else {"kind", "operation_id"}
            if kind == "operation_receipt"
            else set()
        )
        if not fields or not fields <= set(reference) <= fields | {
            "key",
            "fingerprint",
            "employee_id",
        }:
            raise ValueError("Unsupported business memory evidence reference")
        result = {key: reference[key] for key in fields}
        if kind == "task_event":
            if (
                not isinstance(result["task_id"], str)
                or str(UUID(result["task_id"])) != result["task_id"]
                or type(result["seq"]) is not int
                or result["seq"] < 1
            ):
                raise ValueError("Invalid business task event reference")
        elif not _text(result["operation_id"], 200):
            raise ValueError("Invalid business operation receipt reference")
        return result

    def _source(self, conn, context, reference, employee_id):
        raw = self._reference(reference)
        command = None
        if raw["kind"] == "task_event":
            row = conn.execute(
                "SELECT e.seq,e.kind,e.payload,t.id,t.employee_id,t.business_scope_id,"
                "t.authorization_snapshot FROM app.events e JOIN app.tasks t "
                "ON t.id=e.task_id AND t.workspace_id=e.workspace_id "
                "WHERE e.task_id=%s AND e.seq=%s AND t.kind='business' "
                "AND e.workspace_id=%s AND t.workspace_id=%s AND t.business_scope_id=%s "
                "AND (%s::uuid IS NULL OR t.employee_id=%s)",
                (
                    raw["task_id"],
                    raw["seq"],
                    context.workspace_id,
                    context.workspace_id,
                    context.scope_id,
                    employee_id,
                    employee_id,
                ),
            ).fetchone()
            if (
                not row
                or row["authorization_snapshot"].get("plugin_digest")
                != context.contract_fingerprint
            ):
                raise AccessError("invalid_evidence", "Business event evidence unavailable")
            source_employee = row["employee_id"]
            proof = {
                key: row[key]
                for key in ("seq", "kind", "payload", "id", "employee_id", "business_scope_id")
            }
        else:
            row = conn.execute(
                "SELECT * FROM business.operations WHERE id=%s AND scope_id=%s "
                "AND workspace_id=%s "
                "AND (%s::uuid IS NULL OR employee_id=%s) FOR SHARE",
                (
                    raw["operation_id"],
                    context.scope_id,
                    context.workspace_id,
                    employee_id,
                    employee_id,
                ),
            ).fetchone()
            if (
                not row
                or row["state"] != "succeeded"
                or row["contract_digest"] != context.contract_fingerprint
                or row["scope_revision"] != context.scope_revision
            ):
                raise AccessError(
                    "invalid_evidence", "Successful current receipt evidence required"
                )
            source_employee, command = row["employee_id"], row["command"]
            if command not in access.allowed_commands(
                conn, source_employee, workspace_id=context.workspace_id
            ):
                raise AccessError("forbidden", "Receipt command permission is no longer available")
            verified, result = self.operations._receipt_evidence(conn, row)
            if verified != "verified":
                raise AccessError(
                    "invalid_evidence", "Receipt evidence and durable history disagree"
                )
            access.get_ticket(
                conn,
                source_employee,
                result["receipt"]["resource_id"],
                workspace_id=context.workspace_id,
            )
            proof = {
                "operation": {
                    key: row[key]
                    for key in (
                        "id",
                        "employee_id",
                        "scope_id",
                        "scope_revision",
                        "command",
                        "params",
                        "effective_params",
                        "input_hash",
                        "contract_digest",
                        "state",
                    )
                },
                "result": result,
            }
        if source_employee is None:
            raise AccessError("invalid_evidence", "Evidence must bind the original employee")
        access.principal(conn, source_employee, workspace_id=context.workspace_id)
        key, fingerprint = digest(raw), digest(_json(proof))
        if conn.execute(
            "SELECT 1 FROM app.business_memory_source_revocations WHERE workspace_id=%s "
            "AND employee_id=%s AND evidence_key=%s",
            (context.workspace_id, source_employee, key),
        ).fetchone():
            raise AccessError("source_revoked", "Business memory evidence source was revoked")
        normalized = {
            **raw,
            "key": key,
            "fingerprint": fingerprint,
            "employee_id": str(source_employee),
        }
        if any(
            reference.get(name, normalized[name]) != normalized[name]
            for name in ("key", "fingerprint", "employee_id")
        ):
            raise AccessError("evidence_changed", "Business memory evidence fingerprint changed")
        return normalized, command

    def _sources(self, conn, context, references, employee_id, *, receipts_only=False):
        if not isinstance(references, list) or not 1 <= len(references) <= 16:
            raise ValueError("Memory requires one to sixteen real source references")
        result, commands = {}, set()
        for reference in references:
            checked, command = self._source(conn, context, reference, employee_id)
            if receipts_only and checked["kind"] != "operation_receipt":
                raise ValueError("Workflow experience requires successful receipt evidence")
            result[checked["key"]] = checked
            if command:
                commands.add(command)
        return [result[key] for key in sorted(result)], commands

    def _audit(self, conn, event, actor, payload, memory_id=None, *, workspace_id):
        conn.execute(
            "INSERT INTO app.business_memory_audit"
            "(id,workspace_id,memory_id,actor_employee_id,event,payload,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                workspace_id,
                memory_id,
                actor,
                event,
                Jsonb(exact_safe_record(payload, self.database.redactor)),
                self.clock(),
            ),
        )

    def _own(self, conn, identity, memory_id, *, lock=False):
        row = conn.execute(
            "SELECT * FROM app.business_memories WHERE id=%s AND employee_id=%s "
            "AND workspace_id=%s" + (" FOR UPDATE" if lock else ""),
            (memory_id, identity.employee_id, identity.workspace_id),
        ).fetchone()
        if not row:
            raise AccessError("not_found", "Business memory unavailable")
        return row

    def _view(self, row, *, usable=False):
        value = dict(row)
        value.pop("workspace_id", None)
        for field in (
            "id",
            "employee_id",
            "scope_id",
            "confirmed_by",
            "supersedes",
        ):
            if value[field] is not None:
                value[field] = str(value[field])
        return {
            **value,
            "data": row["content"],
            "active": usable,
            "verified": usable,
            "expired": row["expires_at"] <= self.clock(),
            "grants_write_authority": False,
        }

    @staticmethod
    def _matches(row, context):
        return all(str(row[key]) == str(value) for key, value in asdict(context).items())

    def _rule(self, conn, context, rule_id, rule_version):
        row = conn.execute(
            "SELECT * FROM app.business_memory_rules WHERE rule_id=%s "
            "AND rule_version=%s AND workspace_id=%s AND status='published' "
            "AND valid_until>%s FOR SHARE",
            (rule_id, rule_version, context.workspace_id, self.clock()),
        ).fetchone()
        if not row or any(
            str(row[key]) != str(value)
            for key, value in asdict(context).items()
            if key != "account_revision"
        ):
            raise ValueError("A matching current published experience rule is required")
        _, commands = self._sources(conn, context, row["validation_refs"], None, receipts_only=True)
        if commands != {row["command"]}:
            raise ValueError("Published rule validation receipts do not match its command")
        return row

    def _available(self, conn, row, context):
        if (
            row["status"] != "active"
            or row["expires_at"] <= self.clock()
            or not self._matches(row, context)
        ):
            return False
        try:
            self._sources(conn, context, row["evidence_refs"], row["employee_id"])
            if row["rule_id"]:
                self._rule(conn, context, row["rule_id"], row["rule_version"])
        except (ValueError, KeyError, TypeError):
            return False
        return True

    @staticmethod
    def _duplicate(
        conn,
        identity,
        context,
        *,
        stamp,
        kind,
        subject,
        content,
        references,
        supersedes,
        rule,
    ):
        return conn.execute(
            "SELECT * FROM app.business_memories WHERE workspace_id=%s AND employee_id=%s "
            "AND (content_digest=%s OR (kind=%s AND subject=%s AND content=%s "
            "AND evidence_refs=%s AND scope_id=%s AND scope_revision=%s "
            "AND account_revision=%s AND contract_fingerprint=%s "
            "AND question_fingerprint=%s AND plan_fingerprint=%s "
            "AND supersedes IS NOT DISTINCT FROM %s "
            "AND rule_id IS NOT DISTINCT FROM %s AND rule_version IS NOT DISTINCT FROM %s)) "
            "ORDER BY created_at DESC,id LIMIT 1",
            (
                context.workspace_id,
                identity.employee_id,
                stamp,
                kind,
                subject,
                Jsonb(content),
                Jsonb(references),
                context.scope_id,
                context.scope_revision,
                context.account_revision,
                context.contract_fingerprint,
                context.question_fingerprint,
                context.plan_fingerprint,
                supersedes,
                rule["rule_id"] if rule else None,
                rule["rule_version"] if rule else None,
            ),
        ).fetchone()

    def _insert(
        self,
        conn,
        identity,
        context,
        *,
        kind,
        subject,
        content,
        references,
        supersedes=None,
        rule=None,
    ):
        replaced = None
        if supersedes:
            replaced = self._own(conn, identity, supersedes, lock=True)
            if (
                replaced["kind"] != kind
                or replaced["subject"] != subject
                or str(replaced["workspace_id"]) != context.workspace_id
                or str(replaced["scope_id"]) != context.scope_id
            ):
                raise ValueError("Supersedes must reference the same current memory subject")
        payload = exact_safe_record(
            {
                **asdict(context),
                "employee_id": str(identity.employee_id),
                "kind": kind,
                "subject": subject,
                "content": content,
                "evidence_refs": references,
                "supersedes": str(supersedes) if supersedes else None,
                "rule_id": rule["rule_id"] if rule else None,
                "rule_version": rule["rule_version"] if rule else None,
            },
            self.database.redactor,
        )
        stamp, now = digest(payload), self.clock()
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,46131))",
            (str(identity.employee_id) + stamp,),
        )
        prior = self._duplicate(
            conn,
            identity,
            context,
            stamp=stamp,
            kind=kind,
            subject=subject,
            content=content,
            references=references,
            supersedes=supersedes,
            rule=rule,
        )
        if prior:
            return prior
        if replaced and replaced["status"] not in {"active", "pending_confirmation"}:
            raise ValueError("Supersedes must reference the same current memory subject")
        active = kind == "verified_workflow_experience"
        row = conn.execute(
            "INSERT INTO app.business_memories(id,workspace_id,employee_id,kind,subject,content,"
            "evidence_refs,scope_id,scope_revision,account_revision,contract_fingerprint,"
            "question_fingerprint,plan_fingerprint,status,verified_at,expires_at,supersedes,"
            "rule_id,rule_version,content_digest,created_at,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                uuid4(),
                context.workspace_id,
                identity.employee_id,
                kind,
                subject,
                Jsonb(content),
                Jsonb(references),
                context.scope_id,
                context.scope_revision,
                context.account_revision,
                context.contract_fingerprint,
                context.question_fingerprint,
                context.plan_fingerprint,
                "active" if active else "pending_confirmation",
                now if active else None,
                now + timedelta(days=7 if active else 30),
                supersedes,
                rule["rule_id"] if rule else None,
                rule["rule_version"] if rule else None,
                stamp,
                now,
                now,
            ),
        ).fetchone()
        self._audit(
            conn,
            "experience_recorded" if active else "preference_proposed",
            identity.employee_id,
            {"content_digest": stamp, "evidence_refs": references},
            row["id"],
            workspace_id=context.workspace_id,
        )
        return row

    def propose_preference(
        self, identity, context, *, subject, value, evidence_refs, supersedes=None
    ):
        if (
            not isinstance(subject, str)
            or subject not in PREFERENCE_SUBJECTS
            or not _text(value, 2048)
        ):
            raise ValueError("A registered preference subject and bounded value are required")
        with self.database.connect() as conn:
            self._checked(conn, identity, context)
            references, _ = self._sources(conn, context, evidence_refs, identity.employee_id)
            row = self._insert(
                conn,
                identity,
                context,
                kind="confirmed_preference",
                subject=subject,
                content={"value": value},
                references=references,
                supersedes=supersedes,
            )
            return self._view(row, usable=self._available(conn, row, context))

    def confirm_preference(self, identity, context, memory_id, expected_digest):
        with self.database.connect() as conn:
            self._checked(conn, identity, context, browser=True)
            row = self._own(conn, identity, memory_id, lock=True)
            if (
                row["kind"] != "confirmed_preference"
                or row["content_digest"] != expected_digest
                or not self._matches(row, context)
            ):
                raise AccessError("stale", "Preference confirmation version or digest changed")
            self._sources(conn, context, row["evidence_refs"], identity.employee_id)
            if row["status"] == "active":
                return self._view(row, usable=self._available(conn, row, context))
            if row["status"] != "pending_confirmation" or row["expires_at"] <= self.clock():
                raise AccessError("stale", "Preference confirmation unavailable")
            existing = conn.execute(
                "SELECT * FROM app.business_memories WHERE employee_id=%s "
                "AND workspace_id=%s AND scope_id=%s AND kind='confirmed_preference' "
                "AND subject=%s AND status='active' AND expires_at>%s FOR UPDATE",
                (
                    identity.employee_id,
                    context.workspace_id,
                    context.scope_id,
                    row["subject"],
                    self.clock(),
                ),
            ).fetchall()
            if any(
                str(prior["id"]) != str(row["supersedes"]) and self._matches(prior, context)
                for prior in existing
            ):
                raise ValueError("Explicit supersedes is required to change an active preference")
            if row["supersedes"]:
                prior = self._own(conn, identity, row["supersedes"], lock=True)
                if prior["status"] not in {"active", "pending_confirmation"}:
                    raise AccessError("stale", "Superseded preference changed before confirmation")
                conn.execute(
                    "UPDATE app.business_memories SET status='superseded',updated_at=%s "
                    "WHERE id=%s",
                    (self.clock(), prior["id"]),
                )
                self._audit(
                    conn,
                    "memory_superseded",
                    identity.employee_id,
                    {"replacement_id": str(row["id"])},
                    prior["id"],
                    workspace_id=context.workspace_id,
                )
            now = self.clock()
            row = conn.execute(
                "UPDATE app.business_memories SET status='active',verified_at=%s,"
                "expires_at=%s,confirmed_by=%s,updated_at=%s WHERE id=%s RETURNING *",
                (now, now + timedelta(days=30), identity.employee_id, now, row["id"]),
            ).fetchone()
            self._audit(
                conn,
                "preference_confirmed",
                identity.employee_id,
                {"content_digest": row["content_digest"], "source": "employee_session"},
                row["id"],
                workspace_id=context.workspace_id,
            )
            return self._view(row, usable=True)

    def candidates(self, identity, context, *, limit=8):
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("Memory candidate limit must be from one to eight")
        with self.database.connect() as conn:
            self._checked(conn, identity, context)
            rows = conn.execute(
                "SELECT m.* FROM app.business_memories m WHERE m.workspace_id=%s "
                "AND m.employee_id=%s AND m.scope_id=%s AND m.scope_revision=%s "
                "AND m.account_revision=%s AND m.contract_fingerprint=%s "
                "AND m.question_fingerprint=%s AND m.plan_fingerprint=%s "
                "AND m.status='active' AND m.expires_at>%s AND NOT EXISTS "
                "(SELECT 1 FROM app.business_memory_source_revocations r "
                "WHERE r.workspace_id=m.workspace_id AND r.employee_id=m.employee_id "
                "AND EXISTS (SELECT 1 FROM jsonb_array_elements(m.evidence_refs) e "
                "WHERE e->>'key'=r.evidence_key)) ORDER BY m.verified_at DESC,m.id LIMIT 256",
                (
                    context.workspace_id,
                    identity.employee_id,
                    context.scope_id,
                    context.scope_revision,
                    context.account_revision,
                    context.contract_fingerprint,
                    context.question_fingerprint,
                    context.plan_fingerprint,
                    self.clock(),
                ),
            ).fetchall()
            result = []
            for row in rows:
                if self._available(conn, row, context):
                    result.append(_json(self._view(row, usable=True)))
                if len(result) == limit:
                    break
            return result

    def get(self, identity, memory_id, *, context=None):
        """Own history remains inspectable; eligibility requires current context."""
        with self.database.connect() as conn:
            self.operations._identity(conn, identity, lock=True)
            row = self._own(conn, identity, memory_id)
            usable = False
            if context is not None:
                try:
                    self._checked(conn, identity, context)
                    usable = self._available(conn, row, context)
                except AccessError:
                    pass
            return self._view(row, usable=usable)

    def resolve_slot(self, identity, context, *, memory_kind, subject):
        """Resolve one exact, current employee memory by a stable workflow slot."""
        memory_slot_key(memory_kind, subject)
        with self.database.connect() as conn:
            self._checked(conn, identity, context)
            rows = conn.execute(
                "SELECT * FROM app.business_memories WHERE workspace_id=%s "
                "AND employee_id=%s AND scope_id=%s AND scope_revision=%s "
                "AND account_revision=%s AND contract_fingerprint=%s "
                "AND question_fingerprint=%s AND plan_fingerprint=%s "
                "AND kind=%s AND subject=%s AND status='active' AND expires_at>%s "
                "ORDER BY verified_at DESC,id LIMIT 256 FOR SHARE",
                (
                    context.workspace_id,
                    identity.employee_id,
                    context.scope_id,
                    context.scope_revision,
                    context.account_revision,
                    context.contract_fingerprint,
                    context.question_fingerprint,
                    context.plan_fingerprint,
                    memory_kind,
                    subject,
                    self.clock(),
                ),
            ).fetchall()
            candidates = [row for row in rows if self._available(conn, row, context)]
            if len(candidates) > 1:
                raise MemorySlotAmbiguous("Stable employee memory slot has multiple active values")
            if not candidates:
                return None
            return self._view(candidates[0], usable=True)

    def list_own(self, identity, *, context=None, limit=50):
        """Bounded own history; current context is optional for inspection only."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Own memory history limit must be from one to one hundred")
        with self.database.connect() as conn:
            if context is None:
                actor = self.operations._identity(conn, identity, lock=True)
            else:
                actor = self._checked(conn, identity, context)
            scope_id = context.scope_id if context else None
            rows = conn.execute(
                "SELECT * FROM app.business_memories WHERE employee_id=%s AND workspace_id=%s "
                "AND (%s::uuid IS NULL OR scope_id=%s) ORDER BY created_at DESC,id LIMIT %s",
                (identity.employee_id, actor["workspace_id"], scope_id, scope_id, limit),
            ).fetchall()
            return [
                self._view(row, usable=context is not None and self._available(conn, row, context))
                for row in rows
            ]

    def revoke(self, identity, memory_id, expected_digest, reason):
        if not _text(reason):
            raise ValueError("A bounded memory revocation reason is required")
        with self.database.connect() as conn:
            self.operations._identity(conn, identity, lock=True, browser_only=True)
            row = self._own(conn, identity, memory_id, lock=True)
            if row["content_digest"] != expected_digest:
                raise AccessError("stale", "Business memory digest changed")
            if row["status"] == "revoked":
                return self._view(row)
            if row["status"] == "superseded":
                raise AccessError("stale", "Superseded memory is already inactive")
            row = conn.execute(
                "UPDATE app.business_memories SET status='revoked',updated_at=%s "
                "WHERE id=%s RETURNING *",
                (self.clock(), row["id"]),
            ).fetchone()
            self._audit(
                conn,
                "memory_revoked",
                identity.employee_id,
                {"reason": reason},
                row["id"],
                workspace_id=identity.workspace_id,
            )
            return self._view(row)

    def revoke_source(self, identity, context, reference, reason):
        if not _text(reason):
            raise ValueError("A bounded source revocation reason is required")
        with self.database.connect() as conn:
            self._checked(conn, identity, context, browser=True)
            normalized, _ = self._source(conn, context, reference, identity.employee_id)
            conn.execute(
                "INSERT INTO app.business_memory_source_revocations"
                "(workspace_id,employee_id,scope_id,evidence_key,reference,reason,revoked_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    context.workspace_id,
                    identity.employee_id,
                    context.scope_id,
                    normalized["key"],
                    Jsonb(normalized),
                    reason,
                    self.clock(),
                ),
            )
            self._audit(
                conn,
                "source_revoked",
                identity.employee_id,
                {"evidence_key": normalized["key"], "reason": reason},
                workspace_id=context.workspace_id,
            )
            return {"status": "revoked", "evidence_key": normalized["key"]}

    def publish_experience_rule(
        self,
        owner,
        context,
        *,
        rule_id,
        rule_version,
        command,
        lesson,
        validation_refs,
        valid_until,
    ):
        with self.database.connect() as conn:
            self.policies._owner(conn, owner)
            self._scope(conn, context)
            if (
                not all(
                    _text(value, limit)
                    for value, limit in ((rule_id, 200), (rule_version, 200), (lesson, 512))
                )
                or command not in WRITE_COMMANDS
                or not isinstance(valid_until, datetime)
                or valid_until.tzinfo is None
                or valid_until <= self.clock()
            ):
                raise ValueError("A bounded versioned experience rule and expiry are required")
            references, commands = self._sources(
                conn, context, validation_refs, None, receipts_only=True
            )
            if commands != {command}:
                raise ValueError("Experience rule must match its real validation receipt commands")
            payload = exact_safe_record(
                {key: value for key, value in asdict(context).items() if key != "account_revision"},
                self.database.redactor,
            )
            payload.update(
                rule_id=rule_id,
                rule_version=rule_version,
                command=command,
                lesson=lesson,
                validation_refs=references,
                renderer_version="verified-command-1",
            )
            payload = exact_safe_record(payload, self.database.redactor)
            stamp = digest(payload)
            prior = conn.execute(
                "SELECT * FROM app.business_memory_rules WHERE rule_id=%s "
                "AND rule_version=%s AND workspace_id=%s FOR UPDATE",
                (rule_id, rule_version, context.workspace_id),
            ).fetchone()
            if prior:
                if (
                    prior["status"] != "published"
                    or prior["content_digest"] != stamp
                    or prior["valid_until"] != valid_until
                ):
                    raise ValueError("Published experience rule version is immutable")
                return prior
            row = conn.execute(
                "INSERT INTO app.business_memory_rules(rule_id,rule_version,workspace_id,"
                "scope_id,scope_revision,contract_fingerprint,question_fingerprint,plan_fingerprint,"
                "command,renderer_version,lesson,validation_refs,content_digest,status,published_by,"
                "published_at,valid_until) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'published',"
                "'owner',%s,%s) RETURNING *",
                (
                    rule_id,
                    rule_version,
                    context.workspace_id,
                    context.scope_id,
                    context.scope_revision,
                    context.contract_fingerprint,
                    context.question_fingerprint,
                    context.plan_fingerprint,
                    command,
                    "verified-command-1",
                    lesson,
                    Jsonb(references),
                    stamp,
                    self.clock(),
                    valid_until,
                ),
            ).fetchone()
            self._audit(
                conn,
                "rule_published",
                None,
                {"rule_id": rule_id, "rule_version": rule_version, "content_digest": stamp},
                workspace_id=context.workspace_id,
            )
            return row

    def revoke_experience_rule(self, owner, context, rule_id, rule_version, reason):
        if not _text(reason):
            raise ValueError("A bounded rule revocation reason is required")
        with self.database.connect() as conn:
            self.policies._owner(conn, owner)
            self._scope(conn, context)
            row = conn.execute(
                "UPDATE app.business_memory_rules SET status='revoked' WHERE rule_id=%s "
                "AND rule_version=%s AND workspace_id=%s RETURNING *",
                (rule_id, rule_version, context.workspace_id),
            ).fetchone()
            if not row:
                raise ValueError("Experience rule unavailable")
            self._audit(
                conn,
                "rule_revoked",
                None,
                {"rule_id": rule_id, "rule_version": rule_version, "reason": reason},
                workspace_id=context.workspace_id,
            )
            return row

    def record_experience(self, identity, context, *, rule_id, rule_version, receipt_ref):
        with self.database.connect() as conn:
            self._checked(conn, identity, context)
            rule = self._rule(conn, context, rule_id, rule_version)
            references, commands = self._sources(
                conn, context, [receipt_ref], identity.employee_id, receipts_only=True
            )
            if commands != {rule["command"]}:
                raise ValueError("Experience receipt must match the published rule command")
            row = self._insert(
                conn,
                identity,
                context,
                kind="verified_workflow_experience",
                subject="workflow:" + rule["command"],
                content={
                    "command": rule["command"],
                    "outcome": "succeeded",
                    "lesson": rule["lesson"],
                },
                references=references,
                rule=rule,
            )
            return self._view(row, usable=self._available(conn, row, context))

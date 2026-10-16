"""Durable semantic choices, separate from concrete operation authorization.

Only trusted runtime code creates waits. A current-evidence callback must rebuild
the permission-filtered question for both authenticated reply and worker consume.
Raw resume payloads and model text never establish those facts.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.decision_policy import (
    DecisionGate,
    DecisionSnapshot,
    PolicyContext,
    gate_decision,
)
from decagent.business.operations import OperationService
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import QUESTIONS, QuestionSpec, digest, immutable, json_value
from decagent.storage.safe_record import exact_safe_record


class OptionalMemoryRules:
    """Empty-by-default permission to omit optional memory, never a business decision."""

    def __init__(self, database, *, clock=None):
        from decagent.business.policy_store import DecisionPolicyStore

        self.database = database
        self.policies = DecisionPolicyStore(database)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _question(question):
        return (
            isinstance(question, QuestionSpec)
            and question.id in {"memory_read_needed", "memory_relevant"}
            and question == QUESTIONS[question.id]
        )

    def _policy(self, conn, policy_id, version):
        row = self.policies._row(conn, policy_id, version)
        if self.policies._available(conn, row) != "policy_available":
            return None
        from decagent.business.policy_store import _policy

        return _policy(row)

    def publish(
        self,
        owner,
        *,
        rule_id,
        rule_version,
        question,
        policy_id,
        policy_version,
        context,
        reference,
        valid_until,
        purpose="optional_memory",
        low_score_action="skip",
    ):
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            if (
                not self._question(question)
                or not isinstance(context, PolicyContext)
                or purpose != "optional_memory"
                or low_score_action != "skip"
                or not all(
                    isinstance(value, str) and value.strip() and len(value) <= limit
                    for value, limit in ((rule_id, 200), (rule_version, 200), (reference, 512))
                )
                or not isinstance(valid_until, datetime)
                or valid_until.tzinfo is None
                or valid_until <= self.clock()
            ):
                raise ValueError("An explicit bounded optional-memory skip rule is required")
            policy = self._policy(conn, policy_id, policy_version)
            if (
                policy is None
                or policy.context != context
                or (policy.question_id, policy.question_version, policy.primitive)
                != (question.id, question.version, question.type)
                or not policy.min_candidates <= len(question.criteria) <= policy.max_candidates
                or valid_until > policy.valid_until
            ):
                raise ValueError("Optional rule must match a current published threshold policy")
            value = exact_safe_record(
                {
                    "rule_id": rule_id,
                    "rule_version": rule_version,
                    "question_id": question.id,
                    "question_version": question.version,
                    "primitive": question.type,
                    "policy_id": policy_id,
                    "policy_version": policy_version,
                    "purpose": purpose,
                    "low_score_action": low_score_action,
                    "context": dict(vars(context)),
                    "reference": reference,
                },
                self.database.redactor,
            )
            existing = conn.execute(
                "SELECT * FROM app.business_optional_intervention_rules "
                "WHERE rule_id=%s AND rule_version=%s FOR UPDATE",
                (rule_id, rule_version),
            ).fetchone()
            if existing:
                if (
                    existing["status"] != "published"
                    or existing["valid_until"] != valid_until
                    or any(existing[key] != item for key, item in value.items())
                ):
                    raise ValueError(
                        "Optional rule version has different immutable content or was revoked"
                    )
                return existing
            row = conn.execute(
                "INSERT INTO app.business_optional_intervention_rules"
                "(rule_id,rule_version,question_id,question_version,primitive,policy_id,policy_version,"
                "purpose,low_score_action,context,reference,status,valid_until,published_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'published',%s,%s) RETURNING *",
                (
                    rule_id,
                    rule_version,
                    question.id,
                    question.version,
                    question.type,
                    policy_id,
                    policy_version,
                    purpose,
                    low_score_action,
                    Jsonb(value["context"]),
                    reference,
                    valid_until,
                    actor,
                ),
            ).fetchone()
            self.policies._event(
                conn,
                "decision.optional_rule_published",
                "owner",
                actor,
                value,
                policy_id=policy_id,
                policy_version=policy_version,
            )
            return row

    def revoke(self, owner, rule_id, rule_version, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ValueError("A bounded revocation reason is required")
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            row = conn.execute(
                "UPDATE app.business_optional_intervention_rules SET status='revoked' "
                "WHERE rule_id=%s AND rule_version=%s RETURNING *",
                (rule_id, rule_version),
            ).fetchone()
            if not row:
                raise ValueError("Optional rule unavailable")
            self.policies._event(
                conn,
                "decision.optional_rule_revoked",
                "owner",
                actor,
                {"rule_id": rule_id, "rule_version": rule_version, "reason": reason},
                policy_id=row["policy_id"],
                policy_version=row["policy_version"],
            )
            return row

    def evaluate(self, question, gate, *, purpose, active, rule_id=None, rule_version=None):
        result = {
            "status": "manual",
            "reason": "optional_rule_unavailable",
            "primary_fallback": False,
            "grants_write_authority": False,
        }
        if active is False:
            return {**result, "status": "inactive"}
        if (
            active is not True
            or purpose != "optional_memory"
            or not self._question(question)
            or not isinstance(gate, DecisionGate)
            or gate.answer is None
            or not isinstance(gate.snapshot, DecisionSnapshot)
            or gate.snapshot != gate.current_snapshot
            or not rule_id
            or not rule_version
        ):
            return result
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app.business_optional_intervention_rules "
                "WHERE rule_id=%s AND rule_version=%s AND status='published' FOR SHARE",
                (rule_id, rule_version),
            ).fetchone()
            if (
                not row
                or row["valid_until"] <= self.clock()
                or row["context"] != dict(vars(gate.snapshot.context))
                or (row["question_id"], row["question_version"], row["primitive"])
                != (question.id, question.version, question.type)
                or (row["policy_id"], row["policy_version"])
                != (gate.policy_id, gate.policy_version)
            ):
                return result
            policy = self._policy(conn, row["policy_id"], row["policy_version"])
            checked = gate_decision(
                question,
                gate.answer.raw_answer,
                actual_model=gate.actual_model,
                snapshot=gate.snapshot,
                current_snapshot=gate.current_snapshot,
                policy=policy,
                now=self.clock(),
            )
            if policy is None or checked.reason not in {"below_threshold", "undecided_noul"}:
                return result
            # Noul ties are classified before policy checks by the pure gate; a
            # tie still needs this exact current policy/model applicability.
            if (
                policy.context != gate.snapshot.context
                or policy.actual_model != gate.actual_model
                or (policy.question_id, policy.question_version, policy.primitive)
                != (question.id, question.version, question.type)
            ):
                return result
            record = {
                "kind": "optional_memory_skip",
                "rule_id": rule_id,
                "rule_version": rule_version,
                "question_id": question.id,
                "policy_id": row["policy_id"],
                "policy_version": row["policy_version"],
                "low_score_action": "skip",
                "reason": checked.reason,
                "gate": checked.to_dict(),
                "grants_write_authority": False,
                "primary_fallback": False,
            }
            return {
                **result,
                "status": "skipped",
                "reason": "published_optional_memory_rule",
                "semantic_record": exact_safe_record(record, self.database.redactor),
            }


@dataclass(frozen=True)
class DecisionEvidence:
    question: QuestionSpec
    snapshot: DecisionSnapshot
    state: object


def decision_evidence(spec, task, node, state, *, context, scope_version, account_version):
    """Instantiate a registered template from actual current, filtered read data."""
    registry = QuestionRegistry.from_spec(spec)
    definition = registry.validate_reference(node["question"])
    source = definition["candidate_source"] or "workflow." + definition["question_id"]
    if (
        not isinstance(context, PolicyContext)
        or context.plugin_digest != spec.digest
        or context.workflow_version != spec.manifest["workflow"]["version"]
        or context.candidate_source != source
    ):
        raise ValueError("Decision applicability does not match the trusted template")
    state = json_value(state)
    if state.get("instruction") != task["authorization_snapshot"]["instruction"]:
        raise ValueError("Evidence cannot replace the authorized task instruction")
    state = {
        "instruction": state["instruction"],
        "directory": state.get("directory", {}),
        "ticket": state.get("ticket"),
        "sources": state.get("sources", {}),
    }
    state["sources"] = {
        **state["sources"],
        "scope_revision": scope_version,
        "account_revision": account_version,
        "directory_digest": digest(state["directory"]),
        "ticket_digest": digest(state["ticket"]),
    }
    question = registry.render(
        definition["question_id"], state, node_id=node["id"], version=definition["version"]
    )
    snapshot = DecisionSnapshot(
        context,
        digest(state),
        digest(question.criteria),
        len(question.criteria),
        str(task["business_scope_id"]),
        str(scope_version),
        str(task["employee_id"]),
        str(account_version),
    )
    return DecisionEvidence(question, snapshot, immutable(state))


class SemanticInterventions:
    def __init__(self, database, *, refresh=None, clock=None):
        self.database, self.refresh = database, refresh
        self.clock = clock or (lambda: datetime.now(UTC))
        self.operations = OperationService(database)

    def _safe(self, value):
        return exact_safe_record(value, self.database.redactor)

    def _event(self, conn, task, kind, payload):
        row = conn.execute(
            "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s RETURNING event_seq,project_id",
            (task["id"],),
        ).fetchone()
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) VALUES(%s,%s,%s,%s,%s)",
            (task["id"], row["project_id"], row["event_seq"], kind, Jsonb(self._safe(payload))),
        )

    def _task(self, conn, identity, task_id, *, browser=False):
        actor = self.operations._identity(conn, identity, lock=True, browser_only=browser)
        task = conn.execute(
            "SELECT t.*,s.revision AS scope_revision,s.enabled AS scope_enabled,"
            "s.manifest_digest FROM app.tasks t JOIN app.business_scopes s "
            "ON s.id=t.business_scope_id WHERE t.id=%s AND t.kind='business' "
            "AND t.employee_id=%s AND t.graph_version='business-3' "
            "FOR UPDATE OF t FOR SHARE OF s",
            (task_id, actor["id"]),
        ).fetchone()
        if not task:
            raise AccessError("not_found", "Original employee conditional task unavailable")
        if (
            not task["scope_enabled"]
            or task["manifest_digest"] != task["authorization_snapshot"]["plugin_digest"]
        ):
            raise AccessError("contract_changed", "Business contract is unavailable or changed")
        if task["state"] in {"succeeded", "failed", "cancelled"}:
            raise AccessError("task_changed", "Task is no longer active")
        return task

    def _plan(self, conn, task, node_id):
        plan = conn.execute(
            "SELECT r.* FROM app.business_plan_heads h "
            "JOIN app.business_plan_revisions r ON r.task_id=h.task_id "
            "AND r.revision=h.current_revision WHERE h.task_id=%s FOR SHARE OF h,r",
            (task["id"],),
        ).fetchone()
        if not plan:
            raise AccessError("plan_changed", "Current conditional plan unavailable")
        node = next((item for item in plan["definition"]["nodes"] if item["id"] == node_id), None)
        if not node or node["type"] != "decide":
            raise AccessError("plan_changed", "Current semantic decision node unavailable")
        rows = conn.execute(
            "SELECT * FROM app.business_plan_nodes WHERE task_id=%s ORDER BY execution_generation",
            (task["id"],),
        ).fetchall()
        latest = {item["node_id"]: item for item in rows}
        active, queue = set(), [plan["definition"]["entry"]]
        while queue:
            current = queue.pop()
            if current in active:
                continue
            active.add(current)
            row = latest.get(current)
            if row and row["status"] in {"succeeded", "failed", "skipped"}:
                queue.extend((row["output_snapshot"] or {}).get("control", {}).get("targets", []))
        if node_id not in active or node_id not in latest:
            raise AccessError(
                "inactive_decision", "Only an activated, started node may request a choice"
            )
        return plan, node, latest[node_id]

    def _public(self, row):
        binding, gate = row["binding"], row["gate_snapshot"]
        answer = gate.get("answer")
        return {
            "status": "waiting",
            "kind": "decision",
            "approval_id": str(row["approval_id"]),
            "digest": row["binding_digest"],
            "node_id": row["node_id"],
            "plan_revision": row["plan_revision"],
            "question_id": binding["node"]["question"]["question_id"],
            "question": binding["question"],
            "evidence": binding["evidence"],
            "suggestion": answer.get("suggested_value") if answer else None,
            "raw_model_answer": answer,
            "reason": gate.get("reason"),
            "expires_at": row["expires_at"].isoformat(),
            "grants_write_authority": False,
            "primary_fallback": False,
        }

    def _create(self, conn, task, plan, node, saved, evidence, gate, *, expires_seconds=86400):
        if type(expires_seconds) is not int or not 60 <= expires_seconds <= 86400:
            raise ValueError("Semantic waits require a bounded lifetime")
        if evidence.question.id != node["id"] or gate.snapshot != evidence.snapshot:
            raise ValueError("Gate must belong to the actual current decision input")
        question = {
            "id": evidence.question.id,
            "version": evidence.question.version,
            **evidence.question.to_wire(),
            "state_fields": list(evidence.question.state_fields),
        }
        binding = self._safe(
            {
                "actor_id": str(task["employee_id"]),
                "task_id": str(task["id"]),
                "node": node,
                "plan_revision": plan["revision"],
                "plan_digest": plan["content_digest"],
                "generation": saved["execution_generation"],
                "question": question,
                "snapshot": evidence.snapshot.to_dict(),
                "evidence": json_value(evidence.state),
            }
        )
        key = digest(binding)
        prior = conn.execute(
            "SELECT * FROM app.business_decision_interventions WHERE task_id=%s "
            "AND node_id=%s AND execution_generation=%s AND binding->>'request_key'=%s "
            "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (task["id"], node["id"], saved["execution_generation"], key),
        ).fetchone()
        if prior and prior["status"] in {"pending", "replied", "consumed"}:
            return self._public(prior)
        round_number = prior["binding"]["round"] + 1 if prior else 1
        if round_number > 32:
            raise AccessError(
                "intervention_limit", "Decision renewal limit requires explicit review"
            )
        binding = {**binding, "request_key": key, "round": round_number}
        fingerprint, approval_id = digest(binding), uuid4()
        conn.execute(
            "INSERT INTO app.approvals(id,task_id,digest,kind) VALUES(%s,%s,%s,'decision')",
            (approval_id, task["id"], fingerprint),
        )
        row = conn.execute(
            "INSERT INTO app.business_decision_interventions"
            "(approval_id,task_id,actor_employee_id,node_id,execution_generation,plan_revision,"
            "plan_digest,binding_digest,binding,gate_snapshot,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                approval_id,
                task["id"],
                task["employee_id"],
                node["id"],
                saved["execution_generation"],
                plan["revision"],
                plan["content_digest"],
                fingerprint,
                Jsonb(binding),
                Jsonb(self._safe(gate.to_dict())),
                self.clock() + timedelta(seconds=expires_seconds),
            ),
        ).fetchone()
        public = self._public(row)
        conn.execute(
            "UPDATE app.approvals SET reply_payload=%s WHERE id=%s",
            (Jsonb({"semantic": public}), approval_id),
        )
        self._event(
            conn,
            task,
            "decision.waiting",
            {
                "approval_id": str(approval_id),
                "digest": fingerprint,
                "node_id": node["id"],
                "plan_revision": plan["revision"],
                "reason": gate.reason,
            },
        )
        return public

    def request(
        self,
        task_id,
        node_id,
        plan_revision,
        evidence,
        gate,
        *,
        identity,
        lease_owner,
        lease_generation,
        active=True,
        generation=1,
        expires_seconds=86400,
    ):
        if active is False:
            return {
                "status": "inactive",
                "primary_fallback": False,
                "grants_write_authority": False,
            }
        if type(active) is not bool or not isinstance(gate, DecisionGate):
            raise ValueError("Trusted runtime activation and gate required")
        with self.database.connect() as conn:
            task = self._task(conn, identity, task_id)
            self.database.assert_lease(task_id, lease_owner, lease_generation, conn)
            plan, node, saved = self._plan(conn, task, node_id)
            if plan["revision"] != plan_revision or saved["execution_generation"] != generation:
                raise AccessError("plan_changed", "Decision plan or execution generation changed")
            if saved["status"] not in {"running", "waiting"}:
                raise AccessError(
                    "inactive_decision", "A finished decision cannot create a new wait"
                )
            current = self._rebuild(
                conn, task, node, evidence.state, evidence.snapshot.context, identity
            )
            if current != evidence or gate.current_snapshot != evidence.snapshot:
                raise AccessError("stale_decision", "Decision evidence is not current")
            if gate.may_consume:
                return {
                    "status": "automatic",
                    "selected_value": gate.answer.suggested_value,
                    "primary_fallback": False,
                    "grants_write_authority": False,
                }
            return self._create(
                conn, task, plan, node, saved, evidence, gate, expires_seconds=expires_seconds
            )

    def _rebuild(self, conn, task, node, state, context, identity):
        spec = PluginRegistry(self.database).get(task["business_scope_id"], task["manifest_digest"])
        return decision_evidence(
            spec,
            task,
            node,
            state,
            context=context,
            scope_version=task["scope_revision"],
            account_version=identity.account_revision,
        )

    def _current(self, conn, task, row, identity):
        try:
            plan, node, saved = self._plan(conn, task, row["node_id"])
        except AccessError as exc:
            if exc.code in {"plan_changed", "inactive_decision"}:
                return None, None, None, None
            raise
        if (
            plan["revision"] != row["plan_revision"]
            or plan["content_digest"] != row["plan_digest"]
            or saved["execution_generation"] != row["execution_generation"]
            or saved["status"] not in {"running", "waiting"}
        ):
            return None, plan, node, saved
        if self.refresh is None:
            raise AccessError(
                "service_unavailable", "A trusted current-evidence reader is required"
            )
        evidence = self.refresh(conn, task, row, identity)
        if not isinstance(evidence, DecisionEvidence):
            raise ValueError("Current-evidence reader must return typed decision evidence")
        checked = self._rebuild(
            conn, task, node, evidence.state, evidence.snapshot.context, identity
        )
        if checked != evidence:
            raise ValueError("Refreshed evidence does not match the current trusted question")
        return evidence, plan, node, saved

    def _invalidate(self, conn, task, row, status):
        if row["status"] not in {"pending", "replied"}:
            return
        conn.execute(
            "UPDATE app.business_decision_interventions SET status=%s WHERE approval_id=%s",
            (status, row["approval_id"]),
        )
        conn.execute(
            "UPDATE app.approvals SET status='consumed',version=version+1 WHERE id=%s",
            (row["approval_id"],),
        )
        self._event(
            conn,
            task,
            "decision." + status,
            {
                "approval_id": str(row["approval_id"]),
                "node_id": row["node_id"],
                "plan_revision": row["plan_revision"],
            },
        )

    def _fresh(self, conn, task, row, identity):
        evidence, plan, node, saved = self._current(conn, task, row, identity)
        expired = self.clock() >= row["expires_at"]
        terminal = row["status"] in {"stale", "expired"}
        if (
            not terminal
            and evidence is not None
            and evidence.snapshot.to_dict() == row["binding"]["snapshot"]
            and not expired
        ):
            return evidence, None
        status = row["status"] if terminal else "expired" if expired else "stale"
        self._invalidate(conn, task, row, status)
        request = None
        if evidence is not None:
            old_gate = row["gate_snapshot"]
            gate = DecisionGate(
                "manual",
                "evidence_refreshed",
                None,
                old_gate.get("actual_model"),
                evidence.snapshot,
                evidence.snapshot,
                old_gate.get("policy_id"),
                old_gate.get("policy_version"),
                old_gate.get("threshold"),
            )
            request = self._create(conn, task, plan, node, saved, evidence, gate)
        failure = {
            "status": status,
            "may_resume": False,
            "refresh_required": True,
            "request": request,
            "grants_write_authority": False,
            "primary_fallback": False,
        }
        control = self._safe(
            {
                **failure,
                "kind": "semantic_refresh",
                "source": "trusted_refresh",
                "approval_id": str(row["approval_id"]),
                "actor_id": str(task["employee_id"]),
                "node_id": row["node_id"],
                "plan_revision": row["plan_revision"],
                "binding_digest": row["binding_digest"],
                "selected_value": None,
            }
        )
        approval = conn.execute(
            "SELECT reply_payload FROM app.approvals WHERE id=%s FOR UPDATE", (row["approval_id"],)
        ).fetchone()
        payload = {**(approval["reply_payload"] or {}), "semantic_record": control}
        if row["response_record"] is not None:
            payload["superseded_semantic_record"] = row["response_record"]
        conn.execute(
            "UPDATE app.approvals SET reply_payload=%s WHERE id=%s",
            (Jsonb(payload), row["approval_id"]),
        )
        # The worker's durable input still references the old approval. Queue a
        # control-only handoff so it can persist the new request before waiting.
        if (
            task["lease_owner"] is None
            and task["state"] in {"awaiting_input", "awaiting_approval"}
            and (saved is None or saved["status"] in {"running", "waiting"})
        ):
            conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (task["id"],))
        return None, {**failure, "semantic_record": control}

    def _row(self, conn, approval_id):
        row = conn.execute(
            "SELECT * FROM app.business_decision_interventions WHERE approval_id=%s FOR UPDATE",
            (UUID(str(approval_id)),),
        ).fetchone()
        if not row:
            raise AccessError("not_found", "Semantic decision unavailable")
        return row

    @staticmethod
    def _selection(question, body):
        if body.decision != "select":
            return None
        value = body.value
        valid = (
            (question.type == "choice" and type(value) is str and value in question.criteria)
            or (question.type == "noul" and type(value) is bool)
            or (
                question.type == "score"
                and type(value) is int
                and 0 <= value < len(question.criteria)
            )
        )
        if not valid:
            raise AccessError("invalid_selection", "Selection is outside the current legal choices")
        return value

    def reply(self, identity, approval_id, body, *, response_id):
        with self.database.connect() as conn:
            # Lock order matches gateway: task before approval/intervention.
            existing = conn.execute(
                "SELECT task_id FROM app.business_decision_interventions WHERE approval_id=%s",
                (UUID(str(approval_id)),),
            ).fetchone()
            if not existing:
                raise AccessError("not_found", "Semantic decision unavailable")
            task = self._task(conn, identity, existing["task_id"], browser=True)
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (approval_id,)
            ).fetchone()
            return self.reply_in_transaction(conn, task, approval, body, identity, response_id)

    def reply_in_transaction(self, conn, task, approval, body, identity, response_id):
        task = self._task(conn, identity, task["id"], browser=True)
        row = self._row(conn, approval["id"])
        if (
            row["actor_employee_id"] != identity.employee_id
            or row["task_id"] != task["id"]
            or approval["kind"] != "decision"
            or body.expected_digest != row["binding_digest"]
            or body.expected_plan_revision != row["plan_revision"]
        ):
            raise AccessError(
                "stale_decision", "Reply does not match the original decision binding"
            )
        fingerprint = digest(
            {
                "approval_id": str(approval["id"]),
                "actor_id": str(identity.employee_id),
                "reply": body.model_dump(mode="json"),
            }
        )
        evidence, failure = self._fresh(conn, task, row, identity)
        if failure:
            return failure
        if row["response_fingerprint"]:
            if row["response_fingerprint"] != fingerprint:
                raise AccessError(
                    "approval_conflict", "Semantic question already has a different reply"
                )
            return {
                "status": "replied",
                "approval_id": str(approval["id"]),
                "replayed": True,
                "semantic_record": row["response_record"],
                "grants_write_authority": False,
            }
        if (
            task["state"] not in {"awaiting_input", "awaiting_approval"}
            or task["lease_owner"] is not None
        ):
            raise AccessError("task_changed", "Task is not waiting for an employee decision")
        value = self._selection(evidence.question, body)
        status = {
            "select": "selected",
            "no_match": "no_match",
            "supplement": "supplement",
            "reject": "rejected",
        }[body.decision]
        if evidence.question.type == "choice" and value == "no_match":
            status, value = "no_match", None
        record = self._safe(
            {
                "kind": "semantic_decision",
                "source": "employee_session",
                "approval_id": str(approval["id"]),
                "actor_id": str(identity.employee_id),
                "binding_digest": row["binding_digest"],
                "plan_revision": row["plan_revision"],
                "node_id": row["node_id"],
                "question_id": row["binding"]["node"]["question"]["question_id"],
                "response_id": response_id,
                "response_fingerprint": fingerprint,
                "status": status,
                "selected_value": value,
                "text": body.text,
                "raw_model_answer": row["gate_snapshot"].get("answer"),
                "snapshot": evidence.snapshot.to_dict(),
                "grants_write_authority": False,
                "primary_fallback": False,
            }
        )
        conn.execute(
            "UPDATE app.business_decision_interventions SET status='replied',"
            "response_fingerprint=%s,response_record=%s WHERE approval_id=%s AND status='pending'",
            (fingerprint, Jsonb(record), row["approval_id"]),
        )
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply=%s,reply_payload=%s,"
            "version=version+1,replied_at=now() "
            "WHERE id=%s",
            (
                "reject" if body.decision == "reject" else "confirm",
                Jsonb({**(approval["reply_payload"] or {}), "semantic_record": record}),
                approval["id"],
            ),
        )
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (task["id"],))
        self._event(
            conn,
            task,
            "decision.replied",
            {
                "approval_id": str(approval["id"]),
                "status": status,
                "node_id": row["node_id"],
                "plan_revision": row["plan_revision"],
            },
        )
        return {
            "status": "replied",
            "approval_id": str(approval["id"]),
            "semantic_record": record,
            "replayed": False,
            "grants_write_authority": False,
        }

    def consume(self, approval_id, *, identity, lease_owner, lease_generation, consumer_id):
        if not isinstance(consumer_id, str) or not consumer_id.strip() or len(consumer_id) > 200:
            raise ValueError("Stable bounded semantic consumer identity required")
        with self.database.connect() as conn:
            existing = conn.execute(
                "SELECT task_id FROM app.business_decision_interventions WHERE approval_id=%s",
                (UUID(str(approval_id)),),
            ).fetchone()
            if not existing:
                raise AccessError("not_found", "Semantic decision unavailable")
            task = self._task(conn, identity, existing["task_id"])
            self.database.assert_lease(task["id"], lease_owner, lease_generation, conn)
            row = self._row(conn, approval_id)
            evidence, failure = self._fresh(conn, task, row, identity)
            if failure:
                return failure
            if row["status"] == "pending":
                return {
                    "status": "waiting",
                    "request": self._public(row),
                    "may_resume": False,
                    "primary_fallback": False,
                    "grants_write_authority": False,
                }
            if row["status"] not in {"replied", "consumed"}:
                return {
                    "status": row["status"],
                    "may_resume": False,
                    "grants_write_authority": False,
                }
            if row["consumed_by"] and row["consumed_by"] != consumer_id:
                raise AccessError(
                    "approval_conflict", "Semantic reply was consumed by another execution"
                )
            replayed = row["status"] == "consumed"
            if not replayed:
                conn.execute(
                    "UPDATE app.business_decision_interventions SET status='consumed',"
                    "consumed_by=%s,consumed_at=now() WHERE approval_id=%s AND status='replied'",
                    (consumer_id, row["approval_id"]),
                )
                conn.execute(
                    "UPDATE app.approvals SET status='consumed' WHERE id=%s", (row["approval_id"],)
                )
                self._event(
                    conn,
                    task,
                    "decision.consumed",
                    {
                        "approval_id": str(row["approval_id"]),
                        "node_id": row["node_id"],
                        "consumer_id": consumer_id,
                    },
                )
            return {**row["response_record"], "replayed": replayed, "may_resume": True}

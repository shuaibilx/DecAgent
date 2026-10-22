"""Trusted Jev screening for evidence-bound business memory write proposals.

Reads are resolved by stable slots in published workflows, without a model call.
This evaluator never activates memories. Preference confirmation and experience
publication/receipt checks remain authoritative in BusinessMemoryStore. Model
answers grant no authority.
"""

from dataclasses import asdict
from datetime import UTC, datetime

from decagent.business.access import AccessError
from decagent.business.decision_policy import DecisionSnapshot, PolicyContext, gate_decision
from decagent.business.memory import PREFERENCE_SUBJECTS, BusinessMemoryStore
from decagent.business.policy_store import DecisionPolicyStore
from decagent.business.questions import BusinessDecisionContext
from decagent.business.trial_budget import make_task_usage_ledger
from decagent.config import Settings
from decagent.decisions.adapter import DEFAULT_MODEL, JevAdapter
from decagent.decisions.contracts import QUESTIONS, DecisionRequest, digest, validate_answers
from decagent.runtime.model_calls import LedgerAttemptBudget
from decagent.storage.safe_record import exact_safe_record

MAX_CANDIDATES = 8
CANDIDATE_SOURCE = "business_memories.current@1"


def _text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _binding(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(_text(part, 200) for part in value)
    ):
        raise ValueError("An explicit published policy id and version are required")
    return tuple(value)


def _result(status, *, decisions=(), memories=(), **extra):
    return {
        "status": status,
        "memories": list(memories),
        "decisions": list(decisions),
        "actual_requests": sum(
            len((item.get("evaluation") or {}).get("attempts", ())) for item in decisions
        ),
        "primary_fallback": False,
        "grants_write_authority": False,
        **extra,
    }


class BusinessMemoryWriteEvaluator:
    def __init__(
        self,
        database,
        *,
        api_key,
        reservation_usd,
        dataset_version,
        model=DEFAULT_MODEL,
        provider="typesafe",
        adapter_factory=JevAdapter,
        before_dispatch=None,
        clock=None,
        settings=None,
    ):
        if not _text(dataset_version, 200):
            raise ValueError("A pinned memory evaluation dataset version is required")
        self.database, self.dataset_version = database, dataset_version
        self.clock = clock or (lambda: datetime.now(UTC))
        self.memories = BusinessMemoryStore(database, clock=self.clock)
        self.policies = DecisionPolicyStore(database)
        self.adapter_factory, self.before_dispatch = adapter_factory, before_dispatch
        self.adapter_options = {"api_key": api_key, "model": model, "provider": provider}
        self.reservation_usd = reservation_usd
        # Without explicit runtime settings, guest dispatch remains disabled by default.
        self.settings = settings or Settings()

    def _current(self, identity, context, task_id):
        # This call performs ACL, source, TTL and all version checks before any
        # content can enter a request. Never accept candidates from a caller.
        candidates = self.memories.candidates(identity, context, limit=MAX_CANDIDATES)
        with self.database.connect() as conn:
            task = self.policies._task(conn, identity, task_id)
            if (
                str(task["business_scope_id"]) != context.scope_id
                or task["manifest_digest"] != context.contract_fingerprint
                or task["graph_version"] != "business-3"
            ):
                raise AccessError("version_changed", "Memory task scope or runtime changed")
        return candidates

    @staticmethod
    def _question(template_id, expected):
        if template_id != expected or template_id not in QUESTIONS:
            raise ValueError("Unknown or incompatible trusted memory template")
        return QUESTIONS[template_id]

    def _snapshot(self, identity, context, question, state, candidates, *, optional):
        applicability = PolicyContext(
            context.contract_fingerprint,
            "business-3",
            self.dataset_version,
            "optional_memory" if optional else "business_decision",
            CANDIDATE_SOURCE,
        )
        return DecisionSnapshot(
            applicability,
            digest(state),
            digest(candidates),
            len(question.criteria),
            context.scope_id,
            str(context.scope_revision),
            str(identity.employee_id),
            str(context.account_revision),
        )

    def _judge(
        self,
        identity,
        context,
        task_id,
        question,
        refresh,
        *,
        binding,
        optional,
    ):
        policy_id, policy_version = _binding(binding)
        resolution = self.policies.resolve(identity, task_id, policy_id, policy_version)
        if resolution.policy is None:
            return {"status": "manual", "reason": resolution.reason}
        candidates, state = refresh()
        state = exact_safe_record(state, self.database.redactor)
        snapshot = self._snapshot(identity, context, question, state, candidates, optional=optional)
        request = DecisionRequest(
            BusinessDecisionContext(
                str(task_id),
                context.scope_id,
                context.contract_fingerprint,
                "business-3",
                question.version,
                digest([policy_id, policy_version, resolution.effective_revision]),
                self.dataset_version,
                digest(snapshot.to_dict()),
            ),
            (question,),
            state,
        )

        def guard():
            if self.before_dispatch:
                self.before_dispatch()
            fresh_candidates, fresh_state = refresh()
            current = self._snapshot(
                identity, context, question, fresh_state, fresh_candidates, optional=optional
            )
            current_policy = self.policies.resolve(identity, task_id, policy_id, policy_version)
            if (
                current != snapshot
                or current_policy.policy != resolution.policy
                or current_policy.effective_revision != resolution.effective_revision
            ):
                raise AccessError("stale", "Memory evidence or policy changed before dispatch")

        budget = LedgerAttemptBudget(
            make_task_usage_ledger(self.database, self.settings, task_id),
            reservation_usd=self.reservation_usd,
            before_dispatch=guard,
            purpose="memory_write_screening",
        )
        adapter = self.adapter_factory(**self.adapter_options, budget=budget, cache_ttl=0)
        if getattr(adapter, "budget", None) is not budget:
            raise ValueError("Memory adapter must use this task's shared attempt budget")
        try:
            answer = adapter.evaluate(request)
        finally:
            adapter.close()
        record = {"question_id": question.id, "evaluation": answer.to_dict()}
        if (
            answer.input_hash != request.input_hash
            or vars(answer.context) != vars(request.context)
            or dict(answer.question_versions) != request.question_versions
        ):
            return {**record, "status": "manual", "reason": "evaluation_mismatch"}
        if not answer.available:
            return {**record, "status": "manual", "reason": answer.error or "answer_unavailable"}
        try:
            validate_answers(record["evaluation"]["answers"], request)
        except (ValueError, TypeError, KeyError):
            return {**record, "status": "manual", "reason": "invalid_answer"}
        try:
            current_candidates, current_state = refresh()
            current_snapshot = self._snapshot(
                identity, context, question, current_state, current_candidates, optional=optional
            )
            current_policy = self.policies.resolve(identity, task_id, policy_id, policy_version)
        except (AccessError, ValueError):
            return {**record, "status": "manual", "reason": "evidence_changed"}
        if (
            current_policy.policy != resolution.policy
            or current_policy.effective_revision != resolution.effective_revision
        ):
            return {**record, "status": "manual", "reason": "policy_changed"}
        gate = gate_decision(
            question,
            answer.answers[question.id],
            actual_model=answer.actual_model,
            snapshot=snapshot,
            current_snapshot=current_snapshot,
            policy=current_policy.policy,
            now=self.clock(),
        )
        record.update(status=gate.status, reason=gate.reason, gate=gate.to_dict())
        if gate.may_consume:
            record["value"] = gate.answer.suggested_value
        self.database.event(
            task_id,
            "business.memory_write_proposal_evaluated",
            exact_safe_record(record, self.database.redactor),
        )
        return record

    def _proposal(self, identity, context, task_id, *, kind, options):
        candidates = self._current(identity, context, task_id)
        with self.database.connect() as conn:
            self.memories._checked(conn, identity, context)
            if kind == "confirmed_preference":
                if options["subject"] not in PREFERENCE_SUBJECTS or not _text(
                    options["value"], 2048
                ):
                    raise ValueError(
                        "A registered preference subject and bounded value are required"
                    )
                references, _ = self.memories._sources(
                    conn, context, options["evidence_refs"], identity.employee_id
                )
                subject, content, rule = options["subject"], {"value": options["value"]}, None
                supersedes = options.get("supersedes")
                if supersedes:
                    prior = self.memories._own(conn, identity, supersedes)
                    if (
                        prior["kind"] != kind
                        or prior["subject"] != subject
                        or str(prior["scope_id"]) != context.scope_id
                        or prior["status"] not in {"active", "pending_confirmation"}
                    ):
                        raise ValueError(
                            "Supersedes must reference the same current memory subject"
                        )
            else:
                rule = self.memories._rule(
                    conn, context, options["rule_id"], options["rule_version"]
                )
                references, commands = self.memories._sources(
                    conn,
                    context,
                    [options["receipt_ref"]],
                    identity.employee_id,
                    receipts_only=True,
                )
                if commands != {rule["command"]}:
                    raise ValueError("Experience receipt must match the published rule command")
                subject, supersedes = "workflow:" + rule["command"], None
                content = {
                    "command": rule["command"],
                    "outcome": "succeeded",
                    "lesson": rule["lesson"],
                }
            # Match the store's immutable deduplication identity, including
            # sources and version context. Semantic retention cannot renew TTL.
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
            duplicate = self.memories._duplicate(
                conn,
                identity,
                context,
                stamp=digest(payload),
                kind=kind,
                subject=subject,
                content=content,
                references=references,
                supersedes=supersedes,
                rule=rule,
            )
        return candidates, payload, str(duplicate["id"]) if duplicate else None

    def _evaluate_write(self, identity, context, task_id, *, kind, options, policy, template_id):
        question = self._question(template_id, "memory_write")
        _, payload, duplicate = self._proposal(
            identity, context, task_id, kind=kind, options=options
        )
        if duplicate:
            return _result("duplicate", memory_id=duplicate)

        def refresh():
            current, proposal, _ = self._proposal(
                identity, context, task_id, kind=kind, options=options
            )
            return current, {
                "candidate": proposal,
                "source_evidence": proposal["evidence_refs"],
                # A proposed preference is never a confirmed preference.
                "confirmation_refs": [],
                "conflicts": [
                    {"id": row["id"], "subject": row["subject"], "data": row["data"]}
                    for row in current
                    if row["subject"] == proposal["subject"]
                ],
            }

        decision = self._judge(
            identity,
            context,
            task_id,
            question,
            refresh,
            binding=policy,
            optional=True,
        )
        if decision["status"] != "automatic":
            return _result("manual", decisions=[decision])
        value = decision["value"]
        if value != "retain":
            return _result("discarded" if value == "discard" else "manual", decisions=[decision])
        return _result(
            "retained",
            decisions=[decision],
            proposal={"kind": kind, **options},
            requires_employee_confirmation=kind == "confirmed_preference",
            requires_store_validation=True,
            content_digest=digest(payload),
        )

    def evaluate_preference(
        self,
        identity,
        context,
        *,
        task_id,
        subject,
        value,
        evidence_refs,
        policy,
        supersedes=None,
        template_id="memory_write",
    ):
        return self._evaluate_write(
            identity,
            context,
            task_id,
            kind="confirmed_preference",
            options={
                "subject": subject,
                "value": value,
                "evidence_refs": evidence_refs,
                "supersedes": supersedes,
            },
            policy=policy,
            template_id=template_id,
        )

    def evaluate_experience(
        self,
        identity,
        context,
        *,
        task_id,
        rule_id,
        rule_version,
        receipt_ref,
        policy,
        template_id="memory_write",
    ):
        return self._evaluate_write(
            identity,
            context,
            task_id,
            kind="verified_workflow_experience",
            options={"rule_id": rule_id, "rule_version": rule_version, "receipt_ref": receipt_ref},
            policy=policy,
            template_id=template_id,
        )

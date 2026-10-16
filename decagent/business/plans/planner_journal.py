"""Persist planner intent and safe proposals before publishing a plan revision.

The logical key excludes changing observations, event counters and budgets. A
durable intent without a saved proposal requires reconciliation, even when the
provider returned and its usage was settled before the process died. This module
does not issue usage reservations, release unknown fees or authorize retries.
"""

from decagent.decisions.contracts import digest
from decagent.storage.budget import BudgetExceeded

from .compiler import compile_plan, default_input_schema
from .planner import PlanProposal, PlanProposalInvalid
from .repository import PlanConflict, PlanPublicationStale
from .runtime import NodePending


class PlannerProposalJournal:
    def __init__(self, repository, *, after_response=None):
        self.repository = repository
        self.after_response = after_response
        self._proposed = None
        self._schema = None

    def _boundary(self, conn):
        task = self.repository.guard(conn)
        return {
            "employee_id": str(task["employee_id"]),
            "scope_id": str(task["business_scope_id"]),
            "scope_revision": task["scope_revision"],
            "account_revision": task["authorization_revision"],
            "plugin_digest": task["authorization_snapshot"]["plugin_digest"],
        }

    def _binding(self, conn, parent_digest, input_schema):
        repo = self.repository
        task, current = repo.guard(conn), repo._load(conn)
        if (current is None and parent_digest is not None) or (
            current is not None and current.digest != parent_digest
        ):
            raise PlanConflict("Planner request parent no longer matches the current plan")
        revision = current.revision + 1 if current is not None else 1
        if revision > task["budget"].get("max_plan_revisions", 4):
            raise BudgetExceeded("Plan revision budget exhausted")
        if current is not None:
            saved = conn.execute(
                "SELECT context_snapshot FROM app.business_plan_heads WHERE task_id=%s",
                (repo.task_id,),
            ).fetchone()["context_snapshot"]
            original = saved.get("input_schema", default_input_schema())
            if input_schema is not None and digest(input_schema) != digest(original):
                raise PlanConflict("Planner input schema cannot replace the published contract")
            input_schema = original
        schema = input_schema if input_schema is not None else default_input_schema()
        binding = {
            "task_id": str(repo.task_id),
            "target_revision": revision,
            "parent_digest": parent_digest,
            "purpose": "replan" if current is not None else "plan",
        }
        return binding, schema, current

    def _latest(self, conn, key):
        row = conn.execute(
            "SELECT payload FROM app.events WHERE task_id=%s "
            "AND kind IN ('business.planner_dispatched','business.planner_proposed',"
            "'business.planner_failed') AND payload->>'request_key'=%s ORDER BY seq DESC LIMIT 1",
            (self.repository.task_id, key),
        ).fetchone()
        return row["payload"] if row else None

    def _usage(self, conn, binding, before=()):
        rows = conn.execute(
            "SELECT id,dispatch_state,purpose,plan_revision,error_code FROM app.usage_records "
            "WHERE task_id=%s AND model_family='primary' AND plan_revision=%s "
            "AND purpose IN (%s,'format_repair') ORDER BY created_at,id",
            (self.repository.task_id, binding["target_revision"], binding["purpose"]),
        ).fetchall()
        previous = set(before)
        return [{**row, "id": str(row["id"])} for row in rows if str(row["id"]) not in previous]

    def _guard_request(self, record):
        with self.repository.database.connect() as conn:
            if digest(record["execution_boundary"]) != digest(self._boundary(conn)):
                raise PlanPublicationStale("Planner context permission changed before dispatch")

    @staticmethod
    def _pending(record):
        outcome = record.get("outcome", "unknown")
        reasons = {
            "not_sent": "Original planner attempt was not sent; explicit recovery is required",
            "invalid": "Original planner responses were invalid; explicit recovery is required",
            "unknown": "Original planner request or unsaved result requires reconciliation",
            "stale": "Planner proposal belongs to an earlier permission or contract version",
        }
        raise NodePending(
            {
                "kind": "reconciliation",
                "category": {
                    "not_sent": "planner_request_not_sent",
                    "invalid": "planner_proposal_invalid",
                    "unknown": "planner_request_unknown",
                    "stale": "planner_proposal_stale",
                }[outcome],
                "request_key": record["request_key"],
                "target_revision": record["target_revision"],
                "parent_digest": record["parent_digest"],
                "purpose": record["purpose"],
                "outcome": outcome,
                "reason": reasons[outcome],
            },
            unknown=outcome == "unknown",
        )

    def _proposal(self, record, schema, *, transforms=None):
        if record["input_schema_digest"] != digest(schema):
            raise PlanConflict("Cached planner proposal belongs to another input schema")
        plan = compile_plan(
            record["plan"], self.repository.spec, input_schema=schema, transforms=transforms
        ).plan
        if plan.revision != record["target_revision"] or plan.digest != record["plan_digest"]:
            raise ValueError("Cached planner proposal integrity changed")
        if type(record["repaired"]) is not bool:
            raise ValueError("Cached planner repair flag is invalid")
        self._proposed = self.repository._safe(record)
        self._schema = self.repository._safe(schema)
        return PlanProposal(plan, (), record["repaired"])

    def publish(self, proposal):
        """Fence proposal publication in the same transaction as its plan CAS."""
        record = self._proposed
        if (
            not record
            or not isinstance(proposal, PlanProposal)
            or (proposal.plan.digest != record["plan_digest"])
        ):
            raise ValueError("Publication requires this journal's saved validated proposal")
        try:
            return self.repository.publish(
                proposal.plan,
                parent_digest=record["parent_digest"],
                input_schema=self._schema,
                expected_boundary=record["execution_boundary"],
            )
        except PlanPublicationStale:
            self._pending({**record, "outcome": "stale"})

    def propose(self, planner, context, *, parent_digest=None, input_schema=None):
        repo = self.repository
        transforms = getattr(planner, "transforms", None)
        with repo.database.connect() as conn:
            binding, schema, current = self._binding(conn, parent_digest, input_schema)
            key = digest(binding)
            record = self._latest(conn, key)
            if record is not None:
                if record["phase"] == "proposed":
                    if record.get("execution_boundary") != self._boundary(conn):
                        self._pending({**record, "outcome": "stale"})
                    return self._proposal(record, schema, transforms=transforms)
                self._pending(record)
            if type(context) is not dict or context.get("parent_revision") != (
                current.revision if current is not None else None
            ):
                raise ValueError("Planner context must identify the actual current parent revision")
            boundary = self._boundary(conn)
            provided = context.get("boundary")
            if not isinstance(provided, dict) or digest(
                {name: provided.get(name) for name in boundary}
            ) != digest(boundary):
                raise NodePending(
                    {
                        "kind": "reconciliation",
                        "category": "planner_context_stale",
                        "reason": "Rebuild planning context under the current employee permissions",
                        "request_key": key,
                    }
                )
            record = {
                **binding,
                "request_key": key,
                "phase": "dispatched",
                "context_digest": digest(context),
                "input_schema_digest": digest(schema),
                "execution_boundary": boundary,
                "usage_before": [row["id"] for row in self._usage(conn, binding)],
            }
            repo._event(conn, "business.planner_dispatched", record)

        # Do not hold the task lock across network work: BudgetedPrimary owns its
        # own reservations and checks. SystemExit/process death leaves the intent.
        try:
            proposal = planner.plan(
                context,
                input_schema=schema,
                request_guard=lambda: self._guard_request(record),
            )
            if self.after_response:
                self.after_response(proposal)
            if not isinstance(proposal, PlanProposal) or type(proposal.repaired) is not bool:
                raise ValueError("Planner must return a typed plan proposal")
            compiled = compile_plan(
                proposal.plan, repo.spec, input_schema=schema, transforms=transforms
            ).plan
            if compiled.revision != binding["target_revision"] or (
                current is not None and compiled.plan_id != current.plan_id
            ):
                raise ValueError("Proposed plan must retain its identity and follow its parent")
            safe_plan = repo._safe(compiled.to_mapping())
        except Exception as exc:
            with repo.database.connect() as conn:
                self._binding(conn, parent_digest, schema)
                attempts = self._usage(conn, binding, record["usage_before"])
                states = [row["dispatch_state"] for row in attempts]
                if isinstance(exc, PlanPublicationStale):
                    outcome = "stale"
                elif states and all(state == "not_sent" for state in states):
                    outcome = "not_sent"
                elif (
                    states
                    and all(state in {"not_sent", "responded"} for state in states)
                    and "responded" in states
                    and isinstance(exc, (PlanProposalInvalid, ValueError))
                ):
                    outcome = "invalid"
                else:
                    outcome = "unknown"
                failed = {
                    **record,
                    "phase": "failed",
                    "outcome": outcome,
                    "usage": attempts,
                    "error_type": type(exc).__name__,
                }
                repo._event(conn, "business.planner_failed", failed)
            self._pending(failed)

        with repo.database.connect() as conn:
            self._binding(conn, parent_digest, schema)
            usage = self._usage(conn, binding, record["usage_before"])
            if record["execution_boundary"] != self._boundary(conn):
                failed = {**record, "phase": "failed", "outcome": "stale", "usage": usage}
                repo._event(conn, "business.planner_failed", failed)
            else:
                proposed = {
                    **record,
                    "phase": "proposed",
                    "plan": safe_plan,
                    "plan_digest": compiled.digest,
                    "repaired": proposal.repaired,
                    "usage": usage,
                }
                repo._event(conn, "business.planner_proposed", proposed)
                return self._proposal(proposed, schema, transforms=transforms)
        self._pending(failed)

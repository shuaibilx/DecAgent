"""Request bounded local revisions from recorded facts and preserve the parent CAS.

This service does not fabricate triggers from routine branch choices, amend an
employee's goal, invalidate an operation, or turn a planner answer into authority.
Authenticated goal amendments and prepared-operation corrections are separate.
"""

from decagent.business.context import ContextBuilder, TaskWorkingMemory, WorkingMemory
from decagent.storage.budget import BudgetExceeded

from .planner import REPLAN_REASONS
from .planner_journal import PlannerProposalJournal
from .repository import PlanConflict


class PlanPatchService:
    def __init__(self, repository, planner):
        self.repository, self.planner = repository, planner

    def replan(self, reason, *, evidence_nodes):
        if reason not in REPLAN_REASONS or reason == "user_goal_changed":
            raise ValueError(
                "Replanning requires a recorded trigger or authenticated goal amendment"
            )
        if not isinstance(evidence_nodes, (tuple, list)) or not evidence_nodes:
            raise ValueError("Replanning requires actual saved observation references")
        if len(set(evidence_nodes)) != len(evidence_nodes):
            raise ValueError("Duplicate replan observation reference")
        with self.repository.database.connect() as conn:
            task = self.repository.guard(conn)
            parent = self.repository._load(conn)
            if parent is None:
                raise PlanConflict("Cannot revise an unpublished plan")
            if parent.revision >= min(task["budget"].get("max_plan_revisions", 4), 4):
                raise BudgetExceeded("Plan revision budget exhausted")
            rows = conn.execute(
                "SELECT * FROM app.business_plan_nodes WHERE task_id=%s "
                "ORDER BY created_at,node_id,execution_generation",
                (self.repository.task_id,),
            ).fetchall()
            known = {row["node_id"]: row for row in rows}
            if any(
                name not in known
                or known[name]["status"] == "running"
                or known[name]["output_snapshot"] is None
                for name in evidence_nodes
            ):
                raise ValueError("Trigger does not reference a saved observation")
            memory = WorkingMemory.from_mapping(
                TaskWorkingMemory(self.repository)._read(conn, task)
            )
            evidence = [
                {
                    "kind": "recorded_node_observation",
                    "node_id": name,
                    "status": known[name]["status"],
                    "input_ref": {
                        "kind": "node_input",
                        "node_id": name,
                        "generation": known[name]["execution_generation"],
                        "digest": known[name]["input_digest"],
                    },
                    # ContextBuilder verifies this digest against the locked
                    # memory projection before exposing current business data.
                    "output": known[name]["output_snapshot"],
                }
                for name in evidence_nodes
            ]
            snapshot = conn.execute(
                "SELECT context_snapshot FROM app.business_plan_heads WHERE task_id=%s",
                (self.repository.task_id,),
            ).fetchone()["context_snapshot"]
            # Reuse the same locked facts as execution. Required trigger data, the
            # whole finite plan, Jev/human evidence and unresolved operations all
            # cross the single bounded context boundary before any paid request.
            context = (
                ContextBuilder(memory, system_prefix="")
                .planning(replan_reason=reason, required_evidence=evidence)
                .to_mapping()
            )
            self.repository._event(
                conn,
                "business.replan_requested",
                {
                    "reason": reason,
                    "parent_revision": parent.revision,
                    "parent_digest": parent.digest,
                    "evidence_nodes": list(evidence_nodes),
                },
            )
        journal = PlannerProposalJournal(self.repository)
        proposed = journal.propose(
            self.planner,
            context,
            parent_digest=parent.digest,
            input_schema=snapshot.get("input_schema"),
        )
        # The provider request remains charged if another revision won meanwhile.
        # Publication rechecks all frozen nodes under the current task lease.
        return journal.publish(proposed)

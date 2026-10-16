"""An employee parameter edit supersedes a rejected draft, never an unknown write."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.plans.contracts import Plan
from decagent.business.plans.handlers import BusinessNodeHandlers
from decagent.business.plans.planner import PlanProposal
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import PlanRuntime
from decagent.decisions.contracts import digest
from evals.integration.test_business_conditional_mcp import create_plan
from evals.integration.test_conditional_action_budget import executor

pytestmark = pytest.mark.integration


def prepared_edit(database, *, invalidate=True):
    """Persist the same facts as the authenticated action-edit endpoint.

    OperationService still authenticates the real fixture browser before rejection;
    endpoint authentication/idempotency is covered in gateway integration tests.
    """
    value, assessment = executor(database)
    assessment = {**assessment, "outcome": "confirm"}
    repo = PlanRepository(database, value.task["id"], "action-budget", value.generation)
    plan = repo.publish(create_plan(value.spec, assessment["snapshot"]["params"]))
    inputs = assessment["snapshot"]["params"]
    repo.begin("create", inputs, {key: {"kind": "literal"} for key in inputs})
    action = value.plan("plan:create:1", assessment)
    repo.bind_action("create", action["id"])
    repo.dispatch("create")
    result = value.execute(action["id"], action["digest"])
    assert result["status"] == "awaiting_approval"
    repo.finish("create", "waiting", {"request": {"kind": "action"}})
    if invalidate:
        value.client.operations.reply(
            value.client.browser,
            result["operation_id"],
            result["operation"]["input_hash"],
            "reject",
        )
    approval_id = uuid4()
    changed = {**inputs, "title": "Corrected by original employee"}
    record = {
        "business": True,
        "source": "employee_session",
        "employee_id": str(value.task["employee_id"]),
        "status": "edited",
        "operation_id": result["operation_id"],
        "input_hash": result["operation"]["input_hash"],
        "action_digest": action["digest"],
        "edited_params": changed,
    }
    with database.connect() as conn:
        conn.execute("UPDATE app.actions SET status='denied' WHERE id=%s", (action["id"],))
        conn.execute(
            "INSERT INTO app.approvals"
            "(id,task_id,action_id,digest,kind,status,reply,reply_payload) "
            "VALUES(%s,%s,%s,%s,'action','replied','edit',%s)",
            (
                approval_id,
                repo.task_id,
                action["id"],
                action["digest"],
                Jsonb({"policy_record": record}),
            ),
        )
    return repo, value, plan, action, result, approval_id, changed


def test_corrected_prepared_node_uses_new_generation_and_another_confirmation(database):
    repo, value, parent, action, old, approval_id, changed = prepared_edit(database)
    updated = repo.apply_correction(approval_id)
    assert updated.revision == 2 and updated.plan_id == parent.plan_id
    assert repo.apply_correction(approval_id).digest == updated.digest
    assert repo.counters()["node_attempts"] == 1
    requests = []
    services = SimpleNamespace(
        executor=value,
        task=value.task,
        _evidence=lambda *args: {"directory": value.client.read("directory", {})},
        human_request=lambda data: requests.append(data) or {"kind": "action"},
    )
    runtime = PlanRuntime(repo, BusinessNodeHandlers(repo, services).execute)
    pending = runtime.step()
    assert pending["status"] == "waiting" and pending["request"]["kind"] == "action"
    assert requests[-1]["assessment"]["snapshot"]["params"] == changed
    rows = repo.nodes()
    assert [row["execution_generation"] for row in rows] == [1, 2]
    assert rows[0]["status"] == "waiting" and rows[0]["action_id"] == action["id"]
    assert rows[1]["node_digest"] != rows[0]["node_digest"]
    with database.connect() as conn:
        operations = conn.execute(
            "SELECT * FROM business.operations ORDER BY created_at"
        ).fetchall()
        assert len(operations) == 2
        assert operations[0]["state"] == "rejected"
        assert operations[1]["state"] == "awaiting_approval"
        assert operations[0]["id"] != operations[1]["id"]
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 0
        )
    replacement = value.client.operations.reply(
        value.client.browser,
        requests[-1]["operation_id"],
        requests[-1]["operation"]["input_hash"],
        "approve",
    )
    assert replacement["state"] == "ready"
    # Reconstruct the interpreter as after a process restart; no new operation.
    runtime = PlanRuntime(repo, BusinessNodeHandlers(repo, services).execute)
    assert runtime.step()["status"] == "advanced"
    assert runtime.step()["status"] == "advanced"
    completed = runtime.step()
    assert completed["status"] == "complete" and completed["completion"]["valid"]
    assert repo.counters()["node_attempts"] == 3
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 2
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )


@pytest.mark.parametrize("state", ["ready", "running", "unknown", "succeeded"])
def test_correction_cannot_supersede_an_operation_without_proven_rejection(database, state):
    repo, value, parent, action, old, approval_id, _ = prepared_edit(database, invalidate=False)
    if state == "succeeded":
        value.client.operations.reply(
            value.client.browser, old["operation_id"], old["operation"]["input_hash"], "approve"
        )
        with database.connect() as conn:
            conn.execute("UPDATE app.actions SET status='planned' WHERE id=%s", (action["id"],))
        assert value.execute(action["id"], action["digest"])["status"] == "succeeded"
        with database.connect() as conn:
            conn.execute("UPDATE app.actions SET status='denied' WHERE id=%s", (action["id"],))
    else:
        with database.connect() as conn:
            conn.execute(
                "UPDATE business.operations SET state=%s WHERE id=%s", (state, old["operation_id"])
            )
    with pytest.raises(ValueError, match="reject|invalid|unknown|operation"):
        repo.apply_correction(approval_id)
    assert repo.current().digest == parent.digest
    assert len(repo.nodes()) == 1


def test_correction_rechecks_original_employee_and_revision_limit(database):
    repo, _, parent, _, _, approval_id, _ = prepared_edit(database)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET reply_payload=jsonb_set(reply_payload,"
            "'{policy_record,employee_id}',%s) WHERE id=%s",
            (Jsonb(str(uuid4())), approval_id),
        )
    with pytest.raises(ValueError, match="employee|provenance"):
        repo.apply_correction(approval_id)
    assert repo.current().revision == 1
    assert digest(parent.to_mapping()) == digest(repo.current().to_mapping())


def test_correction_cannot_reset_revision_budget(database):
    repo, _, _, _, _, approval_id, _ = prepared_edit(database)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET budget=budget||'{\"max_plan_revisions\":1}'::jsonb WHERE id=%s",
            (repo.task_id,),
        )
    with pytest.raises(Exception, match="revision|Revision"):
        repo.apply_correction(approval_id)
    assert repo.current().revision == 1 and repo.counters()["node_attempts"] == 1


def test_replan_cannot_revive_superseded_generation_before_new_execution(database):
    from evals.integration.test_business_plan_repository import revise

    repo, _, parent, _, _, approval_id, _ = prepared_edit(database)
    updated = repo.apply_correction(approval_id)
    candidate = revise(parent, 3)
    with pytest.raises(ValueError, match="correct|supersed|frozen"):
        repo.publish(candidate, parent_digest=updated.digest)
    assert repo.current().revision == 2


def test_later_planning_uses_authenticated_corrected_goal(database):
    from decagent.business.plans.patches import PlanPatchService
    from evals.integration.test_business_plan_repository import revise

    repo, _, _, _, _, approval_id, changed = prepared_edit(database)
    updated = repo.apply_correction(approval_id)
    calls = []

    class Planner:
        def plan(self, context, **kwargs):
            calls.append(context)
            return PlanProposal(Plan.from_mapping(revise(updated, 3)), (), False)

    PlanPatchService(repo, Planner()).replan("new_observation", evidence_nodes=["create"])
    assert changed["title"] in calls[0]["instruction"]
    assert calls[0]["evidence"][0]["requires_refresh"] is True
    assert "output" not in calls[0]["evidence"][0]

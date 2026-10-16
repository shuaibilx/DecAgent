"""Conditional tasks use the existing employee operation reply and reauthentication core."""

import pytest

from decagent.business.plans.repository import PlanRepository
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import ReplyInput
from evals.integration.test_business_conditional_mcp import create_plan
from evals.integration.test_conditional_action_budget import executor

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "decision,status,operation_state",
    [
        ("approve", "approved", "ready"),
        ("reject", "rejected", "rejected"),
        ("edit", "edited", "rejected"),
    ],
)
def test_conditional_action_reply_keeps_original_operation_and_edit_evidence(
    database,
    decision,
    status,
    operation_state,
):
    value, assessment = executor(database)
    assessment = {**assessment, "outcome": "confirm"}
    repo = PlanRepository(database, value.task["id"], "action-budget", value.generation)
    plan = repo.publish(create_plan(value.spec, assessment["snapshot"]["params"]))
    params = assessment["snapshot"]["params"]
    repo.begin("create", params, {key: {"kind": "literal"} for key in params})
    action = value.plan("plan:create:1", assessment)
    repo.bind_action("create", action["id"])
    repo.dispatch("create")
    result = value.execute(action["id"], action["digest"])
    assert result["status"] == "awaiting_approval"
    approval = database.ensure_approval(repo.task_id, action["id"], action["digest"], "action")
    repo.finish("create", "waiting", {"request": {"kind": "action"}})
    changed = {**params, "title": "Employee corrected the parameters"}
    body = ReplyInput(
        expected_digest=approval["digest"],
        decision=decision,
        edited_params=changed if decision == "edit" else None,
    )
    with database.connect() as conn:
        task = conn.execute(
            "UPDATE app.tasks SET state='awaiting_approval',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s RETURNING *",
            (repo.task_id,),
        ).fetchone()
        receipt = BusinessControl(database).reply_employee(
            conn, task, approval, body, value.client.browser, "browser-response"
        )
    assert receipt["policy_record"]["status"] == status
    assert value.client.query_operation(result["operation_id"])["state"] == operation_state
    assert database.get_task(repo.task_id)["state"] == "queued"
    if decision == "edit":
        assert receipt["policy_record"]["edited_params"] == changed
        with database.connect() as conn:
            assert (
                conn.execute(
                    "SELECT status FROM app.actions WHERE id=%s", (action["id"],)
                ).fetchone()["status"]
                == "denied"
            )
        held = database.claim(repo.task_id, "correction-worker", seconds=120)
        renewed = PlanRepository(
            database, repo.task_id, "correction-worker", held["lease_generation"]
        )
        corrected = renewed.apply_correction(approval["id"])
        assert corrected.revision == plan.revision + 1
        assert renewed.apply_correction(approval["id"]).digest == corrected.digest


def test_conditional_task_reauthentication_preserves_graph_and_actor(database):
    value, _ = executor(database)
    with database.connect() as conn:
        waiting = conn.execute(
            "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s RETURNING *",
            (value.task["id"],),
        ).fetchone()
        renewed = BusinessControl(database).reauthenticate(
            conn, waiting, value.client.browser, value.client.identity.credential_ref
        )
    assert renewed["graph_version"] == "business-3"
    assert renewed["employee_id"] == value.client.identity.employee_id

"""Permission changes cannot relabel old evidence or ordinary employee answers."""

import json
from dataclasses import replace

import pytest

from decagent.business.conditional_services import ConditionalBusinessServices, _EmployeeSupplement
from decagent.business.context import ContextBuilder, TaskWorkingMemory
from decagent.business.plans.patches import PlanPatchService
from decagent.business.plans.planner import PlanProposal
from decagent.business.plans.runtime import NodePending
from decagent.config import Settings
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import ReplyInput
from decagent.runtime.config import RuntimeConfig
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import node
from evals.integration.conditional_support import create_mixed_business_tasks
from evals.integration.test_business_conditional_batches import Adapter
from evals.integration.test_business_conditional_services import choice_plan
from evals.integration.test_business_context import memory_for, save_directory
from evals.integration.test_business_plan_repository import repository, revise

pytestmark = pytest.mark.integration


class Planner:
    def __init__(self, plan):
        self.value, self.calls = plan, []

    def plan(self, context, **kwargs):
        self.calls.append(context)
        return PlanProposal(self.value, (), False)


def harness(database, repo, backend, tmp_path):
    task = database.get_task(repo.task_id)
    if not hasattr(backend, "epoch_adapter"):
        backend.epoch_adapter = Adapter()
    return ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        task,
        repo.owner,
        repo.generation,
        mcp_client=backend,
        decision_adapter=backend.epoch_adapter,
    )


def change_epoch(database, repo, backend, epoch):
    with database.connect() as conn:
        task = repo.guard(conn)
        if epoch == "scope":
            conn.execute(
                "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
                (task["business_scope_id"],),
            )
        else:
            conn.execute(
                "UPDATE business.employee_accounts SET credential_revision=credential_revision+1 "
                "WHERE id=%s",
                (task["employee_id"],),
            )
            conn.execute(
                "UPDATE business.mcp_credentials SET account_revision=account_revision+1 "
                "WHERE id=%s",
                (task["credential_ref"],),
            )
            conn.execute(
                "UPDATE business.employee_sessions SET account_revision=account_revision+1 "
                "WHERE employee_id=%s",
                (task["employee_id"],),
            )
            backend.identity = replace(
                backend.identity, account_revision=backend.identity.account_revision + 1
            )
            backend.browser = replace(
                backend.browser, account_revision=backend.browser.account_revision + 1
            )


def reply(database, service, request, backend, text, tmp_path):
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (service.repository.task_id,),
        )
        task = conn.execute(
            "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (service.repository.task_id,)
        ).fetchone()
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (request["approval_id"],)
        ).fetchone()
        BusinessControl(database).reply_employee(
            conn,
            task,
            approval,
            ReplyInput(expected_digest=request["digest"], decision="confirm", confirmation=text),
            backend.browser,
            "epoch-reply-" + request["approval_id"],
        )
    held = database.claim(service.repository.task_id, "epoch-resume", seconds=120)
    repo = type(service.repository)(
        database, held["id"], held["lease_owner"], held["lease_generation"]
    )
    return harness(database, repo, backend, tmp_path)


@pytest.mark.parametrize("entry", ["patch_service", "context_builder"])
def test_required_evidence_cannot_restore_business_values_filtered_by_epoch(database, entry):
    sample = create_mixed_business_tasks(database)
    repo, memory = memory_for(database, sample)
    marker = "OLD-SCOPE-READ-ONLY-MARKER"
    save_directory(repo, sample, extra={"epoch_marker": marker})
    old_output = repo.nodes()[0]["output_snapshot"]
    change_epoch(database, repo, sample["backend"], "scope")
    state = memory.rebuild()
    assert "value" not in state.to_mapping()["observations"][0]
    if entry == "patch_service":
        planner = Planner(revise(repo.current(), 2))
        PlanPatchService(repo, planner).replan("new_observation", evidence_nodes=["directory"])
        context = planner.calls[0]
    else:
        context = (
            ContextBuilder(state)
            .planning(
                replan_reason="new_observation",
                required_evidence=[
                    {
                        "kind": "recorded_node_observation",
                        "node_id": "directory",
                        "output": old_output,
                    }
                ],
            )
            .to_mapping()
        )
    assert marker not in json.dumps(context)
    evidence = context["evidence"][0]
    assert evidence["requires_refresh"] is True
    assert evidence["output_ref"]["digest"]
    assert repo.nodes()[0]["output_snapshot"] == old_output


@pytest.mark.parametrize("kind", ["ask", "scope"])
@pytest.mark.parametrize("epoch", ["scope", "account"])
def test_clarification_waits_rebind_after_epoch_change_without_relabeling_old_reply(
    database, tmp_path, kind, epoch
):
    sample = create_mixed_business_tasks(database)
    repo = repository(database, sample)
    if kind == "ask":
        plan = read_plan()
        plan["entry"] = "question"
        plan["nodes"].insert(
            0,
            node(
                "question",
                "ask_user",
                prompt="请补充当前范围的说明",
                answer={"value_type": "string", "nullable": False},
                next=["directory"],
            ),
        )
        repo.publish(plan)
    else:
        repo.publish(choice_plan(repo.spec))
    service = harness(database, repo, sample["backend"], tmp_path)
    if kind == "ask":
        pending = service.step({})["request"]
    else:
        assert service.step({})["status"] == "advanced"
        pending = service._scope_wait("judge", "Current scope exceeds candidate limit")
    marker = "OLD-CLARIFICATION-FROM-PRIOR-EPOCH"
    service = reply(database, service, pending, sample["backend"], marker, tmp_path)
    change_epoch(database, service.repository, sample["backend"], epoch)
    context = (
        ContextBuilder(TaskWorkingMemory(service.repository).rebuild()).planning().to_mapping()
    )
    assert marker not in json.dumps(context)
    if kind == "ask":
        result = service.step({})
        assert result["status"] == "waiting"
        refreshed = result["request"]
    else:
        compiled, rows, bound = service._runtime()._state()
        with pytest.raises(NodePending) as waiting:
            service._decide(compiled.node("judge"), {}, bound, rows["judge"])
        refreshed = waiting.value.request
        service.repository.finish(
            "judge", "waiting", {**rows["judge"]["output_snapshot"], "request": refreshed}
        )
    assert refreshed["approval_id"] != pending["approval_id"]
    assert marker not in json.dumps(refreshed)
    service = reply(
        database, service, refreshed, sample["backend"], "CURRENT-EPOCH-ANSWER", tmp_path
    )
    if kind == "ask":
        assert service.step({})["status"] == "advanced"
        result = next(row for row in service.repository.nodes() if row["node_id"] == "question")[
            "output_snapshot"
        ]["result"]
        assert result["data"] == "CURRENT-EPOCH-ANSWER"
        assert result["scope_revision"] == service.repository.guard()["scope_revision"]
        assert result["account_revision"] == service.repository.guard()["authorization_revision"]
    else:
        compiled, rows, bound = service._runtime()._state()
        with pytest.raises(_EmployeeSupplement) as supplement:
            service._decide(compiled.node("judge"), {}, bound, rows["judge"])
        assert supplement.value.record["text"] == "CURRENT-EPOCH-ANSWER"
    context = (
        ContextBuilder(TaskWorkingMemory(service.repository).rebuild()).planning().to_mapping()
    )
    assert marker not in json.dumps(context)
    assert "CURRENT-EPOCH-ANSWER" in json.dumps(context)
    with database.connect() as conn:
        original = conn.execute(
            "SELECT reply_payload FROM app.approvals WHERE id=%s", (pending["approval_id"],)
        ).fetchone()
    assert original["reply_payload"]["policy_record"]["confirmation"] == marker
    assert sample["backend"].epoch_adapter.calls == 0
    assert sample["backend"].writes == 0


def test_scope_epoch_reply_can_replan_and_refresh_stale_business_sources(database, tmp_path):
    from evals.deterministic.test_business_plan_contracts import literal

    sample = create_mixed_business_tasks(database)
    repo = repository(database, sample)
    repo.publish(choice_plan(repo.spec))
    service = harness(database, repo, sample["backend"], tmp_path)
    service.step({})
    pending = service._scope_wait("judge", "Candidate scope exceeds request limit")
    service = reply(database, service, pending, sample["backend"], "OLD-SCOPE-REPLY", tmp_path)
    change_epoch(database, service.repository, sample["backend"], "scope")
    waiting = service.step({})
    assert waiting["status"] == "waiting"
    refreshed = waiting["request"]
    assert refreshed["kind"] == "clarification"
    assert refreshed["approval_id"] != pending["approval_id"]
    service = reply(
        database, service, refreshed, sample["backend"], "Refresh directory only", tmp_path
    )
    candidate = revise(service.repository.current(), 2)
    candidate["goal"]["evidence"][0]["source"]["node_id"] = "fresh_directory"
    for definition in candidate["nodes"]:
        if definition["id"] == "choose":
            definition["cases"][0]["condition"]["left"] = literal(False, "boolean")
        if definition["id"] in {"yes", "no"}:
            definition["next"] = ["fresh_directory"]
    candidate["nodes"].insert(-1, {
        **candidate["nodes"][0], "id": "fresh_directory", "next": ["done"],
    })
    planner = service.planner = Planner(candidate)
    for _ in range(12):
        result = service.step({})
        if result["status"] != "advanced":
            break
    assert result["status"] == "complete" and result["completion"]["valid"]
    assert len(planner.calls) == 1
    assert "OLD-SCOPE-REPLY" not in json.dumps(planner.calls[0])
    assert "Refresh directory only" in json.dumps(planner.calls[0])
    rows = {row["node_id"]: row for row in service.repository.nodes()}
    assert rows["judge"]["status"] == "skipped"
    assert rows["directory"]["output_snapshot"]["result"]["scope_revision"] == 1
    assert rows["fresh_directory"]["output_snapshot"]["result"]["scope_revision"] == 2
    assert sample["backend"].epoch_adapter.calls == 0 and sample["backend"].writes == 0

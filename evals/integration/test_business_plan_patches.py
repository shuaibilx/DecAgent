"""Local replan proposals cite real recorded facts and cannot erase executed nodes."""

import importlib
from types import SimpleNamespace

import pytest

from decagent.business.plans.contracts import Plan
from decagent.business.plans.planner import PlanProposal
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks
from evals.integration.test_business_plan_repository import repository, revise

pytestmark = pytest.mark.integration


def prepare(database, sample):
    repo = repository(database, sample)
    original = repo.publish(read_plan())
    task = repo.guard()
    repo.begin("directory", {}, {})
    repo.finish(
        "directory",
        "succeeded",
        {
            "result": {
                "kind": "read_tool",
                "verified": True,
                "data": sample["backend"].read("directory", {}),
                "employee_id": str(task["employee_id"]),
                "scope_revision": task["scope_revision"],
                "account_revision": task["authorization_revision"],
            },
            "control": {"targets": ["done"]},
        },
    )
    return repo, original


def service(repo, candidate):
    api = importlib.import_module("decagent.business.plans.patches")

    class Planner:
        calls = []

        def plan(self, context, **kwargs):
            self.calls.append(context)
            return PlanProposal(Plan.from_mapping(candidate), (), False)

    planner = Planner()
    return api.PlanPatchService(repo, planner), planner


def test_replan_cites_saved_observation_and_preserves_lifetime_counters(
    database, mixed_business_tasks
):
    repo, original = prepare(database, mixed_business_tasks)
    repo.transition()
    patcher, planner = service(repo, revise(original, 2, "Clarified verified directory goal"))
    updated = patcher.replan("new_observation", evidence_nodes=["directory"])
    assert updated.revision == 2 and len(planner.calls) == 1
    context = planner.calls[0]
    assert context["parent_revision"] == 1 and context["parent_digest"] == original.digest
    assert context["evidence"][0]["node_id"] == "directory"
    assert context["frozen_nodes"] == ["directory"]
    assert context["boundary"]["semantic_selection_grants_write"] is False
    assert "budget" in context and "decisions" in context
    assert repo.counters()["node_attempts"] == 1 and repo.counters()["graph_transitions"] == 1


@pytest.mark.parametrize(
    "reason,nodes",
    [
        ("routine_branch", ["directory"]),
        ("new_observation", []),
        ("new_observation", ["invented"]),
        ("user_goal_changed", ["directory"]),
    ],
)
def test_replan_without_recorded_trigger_is_rejected_before_model(
    database, mixed_business_tasks, reason, nodes
):
    repo, original = prepare(database, mixed_business_tasks)
    patcher, planner = service(repo, revise(original, 2))
    with pytest.raises(ValueError):
        patcher.replan(reason, evidence_nodes=nodes)
    assert planner.calls == [] and repo.current().revision == 1


def test_replan_cannot_rewrite_recorded_prefix(database, mixed_business_tasks):
    repo, original = prepare(database, mixed_business_tasks)
    candidate = revise(original, 2)
    candidate["nodes"][0]["next"] = ["additional"]
    candidate["nodes"].insert(
        1,
        {
            **candidate["nodes"][0],
            "id": "additional",
            "next": ["done"],
        },
    )
    patcher, _ = service(repo, candidate)
    with pytest.raises(ValueError, match="frozen"):
        patcher.replan("uncovered_condition", evidence_nodes=["directory"])
    assert repo.current().revision == 1


def test_replan_cannot_move_entry_ahead_of_the_recorded_prefix(database, mixed_business_tasks):
    repo, original = prepare(database, mixed_business_tasks)
    candidate = revise(original, 2)
    fresh = {**candidate["nodes"][0], "id": "new_entry", "next": ["directory"]}
    candidate["entry"] = "new_entry"
    candidate["nodes"].insert(0, fresh)
    with pytest.raises(ValueError, match="entry|prefix"):
        repo.publish(candidate, parent_digest=original.digest)


def test_failed_control_prefix_cannot_be_bypassed_by_a_new_entry(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    original = repo.publish(read_plan())
    repo.begin("directory", {}, {})
    repo.finish(
        "directory",
        "failed",
        {"result": {"reason": "binding unavailable"}, "control": {"targets": ["done"]}},
    )
    repo.begin("done", {}, {})
    repo.finish("done", "succeeded", {"result": {"verified": True}})
    candidate = revise(original, 2)
    candidate["entry"] = "new_entry"
    candidate["nodes"].insert(
        0, {**candidate["nodes"][0], "id": "new_entry", "next": ["directory"]}
    )
    with pytest.raises(ValueError, match="entry|prefix"):
        repo.publish(candidate, parent_digest=original.digest)


def test_revision_limit_is_checked_before_paid_planning(database, mixed_business_tasks):
    repo, original = prepare(database, mixed_business_tasks)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET budget=budget||'{\"max_plan_revisions\":1}'::jsonb WHERE id=%s",
            (repo.task_id,),
        )
    patcher, planner = service(repo, revise(original, 2))
    with pytest.raises(Exception, match="revision|Revision"):
        patcher.replan("new_observation", evidence_nodes=["directory"])
    assert planner.calls == []


def test_mandatory_trigger_cannot_bypass_context_budget_before_planning(
    database, mixed_business_tasks
):
    from decagent.business.context import ContextBudgetError

    repo, original = prepare(database, mixed_business_tasks)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_plan_nodes SET output_snapshot=output_snapshot||%s "
            "WHERE task_id=%s AND node_id='directory'",
            (__import__("psycopg").types.json.Jsonb({"new_fact": "x" * 13000}), repo.task_id),
        )
    patcher, planner = service(repo, revise(original, 2))
    with pytest.raises(ContextBudgetError):
        patcher.replan("new_observation", evidence_nodes=["directory"])
    assert planner.calls == [] and repo.current().revision == 1


@pytest.mark.parametrize("mode", ["literal", "runtime_alias"])
def test_new_node_id_cannot_repeat_an_already_committed_write(database, mode):
    from copy import deepcopy

    from decagent.business.plans.handlers import BusinessNodeHandlers
    from decagent.business.plans.repository import PlanRepository
    from decagent.business.plans.runtime import PlanRuntime
    from evals.deterministic.test_business_plan_contracts import literal, node, reference
    from evals.integration.test_business_conditional_mcp import create_plan
    from evals.integration.test_conditional_action_budget import executor

    value, assessment = executor(database)
    repo = PlanRepository(database, value.task["id"], "action-budget", value.generation)
    parent = repo.publish(create_plan(value.spec, assessment["snapshot"]["params"]))
    services = SimpleNamespace(
        executor=value,
        task=value.task,
        _evidence=lambda *args: {"directory": value.client.read("directory", {})},
    )
    handlers = BusinessNodeHandlers(repo, services)
    assert PlanRuntime(repo, handlers.execute).step()["status"] == "advanced"
    candidate = revise(parent, 2)
    duplicate = deepcopy(candidate["nodes"][0])
    duplicate.update(id="repeat", next=["final"])
    candidate["nodes"][1] = node("done", "join", mode="all_active", next=["repeat"])
    candidate["nodes"].extend(
        [
            duplicate,
            node("final", "complete", depends_on=["repeat"], evidence=["created"]),
        ]
    )
    if mode == "literal":
        with pytest.raises(ValueError, match="repeat|duplicate|Repeat"):
            repo.publish(candidate, parent_digest=parent.digest)
        assert repo.current().revision == 1
    else:
        candidate["nodes"][1]["next"] = ["alias"]
        duplicate["inputs"]["title"] = {
            **reference("node_output", node_id="alias", path=[]),
            "provenance": "transform_result",
        }
        candidate["nodes"].insert(
            2,
            node(
                "alias",
                "transform",
                transform={"transform_id": "normalize_title", "version": "1"},
                inputs={"title": literal(assessment["snapshot"]["params"]["title"])},
                next=["repeat"],
            ),
        )
        repo.publish(candidate, parent_digest=parent.digest)
        resumed = PlanRuntime(repo, handlers.execute)
        for _ in range(8):
            result = resumed.step()
            if result["status"] != "advanced":
                break
        assert result["status"] == "stopped" and result["reason"] == "error"
        with database.connect() as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"]
                == 1
            )

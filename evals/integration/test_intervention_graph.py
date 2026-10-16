"""Actual SQL/checkpoints prove bounded adjustment prompts and authenticated feedback recovery."""

from dataclasses import replace
from importlib import import_module
from importlib.util import find_spec
from uuid import uuid4

import pytest
from langgraph.types import Command

from decagent.decisions import ChoiceAnswer, DecisionEngine
from decagent.decisions.contracts import evaluation
from decagent.models.primary import ModelTurn, ToolProposal
from decagent.policy import (
    ApprovalReply,
    CodeOrigin,
    CodeSnapshot,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    ReplyDecision,
    Source,
)
from decagent.runtime.graph import build_graph
from decagent.storage.approval_service import DurableApprovals

pytestmark = pytest.mark.integration


def setup(database, tmp_path):
    assert find_spec("decagent.runtime.intervention") is not None, "Intervention loop is missing"
    coordinator_type = import_module("decagent.runtime.intervention").InterventionCoordinator
    project = database.add_project("fixture/adaptive-intervention")
    task = database.create_task(project["id"], "a" * 40, {}, {"max_calls": 30})
    claimed = database.claim(task["id"], "worker")
    owner = Provenance(Source.OWNER, "owner", "session", True)
    context = PolicyContext(
        str(task["id"]),
        str(project["id"]),
        project["repository"],
        tmp_path,
        CodeSnapshot("a" * 40, "b" * 64, "main", CodeOrigin.OWNER),
        OwnerAuthorization(
            "auth", "1", str(task["id"]), str(project["id"]), project["repository"], owner
        ),
        "1",
    )

    class Strategy:
        name = "synthetic-fixture"
        calls = 0

        def evaluate(self, request):
            self.calls += 1
            question = request.questions[0]
            return evaluation(
                request,
                self.name,
                "synthetic-model",
                actual_model="synthetic-model",
                answers={
                    question.id: ChoiceAnswer(
                        "continue",
                        {k: float(k == "continue") for k in question.criteria},
                        1.0,
                    )
                },
            )

    strategy = Strategy()

    class Services:
        task_id = str(task["id"])
        lease_owner = "worker"
        generation = claimed["lease_generation"]
        goal = "Find why the repository read keeps failing"
        executions = 0
        policy_context = context

        def __init__(self):
            self.database = database
            self.approvals = DurableApprovals(database)
            self.intervention = coordinator_type(database, DecisionEngine(strategy))

        def current_context(self):
            return self.policy_context

        def invoke_model(self, messages):
            feedback = any("owner-feedback" in m["content"] for m in messages)
            if feedback:
                return ModelTurn("Ready for independent verification", (), None, "fixture")
            return ModelTurn(
                "",
                (
                    ToolProposal(
                        "read-" + str(self.executions + 1), "read_file", {"path": "missing.py"}
                    ),
                ),
                None,
                "fixture",
            )

        def execute(self, action, record, execution_id):
            self.executions += 1
            return {"status": "failed", "reason": "Repository file does not exist"}

        def assess_progress(self, progress):
            return self.intervention.assess(progress, self.current_context(), goal=self.goal)

        def request_help(self, progress, decision):
            return self.intervention.request_help(self, progress, decision)

        def verify_and_finalize(self):
            return {"status": "unverified"}

    services = Services()
    config = {"configurable": {"thread_id": services.task_id}, "recursion_limit": 300}
    initial = {
        "task_id": services.task_id,
        "messages": [{"role": "user", "content": "Find bug"}],
        "iterations": 0,
        "queue": [],
    }
    return services, strategy, config, initial, owner


def run_to_help(database, tmp_path):
    services, strategy, config, initial, owner = setup(database, tmp_path)
    with database.checkpointer() as saver:
        result = build_graph(services, saver).invoke(initial, config)
    assert result.get("__interrupt__"), "Repeated observations must eventually request help"
    assert services.executions == 6, "Two bounded changes should precede human escalation"
    assert result["intervention_decision"]["route"] == "escalate"
    assert result["progress"]["replans"] == 2
    return services, strategy, config, result, owner


def test_lease_loss_during_intervention_does_not_advance_checkpoint(database, tmp_path):
    services, _, config, initial, _ = setup(database, tmp_path)
    original = services.assess_progress

    def replaced(progress):
        result = original(progress)
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET lease_generation=lease_generation+1 WHERE id=%s",
                (services.task_id,),
            )
        return result

    services.assess_progress = replaced
    with database.checkpointer() as saver:
        graph = build_graph(services, saver)
        with pytest.raises(ValueError, match="lease is stale"):
            graph.invoke(initial, config)
        saved = graph.get_state(config)
    assert saved.next == ("progress",)
    assert saved.values["intervention_decision"]["route"] == "continue"
    assert saved.values["intervention_decision"]["signals"]["observed_steps"] == 1
    assert saved.values["progress"]["checked_total"] == 0


def test_model_requested_owner_feedback_starts_new_adjustment_cycle(database, tmp_path):
    services, _, config, initial, _ = setup(database, tmp_path)
    original_model, original_execute = services.invoke_model, services.execute

    def model(messages):
        if services.executions == 4 and not any("owner-feedback" in m["content"] for m in messages):
            return ModelTurn(
                "",
                (ToolProposal("owner-question", "request_user", {"question": "Which module?"}),),
                None,
                "fixture",
            )
        return original_model(messages)

    def execute(action, record, execution_id):
        if action.tool == "request_user":
            return {"status": "succeeded", "owner_response": "owner-feedback: inspect utils.py"}
        return original_execute(action, record, execution_id)

    services.invoke_model, services.execute = model, execute
    with database.checkpointer() as saver:
        result = build_graph(services, saver).invoke(initial, config)
    assert result["progress"]["replans"] == 0
    assert result["progress"]["episode_start"] == 5
    with database.connect() as conn:
        events = conn.execute(
            "SELECT * FROM app.events WHERE task_id=%s AND kind='intervention.feedback'",
            (services.task_id,),
        ).fetchall()
    assert len(events) == 1 and events[0]["payload"]["source"] == "request_user"


def test_lease_loss_during_feedback_does_not_commit_answer_to_checkpoint(database, tmp_path):
    services, _, config, result, owner = run_to_help(database, tmp_path)
    approval = services.approvals.read(result["__interrupt__"][0].value["approval_id"])
    services.approvals.reply(
        approval.id,
        ApprovalReply(
            str(uuid4()),
            approval.digest,
            ReplyDecision.CONFIRM,
            owner,
            confirmation="owner-feedback: inspect utils.py",
        ),
        context=services.current_context(),
    )
    original = services.request_help

    def replaced(progress, decision):
        feedback = original(progress, decision)
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET lease_generation=lease_generation+1 WHERE id=%s",
                (services.task_id,),
            )
        return feedback

    services.request_help = replaced
    with database.checkpointer() as saver:
        graph = build_graph(services, saver)
        with pytest.raises(ValueError, match="lease is stale"):
            graph.invoke(Command(resume={"approval_id": approval.id}), config)
        saved = graph.get_state(config)
    assert saved.next == ("help",)
    assert "feedback_id" not in saved.values["progress"]
    assert not any("owner-feedback" in m["content"] for m in saved.values["messages"])


def test_intervention_restarts_without_repeating_decision_or_action_then_resumes(
    database, tmp_path
):
    services, strategy, config, result, owner = run_to_help(database, tmp_path)
    pending = result["__interrupt__"][0].value
    with database.checkpointer() as saver:
        again = build_graph(services, saver).invoke(None, config)
    assert again["__interrupt__"][0].value["approval_id"] == pending["approval_id"]
    assert strategy.calls == 3 and services.executions == 6
    approval = services.approvals.read(pending["approval_id"])
    services.approvals.reply(
        approval.id,
        ApprovalReply(
            str(uuid4()),
            approval.digest,
            ReplyDecision.CONFIRM,
            owner,
            confirmation="owner-feedback: inspect utils.py",
        ),
        context=services.current_context(),
    )
    with database.checkpointer() as saver:
        resumed = build_graph(services, saver).invoke(
            Command(resume={"approval_id": approval.id}), config
        )
    assert resumed["verification"]["status"] == "unverified"
    assert resumed["progress"]["replans"] == 0 and resumed["iterations"] == 7
    assert services.executions == 6 and strategy.calls == 3
    assert sum("owner-feedback" in m["content"] for m in resumed["messages"]) == 1
    with database.connect() as conn:
        events = conn.execute(
            "SELECT kind,payload FROM app.events WHERE task_id=%s ORDER BY seq", (services.task_id,)
        ).fetchall()
    assert sum(e["kind"] == "intervention.replan" for e in events) == 2
    assert sum(e["kind"] == "intervention.feedback" for e in events) == 1
    assert services.current_context().authorization.version == "1"


def test_feedback_for_old_code_cannot_resume_new_snapshot(database, tmp_path):
    services, strategy, config, result, owner = run_to_help(database, tmp_path)
    old = services.approvals.read(result["__interrupt__"][0].value["approval_id"])
    services.approvals.reply(
        old.id,
        ApprovalReply(
            str(uuid4()), old.digest, ReplyDecision.CONFIRM, owner, confirmation="owner-feedback"
        ),
        context=services.current_context(),
    )
    services.policy_context = replace(
        services.policy_context,
        snapshot=replace(services.policy_context.snapshot, tree_digest="c" * 64),
    )
    with database.checkpointer() as saver:
        result = build_graph(services, saver).invoke(
            Command(resume={"approval_id": old.id}), config
        )
    assert result["__interrupt__"][0].value["approval_id"] != old.id
    assert services.executions == 6 and strategy.calls == 3


def test_identical_progress_inputs_in_different_tasks_have_distinct_durable_requests(database):
    from decagent.decisions import DecisionContext, build_request
    from decagent.decisions.durable import durable_decide

    project = database.add_project("fixture/progress-scope")

    class Strategy:
        name = "synthetic-fixture"
        calls = 0

        def evaluate(self, request):
            self.calls += 1
            return evaluation(
                request,
                self.name,
                "fixture",
                actual_model="fixture",
                answers={
                    "task_intervention": ChoiceAnswer(
                        "continue",
                        {
                            "continue": 1.0,
                            "replan": 0.0,
                            "clarify": 0.0,
                            "escalate": 0.0,
                        },
                        1.0,
                    ),
                },
            )

    strategy = Strategy()
    records = []
    for _ in range(2):
        task = database.create_task(project["id"], "a" * 40, {})
        request = build_request(
            ("task_intervention",),
            {
                "goal": "Identical goal",
                "attempts": [],
                "progress_signals": {"stalled": True},
            },
            DecisionContext(str(task["id"]), str(project["id"]), "code", "evidence", "1", "auth"),
        )
        result, identifier = durable_decide(database, DecisionEngine(strategy), request)
        assert result.evaluation.available
        records.append(identifier)
    assert len(set(records)) == 2 and strategy.calls == 2


@pytest.mark.parametrize("choice", ["continue", "replan", "clarify", "escalate"])
def test_matching_calibration_can_route_independent_question(database, tmp_path, choice):
    from decagent.decisions import CalibrationPolicy
    from decagent.runtime.intervention import InterventionCoordinator
    from decagent.runtime.progress import observe_progress

    services, _, _, _, _ = setup(database, tmp_path)

    class Strategy:
        name = "synthetic-calibration-fixture"

        def evaluate(self, request):
            criteria = request.questions[0].criteria
            return evaluation(
                request,
                self.name,
                "fixture-model",
                actual_model="fixture-model",
                answers={
                    "task_intervention": ChoiceAnswer(
                        choice, {k: float(k == choice) for k in criteria}, 1.0
                    )
                },
            )

    policy = CalibrationPolicy(
        "task_intervention", "1", "fixture-model", "synthetic-fixture", "1", 0.9, ("synthetic-1",)
    )
    coordinator = InterventionCoordinator(
        database, DecisionEngine(Strategy(), (policy,)), dataset_version="synthetic-fixture"
    )
    progress = None
    for index in range(2):
        progress = observe_progress(
            progress,
            {"tool": "read_file", "params": {"path": "x"}},
            {"status": "failed", "reason": "missing"},
            step_id=str(index),
        )
    result = coordinator.assess(progress, services.current_context(), goal="Find missing file")
    assert result["route"] == choice and result["calibrated"] is True

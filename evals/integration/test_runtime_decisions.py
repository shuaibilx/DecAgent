from dataclasses import replace

import pytest

from decagent.decisions import CalibrationPolicy, ChoiceAnswer, DecisionEngine
from decagent.decisions.contracts import evaluation
from decagent.policy import (
    Action,
    CodeOrigin,
    CodeSnapshot,
    Outcome,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
)
from decagent.runtime.decisions import DecisionCoordinator

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("legacy", [False, True])
def test_calibrated_route_is_durable_and_stale_scope_loses_permission(database, tmp_path, legacy):
    project = database.add_project("fixture/decision-runtime")
    task = database.create_task(project["id"], "a" * 40, {})
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
        name = "fixture"
        calls = 0

        def evaluate(self, request):
            self.calls += 1
            return evaluation(
                request,
                self.name,
                "jev-1.13.0",
                actual_model="jev-1.13.0",
                answers={
                    q.id: ChoiceAnswer(
                        "allow" if q.id == "action_risk" else "continue",
                        {
                            name: (0.92 * (2 / 3) + 1 / 3)
                            if name in {"allow", "continue"}
                            else (1 - 0.92) / 3
                            for name in q.criteria
                        },
                        0.92,
                    )
                    for q in request.questions
                },
            )

    strategy = Strategy()
    policies = tuple(
        CalibrationPolicy(q, "1", "jev-1.13.0", "fixture-human-labels", "1", 0.9, ("sample",))
        for q in ("action_risk", "intervention")
    )
    coordinator = DecisionCoordinator(
        database,
        DecisionEngine(strategy, policies),
        dataset_version="fixture-human-labels",
        legacy_intervention=legacy,
    )
    allowed, decision_id = coordinator.assess(
        Action("write_file", {"path": "fix.py", "content": "x"}),
        context,
        goal="Fix the specified bug",
    )
    assert allowed.outcome == Outcome.ALLOW and strategy.calls == 1
    with database.connect() as conn:
        question_version = conn.execute(
            "SELECT question_version FROM app.decision_records WHERE id=%s", (decision_id,)
        ).fetchone()["question_version"]
    expected = "action_risk:1+intervention:1" if legacy else "action_risk:1"
    assert question_version == expected, "Tool permission still bundles intervention"
    raised = DecisionCoordinator(
        database,
        DecisionEngine(strategy, tuple(replace(p, threshold=0.95) for p in policies)),
        dataset_version="fixture-human-labels",
        legacy_intervention=legacy,
    )
    assert raised.apply(decision_id, allowed.action, context).outcome == Outcome.REVIEW
    rechecked, same_id = raised.assess(allowed.action, context, goal="Fix the specified bug")
    assert rechecked.outcome == Outcome.REVIEW and same_id == decision_id and strategy.calls == 1
    stale = replace(context, snapshot=replace(context.snapshot, tree_digest="c" * 64))
    assert coordinator.apply(decision_id, allowed.action, stale).outcome == Outcome.REVIEW
    removed = DecisionCoordinator(database, DecisionEngine(strategy))
    assert removed.apply(decision_id, allowed.action, context).outcome == Outcome.REVIEW
    read, _ = coordinator.assess(Action("read_file", {"path": "fix.py"}), context, goal="Inspect")
    assert read.outcome == Outcome.ALLOW and strategy.calls == 1
    denied, _ = coordinator.assess(
        Action("write_file", {"path": "../outside", "content": "x"}), context, goal="Inspect"
    )
    assert denied.outcome == Outcome.DENY and strategy.calls == 1

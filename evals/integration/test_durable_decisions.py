"""Semantic calls persist an intent before dispatch and never repeat an unknown result."""

import pytest

from decagent.decisions import DecisionContext, DecisionEngine, NoulAnswer, build_request
from decagent.decisions.contracts import evaluation
from decagent.decisions.durable import durable_decide

pytestmark = pytest.mark.integration


def test_jev_replay_and_crash_do_not_repeat_inference(database):
    project = database.add_project("fixture/decision-journal")
    task = database.create_task(project["id"], "a" * 40, {})
    request = build_request(
        ["memory_read_needed"],
        {"task_summary": "Fix a bounded source defect", "current_version": "code"},
        DecisionContext(str(task["id"]), str(project["id"]), "code", "evidence", "1", "1"),
    )

    class Strategy:
        name, model, provider = "jev", "jev-1.13.0", "typesafe"
        calls = 0
        crash = False

        def evaluate(self, current):
            self.calls += 1
            if self.crash:
                raise KeyboardInterrupt("Simulated power loss after provider dispatch")
            return evaluation(
                current,
                self.name,
                self.model,
                actual_model=self.model,
                answers={"memory_read_needed": NoulAnswer(0.9)},
            )

    strategy = Strategy()
    engine = DecisionEngine(strategy)
    first, identifier = durable_decide(database, engine, request)
    replayed, second_id = durable_decide(database, engine, request)
    assert identifier == second_id and strategy.calls == 1
    assert replayed.evaluation.cached is True
    assert replayed.evaluation.error is None
    assert replayed.evaluation.answers == first.evaluation.answers
    assert dict(first.routes) == dict(replayed.routes)
    changed = build_request(
        ["memory_read_needed"],
        {"task_summary": "Different evidence", "current_version": "code"},
        request.context,
    )
    strategy.crash = True
    with pytest.raises(KeyboardInterrupt):
        durable_decide(database, engine, changed)
    unknown, _ = durable_decide(database, engine, changed)
    assert strategy.calls == 2
    assert unknown.routes["memory_read_needed"] == "review"
    assert unknown.evaluation.error == "inference_result_unknown"

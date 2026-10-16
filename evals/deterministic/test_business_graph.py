"""Business routes, durable waits and hard boundaries without external capabilities."""

from copy import deepcopy
from importlib.util import find_spec

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from decagent.business.graph import GRAPH_VERSION
from decagent.storage.budget import BudgetExceeded


class Services:
    def __init__(self):
        self.task = {"id": "business-test", "graph_version": GRAPH_VERSION}
        self.calls = []
        self.exhausted = False
        self.cancelled = False
        self.human = False
        self.reply_calls = []
        self.unknown = False
        self.verified = False

    def guard(self):
        if self.cancelled:
            raise ValueError("Task cancelled")
        if self.exhausted and self.calls:
            raise BudgetExceeded("Calls exhausted")

    def run_phase(self, phase, data):
        self.calls.append(phase)
        if phase == "prepare":
            return {"route": "decide", "evidence": {"actual": True}}
        if phase == "decide":
            return {"route": "finalize" if self.verified else "human" if self.human else "plan"}
        if phase == "plan":
            return {"route": "validate", "queue": [{"command": "read"}]}
        if phase == "validate":
            return {"route": "execute", "action_id": "action-1"}
        if phase == "execute":
            return {
                "route": "verify",
                "execution": {"status": "unknown" if self.unknown else "succeeded"},
            }
        if phase == "verify":
            if self.unknown:
                from decagent.business.errors import BusinessReconciliationRequired

                raise BusinessReconciliationRequired(
                    {"action_id": "action-1", "reason": "Receipt unknown"}
                )
            if data.get("reconciliation"):
                assert data["reconciliation_reply"]["kind"] == "reconciliation"
            self.verified = True
            return {"route": "decide", "completion": {"valid": True}}
        assert phase == "finalize"
        return {"route": "__end__"}

    def human_request(self, data):
        return {"kind": "clarification", "approval_id": "approval-1", "digest": "a" * 64}

    def human_reply(self, data, reply):
        assert data["human_request"]["approval_id"] == reply["approval_id"]
        self.reply_calls.append(reply)
        return {"route": "execute", "human_kind": None}


def graph(services, saver=None):
    assert find_spec("decagent.business.graph") is not None, "Business graph is not implemented"
    from decagent.business.graph import build_graph

    return build_graph(services, saver or InMemorySaver())


def initial(**updates):
    return {
        "data": {
            "task_id": "business-test",
            "graph_version": GRAPH_VERSION,
            "instruction": "Read a ticket",
            "strategy": "rule",
            **updates,
        }
    }


CONFIG = {"configurable": {"thread_id": "business-test"}}


def test_graph_merges_partial_data_and_runs_each_route_once():
    services = Services()
    input_state = initial()
    original = deepcopy(input_state)
    result = graph(services).invoke(input_state, CONFIG)
    assert services.calls == [
        "prepare",
        "decide",
        "plan",
        "validate",
        "execute",
        "verify",
        "decide",
        "finalize",
    ]
    assert result["data"]["completion"]["valid"] is True
    assert result["data"]["evidence"] == {"actual": True}
    assert result["data"]["steps"] == 8
    assert input_state == original


def test_human_binding_is_checkpointed_before_interrupt_and_reused_on_resume():
    services = Services()
    services.human = True
    compiled = graph(services)
    result = compiled.invoke(initial(), CONFIG)
    assert result["__interrupt__"][0].value["approval_id"] == "approval-1"
    assert compiled.get_state(CONFIG).values["data"]["human_request"]["digest"] == "a" * 64
    assert services.reply_calls == []
    result = compiled.invoke(Command(resume={"approval_id": "approval-1"}), CONFIG)
    assert result["data"]["completion"]["valid"] is True
    assert services.reply_calls == [{"approval_id": "approval-1"}]
    assert services.calls.count("prepare") == 1


def test_budget_resume_checks_current_budget_before_continuing():
    services = Services()
    services.exhausted = True
    compiled = graph(services)
    paused = compiled.invoke(initial(), CONFIG)
    assert paused["__interrupt__"][0].value["kind"] == "budget"
    repeated = compiled.invoke(Command(resume={"kind": "budget"}), CONFIG)
    assert repeated["__interrupt__"][0].value["kind"] == "budget"
    assert services.calls == ["prepare"]
    services.exhausted = False
    finished = compiled.invoke(Command(resume={"kind": "budget"}), CONFIG)
    assert finished["data"]["completion"]["valid"] is True
    assert services.calls.count("prepare") == 1


def test_reconciliation_resume_verifies_before_any_repeat_write():
    services = Services()
    services.unknown = True
    compiled = graph(services)
    paused = compiled.invoke(initial(), CONFIG)
    assert paused["__interrupt__"][0].value["kind"] == "reconciliation"
    repeated = compiled.invoke(Command(resume={"kind": "reconciliation"}), CONFIG)
    assert repeated["__interrupt__"][0].value["kind"] == "reconciliation"
    assert services.calls.count("execute") == 1
    services.unknown = False
    finished = compiled.invoke(Command(resume={"kind": "reconciliation"}), CONFIG)
    assert finished["data"]["completion"]["valid"] is True
    assert services.calls.count("execute") == 1


@pytest.mark.parametrize("route", ["run_command", "__start__", "budget_wait", None, ["execute"]])
def test_untrusted_routes_are_rejected(route):
    services = Services()
    services.run_phase = lambda phase, data: {"route": route}
    with pytest.raises(ValueError, match="route"):
        graph(services).invoke(initial(), CONFIG)


def test_task_phase_count_is_bounded_and_completion_is_false():
    services = Services()
    services.run_phase = lambda phase, data: {"route": "decide", "completion": {"valid": True}}
    result = graph(services).invoke(initial(), CONFIG)
    assert result["data"]["steps"] == 128
    assert result["data"]["boundary"] == "step_limit"
    assert result["data"]["completion"]["valid"] is False


def test_cancellation_guard_stops_before_reading_or_writing():
    services = Services()
    services.cancelled = True
    with pytest.raises(ValueError, match="cancelled"):
        graph(services).invoke(initial(), CONFIG)
    assert services.calls == []


@pytest.mark.parametrize("changes", [{"task_id": "other"}, {"graph_version": "future"}])
def test_graph_rejects_checkpoint_identity_mismatch(changes):
    services = Services()
    with pytest.raises(ValueError, match="Checkpoint"):
        graph(services).invoke(initial(**changes), CONFIG)
    assert services.calls == []


def test_foreign_human_resume_is_rejected_before_service_reply():
    services = Services()
    services.human = True
    compiled = graph(services)
    compiled.invoke(initial(), CONFIG)
    with pytest.raises(ValueError, match="current durable business approval"):
        compiled.invoke(Command(resume={"approval_id": "foreign-approval"}), CONFIG)
    assert services.reply_calls == []


def test_cancellation_during_human_wait_is_checked_before_resume():
    services = Services()
    services.human = True
    compiled = graph(services)
    compiled.invoke(initial(), CONFIG)
    services.cancelled = True
    with pytest.raises(ValueError, match="cancelled"):
        compiled.invoke(Command(resume={"approval_id": "approval-1"}), CONFIG)
    assert services.reply_calls == []


def test_unfinished_checkpoint_is_resumed_with_none_without_fresh_initial_input():
    from decagent.business.worker import BusinessWorker
    from decagent.config import Settings

    services = Services()
    compiled = graph(services)
    compiled.invoke(initial(), CONFIG, interrupt_before=["execute"])
    assert compiled.get_state(CONFIG).next == ("execute",)
    instance = BusinessWorker(None, Settings())
    assert instance._input(compiled, CONFIG, services.task) is None
    result = compiled.invoke(None, CONFIG)
    assert result["data"]["completion"]["valid"] is True
    assert services.calls.count("prepare") == 1


def test_phase_limit_preserves_reconciliation_and_only_resumes_receipt_verification():
    services = Services()
    services.unknown = True
    writes = []

    def run(phase, data):
        services.calls.append(phase)
        if phase == "execute":
            writes.append("write")
        if phase == "verify":
            if services.unknown:
                from decagent.business.errors import BusinessReconciliationRequired

                raise BusinessReconciliationRequired({"action_id": "action-1", "reason": "Unknown"})
            assert data["steps"] == 128
            assert data["reconciliation_reply"]["kind"] == "reconciliation"
            return {
                "route": "execute",  # Even a retry proposal cannot cross the phase bound.
                "reconciliation": None,
                "execution": None,
                "observations": [{"action_id": "action-1", "receipt": "checked"}],
                "completion": {"valid": True},
            }
        return {"route": "verify", "action_id": "action-1"}

    services.run_phase = run
    compiled = graph(services)
    paused = compiled.invoke(initial(steps=126), CONFIG)
    assert paused["data"]["steps"] == 128
    assert paused["__interrupt__"][0].value["kind"] == "reconciliation"
    repeated = compiled.invoke(Command(resume={"kind": "reconciliation"}), CONFIG)
    assert repeated["data"]["steps"] == 128
    assert repeated["__interrupt__"][0].value["kind"] == "reconciliation"
    services.unknown = False
    finished = compiled.invoke(Command(resume={"kind": "reconciliation"}), CONFIG)
    assert finished["data"]["steps"] == 128
    assert finished["data"]["boundary"] == "step_limit"
    assert finished["data"]["completion"]["valid"] is False
    assert finished["data"]["observations"][0]["receipt"] == "checked"
    assert writes == []


def test_phase_limit_budget_wait_retains_unknown_execution_for_reconciliation():
    services = Services()
    services.exhausted = True

    def run(phase, data):
        services.calls.append(phase)
        return {"route": "verify", "execution": {"status": "unknown"}, "action_id": "action-1"}

    services.run_phase = run
    compiled = graph(services)
    paused = compiled.invoke(initial(steps=126), CONFIG)
    assert paused["__interrupt__"][0].value["kind"] == "budget"
    assert paused["data"]["steps"] == 128
    services.exhausted = False
    reconciled = compiled.invoke(Command(resume={"kind": "budget"}), CONFIG)
    assert reconciled["__interrupt__"][0].value["kind"] == "reconciliation"
    assert services.calls == ["prepare"]

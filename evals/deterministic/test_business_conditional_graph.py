"""Separate interrupt nodes do not replay external node execution on resume."""

import importlib

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


class Services:
    task = {"id": "task"}

    def __init__(self):
        self.calls = self.prepares = self.resumes = 0

    def guard(self):
        pass

    def prepare(self, data):
        self.prepares += 1

    def step(self, data):
        self.calls += 1
        if self.calls == 1:
            return {"status": "waiting", "request": {"kind": "budget", "reason": "bound"}}
        return {"status": "complete", "completion": {"valid": True}}

    def resume(self, data, reply):
        assert reply["kind"] == "budget"
        self.resumes += 1


def graph(services):
    api = importlib.import_module("decagent.business.conditional_graph")
    return api.build_graph(services, InMemorySaver())


def initial():
    return {"data": {"task_id": "task", "graph_version": "business-3"}}


def test_interrupt_resume_does_not_replay_effectful_preparation():
    service = Services()
    value = graph(service)
    config = {"configurable": {"thread_id": "task"}}
    waiting = value.invoke(initial(), config)
    assert waiting["__interrupt__"][0].value["kind"] == "budget"
    result = value.invoke(Command(resume={"kind": "budget"}), config)
    assert result["data"]["completion"]["valid"]
    assert (service.prepares, service.calls, service.resumes) == (1, 2, 1)


def test_identity_and_version_are_checked_before_execution():
    service = Services()
    data = initial()
    data["data"]["graph_version"] = "business-2"
    with pytest.raises(ValueError, match="version"):
        graph(service).invoke(data, {"configurable": {"thread_id": "wrong"}})
    assert service.calls == service.prepares == 0


def test_resume_kind_must_match_current_wait():
    service = Services()
    value = graph(service)
    config = {"configurable": {"thread_id": "task"}}
    value.invoke(initial(), config)
    with pytest.raises(ValueError, match="match"):
        value.invoke(Command(resume={"kind": "action"}), config)
    assert service.calls == 1

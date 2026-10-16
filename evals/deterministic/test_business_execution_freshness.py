"""A fresh directory can revoke dispatch without changing the resource digest."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest

TICKET = "00000000-0000-0000-0000-000000000001"
PERSON = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"


@pytest.mark.parametrize("change,outcome", [("namesake", "clarify"), ("inactive", "deny")])
def test_fresh_directory_revokes_an_undispatched_assignment(change, outcome):
    service = BusinessServices.__new__(BusinessServices)
    service.spec = PluginSpec.from_mapping(builtin_manifest())
    service.task = {"id": "task", "business_scope_id": "scope"}
    instruction = "请分配给张三"
    params = {"ticket_id": TICKET, "person_id": PERSON, "expected_revision": 1}
    evidence = {
        "ticket": {"id": TICKET, "revision": 1, "status": "open", "assignee_id": None},
        "directory": {"people": [{"id": PERSON, "name": "张三", "team": "IT", "active": True}]},
    }
    original = assess_action(service.spec, "task", "scope", instruction, "assign", params, evidence)
    assert original["outcome"] == "allow"
    fresh = deepcopy(evidence)
    if change == "namesake":
        fresh["directory"]["people"].append(
            {"id": OTHER, "name": "张三", "team": "HR", "active": True}
        )
    else:
        fresh["directory"]["people"][0]["active"] = False
    current = assess_action(service.spec, "task", "scope", instruction, "assign", params, fresh)
    assert current["outcome"] == outcome and current["digest"] == original["digest"]
    dispatched = []

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *args: SimpleNamespace(
                fetchone=lambda: {"status": "planned", "digest": original["digest"]}
            )
        )

    service.database = SimpleNamespace(connect=connect)
    service.executor = SimpleNamespace(
        guard=lambda conn: None,
        execute=lambda *args: dispatched.append(args) or {"status": "succeeded"},
    )
    service._evidence = lambda *args: fresh
    update = service._execute(
        {
            "action_id": "action",
            "action_index": 4,
            "assessment": original,
            "instruction": instruction,
            "queue": [{"command": "assign", "params": params}],
        }
    )
    assert update["route"] == "validate" and update["approval_id"] is None
    assert update["action_index"] == 5
    assert not dispatched

"""All planners share deterministic authorization, candidate and revision validation."""

from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest

TICKET = "00000000-0000-0000-0000-000000000001"
PERSON = "00000000-0000-0000-0000-000000000002"
CATEGORY = "00000000-0000-0000-0000-000000000003"


def assess(command, params, **updates):
    arguments = {
        "spec": PluginSpec.from_mapping(builtin_manifest()),
        "task_id": "task",
        "scope_id": "scope",
        "instruction": "请分配给张三",
        "command": command,
        "params": params,
        "evidence": {
            "ticket": {"id": TICKET, "status": "open", "revision": 1, "assignee_id": None},
            "directory": {
                "people": [{"id": PERSON, "name": "张三", "team": "IT", "active": True}],
                "categories": [{"id": CATEGORY, "name": "IT"}],
            },
        },
    }
    arguments.update(updates)
    return assess_action(**arguments)


def test_initial_explicit_assignment_auto_but_reassignment_requires_confirmation():
    params = {"ticket_id": TICKET, "person_id": PERSON, "expected_revision": 1}
    first = assess("assign", params)
    assert first["outcome"] == "allow"
    evidence = {
        "ticket": {"id": TICKET, "status": "assigned", "revision": 1, "assignee_id": PERSON},
        "directory": {
            "people": [{"id": PERSON, "name": "张三", "team": "IT", "active": True}],
            "categories": [],
        },
    }
    reassignment = assess("assign", params, evidence=evidence)
    assert reassignment["outcome"] == "confirm"
    assert reassignment["digest"] != first["digest"]


def test_closing_requires_resolved_state_and_stale_revision_never_auto_executes():
    assert assess("close", {"ticket_id": TICKET, "expected_revision": 1})["outcome"] == "deny"
    assert (
        assess("assign", {"ticket_id": TICKET, "person_id": PERSON, "expected_revision": 2})[
            "outcome"
        ]
        == "stale"
    )


def test_fabricated_person_unknown_parameters_and_unrequested_writes_are_rejected():
    assert (
        assess("assign", {"ticket_id": TICKET, "person_id": CATEGORY, "expected_revision": 1})[
            "outcome"
        ]
        == "deny"
    )
    assert (
        assess(
            "assign",
            {"ticket_id": TICKET, "person_id": PERSON, "expected_revision": 1, "extra": True},
        )["outcome"]
        == "deny"
    )
    assert assess("create", {"title": "Fixture", "category_id": CATEGORY})["outcome"] == "confirm"


def test_duplicate_names_require_explicit_disambiguation_even_with_selected_id():
    evidence = {
        "ticket": {"id": TICKET, "status": "open", "revision": 1, "assignee_id": None},
        "directory": {
            "people": [
                {"id": PERSON, "name": "张三", "team": "IT", "active": True},
                {"id": CATEGORY, "name": "张三", "team": "HR", "active": True},
            ],
            "categories": [],
        },
    }
    params = {"ticket_id": TICKET, "person_id": PERSON, "expected_revision": 1}
    assert assess("assign", params, evidence=evidence)["outcome"] == "clarify"
    assert (
        assess("assign", params, evidence=evidence, instruction="请分配给IT部门的张三")["outcome"]
        == "allow"
    )

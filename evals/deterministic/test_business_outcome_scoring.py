"""Business outcomes tolerate verification reads, never unrequested mutations."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from decagent.evaluation.business.harness import BenchmarkHarness


def action(command, params=None, *, status="succeeded", attempted=True, resource=None):
    return {
        "id": uuid4().hex,
        "status": status,
        "snapshot": {"command": command, "params": params or {}},
        "receipt": {"data": {"receipt": {"resource_id": resource}}},
        "attempted": attempted,
    }


class RecordedDatabase:
    def __init__(self, actions):
        self.actions = actions

    @contextmanager
    def connect(self):
        yield self

    def execute(self, query, params):
        if "SELECT * FROM app.actions" in query:
            rows = self.actions
        elif "SELECT action_id FROM app.action_attempts" in query:
            rows = [{"action_id": a["id"]} for a in self.actions if a["attempted"]]
        else:
            rows = []
        return SimpleNamespace(fetchall=lambda: rows)


def observe(actions, expected, *, state="succeeded", ticket=None, actual_ticket=None, forbidden=()):
    harness = object.__new__(BenchmarkHarness)
    harness.database = RecordedDatabase(actions)
    harness.spec = PluginSpec.from_mapping(builtin_manifest())
    harness.main_free_quota = False
    harness.budget = {"allow_unknown_price": False}
    runner = SimpleNamespace(calls=[])

    def get(*args):
        runner.calls.append({})
        return {
            "ok": True,
            "data": {"ticket": actual_ticket if actual_ticket is not None else ticket},
        }

    runner.run = get
    return harness._observe(
        {"id": "task", "state": state, "active_seconds": 0},
        {
            "terminal": "clarification" if state == "awaiting_input" else "succeeded",
            "actions": expected,
            "ticket": ticket,
            "forbidden_commands": list(forbidden),
        },
        runner,
    )


@pytest.mark.parametrize("read", ["get", "receipt", "directory", "list"])
def test_correct_mutation_allows_supplementary_read(read):
    params = {"category_id": "network", "priority": "normal"}
    actions = [action(read), action("create", params), action(read)]
    assert observe(actions, [{"command": "create", "params": params}])["correct"]


def test_required_read_goal_still_matches_requested_parameters():
    target = {"ticket_id": "wanted"}
    expected = [{"command": "get", "params": target}]
    assert observe([action("directory"), action("get", target), action("receipt")], expected)[
        "correct"
    ]
    assert not observe([action("get", {"ticket_id": "other"})], expected)["correct"]
    assert not observe([action("directory")], expected)["correct"]
    assert not observe([], expected)["correct"]


def test_required_read_cannot_be_skipped_before_a_write():
    expected = [
        {"command": "get", "params": {"ticket_id": "wanted"}},
        {"command": "start", "params": {"ticket_id": "wanted"}},
    ]
    assert not observe([action("start", expected[1]["params"]), action("get")], expected)["correct"]


@pytest.mark.parametrize(
    "actions",
    [
        [action("create", {"priority": "urgent"})],
        [action("create", {"priority": "normal"}), action("comment")],
        [action("create", {"priority": "normal"}), action("create", {"priority": "normal"})],
        [action("comment"), action("create", {"priority": "normal"})],
    ],
)
def test_wrong_duplicate_or_extra_mutation_is_incorrect(actions):
    expected = [{"command": "create", "params": {"priority": "normal"}}]
    assert not observe(actions, expected)["correct"]


def test_read_only_goal_rejects_unrequested_write():
    expected = [{"command": "directory", "params": {}}]
    assert not observe([action("directory"), action("create")], expected)["correct"]


def test_clarification_allows_reads_but_rejects_attempted_write():
    assert observe([action("directory"), action("get")], [], state="awaiting_input")["correct"]
    row = observe(
        [action("directory"), action("create", status="failed")],
        [],
        state="awaiting_input",
    )
    assert not row["correct"]
    assert row["forbidden_writes"] == 1


def test_correct_actions_still_require_final_ticket_and_single_resource():
    params = {"priority": "normal"}
    expected = [{"command": "create", "params": params}]
    row = observe(
        [action("create", params, resource="one"), action("get", resource="two")],
        expected,
        ticket={"status": "open"},
    )
    assert not row["correct"]


def test_supplementary_reads_cannot_hide_wrong_final_ticket():
    params = {"priority": "normal"}
    row = observe(
        [action("create", params, resource="one"), action("get")],
        [{"command": "create", "params": params}],
        ticket={"status": "open", "priority": "normal"},
        actual_ticket={"status": "closed", "priority": "urgent"},
    )
    assert not row["correct"]


def test_final_ticket_verification_is_saved_in_trial_evidence():
    ticket = {"status": "open", "priority": "normal"}
    params = {"priority": "normal"}
    row = observe(
        [action("create", params, resource="one"), action("get")],
        [{"command": "create", "params": params}],
        ticket=ticket,
    )
    assert row["scorer_verified_ticket"] == ticket


def test_supplementary_receipt_read_with_no_receipt_keeps_correct_outcome():
    params = {"priority": "normal"}
    read = action("receipt")
    read["receipt"] = {"data": {"receipt": None}}
    row = observe(
        [action("create", params, resource="one"), read],
        [{"command": "create", "params": params}],
        ticket={"status": "open", "priority": "normal"},
    )
    assert row["correct"]
    assert row["scorer_verified_ticket"]["status"] == "open"


def test_forbidden_failed_attempt_cannot_be_hidden_by_a_later_correct_action():
    expected = [{"command": "directory", "params": {}}]
    row = observe([action("close", status="failed"), action("directory")], expected)
    assert not row["correct"]
    assert row["forbidden_writes"] == 1

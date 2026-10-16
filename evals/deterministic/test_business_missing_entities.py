"""Missing target evidence asks for a concrete fact before state-transition judgment."""

from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest


def test_missing_ticket_is_a_clarification_not_an_invented_state():
    outcome = assess_action(
        PluginSpec.from_mapping(builtin_manifest()),
        "task",
        "scope",
        "请关闭工单",
        "close",
        {"ticket_id": "00000000-0000-0000-0000-000000000001", "expected_revision": 1},
        {"ticket": None},
    )
    assert outcome["outcome"] == "clarify"

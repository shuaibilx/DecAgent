"""A typed literal instruction grants authority to its reviewed parameters."""

import json

from decagent.business.actions import assess_action
from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest


def test_main_plan_cannot_change_literal_create_title_and_keep_automatic_authority():
    spec = PluginSpec.from_mapping(builtin_manifest())
    requested = {"title": "Alpha", "category_id": "00000000-0000-4000-8000-000000000001"}
    result = assess_action(
        spec,
        "task",
        "scope",
        json.dumps({"command": "create", "params": requested}),
        "create",
        {**requested, "title": "Beta"},
        {"directory": {"categories": [{"id": requested["category_id"], "name": "Network"}]}},
    )
    assert result["outcome"] == "deny"


def test_finish_cannot_accept_a_verified_write_with_the_wrong_literal_parameters():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from decagent.business.services import BusinessServices

    @contextmanager
    def connect():
        yield SimpleNamespace(
            execute=lambda *args: SimpleNamespace(
                fetchall=lambda: [
                    {"id": "action-1", "status": "succeeded", "receipt": {"verified": True}}
                ]
            )
        )

    service = BusinessServices.__new__(BusinessServices)
    service.database = SimpleNamespace(connect=connect)
    service.task = {"id": "task"}
    service.spec = PluginSpec.from_mapping(builtin_manifest())
    result = service._finalize(
        {
            "instruction": json.dumps({"command": "create", "params": {"title": "Alpha"}}),
            "completion": {"valid": True, "summary": "Model says done"},
            "observations": [
                {
                    "action_id": "action-1",
                    "command": "create",
                    "params": {"title": "Beta"},
                    "data": {},
                }
            ],
        }
    )
    assert result["completion"]["valid"] is False

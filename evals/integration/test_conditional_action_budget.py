"""The last allowed business action is permitted; quota never hides overall limits."""

import json
from decimal import Decimal

import pytest

from decagent.business.actions import assess_action
from decagent.business.execution import BusinessExecutor
from decagent.business.registry import PluginRegistry
from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def executor(database, *, budget=None, command="create"):
    backend = FixtureTickets(database)
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    directory = backend.read("directory", {})
    params = {"title": "Final permitted action", "category_id": directory["categories"][0]["id"]}
    if command == "directory":
        params = {}
    instruction = json.dumps({"command": command, "params": params})
    task = database.create_business_task(
        scope["id"],
        {"instruction": instruction, "plugin_digest": spec.digest},
        budget,
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    task = database.claim(task["id"], "action-budget", seconds=120)
    value = BusinessExecutor(
        database, spec, task, "action-budget", task["lease_generation"], mcp_client=backend
    )
    assessment = assess_action(
        spec,
        str(task["id"]),
        str(scope["id"]),
        instruction,
        command,
        params,
        {"directory": directory},
    )
    return value, assessment


def test_one_action_limit_allows_one_committed_write_and_blocks_second(database):
    value, assessment = executor(database, budget={"max_actions": 1})
    first = value.plan("first", assessment)
    assert value.execute(first["id"], first["digest"])["status"] == "succeeded"
    second = value.plan("second", assessment)
    with pytest.raises(BudgetExceeded):
        value.execute(second["id"], second["digest"])
    with database.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )


def test_conditional_reads_ignore_model_count_but_enforce_cost(database):
    value, _ = executor(
        database,
        command="directory",
        budget={
            "max_primary_calls": 1,
            "primary_target": 1,
            "max_calls": 1,
            "max_tokens": 10,
            "max_cost": "1",
        },
    )
    ledger = UsageLedger(database, value.task["id"])
    receipt = ledger.reserve_model(
        "primary", 1, "qwen", 10, Decimal(".01"), model_family="primary", purpose="plan"
    )
    ledger.settle(receipt, 10, Decimal(".01"))
    assert value.read("directory", {})["categories"]
    with database.connect() as conn:
        conn.execute(
            'UPDATE app.tasks SET budget=budget||\'{"max_cost":"0.005"}\'::jsonb WHERE id=%s',
            (value.task["id"],),
        )
    with pytest.raises(BudgetExceeded):
        value.read("directory", {})

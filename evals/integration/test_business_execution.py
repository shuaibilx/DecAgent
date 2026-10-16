"""A lost write response is reconciled through a transactional receipt, never blind retry."""

from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.actions import assess_action
from decagent.business.execution import BusinessExecutor
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def setup(database, *, lose=False, budget=None):
    store = FixtureTickets(database, lose=lose)
    assert store.execute("seed", {}, "seed")["ok"]
    registry = PluginRegistry(database)
    scope = registry.register(builtin_manifest())
    spec = registry.get(scope["id"])
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "创建IT工单",
            "plugin_digest": spec.digest,
            "provenance": {"owner_id": "owner", "authenticated": True},
        },
        budget,
        employee_id=store.identity.employee_id,
        credential_ref=store.identity.credential_ref,
    )
    claimed = database.claim(task["id"], "worker", seconds=120)
    runner = store
    executor = BusinessExecutor(
        database, spec, claimed, "worker", claimed["lease_generation"], mcp_client=runner
    )
    directory = store.execute("directory", {}, "directory")["data"]
    params = {"title": "VPN fails", "category_id": directory["categories"][0]["id"]}
    assessment = assess_action(
        spec,
        str(task["id"]),
        str(scope["id"]),
        "创建IT工单",
        "create",
        params,
        {"directory": directory},
    )
    return executor, runner, assessment


def test_write_receipt_verified_and_completed_action_reused(database):
    executor, runner, assessment = setup(database)
    action = executor.plan("create-1", assessment)
    result = executor.execute(action["id"], assessment["digest"])
    assert result["status"] == "succeeded"
    assert result["data"]["ticket"]["status"] == "open"
    assert executor.execute(action["id"], assessment["digest"]) == result
    assert runner.writes == 1


def test_response_loss_reconciles_receipt_without_duplicate_write(database):
    executor, runner, assessment = setup(database, lose=True)
    action = executor.plan("create-lost", assessment)
    assert executor.execute(action["id"], assessment["digest"])["status"] == "unknown"
    result = executor.reconcile(action["id"])
    assert result["status"] == "succeeded" and runner.writes == 1
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )


def test_denied_and_unconfirmed_writes_cannot_run(database):
    executor, runner, assessment = setup(database)
    confirmation = {**assessment, "outcome": "confirm"}
    action = executor.plan("confirm-1", confirmation)
    assert executor.execute(action["id"], assessment["digest"])["status"] == "awaiting_approval"
    assert runner.writes == 0
    with pytest.raises(ValueError):
        executor.execute(uuid4(), assessment["digest"])


def consume_last_model_budget(executor, bound, *, status="settled"):
    ledger = UsageLedger(executor.database, executor.task["id"])
    reservation = ledger.reserve("main-last-call", 1, "primary", 7, Decimal("0.01"))
    if status == "settled":
        ledger.settle(reservation, 7, Decimal("0.01"))
    elif status == "unknown":
        ledger.settle(reservation, 3, None)
    if bound == "max_active_seconds":
        ledger.charge_active(1)


@pytest.mark.parametrize(
    ("bound", "limit"),
    [("max_calls", 1), ("max_tokens", 7), ("max_cost", "0.01"), ("max_active_seconds", 1)],
)
def test_exhausted_durable_budget_blocks_new_action_before_mcp_prepare(database, bound, limit):
    executor, runner, assessment = setup(database, budget={bound: limit})
    action = executor.plan("create-budget", assessment)
    consume_last_model_budget(executor, bound)
    with pytest.raises(BudgetExceeded, match="budget|time"):
        executor.execute(action["id"], assessment["digest"])
    assert runner.writes == 0
    with database.connect() as conn:
        assert (
            conn.execute("SELECT status FROM app.actions WHERE id=%s", (action["id"],)).fetchone()[
                "status"
            ]
            == "planned"
        )
        assert conn.execute("SELECT count(*) AS n FROM app.action_attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"] == 0
        conn.execute(
            "UPDATE app.tasks SET budget=%s WHERE id=%s",
            (
                Jsonb(
                    {"max_calls": 2, "max_tokens": 14, "max_cost": "0.02", "max_active_seconds": 2}
                ),
                executor.task["id"],
            ),
        )
    assert executor.execute(action["id"], assessment["digest"])["status"] == "succeeded"
    assert runner.writes == 1


@pytest.mark.parametrize("status", ["reserved", "unknown"])
def test_unsettled_reservations_count_conservatively_before_new_action(database, status):
    executor, runner, assessment = setup(database, budget={"max_tokens": 7})
    action = executor.plan("create-held-budget", assessment)
    consume_last_model_budget(executor, "max_tokens", status=status)
    with pytest.raises(BudgetExceeded, match="Token budget"):
        executor.execute(action["id"], assessment["digest"])
    assert runner.writes == 0


@pytest.mark.parametrize(
    ("bound", "limit"),
    [("max_calls", 1), ("max_tokens", 7), ("max_cost", "0.01"), ("max_active_seconds", 1)],
)
def test_exhausted_budget_preserves_unknown_receipt_reconciliation_and_completed_replay(
    database, bound, limit
):
    executor, runner, assessment = setup(database, lose=True, budget={bound: limit})
    action = executor.plan("create-before-budget-exhaustion", assessment)
    assert executor.execute(action["id"], assessment["digest"])["status"] == "unknown"
    consume_last_model_budget(executor, bound)
    result = executor.execute(action["id"], assessment["digest"])
    assert result["status"] == "succeeded"
    assert result["verification"]["input_bound"] is True
    assert executor.execute(action["id"], assessment["digest"]) == result
    assert runner.writes == 1

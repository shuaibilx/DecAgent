from decimal import Decimal

import httpx
import pytest
from langchain_core.messages import AIMessage

from decagent.decisions import JevAdapter
from decagent.models.primary import PrimaryModel
from decagent.runtime.model_calls import BudgetedPrimary, LedgerAttemptBudget, TokenPrice
from decagent.storage.budget import BudgetExceeded
from decagent.storage.usage import UsageLedger

pytestmark = pytest.mark.integration


def test_model_network_request_is_preceded_by_durable_reservation(database):
    project = database.add_project("fixture/model-ledger")
    task = database.create_task(
        project["id"], "a" * 40, {}, {"max_calls": 1, "max_tokens": 100000, "max_cost": 1}
    )
    calls = []

    class Client:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            with database.connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
                ).fetchall()
            assert len(rows) == 1 and rows[0]["status"] == "reserved"
            calls.append(True)
            return AIMessage(
                content="done",
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            )

    primary = BudgetedPrimary(
        PrimaryModel(Client()),
        UsageLedger(database, task["id"]),
        price=TokenPrice("fixture-price", Decimal("1"), Decimal("2")),
    )
    assert primary.respond([{"role": "user", "content": "diagnostic"}]).text == "done"
    with pytest.raises(BudgetExceeded):
        primary.respond([{"role": "user", "content": "diagnostic"}])
    assert len(calls) == 1


def test_failed_network_and_unknown_price_keep_reservation(database):
    project = database.add_project("fixture/unknown-price")
    task = database.create_task(project["id"], "a" * 40, {}, {"max_calls": 3, "max_cost": 1})

    class Client:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            raise RuntimeError("Provider unavailable")

    primary = BudgetedPrimary(
        PrimaryModel(Client()),
        UsageLedger(database, task["id"]),
        unknown_cost_reservation=Decimal("1"),
    )
    with pytest.raises(RuntimeError):
        primary.respond([])
    with pytest.raises(BudgetExceeded):
        primary.respond([])
    with database.connect() as conn:
        row = conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
        ).fetchone()
    assert row["status"] == "unknown" and row["reserved_cost"] == 1 and row["actual_cost"] is None


def test_jev_retries_share_the_same_primary_ledger(database):
    from decagent.decisions import DecisionContext, build_request

    project = database.add_project("fixture/jev-ledger")
    task = database.create_task(project["id"], "a" * 40, {}, {"max_calls": 1, "max_cost": 1})
    ledger = UsageLedger(database, task["id"])
    adapter = JevAdapter(
        api_key="fixture",
        provider="openrouter",
        model="jev-latest",
        budget=LedgerAttemptBudget(ledger, reservation_usd=0.01),
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(529, json={}))),
    )
    request = build_request(
        ["memory_read_needed"],
        {"task_summary": "bug", "current_version": "1"},
        DecisionContext(str(task["id"]), str(project["id"]), "1", "1", "1", "1"),
    )
    result = adapter.evaluate(request)
    assert len(result.attempts) == 1 and result.error == "budget_exceeded"
    with database.connect() as conn:
        row = conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (task["id"],)
        ).fetchone()
    assert row["status"] == "unknown" and row["reserved_cost"] == Decimal(".01")


def test_budget_retry_with_unknown_response_never_dispatches_again(database, tmp_path):
    from psycopg.types.json import Jsonb

    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.operations import WaitingRequired
    from decagent.runtime.services import RuntimeServices

    project = database.add_project("fixture/budget-retry")
    task = database.create_task(
        project["id"],
        "a" * 40,
        {
            "issue": "Fix one bug",
            "repository": project["repository"],
            "task_id": "diagnostic-fixture",
        },
        {"max_calls": 1, "max_tokens": 100000, "max_cost": "1"},
    )
    claim = database.claim(task["id"], "budget-retry", seconds=120)
    ledger = UsageLedger(database, task["id"])
    prior = ledger.reserve("prior-call", 1, "fixture", 1, Decimal("0"))
    ledger.settle(prior, 1, Decimal("0"))

    class Client:
        calls = 0

        def respond(self, _messages, *, stream=False):
            self.calls += 1
            raise RuntimeError("Injected response loss after dispatch")

    client = Client()
    service = RuntimeServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(strategy="rule", main_price=TokenPrice("fixture", Decimal(0), Decimal(0))),
        claim,
        "budget-retry",
        claim["lease_generation"],
        broker_client=lambda *_: None,
        primary=client,
        github=object(),
    )
    with pytest.raises(BudgetExceeded):
        service.invoke_model([])
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET budget=%s WHERE id=%s",
            (
                Jsonb({"max_calls": 3, "max_tokens": 100000, "max_cost": "1"}),
                task["id"],
            ),
        )
    with pytest.raises(WaitingRequired):
        service.invoke_model([])
    with pytest.raises(WaitingRequired):
        service.invoke_model([])
    assert client.calls == 1


def test_cancel_during_retrieval_prevents_new_primary_request(database, tmp_path):
    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.services import RuntimeServices

    project = database.add_project("fixture/cancel-inference")
    task = database.create_task(
        project["id"],
        "a" * 40,
        {"issue": "Fix one bug", "repository": project["repository"], "task_id": "fixture"},
        {"max_calls": 3, "max_cost": "1"},
    )
    claim = database.claim(task["id"], "cancel-test", seconds=120)

    class Client:
        calls = 0

        def respond(self, _messages, *, stream=False):
            self.calls += 1
            raise AssertionError("Cancelled task reached provider")

    client = Client()
    service = RuntimeServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(strategy="rule", main_price=TokenPrice("fixture", Decimal(0), Decimal(0))),
        claim,
        "cancel-test",
        claim["lease_generation"],
        primary=client,
        broker_client=lambda *_: None,
        github=object(),
    )
    original = service.memories.retrieve

    def cancelled(request):
        with database.connect() as conn:
            conn.execute("UPDATE app.tasks SET state='cancelled' WHERE id=%s", (task["id"],))
        return original(request)

    service.memories.retrieve = cancelled
    with pytest.raises(ValueError, match="lease"):
        service.invoke_model([])
    assert client.calls == 0

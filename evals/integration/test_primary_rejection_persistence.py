"""Rejected provider responses retain known usage without accepting tools or replaying calls."""

from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest
from decagent.config import Settings
from decagent.models.primary import PrimaryModel
from decagent.policy import Redactor
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import TokenPrice
from decagent.runtime.model_diagnostics import ModelDiagnostics
from decagent.runtime.operations import WaitingRequired
from decagent.runtime.services import RuntimeServices
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration
USAGE = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
ACTUAL_MODEL = "observed-fixture-model-1"
SENSITIVE = "synthetic-sensitive-marker-9437"


class RejectedClient:
    def __init__(self, *, known_usage=True):
        self.calls = 0
        self.known_usage = known_usage

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "unregistered_operation", "args": {"body": SENSITIVE}, "id": "bad"}
            ],
            usage_metadata=USAGE if self.known_usage else None,
            response_metadata={"model_name": ACTUAL_MODEL},
        )

    def stream(self, messages):
        self.calls += 1
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "unregistered_operation",
                    "args": '{"body":"' + SENSITIVE + '"}',
                    "id": "bad",
                    "index": 0,
                }
            ],
        )
        yield AIMessageChunk(
            content="",
            usage_metadata=USAGE if self.known_usage else None,
            response_metadata={"model_name": ACTUAL_MODEL},
        )


def business_service(database, client, price, *, redactor=None):
    backend = FixtureTickets(database)
    scope = PluginRegistry(database).register(builtin_manifest())
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "请创建网络工单",
            "strategy": "main",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"owner_id": "owner", "authenticated": True},
        },
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    task = database.claim(task["id"], "usage-fixture", seconds=120)
    return BusinessServices(
        database,
        Settings(database_url=database.url),
        RuntimeConfig(main_price=price),
        task,
        "usage-fixture",
        task["lease_generation"],
        mcp_client=backend,
        primary_factory=lambda settings, protocol: PrimaryModel(
            client, redactor, tool_definitions=protocol.definitions, validator=protocol.validate
        ),
    )


@pytest.mark.parametrize("free", [False, True])
def test_business_rejection_records_usage_model_and_bounded_failure(database, free):
    price = TokenPrice("fixture-known-price", Decimal(0 if free else 1), Decimal(0 if free else 2))
    client = RejectedClient()
    service = business_service(database, client, price)
    data = {"instruction": "请创建网络工单", "strategy": "main"}
    for _ in range(2):
        data = {**data, **service.run_phase("plan", data)}
    assert data["route"] == "human" and data["plan_failures"] == 2
    assert client.calls == 2
    with database.connect() as conn:
        usage = conn.execute(
            "SELECT status,actual_tokens,actual_cost FROM app.usage_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchall()
        records = conn.execute(
            "SELECT resolved_model,result FROM app.decision_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchall()
        actions = conn.execute(
            "SELECT count(*) AS n FROM app.actions WHERE task_id=%s", (service.task["id"],)
        ).fetchone()["n"]
    assert actions == 0
    assert len(usage) == len(records) == 2
    assert all(row["status"] == "settled" and row["actual_tokens"] == 30 for row in usage)
    assert all(row["actual_cost"] == price.cost(20, 10) for row in usage)
    assert all(row["resolved_model"] == ACTUAL_MODEL for row in records)
    for row in records:
        result = row["result"]
        assert result["error"] == "planner_response_invalid"
        assert result["rejection_reason"] == "tool_schema"
        assert result["proposals"] == [] and "assistant" not in result
        assert result["usage"] == USAGE
        assert SENSITIVE not in repr(result)


def repair_service(database, tmp_path, claim, client, *, redactor=None):
    return RuntimeServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(strategy="rule", main_price=TokenPrice("fixture", Decimal(1), Decimal(2))),
        claim,
        "usage-fixture",
        claim["lease_generation"],
        broker_client=lambda *_: None,
        primary=PrimaryModel(client, redactor),
        github=object(),
    )


@pytest.mark.parametrize("known_usage", [False, True])
def test_repair_rejected_response_is_durable_and_never_redispatched_after_rebuild(
    database, tmp_path, known_usage
):
    project = database.add_project("fixture/rejected-response")
    task = database.create_task(
        project["id"],
        "a" * 40,
        {"issue": "Fix one bug", "repository": project["repository"], "task_id": "fixture"},
        {"max_calls": 3, "max_cost": "1"},
    )
    claim = database.claim(task["id"], "usage-fixture", seconds=120)
    client = RejectedClient(known_usage=known_usage)
    service = repair_service(database, tmp_path, claim, client)
    with pytest.raises(WaitingRequired) as captured:
        service.invoke_model([])
    assert captured.value.payload["kind"] == "reconciliation"
    assert "rejected" in captured.value.payload["reason"].lower()
    assert SENSITIVE not in repr(captured.value.payload)
    assert "reservation remains held" not in captured.value.payload["reason"]
    saved = [
        event for event in database.events(task["id"]) if event["kind"].startswith("runtime.model.")
    ]
    assert saved[-1]["payload"]["status"] == "rejected"
    assert saved[-1]["payload"]["reason"] == "tool_schema"
    assert saved[-1]["payload"]["usage"] == (USAGE if known_usage else None)
    assert saved[-1]["payload"]["model"] == ACTUAL_MODEL
    assert "turn" not in saved[-1]["payload"]
    assert SENSITIVE not in repr(saved[-1]["payload"])
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status,actual_tokens,actual_cost FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchone()
    assert row["status"] == ("settled" if known_usage else "unknown")
    assert row["actual_tokens"] == (30 if known_usage else None)
    assert row["actual_cost"] == (Decimal("0.00004") if known_usage else None)
    rebuilt = repair_service(database, tmp_path, claim, client)
    with pytest.raises(WaitingRequired) as restarted:
        rebuilt.invoke_model([])
    assert "rejected" in restarted.value.payload["reason"].lower()
    assert client.calls == 1


@pytest.mark.parametrize("price_known", [False, True])
def test_main_diagnostic_rejection_retains_known_usage_and_replays_same_result(
    database, tmp_path, price_known
):
    client = RejectedClient()
    config = RuntimeConfig(
        main_price=TokenPrice("fixture", Decimal(1), Decimal(2)) if price_known else None,
        unknown_main_reservation=Decimal("0.02"),
    )
    diagnostic = ModelDiagnostics(
        database,
        Settings(data_dir=tmp_path, main_api_key="unpaid-fixture"),
        config,
        primary=PrimaryModel(client),
    )
    result = diagnostic("main", "d" * 64)
    assert result["status"] == "failed"
    assert result["rejection_reason"] == "tool_schema"
    assert result["tokens"] == 30 and result["actual_model"] == ACTUAL_MODEL
    assert result["stream_completed"] is True
    assert result["cost_usd"] == ("0.00004" if price_known else None)
    assert ("held_usd" in result) is (not price_known)
    assert SENSITIVE not in repr(result)
    assert diagnostic("main", "d" * 64) == result and client.calls == 1
    with database.connect() as conn:
        row = conn.execute("SELECT status,actual_cost,result FROM app.model_diagnostics").fetchone()
    assert row["status"] == "failed"
    assert row["actual_cost"] == (Decimal("0.00004") if price_known else None)
    assert row["result"] == result


def test_main_diagnostic_local_preflight_proves_zero_usage_without_retry(database, tmp_path):
    client = RejectedClient()
    diagnostic = ModelDiagnostics(
        database,
        Settings(data_dir=tmp_path, main_api_key="unpaid-fixture"),
        RuntimeConfig(unknown_main_reservation=Decimal("0.02")),
        primary=PrimaryModel(client, Redactor(max_items=1)),
    )
    result = diagnostic("main", "e" * 64)
    assert result["status"] == "failed" and result["rejection_reason"] == "context_limit"
    assert result["tokens"] == 0 and result["cost_usd"] == "0"
    assert result["stream_completed"] is False and result["actual_model"] is None
    assert "held_usd" not in result
    assert diagnostic("main", "e" * 64) == result and client.calls == 0


def test_business_local_preflight_keeps_zero_usage_and_safe_failure(database):
    client = RejectedClient()
    service = business_service(
        database,
        client,
        TokenPrice("fixture", Decimal(1), Decimal(2)),
        redactor=Redactor(max_items=1),
    )
    result = service.run_phase("plan", {"instruction": "请创建网络工单", "strategy": "main"})
    assert result["route"] == "plan" and result["plan_failures"] == 1
    assert client.calls == 0
    with database.connect() as conn:
        usage = conn.execute(
            "SELECT status,actual_tokens,actual_cost FROM app.usage_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchone()
        decision = conn.execute(
            "SELECT resolved_model,result FROM app.decision_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchone()
    assert usage == {"status": "settled", "actual_tokens": 0, "actual_cost": Decimal(0)}
    assert decision["resolved_model"] is None
    assert decision["result"]["rejection_reason"] == "context_limit"
    assert decision["result"]["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert decision["result"]["proposals"] == []


def test_repair_local_preflight_is_durable_and_never_redispatched(database, tmp_path):
    project = database.add_project("fixture/preflight")
    task = database.create_task(
        project["id"],
        "a" * 40,
        {"issue": "Fix one bug", "repository": project["repository"], "task_id": "fixture"},
        {"max_calls": 3, "max_cost": "1"},
    )
    claim = database.claim(task["id"], "usage-fixture", seconds=120)
    client = RejectedClient()
    service = repair_service(database, tmp_path, claim, client, redactor=Redactor(max_items=1))
    with pytest.raises(WaitingRequired) as stopped:
        service.invoke_model([])
    assert "before dispatch" in stopped.value.payload["reason"].lower()
    saved = [
        event for event in database.events(task["id"]) if event["kind"].startswith("runtime.model.")
    ][-1]["payload"]
    assert saved["status"] == "not_dispatched" and saved["reason"] == "context_limit"
    assert saved["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert "turn" not in saved and saved["model"] is None
    with database.connect() as conn:
        usage = conn.execute(
            "SELECT status,actual_tokens,actual_cost FROM app.usage_records WHERE task_id=%s",
            (task["id"],),
        ).fetchone()
    assert usage == {"status": "settled", "actual_tokens": 0, "actual_cost": Decimal(0)}
    rebuilt = repair_service(database, tmp_path, claim, client)
    with pytest.raises(WaitingRequired) as restarted:
        rebuilt.invoke_model([])
    assert "before dispatch" in restarted.value.payload["reason"].lower()
    assert client.calls == 0

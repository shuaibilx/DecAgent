"""Business claims and recovery use real SQL and PostgresSaver without models or GitHub."""

import threading
import time
from importlib.util import find_spec
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from decagent.business.graph import GRAPH_VERSION
from decagent.config import Settings
from decagent.runtime.worker import Worker
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def task(database, **budget):
    with database.connect() as conn:
        registered = conn.execute(
            "INSERT INTO app.business_scopes(id,plugin_id,plugin_version,manifest_digest,manifest) "
            "VALUES(%s,%s,'1',%s,'{}') RETURNING *",
            (uuid4(), "worker-fixture-" + uuid4().hex, "a" * 64),
        ).fetchone()
    backend = FixtureTickets(database)
    return database.create_business_task(
        registered["id"],
        {"instruction": "Offline business task", "plugin_digest": "a" * 64, "strategy": "rule"},
        budget,
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )


def worker(database, tmp_path, node, **kwargs):
    assert find_spec("decagent.business.worker") is not None, "Business worker is not implemented"
    from decagent.business.graph import BusinessState
    from decagent.business.worker import BusinessWorker

    def services_factory(db, settings, config, saved, owner, generation):
        return SimpleNamespace(
            database=db, task=saved, owner=owner, generation=generation, close=lambda: None
        )

    def graph_factory(services, saver):
        def step(state):
            services.database.assert_lease(services.task["id"], services.owner, services.generation)
            data = state["data"]
            return {"data": {**data, **node(services, data)}}

        return (
            StateGraph(BusinessState)
            .add_node("step", step)
            .add_edge(START, "step")
            .add_edge("step", END)
            .compile(checkpointer=saver)
        )

    return BusinessWorker(
        database,
        Settings(data_dir=tmp_path),
        services_factory=services_factory,
        graph_factory=graph_factory,
        **kwargs,
    )


def complete():
    return {"phase": "finalize", "completion": {"valid": True, "summary": "Verified fixture"}}


def test_claim_dispatch_is_scoped_by_kind_and_version(database, tmp_path):
    business = task(database)
    project = database.add_project("fixture/worker-dispatch")
    repair = database.create_task(project["id"], "b" * 40, {})
    instance = worker(database, tmp_path, lambda services, data: complete())
    assert instance.claim()["id"] == business["id"]
    assert Worker(database, Settings()).claim()["id"] == repair["id"]


def test_business_completion_requires_literal_true_validity(database, tmp_path):
    for value in (None, {}, {"valid": 1}, {"valid": False}, {"valid": True}):
        saved = task(database)
        instance = worker(
            database, tmp_path, lambda services, data, value=value: {"completion": value}
        )
        assert instance.run_once()
        assert database.get_task(saved["id"])["state"] == (
            "succeeded" if value == {"valid": True} and type(value["valid"]) is bool else "failed"
        )


def test_business_budget_resume_reuses_same_postgres_checkpoint(database, tmp_path):
    saved = task(database, max_calls=0)
    calls = []

    def node(services, data):
        calls.append(data["task_id"])
        if database.get_task(saved["id"])["budget"]["max_calls"] == 0:
            interrupt({"kind": "budget", "reason": "Budget exhausted"})
        return complete()

    assert worker(database, tmp_path, node).run_once()
    row = database.get_task(saved["id"])
    assert row["state"] == "paused_budget"
    active = row["active_seconds"]
    time.sleep(0.05)
    assert database.get_task(saved["id"])["active_seconds"] == active
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='queued',budget=%s WHERE id=%s",
            (Jsonb({"max_calls": 2}), saved["id"]),
        )
    database.event(saved["id"], "task.resume_requested", {"budget": {"max_calls": 2}})
    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    assert calls == [str(saved["id"])] * 2
    with database.checkpointer() as saver:
        saved_data = saver.get_tuple({"configurable": {"thread_id": str(saved["id"])}}).checkpoint[
            "channel_values"
        ]["data"]
    assert saved_data["task_id"] == str(saved["id"])
    assert saved_data["graph_version"] == GRAPH_VERSION


def test_business_approval_resume_requires_durable_authenticated_reply(database, tmp_path):
    saved = task(database)
    approval = database.ensure_approval(saved["id"], None, "b" * 64, "clarification")
    received = []

    def node(services, data):
        reply = interrupt(
            {"kind": "clarification", "approval_id": str(approval["id"]), "digest": "b" * 64}
        )
        received.append(reply)
        return complete()

    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "awaiting_input"
    for payload in ({}, {"policy_record": {"business": True}}):
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.approvals SET status='replied',reply='confirm',reply_payload=%s "
                "WHERE id=%s",
                (Jsonb(payload), approval["id"]),
            )
            conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
        assert worker(database, tmp_path, node).run_once()
        assert database.get_task(saved["id"])["state"] == (
            "awaiting_input" if not payload else "succeeded"
        )
    assert received == [{"approval_id": str(approval["id"])}]


def test_business_checkpoint_version_is_preserved_for_operator_recovery(database, tmp_path):
    saved = task(database)
    instance = worker(
        database, tmp_path, lambda services, data: interrupt({"kind": "budget", "reason": "Pause"})
    )
    assert instance.run_once()
    with database.checkpointer() as saver:
        graph = instance.graph_factory(
            SimpleNamespace(database=database, task=saved, owner=instance.owner, generation=1),
            saver,
        )
        graph.update_state(
            {"configurable": {"thread_id": str(saved["id"])}},
            {"data": {"task_id": str(saved["id"]), "graph_version": "future", "phase": "prepare"}},
        )
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert instance.run_once()
    assert database.get_task(saved["id"])["state"] == "recovering"
    with database.checkpointer() as saver:
        assert (
            saver.get_tuple({"configurable": {"thread_id": str(saved["id"])}}).checkpoint[
                "channel_values"
            ]["data"]["graph_version"]
            == "future"
        )


def test_cancelled_business_task_cannot_be_overwritten_by_stale_worker(database, tmp_path):
    saved = task(database)

    def node(services, data):
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET state='cancelled',lease_generation=lease_generation+1,"
                "lease_owner=NULL,lease_until=NULL WHERE id=%s",
                (saved["id"],),
            )
        return complete()

    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "cancelled"
    assert not any(event["kind"] == "task.finished" for event in database.events(saved["id"]))


def test_business_heartbeat_prevents_competing_claim(database, tmp_path):
    saved = task(database)
    started, release = threading.Event(), threading.Event()

    def node(services, data):
        started.set()
        assert release.wait(5)
        return complete()

    first = worker(database, tmp_path, node, owner="first", heartbeat_seconds=0.05, lease_seconds=1)
    second = worker(database, tmp_path, node, owner="second")
    thread = threading.Thread(target=first.run_once)
    thread.start()
    try:
        assert started.wait(3)
        time.sleep(1.05)
        assert second.run_once() is False
    finally:
        release.set()
        thread.join(5)
    row = database.get_task(saved["id"])
    assert row["state"] == "succeeded" and row["lease_generation"] == 1
    assert float(row["active_seconds"]) >= 1


def test_prior_resume_request_cannot_resume_a_new_business_interrupt(database, tmp_path):
    saved = task(database)
    completed = []
    database.event(saved["id"], "task.resume_requested", {"budget": {"max_calls": 2}})

    def node(services, data):
        interrupt({"kind": "budget", "reason": "New pause"})
        completed.append(data["task_id"])
        return complete()

    assert worker(database, tmp_path, node).run_once()
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "paused_budget"
    assert completed == []
    database.event(saved["id"], "task.resume_requested", {"budget": {"max_calls": 3}})
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    assert completed == [str(saved["id"])]


def test_unknown_business_result_never_succeeds_from_a_completion_flag(database, tmp_path):
    saved = task(database)
    assert worker(
        database,
        tmp_path,
        lambda services, data: {
            **complete(),
            "execution": {"status": "unknown"},
        },
    ).run_once()
    assert database.get_task(saved["id"])["state"] == "recovering"


def test_production_business_graph_checkpoints_approval_and_reconciliation_separately(
    database, tmp_path
):
    from decagent.business.errors import BusinessReconciliationRequired
    from decagent.business.worker import BusinessWorker

    saved = task(database)
    writes = []
    state = {"unknown": True}

    class ScriptedServices:
        def __init__(self, db, settings, config, saved, owner, generation):
            self.database, self.task, self.owner, self.generation = db, saved, owner, generation

        def guard(self):
            return self.database.assert_lease(self.task["id"], self.owner, self.generation)

        def close(self):
            pass

        def human_request(self, data):
            approval = self.database.ensure_approval(self.task["id"], None, "c" * 64, "action")
            return {"kind": "action", "approval_id": str(approval["id"]), "digest": "c" * 64}

        def human_reply(self, data, reply):
            assert reply["approval_id"] == data["human_request"]["approval_id"]
            return {"route": "execute"}

        def run_phase(self, phase, data):
            if phase == "prepare":
                return {"route": "decide", "evidence": {"read": "persisted"}}
            if phase == "decide":
                return {"route": "human"}
            if phase == "execute":
                writes.append(data["task_id"])
                return {"route": "verify", "execution": {"status": "unknown"}}
            if phase == "verify":
                if state["unknown"]:
                    raise BusinessReconciliationRequired({"reason": "Receipt unavailable"})
                assert data["reconciliation_reply"]["kind"] == "reconciliation"
                assert data["evidence"] == {"read": "persisted"}
                return {
                    "route": "finalize",
                    "completion": {"valid": True},
                    "execution": None,
                    "reconciliation": None,
                }
            assert phase == "finalize"
            return {"route": "__end__"}

    def instance():
        return BusinessWorker(
            database,
            Settings(data_dir=tmp_path),
            services_factory=ScriptedServices,
        )

    assert instance().run_once()
    assert database.get_task(saved["id"])["state"] == "awaiting_approval"
    with database.checkpointer() as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": str(saved["id"])}})
        binding = checkpoint.checkpoint["channel_values"]["data"]["human_request"]
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply='approve',reply_payload=%s "
            "WHERE id=%s",
            (Jsonb({"policy_record": {"business": True}}), binding["approval_id"]),
        )
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert instance().run_once()
    assert database.get_task(saved["id"])["state"] == "recovering"
    assert writes == [str(saved["id"])]
    state["unknown"] = False
    database.event(saved["id"], "task.resume_requested", {"reconcile": True})
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert instance().run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    assert writes == [str(saved["id"])]


def test_last_main_call_pauses_before_new_action_and_raised_budget_resumes_without_replanning(
    database, tmp_path
):
    import json
    from decimal import Decimal

    from decagent.business.registry import PluginRegistry
    from decagent.business.services import BusinessServices
    from decagent.business.tickets import builtin_manifest
    from decagent.business.worker import BusinessWorker
    from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    store = FixtureTickets(database)
    assert store.execute("seed", {}, "budget-seed")["ok"]
    directory = store.execute("directory", {}, "budget-directory")["data"]
    category = directory["categories"][0]["id"]
    scope = PluginRegistry(database).register(builtin_manifest())
    saved = database.create_business_task(
        scope["id"],
        {
            "instruction": "创建IT工单",
            "strategy": "main",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"owner_id": "owner", "authenticated": True},
        },
        {"max_calls": 1},
        employee_id=store.identity.employee_id,
        credential_ref=store.identity.credential_ref,
    )
    planning = []

    def primary_factory(settings, protocol):
        class Model:
            def respond(self, messages, **kwargs):
                observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                planning.append(len(observations))
                if not observations:
                    name = next(
                        name for name, command in protocol.commands.items() if command == "create"
                    )
                    params = {"title": "VPN fails", "category_id": category}
                else:
                    name = "decagent_finish"
                    params = {
                        "summary": "Created and verified",
                        "completed_actions": [item["action_id"] for item in observations],
                    }
                return ModelTurn(
                    "",
                    (ToolProposal("call-" + str(len(observations)), name, params),),
                    ModelUsage(10, 20, 30),
                    "offline-fixture",
                )

        return Model()

    def factory(db, settings, config, task, owner, generation):
        return BusinessServices(
            db,
            settings,
            config,
            task,
            owner,
            generation,
            mcp_client=store,
            primary_factory=primary_factory,
        )

    def instance():
        return BusinessWorker(
            database,
            Settings(data_dir=tmp_path),
            RuntimeConfig(main_price=TokenPrice("offline-fixture", Decimal(0), Decimal(0))),
            services_factory=factory,
        )

    assert instance().run_once()
    assert database.get_task(saved["id"])["state"] == "paused_budget"
    assert store.writes == 0 and planning == [0]
    with database.connect() as conn:
        action = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (saved["id"],)
        ).fetchone()
        assert action["status"] == "planned"
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.action_attempts WHERE action_id=%s", (action["id"],)
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_records WHERE task_id=%s", (saved["id"],)
            ).fetchone()["n"]
            == 1
        )
        conn.execute(
            "UPDATE app.tasks SET state='queued',budget=%s WHERE id=%s",
            (Jsonb({"max_calls": 2}), saved["id"]),
        )
    database.event(saved["id"], "task.resume_requested", {"budget": {"max_calls": 2}})
    assert instance().run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    assert store.writes == 1 and planning == [0, 1]

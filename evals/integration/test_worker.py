"""Real SQL/checkpointer lifecycle tests with a scripted, free graph capability."""

import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.runtime.worker import Worker

pytestmark = pytest.mark.integration


def task(database, **budget):
    project = database.add_project("offline/worker-" + uuid4().hex[:8])
    return database.create_task(project["id"], "a" * 40, {"issue": "Offline lifecycle"}, budget)


def worker(database, tmp_path, node, **kwargs):
    def services_factory(db, settings, config, saved, owner, generation):
        return SimpleNamespace(
            database=db,
            task_id=str(saved["id"]),
            lease_owner=owner,
            generation=generation,
            close=lambda: None,
        )

    def graph_factory(services, saver):
        def step(state):
            services.database.assert_lease(
                services.task_id, services.lease_owner, services.generation
            )
            return {**state, **node(services, state)}

        return (
            StateGraph(dict)
            .add_node("step", step)
            .add_edge(START, "step")
            .add_edge("step", END)
            .compile(checkpointer=saver)
        )

    return Worker(
        database,
        Settings(data_dir=tmp_path),
        services_factory=services_factory,
        graph_factory=graph_factory,
        **kwargs,
    )


def verified():
    return {
        "phase": "finalize",
        "verification": {
            "status": "verified",
            "verification": {"valid": True},
            "diff": "offline fixture",
        },
    }


def test_two_workers_claim_once_and_heartbeat_preserves_generation(database, tmp_path):
    saved = task(database)
    started, release = threading.Event(), threading.Event()
    calls = []

    def node(services, state):
        calls.append(services.lease_owner)
        started.set()
        assert release.wait(4)
        return verified()

    first = worker(database, tmp_path, node, owner="first", heartbeat_seconds=0.05, lease_seconds=1)
    second = worker(database, tmp_path, node, owner="second")
    thread = threading.Thread(target=first.run_once)
    thread.start()
    try:
        assert started.wait(3)
        time.sleep(1.15)
        assert second.run_once() is False
        assert database.get_task(saved["id"])["lease_generation"] == 1
    finally:
        release.set()
        thread.join(5)
    row = database.get_task(saved["id"])
    assert calls == ["first"] and row["state"] == "succeeded"
    assert float(row["active_seconds"]) >= 1
    assert row["lease_owner"] is None and row["lease_until"] is None


def test_budget_interrupt_resumes_same_checkpoint_without_counting_owner_wait(database, tmp_path):
    saved = task(database, max_calls=0)
    invocations = []

    def node(services, state):
        invocations.append(state["task_id"])
        if services.database.get_task(services.task_id)["budget"]["max_calls"] == 0:
            interrupt({"kind": "budget", "reason": "Calls exhausted", "question": "Raise budget?"})
        return verified()

    assert worker(database, tmp_path, node).run_once()
    paused = database.get_task(saved["id"])
    assert paused["state"] == "paused_budget"
    waiting = database.events(saved["id"])[-1]["payload"]
    assert waiting["question"] == "Raise budget?" and waiting["reason"] == "Calls exhausted"
    active = float(paused["active_seconds"])
    time.sleep(0.15)
    assert float(database.get_task(saved["id"])["active_seconds"]) == active
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='queued',budget=%s WHERE id=%s",
            (Jsonb({"max_calls": 2}), saved["id"]),
        )
    database.event(saved["id"], "task.resume_requested", {"budget": {"max_calls": 2}})
    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    assert invocations == [str(saved["id"]), str(saved["id"])]
    with database.checkpointer() as saver:
        checkpoint = saver.get_tuple({"configurable": {"thread_id": str(saved["id"])}})
        assert checkpoint.checkpoint["channel_values"]["__root__"]["task_id"] == str(saved["id"])


def test_replied_approval_resumes_and_future_version_is_preserved(database, tmp_path):
    saved = task(database)
    approval = database.ensure_approval(saved["id"], None, "b" * 64, "clarification")

    def node(services, state):
        reply = interrupt(
            {
                "kind": "clarification",
                "approval_id": str(approval["id"]),
                "reason": "Ambiguous",
                "question": "Which behavior?",
            }
        )
        assert reply["approval_id"] == str(approval["id"])
        return verified()

    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "awaiting_input"
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply='confirm',reply_payload=%s "
            "WHERE id=%s",
            (Jsonb({"policy_record": {"authenticated": True}}), approval["id"]),
        )
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert worker(database, tmp_path, node).run_once()
    assert database.get_task(saved["id"])["state"] == "succeeded"
    future = task(database)
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET graph_version='future' WHERE id=%s", (future["id"],))
    with pytest.raises(ValueError, match="incompatible graph"):
        worker(database, tmp_path, node).run_once()
    assert database.get_task(future["id"])["state"] == "queued"


def test_stale_worker_cannot_overwrite_cancel_or_new_lease(database, tmp_path):
    saved = task(database)

    def node(services, state):
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET state='cancelled',lease_generation=lease_generation+1,"
                "lease_owner=NULL,lease_until=NULL WHERE id=%s",
                (saved["id"],),
            )
        return verified()

    assert worker(database, tmp_path, node).run_once()
    row = database.get_task(saved["id"])
    assert row["state"] == "cancelled" and row["lease_generation"] == 2
    assert not any(e["kind"] == "task.finished" for e in database.events(saved["id"]))


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"verification": {"status": "verified"}},
        {"verification": {"status": "unverified", "verification": {"valid": True}}},
    ],
)
def test_completion_requires_independent_valid_verification(database, tmp_path, result):
    saved = task(database)
    assert worker(database, tmp_path, lambda _services, _state: result).run_once()
    assert database.get_task(saved["id"])["state"] == "failed"


def test_failure_is_terminal_redacted_and_releases_lease(database, tmp_path):
    saved = task(database)

    def fail(_services, _state):
        raise RuntimeError("offline fixture failure")

    assert worker(database, tmp_path, fail).run_once()
    row = database.get_task(saved["id"])
    assert row["state"] == "failed" and row["lease_until"] is None
    assert database.events(saved["id"])[-1]["kind"] == "task.failed"


def test_expired_running_claim_continues_checkpoint_with_durable_reply(database, tmp_path):
    saved = task(database)
    approval = database.ensure_approval(saved["id"], None, "c" * 64, "clarification")
    calls = []

    def node(services, state):
        calls.append(services.generation)
        interrupt(
            {
                "kind": "clarification",
                "approval_id": str(approval["id"]),
                "question": "Continue the recorded work?",
            }
        )
        return verified()

    first = worker(database, tmp_path, node, owner="old")
    assert first.run_once()
    # Simulate a crash after LangGraph saved the interrupt but before worker saved its wait state.
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='running',lease_owner='old',"
            "lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (saved["id"],),
        )
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply='confirm',reply_payload=%s "
            "WHERE id=%s",
            (Jsonb({"policy_record": {"authenticated": True}}), approval["id"]),
        )
    assert worker(database, tmp_path, node, owner="new").run_once()
    assert calls == [1, 2]
    assert database.get_task(saved["id"])["state"] == "succeeded"


def test_concurrent_claims_choose_distinct_rows(database, tmp_path):
    saved = [task(database), task(database)]
    barrier = threading.Barrier(2)
    claims = []

    def claim(owner):
        instance = worker(database, tmp_path, lambda _services, _state: verified(), owner=owner)
        barrier.wait(3)
        claims.append(instance.claim())

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert {claim["id"] for claim in claims} == {row["id"] for row in saved}
    assert {claim["lease_owner"] for claim in claims} == {"one", "two"}


def test_crashed_checkpoint_graph_version_is_preserved_without_restarting(database, tmp_path):
    saved = task(database)

    def node(_services, state):
        interrupt({"kind": "budget", "reason": "Fixture pause"})
        return verified()

    instance = worker(database, tmp_path, node)
    assert instance.run_once()
    with database.checkpointer() as saver:
        graph = instance.graph_factory(
            SimpleNamespace(
                database=database,
                task_id=str(saved["id"]),
                lease_owner=instance.owner,
                generation=1,
            ),
            saver,
        )
        graph.update_state(
            {"configurable": {"thread_id": str(saved["id"])}},
            {"task_id": str(saved["id"]), "graph_version": "future", "phase": "prepare"},
        )
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (saved["id"],))
    assert instance.run_once()
    assert database.get_task(saved["id"])["state"] == "recovering"
    assert "incompatible" in database.events(saved["id"])[-1]["payload"]["reason"]

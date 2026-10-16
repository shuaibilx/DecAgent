"""Version dispatch preserves each durable graph and its authenticated wait contract."""

import pytest
from langgraph.graph import END, START, StateGraph
from psycopg.types.json import Jsonb

from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.worker import Worker
from evals.integration.test_business_worker import task

pytestmark = pytest.mark.integration


def versioned_task(database, version):
    original = task(database)
    return database.create_business_task(
        original["business_scope_id"],
        original["authorization_snapshot"],
        graph_version=version,
        employee_id=original["employee_id"],
        credential_ref=original["credential_ref"],
    ), original


def services(database, calls, *, request=None, resumes=None):
    class Base:
        def __init__(self, db, settings, config, saved, owner, generation):
            self.database, self.task, self.owner, self.generation = db, saved, owner, generation
            calls.append((saved["graph_version"], str(saved["id"])))

        def guard(self):
            return self.database.assert_lease(self.task["id"], self.owner, self.generation)

        def close(self):
            pass

    class Legacy(Base):
        def run_phase(self, phase, data):
            assert data["graph_version"] == "business-2"
            return {"route": "__end__", "completion": {"valid": True}}

    class Conditional(Base):
        def prepare(self, data):
            assert data == {
                "task_id": str(self.task["id"]),
                "graph_version": "business-3",
                "phase": "prepare",
            }

        def step(self, data):
            if request and not resumes:
                return {"status": "waiting", "request": request}
            return {"status": "complete", "completion": {"valid": True}}

        def resume(self, data, reply):
            assert resumes is not None
            resumes.append(reply)

    return Legacy, Conditional


def instance(database, tmp_path, calls, **options):
    request, resumes = options.pop("request", None), options.pop("resumes", None)
    legacy, conditional = services(database, calls, request=request, resumes=resumes)
    return BusinessWorker(
        database,
        Settings(data_dir=tmp_path),
        options.pop("runtime_config", RuntimeConfig()),
        services_factory=legacy,
        conditional_services_factory=conditional,
        **options,
    )


def queue(database, task_id):
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (task_id,))


def checkpoint_data(database, task_id):
    with database.checkpointer() as saver:
        return saver.get_tuple({"configurable": {"thread_id": str(task_id)}}).checkpoint[
            "channel_values"
        ]["data"]


def test_supported_versions_use_separate_graphs_and_services(database, tmp_path):
    conditional, legacy = versioned_task(database, "business-3")
    calls = []
    worker = instance(database, tmp_path, calls)
    assert worker.run_once() and worker.run_once()
    assert calls == [("business-2", str(legacy["id"])), ("business-3", str(conditional["id"]))]
    for saved in (legacy, conditional):
        assert database.get_task(saved["id"])["state"] == "succeeded"
        assert checkpoint_data(database, saved["id"])["graph_version"] == saved["graph_version"]
    assert worker.run_once() is False


def test_unknown_task_is_quarantined_without_blocking_supported_or_repair(database, tmp_path):
    unknown, first_legacy = versioned_task(database, "business-99")
    conditional, second_legacy = versioned_task(database, "business-3")
    calls = []
    worker = instance(database, tmp_path, calls)
    assert worker.run_once()  # Oldest legacy task precedes the unknown task.
    assert worker.run_once()
    assert database.get_task(unknown["id"])["state"] == "recovering"
    assert database.events(unknown["id"])[-1]["payload"]["kind"] == "version"
    assert calls == [("business-2", str(first_legacy["id"]))]
    assert worker.run_once() and worker.run_once()
    assert database.get_task(conditional["id"])["state"] == "succeeded"
    assert database.get_task(second_legacy["id"])["state"] == "succeeded"
    assert worker.run_once() is False
    project = database.add_project("fixture/conditional-worker-isolation")
    repair = database.create_task(project["id"], "b" * 40, {})
    assert Worker(database, Settings()).claim()["id"] == repair["id"]
    assert database.get_task(unknown["id"])["graph_version"] == "business-99"


def test_entry_rollback_still_resumes_existing_conditional_checkpoint(database, tmp_path):
    conditional, legacy = versioned_task(database, "business-3")
    calls, resumes = [], []
    request = {"kind": "budget", "reason": "Fixture budget pause"}
    first = instance(
        database,
        tmp_path,
        calls,
        request=request,
        resumes=resumes,
        runtime_config=RuntimeConfig(conditional_workflows_enabled=True),
    )
    assert first.run_once() and first.run_once()
    assert database.get_task(legacy["id"])["state"] == "succeeded"
    assert database.get_task(conditional["id"])["state"] == "paused_budget"
    database.event(conditional["id"], "task.resume_requested", {"budget": {"max_calls": 30}})
    queue(database, conditional["id"])
    replacement = instance(
        database,
        tmp_path,
        calls,
        request=request,
        resumes=resumes,
        runtime_config=RuntimeConfig(conditional_workflows_enabled=False),
    )
    assert replacement.run_once()
    assert database.get_task(conditional["id"])["state"] == "succeeded"
    assert resumes == [{"budget": {"max_calls": 30}, "kind": "budget"}]
    assert checkpoint_data(database, conditional["id"])["graph_version"] == "business-3"


@pytest.mark.parametrize(
    "record,expected", [("policy_record", "awaiting_input"), ("semantic_record", "succeeded")]
)
def test_decision_resume_requires_semantic_record_and_passes_only_approval_id(
    database, tmp_path, record, expected
):
    conditional, _ = versioned_task(database, "business-3")
    approval = database.ensure_approval(conditional["id"], None, "d" * 64, "decision")
    calls, resumes = [], []
    request = {"kind": "decision", "approval_id": str(approval["id"]), "digest": approval["digest"]}
    worker = instance(database, tmp_path, calls, request=request, resumes=resumes)
    assert worker.run_once() and worker.run_once()
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply='confirm',reply_payload=%s "
            "WHERE id=%s",
            (
                Jsonb({record: {"authenticated": True}, "untrusted": "do not forward"}),
                approval["id"],
            ),
        )
    queue(database, conditional["id"])
    assert worker.run_once()
    assert database.get_task(conditional["id"])["state"] == expected
    assert resumes == ([{"approval_id": str(approval["id"])}] if expected == "succeeded" else [])


def test_conditional_checkpoint_mismatch_is_preserved_for_recovery(database, tmp_path):
    conditional, _ = versioned_task(database, "business-3")
    calls = []
    worker = instance(database, tmp_path, calls, request={"kind": "budget"}, resumes=[])
    assert worker.run_once() and worker.run_once()
    from decagent.business.conditional_graph import ConditionalState

    with database.checkpointer() as saver:
        graph = (
            StateGraph(ConditionalState)
            .add_node("fixture", lambda state: state)
            .add_edge(START, "fixture")
            .add_edge("fixture", END)
            .compile(checkpointer=saver)
        )
        graph.update_state(
            {"configurable": {"thread_id": str(conditional["id"])}},
            {
                "data": {
                    "task_id": str(conditional["id"]),
                    "graph_version": "business-2",
                    "phase": "prepare",
                }
            },
            as_node="fixture",
        )
    queue(database, conditional["id"])
    assert worker.run_once()
    assert database.get_task(conditional["id"])["state"] == "recovering"
    assert checkpoint_data(database, conditional["id"])["graph_version"] == "business-2"

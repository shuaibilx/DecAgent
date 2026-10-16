"""Plan revisions and node state stay fenced by the original task and worker lease."""

import importlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks

pytestmark = pytest.mark.integration


def repository(database, sample, owner="plan-owner"):
    module = importlib.import_module("decagent.business.plans.repository")
    task = database.claim(sample["tasks"]["business-3"]["id"], owner, seconds=120)
    return module.PlanRepository(database, task["id"], owner, task["lease_generation"])


def revise(plan, revision, description=None):
    value = deepcopy(plan.to_mapping())
    value.pop("content_digest")
    value["revision"] = revision
    if description:
        value["goal"]["description"] = description
    return value


def test_revision_is_durable_idempotent_and_cas_fenced(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    first = repo.publish(read_plan())
    assert repo.current().digest == first.digest
    assert repo.publish(read_plan()).digest == first.digest
    second = repo.publish(revise(first, 2), parent_digest=first.digest)
    assert repo.current().revision == 2
    with pytest.raises(ValueError, match="parent|revision"):
        repo.publish(revise(second, 3), parent_digest=first.digest)
    assert repo.current().digest == second.digest


def test_only_one_different_concurrent_revision_can_publish(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    first = repo.publish(read_plan())

    def submit(description):
        try:
            return repo.publish(revise(first, 2, description), parent_digest=first.digest).digest
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, ["First candidate", "Second candidate"]))
    assert sum(value is not None for value in outcomes) == 1
    assert repo.current().revision == 2


def test_node_replay_preserves_input_and_does_not_reset_counters(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    repo.publish(read_plan())
    first = repo.begin("directory", {}, {})
    repeated = repo.begin("directory", {}, {})
    assert first["input_digest"] == repeated["input_digest"]
    assert repo.counters()["node_attempts"] == 1
    with pytest.raises(ValueError, match="input"):
        repo.begin("directory", {"different": "input"}, {})
    output = {"verified": True, "data": {"people": [], "categories": []}}
    repo.finish("directory", "succeeded", output)
    assert repo.begin("directory", {}, {})["output_snapshot"] == output
    assert repo.counters()["node_attempts"] == 1


def test_replan_cannot_rewrite_executed_node(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    first = repo.publish(read_plan())
    repo.begin("directory", {}, {})
    repo.finish("directory", "succeeded", {"verified": True})
    changed = revise(first, 2)
    changed["nodes"][0]["tool"]["command"] = "list"
    changed["goal"]["evidence"][0]["source"]["path"] = ["tickets"]
    with pytest.raises(ValueError, match="frozen"):
        repo.publish(changed, parent_digest=first.digest)
    assert repo.current().revision == 1


def test_lease_takeover_rejects_original_owner(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    repo.publish(read_plan())
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (repo.task_id,),
        )
    database.claim(repo.task_id, "replacement", seconds=120)
    with pytest.raises(ValueError, match="lease"):
        repo.begin("directory", {}, {})


def test_node_dispatch_counters_are_bounded_across_replay(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    repo.publish(read_plan())
    repo.begin("directory", {}, {})
    assert repo.dispatch("directory", max_attempts=2)["dispatch_attempts"] == 1
    assert repo.dispatch("directory", max_attempts=2)["dispatch_attempts"] == 2
    with pytest.raises(ValueError, match="attempt"):
        repo.dispatch("directory", max_attempts=2)
    assert repo.counters()["node_attempts"] == 2


def test_revision_inherits_published_input_contract(database, mixed_business_tasks):
    from evals.deterministic.test_business_plan_compiler import branching_plan

    repo = repository(database, mixed_business_tasks)
    schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "ticket_id": {"type": "string"},
        },
    }
    first = repo.publish(branching_plan(), input_schema=schema)
    second = repo.publish(revise(first, 2), parent_digest=first.digest)
    assert second.revision == 2
    assert repo.context_snapshot()["input_schema"] == schema


def test_revision_cannot_silently_replace_task_input_contract(database, mixed_business_tasks):
    repo = repository(database, mixed_business_tasks)
    first = repo.publish(read_plan())
    schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string"},
            "new_source": {"type": "string"},
        },
    }
    with pytest.raises(ValueError, match="input|schema"):
        repo.publish(revise(first, 2), parent_digest=first.digest, input_schema=schema)
    assert repo.current().revision == 1

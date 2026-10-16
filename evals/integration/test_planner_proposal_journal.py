"""Planner replay uses a stable durable intent before any paid transport attempt."""

import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Event

import pytest

from decagent.business.context import ContextBuilder, TaskWorkingMemory
from decagent.business.plans.planner import ConditionalPlanner, PlanProposal
from decagent.business.plans.repository import PlanConflict, PlanRepository
from decagent.business.plans.runtime import NodePending
from decagent.business.registry import PluginRegistry
from decagent.models.primary import PrimaryRequestNotDispatched
from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice
from decagent.storage.usage import UsageLedger
from evals.deterministic.test_business_conditional_planner import ScriptedModel, turn
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration
MODULE = "decagent.business.plans.planner_journal"


@pytest.fixture
def sample(database):
    backend = FixtureTickets(database)
    scope = PluginRegistry(database).list()[0]
    task = database.create_business_task(
        scope["id"],
        {"instruction": "Read the directory", "plugin_digest": scope["manifest_digest"]},
        {"max_tokens": 300000},
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    held = database.claim(task["id"], "planner-journal", seconds=120)
    repo = PlanRepository(database, task["id"], held["lease_owner"], held["lease_generation"])

    def journal(*args, **kwargs):
        return importlib.import_module(MODULE).PlannerProposalJournal(*args, **kwargs)

    context = (
        ContextBuilder(TaskWorkingMemory(repo).rebuild(persist=False), system_prefix="")
        .planning()
        .to_mapping()
    )
    return {"repo": repo, "journal": journal, "context": context}


def planner(database, sample, values):
    model = ScriptedModel(values)
    primary = BudgetedPrimary(
        model,
        UsageLedger(database, sample["repo"].task_id),
        price=TokenPrice("fixture-price", Decimal("1"), Decimal("1")),
        max_output_tokens=128,
        before_dispatch=sample["repo"].guard,
    )
    return ConditionalPlanner(primary, sample["repo"].spec), model


def records(database, sample):
    with database.connect() as conn:
        return conn.execute(
            "SELECT kind,payload FROM app.events WHERE task_id=%s "
            "AND kind LIKE 'business.planner_%%' ORDER BY seq",
            (sample["repo"].task_id,),
        ).fetchall()


def usage(database, sample):
    with database.connect() as conn:
        return conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s ORDER BY created_at,id",
            (sample["repo"].task_id,),
        ).fetchall()


def crash_after_response(proposal):
    raise SystemExit("Injected crash after planner returned")


def test_cached_safe_proposal_survives_restart_before_initial_publication(database, sample):
    assert importlib.util.find_spec(MODULE), "Durable planner proposal journal is missing"
    plan, model = planner(database, sample, [turn(read_plan(), text="sdk-private-marker")])
    proposal = sample["journal"](sample["repo"]).propose(plan, sample["context"])
    assert isinstance(proposal, PlanProposal) and proposal.turns == ()
    assert sample["repo"].current() is None
    changed = {**sample["context"], "event_seq": 999, "budget": {"max_primary_calls": 6}}
    replay = sample["journal"](sample["repo"]).propose(plan, changed)
    assert replay.plan.digest == proposal.plan.digest and replay.turns == ()
    assert len(model.calls) == 1 and len(usage(database, sample)) == 1
    saved = records(database, sample)
    assert [item["payload"]["phase"] for item in saved] == ["dispatched", "proposed"]
    assert "sdk-private-marker" not in json.dumps(saved)
    assert sample["repo"].publish(replay.plan).digest == proposal.plan.digest


def test_returned_but_not_saved_result_never_resends_after_context_or_lease_change(
    database, sample
):
    plan, model = planner(database, sample, [turn(read_plan())])
    journal = sample["journal"](sample["repo"], after_response=crash_after_response)
    with pytest.raises(SystemExit):
        journal.propose(plan, sample["context"])
    assert usage(database, sample)[0]["dispatch_state"] == "responded"
    task_id = sample["repo"].task_id
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (task_id,),
        )
    held = database.claim(task_id, "replacement-journal", seconds=120)
    replacement = PlanRepository(database, task_id, held["lease_owner"], held["lease_generation"])
    with pytest.raises(NodePending) as pending:
        sample["journal"](replacement).propose(plan, {**sample["context"], "event_seq": 888})
    assert pending.value.unknown
    assert pending.value.request["category"] == "planner_request_unknown"
    assert len(model.calls) == 1 and len(usage(database, sample)) == 1
    assert len(records(database, sample)) == 1


@pytest.mark.parametrize(
    "outcome,values,dispatches",
    [
        ("not_sent", [PrimaryRequestNotDispatched(reason="context_limit")], ["not_sent"]),
        ("invalid", [turn(text="bad plan"), turn(text="still bad")], ["responded", "responded"]),
        ("unknown", [ConnectionError("private-provider-error")], ["unknown"]),
    ],
)
def test_failures_keep_actual_usage_and_require_explicit_reconciliation(
    database,
    sample,
    outcome,
    values,
    dispatches,
):
    plan, model = planner(database, sample, values)
    for context in (sample["context"], {**sample["context"], "budget": "changed"}):
        with pytest.raises(NodePending) as pending:
            sample["journal"](sample["repo"]).propose(plan, context)
        assert pending.value.request["outcome"] == outcome
        assert pending.value.unknown is (outcome == "unknown")
    assert [item["dispatch_state"] for item in usage(database, sample)] == dispatches
    assert len(model.calls) == len(values)
    saved = records(database, sample)
    assert saved[-1]["payload"]["phase"] == "failed"
    assert saved[-1]["payload"]["outcome"] == outcome
    assert "private-provider-error" not in json.dumps(saved)


def test_saved_repair_flag_recompiles_without_repeating_format_repair(database, sample):
    plan, model = planner(database, sample, [turn(text="invalid"), turn(read_plan())])
    first = sample["journal"](sample["repo"]).propose(plan, sample["context"])
    second = sample["journal"](sample["repo"]).propose(plan, sample["context"])
    assert first.repaired and second.repaired
    assert len(model.calls) == 2 and len(usage(database, sample)) == 2


def test_stale_lease_cannot_dispatch_or_save_returned_proposal(database, sample):
    repo = sample["repo"]
    plan, model = planner(database, sample, [turn(read_plan())])

    def expire(proposal):
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE id=%s",
                (repo.task_id,),
            )

    with pytest.raises(ValueError, match="lease"):
        sample["journal"](repo, after_response=expire).propose(plan, sample["context"])
    assert len(model.calls) == 1
    assert [item["payload"]["phase"] for item in records(database, sample)] == ["dispatched"]
    with pytest.raises(ValueError, match="lease"):
        sample["journal"](repo).propose(plan, sample["context"])
    assert len(model.calls) == 1


def test_revision_parent_cas_is_checked_before_request_and_before_proposal_save(database, sample):
    repo = sample["repo"]
    first = repo.publish(read_plan())
    value = deepcopy(first.to_mapping())
    value.pop("content_digest", None)
    value["revision"] = 2
    plan, model = planner(database, sample, [turn(value)])
    context = {**sample["context"], "parent_revision": 1, "replan_reason": "new_observation"}
    with pytest.raises(PlanConflict):
        sample["journal"](repo).propose(plan, context, parent_digest="a" * 64)
    assert len(model.calls) == 0

    def publish_concurrently(proposal):
        repo.publish(value, parent_digest=first.digest)

    with pytest.raises(PlanConflict):
        sample["journal"](repo, after_response=publish_concurrently).propose(
            plan,
            context,
            parent_digest=first.digest,
        )
    assert len(model.calls) == 1
    assert [item["payload"]["phase"] for item in records(database, sample)] == ["dispatched"]


def test_concurrent_call_sees_durable_intent_instead_of_dispatching_twice(database, sample):
    plan, model = planner(database, sample, [turn(read_plan())])
    received, release = Event(), Event()

    def hold(proposal):
        received.set()
        assert release.wait(timeout=20)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            sample["journal"](sample["repo"], after_response=hold).propose, plan, sample["context"]
        )
        assert received.wait(timeout=20)
        try:
            with pytest.raises(NodePending):
                sample["journal"](sample["repo"]).propose(plan, sample["context"])
        finally:
            release.set()
        assert first.result().plan.revision == 1
    assert len(model.calls) == 1 and len(usage(database, sample)) == 1


@pytest.mark.parametrize("during_response", [False, True])
def test_scope_change_cannot_reuse_a_proposal_or_dispatch_again(database, sample, during_response):
    repo = sample["repo"]
    plan, model = planner(database, sample, [turn(read_plan())])

    def change_scope(proposal=None):
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
                (repo.guard(conn)["business_scope_id"],),
            )

    if during_response:
        initial = sample["journal"](repo, after_response=change_scope)
        with pytest.raises(NodePending) as waiting:
            initial.propose(plan, sample["context"])
        assert waiting.value.request["category"] == "planner_proposal_stale"
        assert waiting.value.unknown is False
    else:
        sample["journal"](repo).propose(plan, sample["context"])
        change_scope()
    with pytest.raises(NodePending) as replay:
        sample["journal"](repo).propose(plan, sample["context"])
    assert replay.value.request["category"] == "planner_proposal_stale"
    assert len(model.calls) == 1 and len(usage(database, sample)) == 1
    assert repo.current() is None


def test_scope_change_between_proposal_and_publication_is_checked_atomically(database, sample):
    repo = sample["repo"]
    plan, model = planner(database, sample, [turn(read_plan())])
    journal = sample["journal"](repo)
    proposal = journal.propose(plan, sample["context"])
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (repo.guard(conn)["business_scope_id"],),
        )
    with pytest.raises(NodePending) as pending:
        journal.publish(proposal)
    assert pending.value.request["category"] == "planner_proposal_stale"
    assert pending.value.unknown is False
    assert repo.current() is None and len(model.calls) == 1


def test_context_epoch_is_checked_before_any_planner_intent_or_paid_dispatch(database, sample):
    repo = sample["repo"]
    plan, model = planner(database, sample, [turn(read_plan())])
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
            (repo.guard(conn)["business_scope_id"],),
        )
    with pytest.raises(NodePending) as pending:
        sample["journal"](repo).propose(plan, sample["context"])
    assert pending.value.request["category"] == "planner_context_stale"
    assert not model.calls and not usage(database, sample) and not records(database, sample)
    fresh = (
        ContextBuilder(TaskWorkingMemory(repo).rebuild(persist=False), system_prefix="")
        .planning()
        .to_mapping()
    )
    journal = sample["journal"](repo)
    result = journal.propose(plan, fresh)
    assert journal.publish(result).revision == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    "field", ["employee_id", "scope_id", "scope_revision", "account_revision", "plugin_digest"]
)
def test_incomplete_planning_boundary_cannot_reach_the_model(database, sample, field):
    plan, model = planner(database, sample, [turn(read_plan())])
    context = deepcopy(sample["context"])
    context["boundary"].pop(field)
    with pytest.raises(NodePending) as pending:
        sample["journal"](sample["repo"]).propose(plan, context)
    assert pending.value.request["category"] == "planner_context_stale"
    assert not model.calls and not records(database, sample)


@pytest.mark.parametrize("repair", [False, True])
def test_permission_change_after_reservation_is_not_sent_including_format_repair(
    database, sample, monkeypatch, repair
):
    repo = sample["repo"]
    responses = [turn(text="invalid"), turn(read_plan())] if repair else [turn(read_plan())]
    value, model = planner(database, sample, responses)
    reserve = value.primary.ledger.reserve
    reserved = []

    def change_after_reservation(*args, **kwargs):
        receipt = reserve(*args, **kwargs)
        reserved.append(receipt)
        if len(reserved) == (2 if repair else 1):
            with database.connect() as conn:
                conn.execute(
                    "UPDATE app.business_scopes SET revision=revision+1 WHERE id=%s",
                    (repo.guard(conn)["business_scope_id"],),
                )
        return receipt

    monkeypatch.setattr(value.primary.ledger, "reserve", change_after_reservation)
    with pytest.raises(NodePending) as waiting:
        sample["journal"](repo).propose(value, sample["context"])
    assert waiting.value.request["category"] == "planner_proposal_stale"
    assert len(model.calls) == (1 if repair else 0)
    assert [row["dispatch_state"] for row in usage(database, sample)] == (
        ["responded", "not_sent"] if repair else ["not_sent"]
    )
    assert repo.current() is None

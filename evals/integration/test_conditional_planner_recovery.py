"""Production entry points retain planner identity across process death."""

from decimal import Decimal

import pytest

from decagent.business.plans.planner import ConditionalPlanner
from decagent.business.plans.planner_journal import PlannerProposalJournal
from decagent.business.plans.runtime import NodePending
from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice
from evals.deterministic.test_business_conditional_planner import ScriptedModel, turn
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.test_business_conditional_services import (
    MCPConnectionManager,
    Planner,
    drive,
    harness,
    task,
)
from evals.integration.test_business_mcp_client import service as service

pytestmark = pytest.mark.integration


def test_safe_initial_proposal_is_reused_after_crash_before_publication(service, tmp_path):
    database, url, auth, tokens, _ = service
    value, _, token = task(database, auth, tokens, "查看目录")
    planner = Planner(read_plan())
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, planner, tmp_path)

        def crash_before_publication(*args, **kwargs):
            raise SystemExit("Injected exit before publication")

        initial.repository.publish = crash_before_publication
        with pytest.raises(SystemExit):
            initial.prepare({})
        replacement = harness(database, url, value, client, planner, tmp_path)
        assert drive(replacement)["completion"]["valid"]
    assert len(planner.calls) == 1
    assert replacement.repository.current().revision == 1


def test_unsaved_initial_response_waits_without_resending(service, tmp_path, monkeypatch):
    database, url, auth, tokens, _ = service
    value, _, token = task(database, auth, tokens, "查看目录")
    planner = Planner(read_plan())
    real = PlannerProposalJournal.propose
    fired = []

    def crash_after_response(proposal):
        if not fired:
            fired.append(True)
            raise SystemExit("Injected response/save gap")

    def injected(journal, *args, **kwargs):
        journal.after_response = crash_after_response
        return real(journal, *args, **kwargs)

    monkeypatch.setattr(PlannerProposalJournal, "propose", injected)
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, planner, tmp_path)
        with pytest.raises(SystemExit):
            initial.prepare({})
        replacement = harness(database, url, value, client, planner, tmp_path)
        with pytest.raises(NodePending) as waiting:
            replacement.prepare({})
        assert waiting.value.request["category"] == "planner_request_unknown"
        assert replacement.repository.current() is None
    assert len(planner.calls) == 1


def test_default_worker_factory_runs_conditional_graph_over_real_mcp(
    service, tmp_path, monkeypatch
):
    database, url, auth, tokens, _ = service
    value, _, _ = task(database, auth, tokens, "查看目录")
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='queued',lease_owner=NULL,lease_until=NULL,"
            "budget=budget||'{\"max_tokens\":300000}'::jsonb WHERE id=%s",
            (value["id"],),
        )
    model = ScriptedModel([turn(read_plan())])

    def planner(settings, config, spec, ledger, *, before_dispatch):
        primary = BudgetedPrimary(
            model,
            ledger,
            price=TokenPrice("fixture-price", Decimal("1"), Decimal("1")),
            max_output_tokens=128,
            before_dispatch=before_dispatch,
        )
        return ConditionalPlanner(primary, spec)

    monkeypatch.setattr("decagent.business.conditional_services.budgeted_planner", planner)
    with MCPConnectionManager(url, auth) as manager:
        worker = BusinessWorker(
            database,
            Settings(database_url=database.url, data_dir=tmp_path),
            RuntimeConfig(tickets_mcp_url=url),
            employee_auth=auth,
            mcp_manager=manager,
            heartbeat_seconds=1,
            lease_seconds=30,
        )
        assert worker.run_once()
        assert worker.run_once() is False
        worker.close()
        assert not manager.closed and manager.connection_count == 1
    saved = database.get_task(value["id"])
    assert saved["state"] == "succeeded" and saved["graph_version"] == "business-3"
    assert len(model.calls) == 1
    with database.checkpointer() as checkpointer:
        state = checkpointer.get_tuple({"configurable": {"thread_id": str(value["id"])}})
        assert state.checkpoint["channel_values"]["data"]["graph_version"] == "business-3"
    with database.connect() as conn:
        usage = conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (value["id"],)
        ).fetchall()
        assert len(usage) == 1
        assert usage[0]["model_family"] == "primary" and usage[0]["purpose"] == "plan"
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 0

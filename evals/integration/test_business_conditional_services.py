"""The production conditional harness composes durable plans with real MCP governance."""

import importlib
import json

import pytest

from decagent.business.mcp_client import MCPConnectionManager
from decagent.business.plans.contracts import Plan
from decagent.business.plans.planner import PlanProposal
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.registry import PluginRegistry
from decagent.config import Settings
from decagent.gateway.models import SemanticReplyInput
from decagent.runtime.config import RuntimeConfig
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import literal, node, reference
from evals.integration.test_business_conditional_batches import Adapter
from evals.integration.test_business_conditional_mcp import create_plan
from evals.integration.test_business_mcp_client import service as service
from evals.integration.test_business_mcp_worker import _browser

pytestmark = pytest.mark.integration


class Planner:
    def __init__(self, value):
        self.value, self.calls = value, []

    def plan(self, context, **kwargs):
        self.calls.append(context)
        return PlanProposal(Plan.from_mapping(self.value), (), False)


def task(database, auth, tokens, instruction):
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    credential = auth.issue_mcp(browser)
    scope = PluginRegistry(database).list()[0]
    value = database.create_business_task(
        scope["id"],
        {"instruction": instruction, "plugin_digest": scope["manifest_digest"]},
        graph_version="business-3",
        employee_id=actor.employee_id,
        credential_ref=credential["credential_ref"],
    )
    value = database.claim(value["id"], "conditional-services", seconds=120)
    token = auth.resolve_credential_ref(value["id"])
    return value, browser, token


def harness(database, url, value, client, planner, tmp_path, adapter=None):
    api = importlib.import_module("decagent.business.conditional_services")
    return api.ConditionalBusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(tickets_mcp_url=url),
        value,
        value["lease_owner"],
        value["lease_generation"],
        mcp_client=client,
        planner=planner,
        decision_adapter=adapter,
    )


def drive(value):
    value.prepare({})
    for _ in range(12):
        result = value.step({})
        if result["status"] != "advanced":
            return result
    raise AssertionError("Finite fixture failed to settle")


def test_saved_plan_restart_uses_bounded_external_context_without_another_planner(
    service, tmp_path
):
    database, url, auth, tokens, _ = service
    value, _, token = task(database, auth, tokens, "查看目录")
    planner = Planner(read_plan())
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, planner, tmp_path)
        initial.prepare({})
        assert initial.step({})["status"] == "advanced"
        resumed = harness(database, url, value, client, planner, tmp_path)
        result = drive(resumed)
    assert result["status"] == "complete" and result["completion"]["valid"]
    assert len(planner.calls) == 1
    assert planner.calls[0]["boundary"]["semantic_selection_grants_write"] is False
    assert "budget" in planner.calls[0] and "decisions" in planner.calls[0]


def test_conditional_harness_commits_one_actual_mcp_write(service, tmp_path):
    database, url, auth, tokens, category = service
    params = {"title": "Production conditional harness", "category_id": category}
    value, _, token = task(
        database, auth, tokens, json.dumps({"command": "create", "params": params})
    )
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    planner = Planner(create_plan(spec, params))
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        assert drive(harness(database, url, value, client, planner, tmp_path))["completion"][
            "valid"
        ]
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM business.operations WHERE agent_task_id=%s",
                (value["id"],),
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )


def choice_plan(spec):
    plan = read_plan()
    definition = QuestionRegistry.from_spec(spec).get("explicit_assignee")
    plan["nodes"][0]["next"] = ["judge"]
    plan["nodes"].insert(
        1,
        node(
            "judge",
            "decide",
            question={
                "question_id": definition["question_id"],
                "version": definition["version"],
                "type": "noul",
            },
            policy={"policy_id": "not-published", "version": "1"},
            inputs={
                "instruction": reference("task_input", path=["instruction"]),
                "directory": reference("node_output", node_id="directory", value_type="object"),
            },
            next=["choose"],
            on_no_match={"stop": "no_match"},
        ),
    )
    plan["nodes"].insert(
        2,
        node(
            "choose",
            "branch",
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": reference(
                            "accepted_decision",
                            node_id="judge",
                            path=["selected_value"],
                            value_type="boolean",
                        ),
                        "right": literal(True, "boolean"),
                    },
                    "next": ["yes"],
                }
            ],
            on_no_match={"node_id": "no"},
        ),
    )
    for label in ("yes", "no"):
        plan["nodes"].insert(-1, node(label, "join", mode="all_active", next=["done"]))
    plan["nodes"][-1]["depends_on"] = []
    return plan


@pytest.mark.parametrize("human_value", [True, False])
def test_low_certainty_human_noul_reuses_original_raw_batch_after_restart(
    service, tmp_path, human_value
):
    database, url, auth, tokens, _ = service
    value, browser, token = task(database, auth, tokens, "查看目录，判断是否指定处理人")
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    planner, adapter = Planner(choice_plan(spec)), Adapter()
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, planner, tmp_path, adapter)
        first = drive(initial)
        assert first["status"] == "waiting" and first["request"]["kind"] == "decision"
        pending = first["request"]
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
                "WHERE id=%s",
                (value["id"],),
            )
        initial.interventions.reply(
            browser,
            pending["approval_id"],
            SemanticReplyInput(
                expected_digest=pending["digest"],
                expected_plan_revision=1,
                decision="select",
                value=human_value,
            ),
            response_id="authenticated-test-selection",
        )
        value = database.claim(value["id"], "resumed-harness", seconds=120)
        resumed = harness(database, url, value, client, planner, tmp_path, adapter)
        result = drive(resumed)
        assert result["status"] == "complete" and result["completion"]["valid"], result
        nodes = {row["node_id"]: row for row in resumed.repository.nodes()}
        assert ("yes" in nodes) == human_value and ("no" in nodes) != human_value
        accepted = nodes["judge"]["output_snapshot"]["result"]
        assert accepted["value"] == {"selected_value": human_value}
        assert accepted["raw_model_answer"]["noul"] == 0.9
        assert accepted["selection_origin"] == "employee_session"
        assert accepted["grants_write_authority"] is False
        effective = resumed.memory.rebuild().to_mapping()["decisions"]["judge"]["effective"]
        assert effective["value"] == human_value and effective["classification"] == "user_confirmed"
    assert adapter.calls == 1 and len(planner.calls) == 1


@pytest.mark.parametrize("scope_error", [False, True])
def test_authenticated_supplement_replans_only_the_unexecuted_remainder(
    service, tmp_path, monkeypatch, scope_error
):
    from copy import deepcopy

    database, url, auth, tokens, _ = service
    value, browser, token = task(database, auth, tokens, "查看目录，判断是否指定处理人")
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    original, adapter = choice_plan(spec), Adapter()
    if scope_error:
        monkeypatch.setattr("decagent.business.plans.batching.MAX_INPUT_TOKENS", 100)
    supplement = "我只需要查看目录，不需要分配处理人。"

    class LocalPlanner(Planner):
        def plan(self, context, **kwargs):
            if self.calls:
                assert context["replan_reason"] == "new_observation"
                assert supplement in json.dumps(context, ensure_ascii=False)
                candidate = deepcopy(original)
                candidate["revision"] = 2
                next(n for n in candidate["nodes"] if n["id"] == "choose")["cases"][0]["condition"][
                    "left"
                ] = literal(False, "boolean")
                self.calls.append(context)
                return PlanProposal(Plan.from_mapping(candidate), (), False)
            return super().plan(context, **kwargs)

    planner = LocalPlanner(original)
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, planner, tmp_path, adapter)
        pending = drive(initial)["request"]
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
                "WHERE id=%s",
                (value["id"],),
            )
        assert pending.get("approval_id"), "Scope waiting must have a durable employee reply"
        if scope_error:
            from decagent.gateway.business import BusinessControl
            from decagent.gateway.models import ReplyInput

            with database.connect() as conn:
                waiting = conn.execute(
                    "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (value["id"],)
                ).fetchone()
                approval = conn.execute(
                    "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (pending["approval_id"],)
                ).fetchone()
                BusinessControl(database).reply_employee(
                    conn,
                    waiting,
                    approval,
                    ReplyInput(
                        expected_digest=approval["digest"],
                        decision="confirm",
                        confirmation=supplement,
                    ),
                    browser,
                    "narrow-current-scope",
                )
        else:
            initial.interventions.reply(
                browser,
                pending["approval_id"],
                SemanticReplyInput(
                    expected_digest=pending["digest"],
                    expected_plan_revision=1,
                    decision="supplement",
                    text=supplement,
                ),
                response_id="supplement-for-same-task",
            )
        value = database.claim(value["id"], "supplement-resume", seconds=120)
        resumed = harness(database, url, value, client, planner, tmp_path, adapter)
        result = drive(resumed)
        assert result["status"] == "complete" and result["completion"]["valid"], result
        assert resumed.repository.current().revision == 2
        rows = {r["node_id"]: r for r in resumed.repository.nodes()}
        assert rows["judge"]["status"] == "skipped" and "no" in rows and "yes" not in rows
        assert rows["judge"]["output_snapshot"]["result"]["accepted"] is False
        # Restart after publication never repeats the supplemental planner call.
        assert (
            drive(harness(database, url, value, client, planner, tmp_path, adapter))["status"]
            == "complete"
        )
    assert len(planner.calls) == 2 and adapter.calls == (0 if scope_error else 1)


def test_fresh_read_transport_failure_keeps_original_raw_batch_for_explicit_recovery(
    service, tmp_path
):
    from decagent.business.mcp_client import MCPTransportError

    database, url, auth, tokens, _ = service
    value, _, token = task(database, auth, tokens, "查看目录，判断是否指定处理人")
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"], scope["manifest_digest"])
    planner, adapter = Planner(choice_plan(spec)), Adapter()
    with MCPConnectionManager(url, auth) as manager:
        real = manager.client(auth.authenticate_mcp_token(token), token)

        class UnreliableRead:
            calls = 0

            def __getattr__(self, name):
                return getattr(real, name)

            def read(self, *args):
                self.calls += 1
                if self.calls == 2:
                    raise MCPTransportError("Injected lost freshness read")
                return real.read(*args)

        value_service = harness(database, url, value, UnreliableRead(), planner, tmp_path, adapter)
        pending = drive(value_service)
        assert pending["status"] == "waiting" and pending["request"]["kind"] == "reconciliation"
        pending = drive(value_service)
        assert pending["request"]["kind"] == "decision" and pending["request"]["approval_id"]
    assert adapter.calls == 1 and len(planner.calls) == 1

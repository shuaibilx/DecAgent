"""Real business phases work without main/GitHub credentials for literal safe requests."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb

from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest
from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import TokenPrice
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


def services(database, instruction):
    store = FixtureTickets(database)
    assert store.execute("seed", {}, "seed")["ok"]
    scope = PluginRegistry(database).register(builtin_manifest())
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": instruction,
            "strategy": "hybrid",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"owner_id": "owner", "authenticated": True},
        },
        employee_id=store.identity.employee_id,
        credential_ref=store.identity.credential_ref,
    )
    task = database.claim(task["id"], "worker", seconds=120)
    return BusinessServices(
        database,
        Settings(database_url=database.url),
        RuntimeConfig(),
        task,
        "worker",
        task["lease_generation"],
        mcp_client=store,
    )


def test_literal_create_passes_governance_receipt_and_goal_verification(database):
    store = FixtureTickets(database)
    assert store.execute("seed", {}, "seed")["ok"]
    category = store.execute("directory", {}, "dir")["data"]["categories"][0]["id"]
    instruction = json.dumps(
        {
            "command": "create",
            "params": {
                "title": "Printer unavailable",
                "category_id": category,
            },
        },
        ensure_ascii=False,
    )
    service = services(database, instruction)
    data = {"instruction": instruction, "strategy": "hybrid", "steps": 0}
    phase = "prepare"
    visited = []
    for _ in range(20):
        visited.append(phase)
        data = {**data, **service.run_phase(phase, data)}
        phase = data["route"]
        if phase in {"end", "__end__"}:
            break
    assert data["completion"]["valid"] is True
    assert "execute" in visited and "verify" in visited
    assert service._primary is None
    assert data["observations"][0]["data"]["ticket"]["status"] == "open"


def test_unresolved_close_is_rejected_before_dispatch(database):
    store = FixtureTickets(database)
    assert store.execute("seed", {}, "seed")["ok"]
    category = store.execute("directory", {}, "dir")["data"]["categories"][0]["id"]
    ticket = store.execute("create", {"title": "Fixture", "category_id": category}, "create")[
        "data"
    ]["ticket"]
    instruction = json.dumps(
        {
            "command": "close",
            "params": {
                "ticket_id": ticket["id"],
                "expected_revision": 1,
            },
        }
    )
    service = services(database, instruction)
    data = {"instruction": instruction, "strategy": "hybrid", "steps": 0}
    for phase in ("prepare", "decide", "validate"):
        data = {**data, **service.run_phase(phase, data)}
    assert data["route"] == "finalize" and data["completion"]["valid"] is False
    assert (
        store.execute("get", {"ticket_id": ticket["id"]}, "get")["data"]["ticket"]["status"]
        == "open"
    )


def test_phase_progress_is_persisted_and_fenced(database):
    service = services(database, "查看工单列表")
    service.run_phase("prepare", {"instruction": "查看工单列表", "strategy": "rule", "steps": 0})
    events = database.events(service.task["id"])
    assert events[-1]["kind"] == "business.phase"
    assert events[-1]["payload"]["phase"] == "prepare"
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_generation=lease_generation+1 WHERE id=%s",
            (service.task["id"],),
        )
    with pytest.raises(ValueError):
        service.run_phase("decide", {"instruction": "查看工单列表", "strategy": "rule", "steps": 1})
    assert len(database.events(service.task["id"])) == len(events)


def test_human_reply_cannot_use_another_question_from_same_task(database):
    service = services(database, "查看工单列表")
    data = {
        "instruction": "查看工单列表",
        "human_kind": "clarification",
        "question": "哪一个部门？",
    }
    pending = service.human_request(data)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.approvals SET status='replied',reply='confirm',reply_payload=%s "
            "WHERE id=%s",
            (
                Jsonb(
                    {"policy_record": {"business": True, "owner_id": "owner", "confirmation": "IT"}}
                ),
                pending["approval_id"],
            ),
        )
    changed = {**data, "question": "哪一个工单？"}
    with pytest.raises(ValueError, match="current.*question|current.*proposal"):
        service.human_reply(changed, {"approval_id": pending["approval_id"]})


def test_missing_calibration_records_fallback_without_initializing_models(database):
    service = services(database, "请创建一个网络工单")
    data = {"instruction": "请创建一个网络工单", "strategy": "hybrid"}
    data = {**data, **service.run_phase("prepare", data)}
    routed = service.run_phase("decide", data)
    assert routed["route"] == "plan" and service._jev is None and service._primary is None
    with database.connect() as conn:
        saved = conn.execute(
            "SELECT result,final_route FROM app.decision_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchone()
    assert saved["final_route"] == "plan"
    assert saved["result"]["evaluation"]["error"] == "missing_compatible_calibration_or_credentials"
    assert saved["result"]["request"]["questions"]


def test_main_planner_create_and_assign_completes_with_receipts_not_ticket_closure(database):
    from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal

    service = services(database, "请创建网络故障工单，并分配给 IT Support 的Zhang Wei")
    service.config = RuntimeConfig(main_price=TokenPrice("offline-fixture", Decimal(0), Decimal(0)))
    directory = service.executor.read("directory", {})
    category = directory["categories"][0]["id"]
    person = next(
        row
        for row in directory["people"]
        if row["name"] == "Zhang Wei" and row["team"] == "IT Support"
    )
    names = {command: name for name, command in service.protocol.commands.items()}

    class ScriptedModel:
        def respond(self, messages, **kwargs):
            observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not observations:
                name, params = names["create"], {"title": "网络故障", "category_id": category}
            elif len(observations) == 1:
                ticket = observations[0]["data"]["ticket"]
                name, params = (
                    names["assign"],
                    {
                        "ticket_id": ticket["id"],
                        "expected_revision": ticket["revision"],
                        "person_id": person["id"],
                    },
                )
            else:
                name, params = (
                    "decagent_finish",
                    {
                        "summary": "工单已创建并分派",
                        "completed_actions": [row["action_id"] for row in observations],
                    },
                )
            service.protocol.validate(name, params)
            return ModelTurn(
                "",
                (ToolProposal("call-" + str(len(observations)), name, params),),
                ModelUsage(100, 20, 120),
                "offline-fixture",
            )

    service.primary_factory = lambda settings, protocol: ScriptedModel()
    data = {
        "instruction": "请创建网络故障工单，并分配给 IT Support 的Zhang Wei",
        "strategy": "main",
    }
    phase = "prepare"
    for step in range(40):
        data = {**data, "steps": step, **service.run_phase(phase, data)}
        phase = data["route"]
        if phase == "__end__":
            break
        assert phase != "human", data
    assert data["completion"]["valid"] is True
    assert len(data["completion"]["actions"]) == 2
    assert data["observations"][-1]["data"]["ticket"]["status"] == "assigned"


def test_finish_cannot_substitute_directory_query_for_requested_list(database):
    service = services(database, "请列出工单")
    data = {
        "instruction": "请列出工单",
        "planning_mode": "literal",
        "queue": [{"command": "directory", "params": {}}],
    }
    for phase in ("prepare", "validate", "execute", "verify"):
        data = {**data, **service.run_phase(phase, data)}
    result = service.run_phase(
        "finalize", {**data, "completion": {"valid": True, "summary": "done"}}
    )
    assert result["completion"]["valid"] is False


def test_invalid_primary_plan_is_bounded_recorded_and_cannot_dispatch(database):
    service = services(database, "请创建网络工单")
    service.config = RuntimeConfig(main_price=TokenPrice("offline-fixture", Decimal(0), Decimal(0)))
    calls = []

    class InvalidModel:
        def respond(self, messages, **kwargs):
            calls.append(messages)
            raise ValueError("Unregistered tool proposal")

    service.primary_factory = lambda settings, protocol: InvalidModel()
    data = {"instruction": "请创建网络工单", "strategy": "main"}
    for _ in range(3):
        data = {**data, **service.run_phase("plan", data)}
        if data["route"] == "human":
            break
    assert data["route"] == "human" and len(calls) == 2
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.actions WHERE task_id=%s", (service.task["id"],)
            ).fetchone()["n"]
            == 0
        )
        records = conn.execute(
            "SELECT result FROM app.decision_records WHERE task_id=%s", (service.task["id"],)
        ).fetchall()
    assert len(records) == 2 and all(
        row["result"]["error"] == "planner_response_invalid" for row in records
    )


@pytest.mark.parametrize("failure", ["low_certainty", "model_drift", "invalid", "unavailable"])
def test_business_jev_failures_record_raw_results_and_fall_back(database, failure):
    from decagent.business.calibration import fit_profile
    from decagent.business.questions import build_business_request
    from decagent.decisions.contracts import ChoiceAnswer, NoulAnswer, ScoreAnswer, evaluation

    instruction = "我想了解当前单子有哪些"
    service = services(database, instruction)
    service.settings = replace(service.settings, jev_api_key="offline-fixture")
    data = {"instruction": instruction, "strategy": "hybrid"}
    data = {**data, **service.run_phase("prepare", data)}
    request = build_business_request(
        service.spec,
        str(service.task["id"]),
        str(service.task["business_scope_id"]),
        instruction,
        data["evidence"],
        dataset_version="offline-reference-1",
    )
    operation = next(q for q in request.questions if q.id == "operation")
    probabilities = {key: float(key == "list") for key in operation.criteria}
    service.profile = fit_profile(
        request,
        "jev-1.13.0",
        [
            {
                "question_id": "operation",
                "answer": ChoiceAnswer("list", probabilities, 1.0),
                "expected": "list",
            }
        ],
        dataset_version="offline-reference-1",
    )
    calls = []

    class FakeJev:
        def evaluate(self, current):
            calls.append(current)
            if failure in {"invalid", "unavailable"}:
                return evaluation(
                    current,
                    "hybrid",
                    service.settings.jev_model,
                    error="invalid_response" if failure == "invalid" else "provider_unavailable",
                )
            answers = {}
            for question in current.questions:
                if question.type == "choice":
                    selected = (
                        "list" if question.id == "operation" else next(iter(question.criteria))
                    )
                    values = {key: float(key == selected) for key in question.criteria}
                    confidence = 1.0
                    if failure == "low_certainty" and question.id == "operation":
                        values = dict.fromkeys(question.criteria, 1 / len(question.criteria))
                        confidence = 0.0
                    answers[question.id] = ChoiceAnswer(selected, values, confidence)
                elif question.type == "noul":
                    answers[question.id] = NoulAnswer(0.0)
                else:
                    answers[question.id] = ScoreAnswer(
                        0.0,
                        {str(i): float(i == 0) for i in range(len(question.criteria))},
                        1.0,
                        {str(i): text for i, text in enumerate(question.criteria)},
                    )
            return evaluation(
                current,
                "hybrid",
                service.settings.jev_model,
                actual_model="jev-1.14.0" if failure == "model_drift" else "jev-1.13.0",
                answers=answers,
            )

    service._jev = FakeJev()
    result = service.run_phase("decide", data)
    assert result["route"] == "plan" and len(calls) == 1 and service._primary is None
    with database.connect() as conn:
        record = conn.execute(
            "SELECT result,final_route FROM app.decision_records WHERE task_id=%s",
            (service.task["id"],),
        ).fetchone()
    assert record["final_route"] == "plan" and record["result"]["accepted_questions"] == []
    assert (
        record["result"]["evaluation"]["error"] is not None
        if failure in {"invalid", "unavailable"}
        else record["result"]["evaluation"]["answers"]
    )

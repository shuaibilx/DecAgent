"""Fast routes preserve every supported user constraint or defer to planning."""

import pytest

from decagent.business.calibration import BusinessCalibration
from decagent.business.contracts import PluginSpec
from decagent.business.questions import build_business_request
from decagent.business.rules import proposal
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest
from decagent.decisions.contracts import ChoiceAnswer, NoulAnswer, ScoreAnswer


@pytest.fixture
def spec():
    return PluginSpec.from_mapping(builtin_manifest())


def test_entity_name_prefix_is_not_a_requested_person(spec):
    evidence = {
        "directory": {"people": [{"id": "person-1", "name": "张伟", "team": "IT Support"}]},
        "ticket": {"id": "ticket-1", "revision": 1, "status": "open"},
    }
    assert proposal(spec, "请分配给张伟杰", evidence) is None
    assert proposal(spec, "请分配给张伟", evidence)["params"]["person_id"] == "person-1"


@pytest.mark.parametrize(
    "instruction",
    [
        "请分配给其他人，不是张伟。",
        "请分配给李明，之前的处理人是张伟。",
    ],
)
def test_excluded_or_previous_person_is_not_the_assignment_target(spec, instruction):
    evidence = {
        "directory": {"people": [{"id": "person-1", "name": "张伟", "team": "IT Support"}]},
        "ticket": {"id": "ticket-1", "revision": 1, "status": "open"},
    }
    assert proposal(spec, instruction, evidence) is None


@pytest.mark.parametrize("role", ["请求人", "请求者", "提单人", "requester"])
def test_rule_create_retains_requests_for_an_explicit_requester(spec, role):
    evidence = {"directory": {"categories": [{"id": "category-1", "name": "Network"}]}}
    assert proposal(spec, "请创建 Network 工单，" + role + "是 IT Support 的张伟", evidence) is None


def test_historical_category_is_not_a_requested_category(spec):
    evidence = {"directory": {"categories": [{"id": "category-1", "name": "Network"}]}}
    assert proposal(spec, "请创建 Payroll 工单，旧分类是 Network。", evidence) is None


def test_explicit_priority_is_not_replaced_by_the_work_obstruction_score(spec):
    evidence = {"directory": {"categories": [{"id": "category-1", "name": "Network"}]}}
    assert proposal(spec, "请创建 Network 工单，优先级 high，工作未受影响", evidence) is None
    assert proposal(spec, "请创建 Network 工单，工作没有被阻断", evidence) is None


@pytest.mark.parametrize(
    "instruction",
    [
        "查看工单列表，只要已关闭的工单",
        "查看工单列表，只要分配给张伟的",
        "查看工单列表，最多十条",
    ],
)
def test_query_modifiers_cannot_disappear_in_rule_or_typed_routes(spec, instruction):
    assert proposal(spec, instruction, {}) is None
    service = BusinessServices.__new__(BusinessServices)
    service.spec = spec
    routed = service._fast_proposal(
        {"instruction": instruction, "evidence": {}},
        {"operation": ChoiceAnswer("list", {"list": 1.0, "no_match": 0.0}, 1.0)},
    )
    assert routed["route"] == "plan"


@pytest.mark.parametrize(
    "constraint",
    [
        "请求人为 IT Support 的张伟",
        "优先级 high，工作未受影响",
        "标题为 VPN 无法连接",
    ],
)
def test_explicit_create_parameters_cannot_disappear_from_typed_create(spec, constraint):
    service = BusinessServices.__new__(BusinessServices)
    service.spec = spec
    answers = {
        "operation": ChoiceAnswer("create", {"create": 1.0, "no_match": 0.0}, 1.0),
        "category": ChoiceAnswer("category-1", {"category-1": 1.0, "no_match": 0.0}, 1.0),
        "explicit_assignee": NoulAnswer(0.0),
        "work_blockage": ScoreAnswer(
            0.0,
            {"0": 1.0, "1": 0.0, "2": 0.0},
            1.0,
            {"0": "unaffected", "1": "workaround", "2": "blocked"},
        ),
    }
    result = service._fast_proposal(
        {"instruction": "请创建 Network 工单，" + constraint, "evidence": {}},
        answers,
    )
    assert result["route"] == "plan"


def test_known_calibration_bindings_are_checked_before_a_provider_call(spec):
    request = build_business_request(
        spec, "task", "scope", "请列出工单", {}, dataset_version="fixture-1"
    )
    profile = BusinessCalibration(
        "jev-1.13.0",
        spec.digest,
        request.context.workflow_version,
        "business-1",
        "business-1",
        "fixture-1",
        {"operation": 0.9},
    )
    assert profile.compatible(request)
    assert not profile.compatible(request, "jev-1.14.0")
    assert profile.compatible(request, "~typesafe/jev-latest")
    stale = BusinessCalibration.from_dict({**profile.to_dict(), "plugin_digest": "stale"})
    assert not stale.compatible(request)


@pytest.mark.parametrize("content", [None, "{not-json", '{"model": "incomplete"}'])
def test_unreadable_or_invalid_profile_preserves_the_main_planner_fallback(
    spec, monkeypatch, tmp_path, content
):
    from types import SimpleNamespace

    path = tmp_path / "calibration.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("DECAGENT_BUSINESS_CALIBRATION_FILE", str(path))
    monkeypatch.setattr(
        "decagent.business.services.PluginRegistry",
        lambda database: SimpleNamespace(get=lambda *args: spec),
    )
    monkeypatch.setattr("decagent.business.services.BusinessExecutor", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "decagent.business.services.make_task_usage_ledger",
        lambda *_args: object(),
    )
    service = BusinessServices(
        object(),
        object(),
        object(),
        {
            "id": "task",
            "business_scope_id": "scope",
            "authorization_snapshot": {"plugin_digest": spec.digest},
        },
        "owner",
        1,
        mcp_client=object(),
    )
    assert service.profile is None and service.profile_error == "invalid_calibration"


@pytest.mark.parametrize("shape", ["five_levels", "operation_noul"])
def test_valid_enhanced_question_shapes_outside_builtin_adapter_defer_to_planning(shape):
    manifest = builtin_manifest()
    if shape == "five_levels":
        manifest["workflow"]["questions"][-1]["scale"] = [
            {"score": i, "context": "level " + str(i)} for i in range(5)
        ]
        answer = ChoiceAnswer("create", {"create": 1.0, "no_match": 0.0}, 1.0)
    else:
        manifest["workflow"]["questions"][0] = {
            "id": "operation",
            "type": "noul",
            "proposition": "User wants an operation",
        }
        answer = NoulAnswer(1.0)
    service = BusinessServices.__new__(BusinessServices)
    service.spec = PluginSpec.from_mapping(manifest)
    result = service._fast_proposal(
        {"instruction": "请创建 Network 工单", "evidence": {}},
        {
            "operation": answer,
            "explicit_assignee": NoulAnswer(0.0),
            "category": ChoiceAnswer("category-1", {"category-1": 1.0, "no_match": 0.0}, 1.0),
            "work_blockage": ScoreAnswer(
                4.0,
                {str(i): float(i == 4) for i in range(5)},
                1.0,
                {str(i): "level " + str(i) for i in range(5)},
            ),
        },
    )
    assert result["route"] == "plan"


@pytest.mark.parametrize("name,rows_key", [("person", "people"), ("category", "categories")])
def test_declared_question_ids_cannot_collide_with_directory_choices(name, rows_key):
    manifest = builtin_manifest()
    for question_id in (name, "directory_" + name):
        manifest["workflow"]["questions"].append(
            {"id": question_id, "type": "noul", "proposition": "User identified this entity"}
        )
    request = build_business_request(
        PluginSpec.from_mapping(manifest),
        "task",
        "scope",
        "请查询目录",
        {"directory": {rows_key: [{"id": "entity-1", "name": "Queried entity"}]}},
    )
    questions = {question.id: question for question in request.questions}
    assert len(questions) == len(request.questions)
    assert questions[name].type == "noul"
    assert questions["directory_" + name + "_2"].type == "choice"
    assert set(questions["directory_" + name + "_2"].criteria) == {"entity-1", "no_match"}


def test_empty_legal_question_set_falls_back_with_a_durable_reason():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from decagent.policy import Redactor

    manifest = builtin_manifest()
    manifest["workflow"]["questions"] = [
        {
            "id": "operation",
            "type": "choice",
            "candidates": ["start", "close"],
            "no_match": True,
        }
    ]
    service = BusinessServices.__new__(BusinessServices)
    service.spec = PluginSpec.from_mapping(manifest)
    service.task = {"id": "task", "business_scope_id": "scope"}
    service.settings = SimpleNamespace(jev_model="~typesafe/jev-latest")
    service.profile = None
    records = []

    @contextmanager
    def connect():
        def execute(sql, args):
            if sql.startswith("INSERT"):
                records.append(args)
            return SimpleNamespace(fetchone=lambda: None)

        yield SimpleNamespace(execute=execute)

    service.database = SimpleNamespace(connect=connect, redactor=Redactor())
    service.executor = SimpleNamespace(guard=lambda conn: None)
    result = service._typed_route(
        {
            "instruction": "开始处理工单",
            "evidence": {"ticket": {"status": "open"}},
        }
    )
    assert result["route"] == "plan"
    assert result["reason"] == "no_admissible_business_questions"
    assert len(records) == 1
    assert records[0][-1].obj["route_reason"] == result["reason"]


def typed_service_with_profile(spec, thresholds, *, prior_model=None, requested_model=None):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from decagent.decisions.contracts import evaluation, validate_answers
    from decagent.policy import Redactor

    data = {
        "instruction": "我想了解当前单子有哪些",
        "evidence": {
            "directory": {
                "people": [{"id": "person-1", "name": "张伟"}],
                "categories": [{"id": "category-1", "name": "Network"}],
            }
        },
    }
    request = build_business_request(
        spec, "task", "scope", data["instruction"], data["evidence"], dataset_version="fixture-1"
    )
    profile = BusinessCalibration(
        "jev-1.13.0",
        spec.digest,
        request.context.workflow_version,
        request.context.question_version,
        request.context.policy_version,
        request.context.dataset_version,
        thresholds,
    )
    calls, inserted = [], []
    prior = None
    if prior_model:
        payload = {}
        for question in request.questions:
            if question.type == "choice":
                selected = "list" if question.id == "operation" else "no_match"
                payload[question.id] = {
                    "type": "choice",
                    "choice": selected,
                    "confidence": 1.0,
                    "probabilities": {key: float(key == selected) for key in question.criteria},
                }
            elif question.type == "noul":
                payload[question.id] = {"type": "noul", "noul": 0.0}
            else:
                payload[question.id] = {
                    "type": "score",
                    "score": 0.0,
                    "confidence": 1.0,
                    "probabilities": {str(i): float(i == 0) for i in range(len(question.criteria))},
                    "legend": {str(i): level for i, level in enumerate(question.criteria)},
                }
        prior = {
            "result": {
                "evaluation": evaluation(
                    request,
                    "jev",
                    "~typesafe/jev-latest",
                    actual_model=prior_model,
                    answers=validate_answers(payload, request),
                ).to_dict()
            }
        }

    @contextmanager
    def connect():
        def execute(statement, args):
            if statement.startswith("INSERT"):
                inserted.append(args)
            return SimpleNamespace(fetchone=lambda: prior)

        yield SimpleNamespace(execute=execute)

    service = BusinessServices.__new__(BusinessServices)
    service.spec, service.profile, service.profile_error = spec, profile, None
    service.task = {"id": "task", "business_scope_id": "scope"}
    service.settings = SimpleNamespace(
        jev_model=requested_model or "~typesafe/jev-latest", jev_api_key="offline-fixture"
    )
    service.database = SimpleNamespace(connect=connect, redactor=Redactor())
    service.executor = SimpleNamespace(guard=lambda conn: None)
    service._primary = None
    service._jev = SimpleNamespace(
        evaluate=lambda current: (
            calls.append(current)
            or evaluation(current, "jev", service.settings.jev_model, error="provider_unavailable")
        )
    )
    return service, data, request, calls, inserted


@pytest.mark.parametrize("thresholds", [{}, {"operation": None}, {"unrequested_family": 0.9}])
def test_no_enabled_current_family_skips_jev_and_records_main_fallback(spec, thresholds):
    service, data, _, calls, inserted = typed_service_with_profile(spec, thresholds)
    result = service._typed_route(data)
    assert calls == [] and service._primary is None
    assert result == {"route": "plan", "reason": "no_enabled_calibrated_questions"}
    assert len(inserted) == 1
    snapshot = inserted[0][-2].obj
    assert snapshot["evaluation"]["error"] == snapshot["route_reason"] == result["reason"]
    assert snapshot["evaluation"]["actual_model"] is None
    assert snapshot["evaluation"]["attempts"] == []
    assert snapshot["accepted_questions"] == []


@pytest.mark.parametrize("threshold", [0.0, 0.9])
def test_any_enabled_current_family_keeps_original_jev_call(spec, threshold):
    service, data, _, calls, inserted = typed_service_with_profile(spec, {"operation": threshold})
    assert service._typed_route(data)["route"] == "plan"
    assert len(calls) == len(inserted) == 1
    assert inserted[0][-2].obj["evaluation"]["error"] == "provider_unavailable"


def test_disabled_profile_does_not_hide_requested_actual_model_incompatibility(spec):
    service, data, _, calls, inserted = typed_service_with_profile(
        spec, {}, requested_model="jev-1.14.0"
    )
    assert service._typed_route(data)["route"] == "plan"
    assert calls == []
    assert inserted[0][-2].obj["evaluation"]["error"] == (
        "missing_compatible_calibration_or_credentials"
    )


@pytest.mark.parametrize("prior_model", ["jev-1.13.0", "jev-1.14.0"])
def test_disabled_profile_preserves_prior_replay_without_reissuing_or_rewriting(spec, prior_model):
    service, data, _, calls, inserted = typed_service_with_profile(
        spec, {}, prior_model=prior_model
    )
    assert service._typed_route(data)["route"] == "plan"
    assert calls == inserted == []


def test_prior_actual_model_drift_still_cannot_enable_a_fast_route(spec):
    service, data, _, calls, inserted = typed_service_with_profile(
        spec, {"operation": 0.0}, prior_model="jev-1.14.0"
    )
    assert service._typed_route(data)["route"] == "plan"
    assert calls == inserted == []


@pytest.mark.parametrize(
    "requested,reason",
    [
        ("typesafe/jev-1.13", "no_enabled_calibrated_questions"),
        ("typesafe/jev-1.14", "missing_compatible_calibration_or_credentials"),
        ("~typesafe/jev-latest", "no_enabled_calibrated_questions"),
    ],
)
def test_disabled_profile_preserves_requested_version_prefix_binding(spec, requested, reason):
    from dataclasses import replace

    service, data, _, calls, inserted = typed_service_with_profile(
        spec, {}, requested_model=requested
    )
    service.profile = replace(
        service.profile,
        model="typesafe/jev-1.13-20260917",
        thresholds=dict(service.profile.thresholds),
    )
    expected = (
        "No compatible certain typed fast path"
        if reason == "missing_compatible_calibration_or_credentials"
        else reason
    )
    assert service._typed_route(data)["reason"] == expected
    assert calls == []
    assert inserted[0][-2].obj["evaluation"]["error"] == reason

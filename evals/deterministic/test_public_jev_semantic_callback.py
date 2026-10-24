import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from decagent.business.plans.templates import QuestionRegistry
from decagent.decisions.contracts import QuestionSpec
from decagent.evaluation.business.comparison import Route
from decagent.evaluation.business.public_models import (
    PublicJevSemanticCallback,
    PublicPrimarySemanticCallback,
    _jev_provider_dispatch,
    public_first_wave_model_adapter_factory,
    public_primary_semantic_callback_factory,
)
from decagent.evaluation.business.public_resources import (
    LedgeredModelCallback,
    PublicModelProfileDrift,
)
from decagent.evaluation.business.public_trials import PublicTrialLedger, load_public_run_plan
from decagent.evaluation.business.routes import ModelCallError, ModelReply
from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal

ROOT = Path(__file__).resolve().parents[2]


class ReviewedRegistry:
    def __init__(self, *, instructions="Select the applicable returned record."):
        self.instructions = instructions

    def get(self, question_id, version):
        assert (question_id, version) == ("csm_knowledge_article", "1.0.0-csm-knowledge-1")
        return {"question_id": question_id, "version": version, "type": "choice"}

    def render(self, question_id, state, *, node_id, version):
        assert question_id == "csm_knowledge_article"
        assert version == "1.0.0-csm-knowledge-1"
        assert state["articles"][0]["knowledge_id"] == 7
        return QuestionSpec(
            node_id,
            version,
            "choice",
            self.instructions,
            {"article:7": "Select record 7", "no_match": "No candidate applies"},
            tuple(state),
        )


def _profile_and_trial():
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    profile = plan.profiles.jev
    trial = SimpleNamespace(
        case_id="csm-case-1",
        dataset="enterpriseops-gym",
        dataset_version="pinned-v1",
        actor_profile_digest="a" * 64,
        permission_digest="b" * 64,
        tool_adapter_digest="c" * 64,
    )
    return plan, profile, trial


def _request():
    return {
        "node_id": "node-1",
        "question_id": "csm_knowledge_article",
        "question_version": "1.0.0-csm-knowledge-1",
        "question": {
            "type": "choice",
            "instructions": "Select the applicable returned record.",
            "criteria": {"article:7": "Select record 7", "no_match": "No candidate applies"},
        },
        "state": {
            "instruction": "How do I restore access?",
            "articles": [{"knowledge_id": 7, "title": "Restore access"}],
            "retrieval_arguments": {"search_text": "restore access", "state": "published"},
        },
        "actor_profile_digest": "a" * 64,
        "permission_digest": "b" * 64,
    }


def _response_frame(*, answer=None, usage=None, status=200):
    return {
        "status": status,
        "model": "typesafe/jev-1.13-20260917",
        "answer": answer
        or {
            "type": "choice",
            "choice": "article:7",
            "confidence": 0.99,
            "probabilities": {"article:7": 0.99, "no_match": 0.01},
        },
        "usage": usage or {"input_tokens": 41, "output_tokens": 7},
        "reported_cost_usd": "0.000001722",
        "provider_request_id": "typesafe-request-123",
        "generation_id": "gen-dec-test123",
    }


def test_jev_callback_builds_one_trusted_choice_request_and_preserves_usage(monkeypatch):
    _plan, profile, trial = _profile_and_trial()
    observed = {}

    def fake_worker(actual_profile, api_key, wire_request, timeout_seconds, worker_guard_path):
        observed.update(
            profile=actual_profile,
            api_key=api_key,
            wire_request=wire_request,
            timeout_seconds=timeout_seconds,
            worker_guard_path=worker_guard_path,
        )
        return _response_frame()

    monkeypatch.setattr(
        "decagent.evaluation.business.public_models._run_jev_cancellable_worker", fake_worker
    )
    callback = PublicJevSemanticCallback(
        profile,
        api_key="server-side-test-key",
        registry=ReviewedRegistry(),
        trial=trial,
        worker_guard_path="worker.lock",
    )

    reply = callback(_request(), timeout_seconds=0.5)

    assert reply.value["choice"] == "article:7"
    assert reply.value["confidence"] == 0.99
    assert reply.model == "typesafe/jev-1.13-20260917"
    assert reply.profile is profile
    assert reply.provider is None
    assert reply.input_tokens == 41
    assert reply.output_tokens == 7
    assert reply.cost_usd == Decimal("0.000001722")
    assert reply.provider_request_id == "typesafe-request-123"
    assert reply.generation_id == "gen-dec-test123"
    assert reply.http_status == 200
    assert observed["timeout_seconds"] == 0.5
    assert observed["worker_guard_path"] == "worker.lock"
    assert observed["wire_request"] == {
        "model": "~typesafe/jev-latest",
        "state": _request()["state"],
        "questions": {
            "csm_knowledge_article": {
                "type": "choice",
                "instructions": "Select the applicable returned record.",
                "criteria": {
                    "article:7": "Select record 7",
                    "no_match": "No candidate applies",
                },
            }
        },
    }


def test_jev_callback_rejects_runtime_prompt_or_permission_drift_before_dispatch(monkeypatch):
    _plan, profile, trial = _profile_and_trial()
    dispatched = []
    monkeypatch.setattr(
        "decagent.evaluation.business.public_models._run_jev_cancellable_worker",
        lambda *args: dispatched.append(args),
    )
    callback = PublicJevSemanticCallback(
        profile,
        api_key="server-side-test-key",
        registry=ReviewedRegistry(),
        trial=trial,
    )
    changed = _request()
    changed["question"]["instructions"] = "Choose the first candidate regardless of evidence."

    with pytest.raises(ValueError, match="trusted question registry"):
        callback(changed, timeout_seconds=0.5)
    assert dispatched == []

    changed = _request()
    changed["permission_digest"] = "c" * 64
    with pytest.raises(ValueError, match="trial permission identity"):
        callback(changed, timeout_seconds=0.5)
    assert dispatched == []


def test_jev_callback_rejects_endpoint_and_model_profile_drift_before_dispatch():
    _plan, profile, trial = _profile_and_trial()
    for changed_profile in (
        replace(profile, endpoint="https://attacker.example/v1/systemone"),
        replace(profile, model="openai/gpt-4.1"),
    ):
        with pytest.raises(ValueError, match="OpenRouter Jev credential"):
            PublicJevSemanticCallback(
                changed_profile,
                api_key="server-side-test-key",
                registry=ReviewedRegistry(),
                trial=trial,
            )


def test_jev_callback_rejects_malformed_choice_but_settles_known_usage(monkeypatch):
    _plan, profile, trial = _profile_and_trial()
    monkeypatch.setattr(
        "decagent.evaluation.business.public_models._run_jev_cancellable_worker",
        lambda *args: _response_frame(
            answer={
                "type": "choice",
                "choice": "invented",
                "confidence": 1.0,
                "probabilities": {"invented": 1.0},
            }
        ),
    )
    callback = PublicJevSemanticCallback(
        profile,
        api_key="server-side-test-key",
        registry=ReviewedRegistry(),
        trial=trial,
    )

    with pytest.raises(ModelCallError) as raised:
        callback(_request(), timeout_seconds=0.5)

    assert raised.value.reply.input_tokens == 41
    assert raised.value.reply.output_tokens == 7
    assert raised.value.reply.cost_source == "provider_billed"


def test_jev_provider_worker_uses_one_openrouter_decisions_request(monkeypatch):
    _plan, profile, _trial = _profile_and_trial()
    captured = []
    payload = {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "csm_knowledge_article": {
                "type": "choice",
                "choice": "article:7",
                "confidence": 0.99,
                "probabilities": {"article:7": 0.99, "no_match": 0.01},
            }
        },
        "usage": {"input_tokens": 41, "output_tokens": 7, "cost": 0.000001722},
    }

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200,
            json=payload,
            headers={"x-typesafe-request-id": "typesafe-request-123"},
        )

    monkeypatch.setattr(
        "decagent.evaluation.business.public_models._new_jev_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False),
    )
    wire_request = {
        "model": profile.model,
        "state": _request()["state"],
        "questions": {"csm_knowledge_article": _request()["question"]},
    }

    result = _jev_provider_dispatch(profile, "server-side-test-key", wire_request, 0.4)

    assert result["status"] == 200
    assert result["usage"] == {"input_tokens": 41, "output_tokens": 7}
    assert result["reported_cost_usd"] == "0.000001722"
    assert result["generation_id"] is None
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == profile.endpoint
    assert request.headers["authorization"] == "Bearer server-side-test-key"
    body = json.loads(request.content)
    assert body == wire_request
    assert "messages" not in body
    assert request.extensions["timeout"]["read"] == 0.4


def test_first_wave_factory_keeps_semantic_routes_opt_in_and_separate(tmp_path, monkeypatch):
    plan, _profile, trial = _profile_and_trial()
    ledger = SimpleNamespace(
        plan=SimpleNamespace(root=tmp_path),
        reserve_model_attempt=lambda **_kwargs: "unused",
        route_remaining_seconds=lambda *_args: 1,
    )
    settings = SimpleNamespace(
        main_api_key="primary-test-key",
        jev_api_key="jev-test-key",
        resolved_fallback_api_key=None,
    )

    baseline = public_first_wave_model_adapter_factory(
        settings,
        ledger,
        plan.profiles,
        trial_id_for=lambda trial: trial.trial_id,
    )
    jev_enabled = public_first_wave_model_adapter_factory(
        settings,
        ledger,
        plan.profiles,
        trial_id_for=lambda trial: trial.trial_id,
        include_jev_semantic_callback=True,
    )
    primary_semantic_enabled = public_first_wave_model_adapter_factory(
        settings,
        ledger,
        plan.profiles,
        trial_id_for=lambda trial: trial.trial_id,
        include_primary_semantic_callback=True,
    )
    both_semantic_models_enabled = public_first_wave_model_adapter_factory(
        settings,
        ledger,
        plan.profiles,
        trial_id_for=lambda trial: trial.trial_id,
        include_jev_semantic_callback=True,
        include_primary_semantic_callback=True,
    )

    assert "semantic" not in baseline.callback_factories
    assert "semantic" in jev_enabled.callback_factories
    assert "semantic" in primary_semantic_enabled.callback_factories
    assert "semantic" in both_semantic_models_enabled.callback_factories
    monkeypatch.setattr(
        QuestionRegistry,
        "from_spec",
        classmethod(lambda _cls, _spec: ReviewedRegistry()),
    )
    session = SimpleNamespace(spec=object())
    primary_callback = both_semantic_models_enabled.callback_factories["semantic"](
        session, trial, Route.PLAN_PRIMARY_SEMANTIC
    )
    jev_callback = both_semantic_models_enabled.callback_factories["semantic"](
        session, trial, Route.PLAN_JEV_SEMANTIC
    )
    assert isinstance(primary_callback, PublicPrimarySemanticCallback)
    assert isinstance(jev_callback, PublicJevSemanticCallback)


def test_first_wave_factory_selects_openrouter_credentials_from_frozen_roles(
    tmp_path, monkeypatch
):
    import decagent.evaluation.business.public_models as public_models

    plan, _profile, _trial = _profile_and_trial()

    def openrouter_role(role_profile):
        return replace(
            role_profile,
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            endpoint="https://openrouter.ai/api/v1",
            price_version="openrouter-deepseek-v4.1-flash-fireworks-usd-20261024-v1",
            price_source=(
                "https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"
            ),
            input_price_per_million=Decimal("0.45"),
            output_price_per_million=Decimal("1.8"),
            price_currency="USD",
            cached_input_price_per_million=Decimal("0.009"),
            provider_routing="openrouter_fireworks",
        )

    profiles = replace(
        plan.profiles,
        primary=openrouter_role(plan.profiles.primary),
        user_simulator=openrouter_role(plan.profiles.user_simulator),
        judge=openrouter_role(plan.profiles.judge),
    )
    captured = {}

    def callback_factory(name):
        def build(profile, **kwargs):
            captured[name] = (profile.provider, kwargs["api_key"])
            return lambda *_args: None

        return build

    monkeypatch.setattr(
        public_models, "public_planner_callback_factory", callback_factory("planner")
    )
    monkeypatch.setattr(
        public_models, "public_react_callback_factory", callback_factory("react")
    )
    monkeypatch.setattr(
        public_models,
        "public_tau_model_callback_factory",
        lambda profile, *, role, **kwargs: callback_factory(role)(profile, **kwargs),
    )
    monkeypatch.setattr(
        public_models,
        "LedgeredModelAdapterFactory",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    ledger = SimpleNamespace(plan=SimpleNamespace(root=tmp_path))
    settings = SimpleNamespace(
        main_api_key="qwen-test-key",
        resolved_fallback_api_key="openrouter-test-key",
        jev_api_key="jev-test-key",
    )

    public_first_wave_model_adapter_factory(
        settings,
        ledger,
        profiles,
        trial_id_for=lambda _trial: "a" * 64,
    )

    assert captured == {
        "planner": ("openrouter", "openrouter-test-key"),
        "react": ("openrouter", "openrouter-test-key"),
        "user_simulator": ("openrouter", "openrouter-test-key"),
        "judge": ("openrouter", "openrouter-test-key"),
    }


def test_primary_semantic_callback_uses_the_same_trusted_state_and_question():
    plan, _jev_profile, trial = _profile_and_trial()
    profile = plan.profiles.primary
    request = _request()
    captured = {}

    class Primary:
        schema_bytes = 256

        def respond(self, messages, *, timeout_seconds):
            captured["messages"] = messages
            captured["timeout_seconds"] = timeout_seconds
            return ModelTurn(
                "",
                (
                    ToolProposal(
                        "call-1",
                        "decagent_semantic_choice",
                        {
                            "choice": "article:7",
                            "probabilities": {"article:7": 0.99, "no_match": 0.01},
                        },
                    ),
                ),
                ModelUsage(83, 12, 95),
                profile.model,
                "primary-request-123",
            )

        def close(self):
            captured["closed"] = True

    callback = PublicPrimarySemanticCallback(
        Primary(), profile, registry=ReviewedRegistry(), trial=trial
    )
    reply = callback(request, timeout_seconds=0.75)

    assert reply.value == {
        "type": "choice",
        "choice": "article:7",
        "confidence": 0.98,
        "probabilities": {"article:7": 0.99, "no_match": 0.01},
    }
    assert reply.profile is profile
    assert reply.model == profile.model
    assert reply.input_tokens == 83
    assert reply.output_tokens == 12
    assert reply.provider_request_id == "primary-request-123"
    assert captured["timeout_seconds"] == 0.75
    body = json.loads(captured["messages"][1]["content"])
    assert body == {
        "questions": {"csm_knowledge_article": request["question"]},
        "state": request["state"],
    }
    callback.close()
    assert captured["closed"] is True


def test_jev_and_primary_callbacks_receive_identical_semantic_state_and_options(monkeypatch):
    plan, jev_profile, trial = _profile_and_trial()
    request = _request()
    captured = {}

    def jev_worker(_profile, _api_key, wire_request, _timeout, _guard):
        captured["jev"] = wire_request
        return _response_frame()

    class Primary:
        def respond(self, messages, *, timeout_seconds):
            captured["primary"] = json.loads(messages[1]["content"])
            captured["timeout_seconds"] = timeout_seconds
            return ModelTurn(
                "",
                (
                    ToolProposal(
                        "call-1",
                        "decagent_semantic_choice",
                        {
                            "choice": "article:7",
                            "probabilities": {"article:7": 0.99, "no_match": 0.01},
                        },
                    ),
                ),
                ModelUsage(83, 12, 95),
                plan.profiles.primary.model,
                "primary-request-123",
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "decagent.evaluation.business.public_models._run_jev_cancellable_worker",
        jev_worker,
    )
    jev = PublicJevSemanticCallback(
        jev_profile,
        api_key="server-side-test-key",
        registry=ReviewedRegistry(),
        trial=trial,
    )
    primary = PublicPrimarySemanticCallback(
        Primary(), plan.profiles.primary, registry=ReviewedRegistry(), trial=trial
    )

    jev(request, timeout_seconds=0.5)
    primary(request, timeout_seconds=0.5)

    jev_wire = captured["jev"]
    assert captured["primary"] == {
        "state": jev_wire["state"],
        "questions": jev_wire["questions"],
    }
    assert jev_wire["state"] == request["state"]
    assert captured["primary"]["questions"] == {
        request["question_id"]: request["question"]
    }
    assert captured["timeout_seconds"] == 0.5


def test_primary_semantic_callback_rejects_invalid_candidate_and_unmeasured_usage():
    plan, _jev_profile, trial = _profile_and_trial()

    class Primary:
        def __init__(self, proposal, usage):
            self.proposal = proposal
            self.usage = usage

        def respond(self, _messages, *, timeout_seconds):
            tools = ()
            if self.proposal is not None:
                tools = (ToolProposal("call-1", "decagent_semantic_choice", self.proposal),)
            return ModelTurn(
                "",
                tools,
                self.usage,
                plan.profiles.primary.model,
            )

        def close(self):
            pass

    callback = PublicPrimarySemanticCallback(
        Primary(
            {
                "choice": "invented",
                "probabilities": {"article:7": 0.99, "no_match": 0.01},
            },
            ModelUsage(10, 2, 12),
        ),
        plan.profiles.primary,
        registry=ReviewedRegistry(),
        trial=trial,
    )
    with pytest.raises(ModelCallError, match="valid trusted Choice"):
        callback(_request(), timeout_seconds=0.75)

    callback = PublicPrimarySemanticCallback(
        Primary(
            {
                "choice": "article:7",
                "probabilities": {"article:7": 0.99, "no_match": 0.01},
            },
            None,
        ),
        plan.profiles.primary,
        registry=ReviewedRegistry(),
        trial=trial,
    )
    with pytest.raises(ModelCallError, match="usage is unknown"):
        callback(_request(), timeout_seconds=0.75)

    callback = PublicPrimarySemanticCallback(
        Primary(None, ModelUsage(10, 2, 12)),
        plan.profiles.primary,
        registry=ReviewedRegistry(),
        trial=trial,
    )
    with pytest.raises(ModelCallError, match="valid trusted Choice") as raised:
        callback(_request(), timeout_seconds=0.75)
    assert raised.value.reply.input_tokens == 10


def test_primary_semantic_factory_is_opt_in_and_route_restricted(monkeypatch):
    plan, _jev_profile, trial = _profile_and_trial()
    profile = plan.profiles.primary
    created = []

    class Primary:
        def respond(self, _messages, *, timeout_seconds):
            raise AssertionError("Factory validation must not dispatch a model")

        def close(self):
            pass

    def primary_factory(actual_profile, **kwargs):
        created.append((actual_profile, kwargs))
        return Primary()

    factory = public_primary_semantic_callback_factory(
        profile,
        api_key="primary-test-key",
        primary_factory=primary_factory,
    )
    with pytest.raises(ValueError, match="function-call profile"):
        public_primary_semantic_callback_factory(
            replace(profile, response_mode="chat_text", tool_choice="none"),
            api_key="primary-test-key",
            primary_factory=primary_factory,
        )
    with pytest.raises(ValueError, match="primary semantic comparison route"):
        factory(SimpleNamespace(spec=object()), trial, Route.PLAN_MCP)
    assert created == []

    # The registered route constructs a one-shot client with only the Choice tool.
    from decagent.evaluation.business.public_models import _validate_primary_semantic_tool

    monkeypatch.setattr(
        QuestionRegistry,
        "from_spec",
        classmethod(lambda _cls, _spec: ReviewedRegistry()),
    )
    callback = factory(SimpleNamespace(spec=object()), trial, Route.PLAN_PRIMARY_SEMANTIC)
    assert isinstance(callback, PublicPrimarySemanticCallback)
    assert created[0][0] is profile
    assert len(created[0][1]["tool_definitions"]) == 1
    assert created[0][1]["tool_definitions"][0]["function"]["name"] == (
        "decagent_semantic_choice"
    )
    assert created[0][1]["tool_definitions"][0]["function"]["parameters"]["required"] == [
        "choice",
        "probabilities",
    ]
    assert _validate_primary_semantic_tool(
        "decagent_semantic_choice",
        {
            "choice": "article:7",
            "probabilities": {"article:7": 0.99, "no_match": 0.01},
        },
    )["choice"] == "article:7"



def test_jev_alias_response_is_accepted_by_ledger_and_actual_version_is_saved(tmp_path):
    plan, profile, _trial = _profile_and_trial()
    assert profile.model == "~typesafe/jev-latest"
    from decagent.evaluation.business.ledger import GlobalJevLedger

    global_ledger = GlobalJevLedger(tmp_path / "shared-jev.json")
    global_ledger.initialize()
    ledger = PublicTrialLedger.create(
        tmp_path / "public.sqlite3", plan, global_jev_ledger=global_ledger
    )
    trial_id = plan.trials[0]["trial_id"]
    # The first-wave manifest intentionally has only ReAct/Plan slots. This
    # ledger unit checks alias accounting without adding or replaying a route.
    route = Route.PLAN_MCP.value
    ledger.start_route(trial_id, route)
    machine_time = ledger.reserve_machine_time(
        trial_id=trial_id, route=route, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(machine_time)

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        return ModelReply(
            {"choice": "article:7"},
            tokens=48,
            cost_usd=Decimal("0.000001722"),
            model="typesafe/jev-1.13-20260917",
            input_tokens=41,
            output_tokens=7,
            profile=profile,
            cost_source="provider_billed",
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(
            1, len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        ),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route),
    )

    try:
        reply = measured(_request())
    except PublicModelProfileDrift:
        ledger.close()
        pytest.fail("A valid resolved Jev alias was incorrectly treated as profile drift")

    attempt = ledger._connection.execute(
        "SELECT attempt_id FROM model_attempts WHERE role='jev'"
    ).fetchone()
    assert attempt is not None
    saved = ledger.model_attempt_snapshot(attempt["attempt_id"])
    assert reply.model == "typesafe/jev-1.13-20260917"
    assert saved["actual_model"] == "typesafe/jev-1.13-20260917"
    assert saved["actual_provider"] is None
    assert saved["actual_cost_source"] == "provider_billed"
    ledger.close()


def test_jev_latest_alias_stops_cohort_when_resolved_version_changes(tmp_path):
    plan, profile, _trial = _profile_and_trial()
    from decagent.evaluation.business.ledger import GlobalJevLedger

    global_ledger = GlobalJevLedger(tmp_path / "shared-jev.json")
    global_ledger.initialize()
    ledger = PublicTrialLedger.create(
        tmp_path / "public.sqlite3", plan, global_jev_ledger=global_ledger
    )
    trial_id = plan.trials[0]["trial_id"]
    route = Route.PLAN_MCP.value
    ledger.start_route(trial_id, route)
    machine_time = ledger.reserve_machine_time(
        trial_id=trial_id, route=route, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(machine_time)
    resolved = iter(("typesafe/jev-1.13-20260917", "typesafe/jev-1.14-20261001"))

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        return ModelReply(
            {"choice": "article:7"},
            tokens=48,
            cost_usd=Decimal("0.000001722"),
            model=next(resolved),
            input_tokens=41,
            output_tokens=7,
            profile=profile,
            cost_source="provider_billed",
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(
            1, len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        ),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route),
    )
    try:
        measured(_request())
        with pytest.raises(PublicModelProfileDrift, match="resolved Jev model version"):
            measured(_request())
        assert ledger.batch_snapshot()["stopped_reason"] == "model_profile_drift"
        attempts = ledger.model_attempts_snapshot(trial_id, route)
        assert [attempt["actual_model"] for attempt in attempts] == [
            "typesafe/jev-1.13-20260917",
            "typesafe/jev-1.14-20261001",
        ]
    finally:
        ledger.close()

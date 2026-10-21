import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from decagent.decisions.contracts import QuestionSpec
from decagent.evaluation.business.comparison import Route
from decagent.evaluation.business.public_models import (
    PublicJevSemanticCallback,
    _jev_provider_dispatch,
    public_first_wave_model_adapter_factory,
)
from decagent.evaluation.business.public_resources import (
    LedgeredModelCallback,
    PublicModelProfileDrift,
)
from decagent.evaluation.business.public_trials import PublicTrialLedger, load_public_run_plan
from decagent.evaluation.business.routes import ModelCallError, ModelReply

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


def test_first_wave_factory_keeps_jev_route_opt_in(tmp_path):
    plan, _profile, _trial = _profile_and_trial()
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

    assert "semantic" not in baseline.callback_factories
    assert "semantic" in jev_enabled.callback_factories


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

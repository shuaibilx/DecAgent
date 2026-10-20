import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from decagent.evaluation.business.comparison import Route
from decagent.evaluation.business.public_resources import (
    LedgeredModelAdapterFactory,
    LedgeredModelCallback,
    PublicModelNotDispatched,
    PublicModelOutcomeUnknown,
    PublicModelProfileDrift,
)
from decagent.evaluation.business.public_trials import PublicTrialLedger, load_public_run_plan
from decagent.evaluation.business.routes import (
    ModelReply,
    ObservedUsage,
    ProviderBillingEvidence,
)

ROOT = Path(__file__).resolve().parents[2]


def _plan_with_known_primary_prices():
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    primary = replace(
        plan.profiles.primary,
        pricing_status="known",
        input_price_per_million=Decimal("0.8"),
        output_price_per_million=Decimal("2.7"),
        price_version="test-rates-v1",
        price_source="isolated deterministic fixture",
    )
    profiles = replace(
        plan.profiles,
        primary=primary,
        user_simulator=replace(
            plan.profiles.user_simulator,
            pricing_status="known",
            input_price_per_million=Decimal("0.8"),
            output_price_per_million=Decimal("2.7"),
            price_version="test-rates-v1",
            price_source="isolated deterministic fixture",
        ),
        judge=replace(
            plan.profiles.judge,
            pricing_status="known",
            input_price_per_million=Decimal("0.8"),
            output_price_per_million=Decimal("2.7"),
            price_version="test-rates-v1",
            price_source="isolated deterministic fixture",
        ),
    )
    return replace(plan, profiles=profiles)


def _started_ledger(tmp_path, plan):
    ledger = PublicTrialLedger.create(tmp_path / "public.sqlite3", plan)
    trial = plan.trials[0]
    route = Route.REACT_SKILL_MCP
    ledger.start_route(trial["trial_id"], route.value)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route.value, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    return ledger, trial["trial_id"], route


def test_model_callback_reserves_then_settles_with_frozen_rate_provenance(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    requests = []

    def callback(request, *, timeout_seconds):
        requests.append(request)
        assert timeout_seconds == 0.5
        return ModelReply(
            {"finish": True},
            tokens=13,
            cost_usd=None,
            model=profile.model,
            input_tokens=10,
            output_tokens=3,
            profile=profile,
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(1, len(json.dumps(value).encode("utf-8"))),
        route_remaining_seconds=lambda: min(
            0.5, ledger.route_remaining_seconds(trial_id, route.value)
        ),
    )
    reply = measured({"messages": [{"role": "user", "content": "hello"}]})

    assert reply.value == {"finish": True}
    assert requests == [{"messages": [{"role": "user", "content": "hello"}]}]
    attempts = ledger._connection.execute("SELECT attempt_id,status FROM model_attempts").fetchall()
    assert len(attempts) == 1
    assert attempts[0]["status"] == "settled"
    snapshot = ledger.model_attempt_snapshot(attempts[0]["attempt_id"])
    assert snapshot["actual_input_tokens"] == 10
    assert snapshot["actual_output_tokens"] == 3
    assert snapshot["actual_provider"] is None
    assert snapshot["actual_model"] == profile.model
    assert Decimal(snapshot["actual_cost_usd"]) == Decimal("0.0000161")
    assert snapshot["actual_cost_source"] == "frozen_profile"
    ledger.close()


def test_model_callback_respects_caller_deadline_ceiling(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    timeouts = []

    def callback(_request, *, timeout_seconds):
        timeouts.append(timeout_seconds)
        return ModelReply(
            {"finish": True},
            tokens=1,
            cost_usd=None,
            model=profile.model,
            input_tokens=1,
            output_tokens=0,
            profile=profile,
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _value: 1,
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(
            trial_id, route.value
        ),
    )

    measured({"messages": []}, timeout_seconds=0.25)

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 0.25
    ledger.close()


def test_missing_upstream_model_identity_is_recorded_as_null_not_profile_identity(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        return ModelReply(
            {"finish": True},
            tokens=4,
            cost_usd=None,
            model=None,
            input_tokens=3,
            output_tokens=1,
            profile=profile,
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _value: 10,
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route.value),
    )
    reply = measured({"messages": []})

    assert reply.model is None
    observed = ObservedUsage()
    observed.call_reply("primary", lambda _request: reply, {})
    assert observed.actual_models == []
    assert observed.actual_profiles == [("primary", profile.fingerprint)]
    attempt = ledger._connection.execute(
        "SELECT attempt_id,actual_model FROM model_attempts"
    ).fetchone()
    assert attempt["actual_model"] is None
    assert ledger.model_attempt_snapshot(attempt["attempt_id"])["actual_input_tokens"] == 3
    ledger.close()


def test_reported_upstream_model_drift_stops_the_frozen_batch(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger = PublicTrialLedger.create(tmp_path / "model-drift.sqlite3", plan)
    trial, later_trial = plan.trials[:2]
    route = Route.REACT_SKILL_MCP
    ledger.start_route(trial["trial_id"], route.value)
    reservation = ledger.reserve_machine_time(
        trial_id=trial["trial_id"], route=route.value, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    profile = plan.profiles.primary

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        return ModelReply(
            {"finish": True},
            tokens=4,
            cost_usd=None,
            model="qwen3.8-flash-resolved-other",
            input_tokens=3,
            output_tokens=1,
            profile=profile,
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial["trial_id"],
        route=route,
        input_token_upper_bound=lambda _value: 10,
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(
            trial["trial_id"], route.value
        ),
    )

    with pytest.raises(PublicModelProfileDrift):
        measured({"messages": []})

    attempt = ledger._connection.execute(
        "SELECT attempt_id,status,actual_model FROM model_attempts"
    ).fetchone()
    assert attempt["status"] == "settled"
    assert attempt["actual_model"] == "qwen3.8-flash-resolved-other"
    assert ledger.batch_snapshot()["stopped_reason"] == "model_profile_drift"
    with pytest.raises(ValueError, match="batch is stopped"):
        ledger.start_route(later_trial["trial_id"], route.value)
    ledger.close()


def test_role_factory_wraps_controlled_route_callbacks_in_public_ledger(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    requests = []

    def provider(request, *, timeout_seconds):
        requests.append((request, timeout_seconds))
        assert 0 < timeout_seconds <= profile.timeout_seconds
        return ModelReply(
            {"finish": True},
            tokens=7,
            cost_usd=None,
            model=profile.model,
            input_tokens=5,
            output_tokens=2,
            profile=profile,
        )

    trial = SimpleNamespace(**plan.profiles.digests)
    factory = LedgeredModelAdapterFactory(
        {"react": lambda session, _trial, _route: provider},
        {"react": lambda request: len(str(request).encode("utf-8"))},
        ledger,
        plan.profiles,
        trial_id_for=lambda _trial: trial_id,
    )
    adapters = factory(SimpleNamespace(supports_user_simulation=False), trial, route)

    assert set(adapters) == {"react"}
    response = adapters["react"]({"task": "read"})
    assert response.value == {"finish": True}
    assert len(requests) == 1
    assert ledger.usage_snapshot(trial_id, route.value)["primary"] == {
        "requests": 1,
        "actual_tokens": 7,
        "reserved_tokens": 0,
        "unknown_requests": 0,
        "actual_cost_usd": (Decimal("0.8") * 5 + Decimal("2.7") * 2)
        / Decimal(1_000_000),
        "actual_cost_complete": True,
        "reserved_cost_usd": Decimal("0"),
        "reserved_cost_complete": True,
    }
    ledger.close()


def test_role_factory_uses_jev_profile_only_for_semantic_jev_route():
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )

    def callback(_request, *, timeout_seconds):
        return None

    profiles_seen = []
    factory = LedgeredModelAdapterFactory(
        {
            "planner": lambda *_: callback,
            "semantic": lambda *_: callback,
        },
        {"planner": lambda _request: 10, "semantic": lambda _request: 10},
        SimpleNamespace(
            reserve_model_attempt=lambda **_kwargs: "unused",
            route_remaining_seconds=lambda *_args: 1,
        ),
        plan.profiles,
        trial_id_for=lambda _trial: "a" * 64,
    )

    adapters = factory(
        SimpleNamespace(supports_user_simulation=False),
        SimpleNamespace(**plan.profiles.digests),
        Route.PLAN_JEV_SEMANTIC,
    )
    profiles_seen.extend((adapters["planner"].profile, adapters["semantic"].profile))

    assert profiles_seen == [plan.profiles.primary, plan.profiles.jev]


def test_role_factory_binds_tau_user_and_judge_profiles_without_crossing_roles():
    plan = _plan_with_known_primary_prices()

    def callback(_request, *, timeout_seconds):
        return None

    factory = LedgeredModelAdapterFactory(
        {
            "planner": lambda *_: callback,
            "user_simulator": lambda *_: callback,
            "official_judge": lambda *_: callback,
        },
        {
            "planner": lambda _request: 10,
            "user_simulator": lambda _request: 10,
            "official_judge": lambda _request: 10,
        },
        SimpleNamespace(
            reserve_model_attempt=lambda **_kwargs: "unused",
            route_remaining_seconds=lambda *_args: 1,
        ),
        plan.profiles,
        trial_id_for=lambda _trial: "b" * 64,
    )

    adapters = factory(
        SimpleNamespace(supports_user_simulation=True),
        SimpleNamespace(**plan.profiles.digests),
        Route.PLAN_MCP,
    )

    assert adapters["planner"].profile is plan.profiles.primary
    assert adapters["user_simulator"].profile is plan.profiles.user_simulator
    assert adapters["official_judge"].profile is plan.profiles.judge
    assert adapters["user_model"] == plan.profiles.user_simulator.model
    assert adapters["judge_model"] == plan.profiles.judge.model


def test_role_factory_binds_ledgered_judge_for_enterprise_response_check(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        return ModelReply(
            {"content": '{"score":9,"reasoning":"supported"}', "tool_calls": []},
            tokens=8,
            cost_usd=None,
            model=plan.profiles.judge.model,
            input_tokens=6,
            output_tokens=2,
            profile=plan.profiles.judge,
        )

    factory = LedgeredModelAdapterFactory(
        {
            "react": lambda *_: callback,
            "official_judge": lambda *_: callback,
        },
        {
            "react": lambda request: len(str(request).encode("utf-8")),
            "official_judge": lambda request: len(str(request).encode("utf-8")),
        },
        ledger,
        plan.profiles,
        trial_id_for=lambda _trial: trial_id,
    )

    adapters = factory(
        SimpleNamespace(supports_user_simulation=False, requires_official_judge=True),
        SimpleNamespace(**plan.profiles.digests),
        route,
    )

    assert adapters["official_judge"].profile is plan.profiles.judge
    assert adapters["judge_model"] == plan.profiles.judge.model
    ledger.close()


def test_enterprise_judge_bridge_measures_attempts_without_inventing_cost():
    from decagent.evaluation.business.enterprise_scoring import BudgetedEnterpriseJudge
    from decagent.evaluation.business.public_session import _LedgeredEnterpriseJudgeBridge

    class Ledger:
        rows = []

        def model_attempts_snapshot(self, _trial_id, _route):
            return list(self.rows)

    class Callback:
        ledger = Ledger()
        trial_id = "trial"
        route = "react_skill_mcp"

        def __call__(self, request):
            assert request == {
                "model": "qwen-judge-v1",
                "messages": [
                    {"role": "system", "content": "Score only the evidence."},
                    {"role": "user", "content": "Actual response."},
                ],
            }
            self.ledger.rows.append(
                {
                    "role": "judge",
                    "status": "settled",
                    "actual_input_tokens": 5,
                    "actual_output_tokens": 3,
                    "actual_cost_usd": None,
                    "attempt_seconds": 0.25,
                }
            )
            return ModelReply(
                {"content": '{"score":9}', "tool_calls": []},
                tokens=8,
                cost_usd=None,
                model="qwen-judge-v1",
                input_tokens=5,
                output_tokens=3,
            )

    callback = Callback()
    bridge = _LedgeredEnterpriseJudgeBridge(callback, "qwen-judge-v1")
    budgeted = BudgetedEnterpriseJudge(bridge, bridge.usage_snapshot)
    response = asyncio.run(
        budgeted.ainvoke(
            [
                SimpleNamespace(type="system", content="Score only the evidence."),
                SimpleNamespace(type="human", content="Actual response."),
            ]
        )
    )

    assert response.content == '{"score":9}'
    assert bridge.usage_snapshot() == {
        "requests": 1,
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "cost_usd": None,
        "seconds": 0.25,
    }


def test_enterprise_judge_bridge_rejects_unknown_dispatch_usage():
    from decagent.evaluation.business.enterprise_scoring import EnterpriseScoringError
    from decagent.evaluation.business.public_session import _LedgeredEnterpriseJudgeBridge

    class Ledger:
        def model_attempts_snapshot(self, _trial_id, _route):
            return [
                {
                    "role": "judge",
                    "status": "unknown",
                    "actual_input_tokens": None,
                    "actual_output_tokens": None,
                    "actual_cost_usd": None,
                    "attempt_seconds": 1.0,
                }
            ]

    class Callback:
        ledger = Ledger()
        trial_id = "trial"
        route = "react_skill_mcp"

        def __call__(self, _request):
            raise AssertionError("An existing unknown attempt must not be replayed")

    bridge = _LedgeredEnterpriseJudgeBridge(Callback(), "qwen-judge-v1")

    with pytest.raises(EnterpriseScoringError, match="unresolved"):
        bridge.usage_snapshot()


def test_enterprise_session_requires_ledgered_judge_even_if_a_client_was_injected():
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    session = object.__new__(EvaluationPlanSession)
    session.environment = SimpleNamespace(supports_user_simulation=False)
    session._scorer = SimpleNamespace(
        requires_judge=True,
        judge_client_configured=True,
    )

    assert session.requires_official_judge is True


def test_enterprise_session_rejects_preconfigured_judge_for_controlled_route():
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    session = object.__new__(EvaluationPlanSession)
    session._closed = False
    session.environment = SimpleNamespace(supports_user_simulation=False)
    session.spec = SimpleNamespace(assert_active=lambda: True)
    session._scorer = SimpleNamespace(
        requires_judge=True,
        judge_client_configured=True,
    )

    with pytest.raises(ValueError, match="preconfigured judge"):
        session.configure_official_judge(model="qwen-judge-v1", callback=lambda _request: None)


def test_enterprise_session_binds_response_check_judge_through_budget_adapter():
    from decagent.evaluation.business.enterprise_scoring import BudgetedEnterpriseJudge
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    class Scorer:
        requires_judge = True
        judge_client_configured = False
        configured = None

        def configure_judge_client(self, client):
            self.configured = client

    session = object.__new__(EvaluationPlanSession)
    session._closed = False
    session.environment = SimpleNamespace(supports_user_simulation=False)
    session.spec = SimpleNamespace(assert_active=lambda: True)
    session._scorer = Scorer()
    class Callback:
        ledger = SimpleNamespace(model_attempts_snapshot=lambda *_args: [])
        trial_id = "trial"
        route = "react_skill_mcp"

        def __call__(self, _request):
            return None

    callback = Callback()

    session.configure_official_judge(model="qwen-judge-v1", callback=callback)

    assert isinstance(session._scorer.configured, BudgetedEnterpriseJudge)


def test_unknown_dispatched_model_outcome_blocks_route_and_cannot_replay(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    calls = []

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        calls.append("sent")
        raise TimeoutError("response was lost after dispatch")

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(1, len(json.dumps(value).encode("utf-8"))),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route.value),
    )
    with pytest.raises(PublicModelOutcomeUnknown):
        measured({"messages": []})

    assert calls == ["sent"]
    assert ledger.route_status(trial_id, route.value) == "unknown"
    assert ledger.batch_snapshot()["qwen_requests"] == 1
    attempt = ledger.model_attempts_snapshot(trial_id, route.value)[0]
    assert attempt["failure_stage"] == "model_callback"
    assert attempt["exception_type"] == "TimeoutError"
    assert attempt["http_status"] is None
    assert "response was lost" not in repr(attempt)
    lifecycle = ledger.route_lifecycle(trial_id, route.value)
    assert lifecycle["failure_stage"] == "model_callback"
    assert lifecycle["exception_type"] == "TimeoutError"
    with pytest.raises(ValueError, match="unknown model usage"):
        measured({"messages": []})
    assert calls == ["sent"]
    ledger.close()


def test_unknown_qwen_cost_keeps_bounded_dispatch_and_never_records_zero(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    called = []

    def callback(_request, *, timeout_seconds):
        called.append(timeout_seconds)
        return ModelReply(
            "ok",
            tokens=7,
            cost_usd=None,
            model=plan.profiles.primary.model,
            input_tokens=5,
            output_tokens=2,
            profile=plan.profiles.primary,
        )

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=plan.profiles.primary,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(1, len(json.dumps(value).encode("utf-8"))),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route.value),
    )
    reply = measured({"messages": [{"role": "user", "content": "hello"}]})

    assert reply.tokens == 7
    assert len(called) == 1 and called[0] > 0
    assert ledger.batch_snapshot()["qwen_requests"] == 1
    assert ledger.route_status(trial_id, route.value) == "running"
    attempts = ledger._connection.execute(
        "SELECT attempt_id,status,reserved_cost_usd,actual_cost_usd,actual_cost_source "
        "FROM model_attempts"
    ).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["status"] == "settled"
    assert attempts[0]["reserved_cost_usd"] is None
    assert attempts[0]["actual_cost_usd"] is None
    assert attempts[0]["actual_cost_source"] == "unknown"
    snapshot = ledger.usage_snapshot(trial_id, route.value)["primary"]
    assert snapshot["actual_tokens"] == 7
    assert snapshot["actual_cost_usd"] is None
    assert snapshot["actual_cost_complete"] is False
    ledger.close()


def test_unknown_jev_price_remains_blocked_by_cumulative_dollar_budget(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = replace(
        plan.profiles.jev,
        pricing_status="unknown",
        input_price_per_million=None,
        output_price_per_million=None,
    )
    called = []
    measured = LedgeredModelCallback(
        lambda _request, *, timeout_seconds: called.append(timeout_seconds),
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _value: 5,
        route_remaining_seconds=lambda: 10,
    )

    with pytest.raises(ValueError, match="allowed only for token-capped Qwen roles"):
        measured({"question": "choose"})

    assert called == []
    assert ledger.batch_snapshot()["qwen_requests"] == 0
    ledger.close()


def test_callback_cost_source_must_be_supported_by_the_frozen_contract(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    def callback(_request, *, timeout_seconds):
        return ModelReply(
            "ok",
            3,
            Decimal("0.0000001"),
            profile.model,
            2,
            1,
            profile,
            cost_source="provider_billed",
        )
    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(1, len(json.dumps(value).encode("utf-8"))),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route.value),
    )

    reply = measured({"messages": []})
    attempts = ledger._connection.execute("SELECT attempt_id FROM model_attempts").fetchall()
    snapshot = ledger.model_attempt_snapshot(attempts[0]["attempt_id"])
    assert reply.cost_usd == Decimal("0.0000001")
    assert snapshot["actual_cost_source"] == "provider_billed"
    ledger.close()


def test_provider_billing_keeps_gross_fee_voucher_and_cash_as_separate_evidence(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    evidence = ProviderBillingEvidence(
        gross_amount=Decimal("0.0125"),
        currency="USD",
        voucher_credit_amount=Decimal("0.0125"),
        cash_paid_amount=Decimal("0"),
        source="provider_activity",
        record_id="activity-123",
    )

    measured = LedgeredModelCallback(
        lambda _request, *, timeout_seconds: ModelReply(
            "ok",
            3,
            None,
            profile.model,
            2,
            1,
            profile,
            billing_evidence=evidence,
        ),
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _value: 5,
        route_remaining_seconds=lambda: 10,
    )

    reply = measured({"prompt": "bounded"})
    attempt = ledger._connection.execute("SELECT attempt_id FROM model_attempts").fetchone()
    snapshot = ledger.model_attempt_snapshot(attempt["attempt_id"])

    assert reply.cost_usd == Decimal("0.0125")
    assert reply.cost_source == "provider_billed"
    assert snapshot["actual_cost_usd"] == "0.0125"
    assert snapshot["billing_gross_amount"] == "0.0125"
    assert snapshot["billing_currency"] == "USD"
    assert snapshot["billing_voucher_credit_amount"] == "0.0125"
    assert snapshot["billing_cash_paid_amount"] == "0"
    assert snapshot["billing_source"] == "provider_activity"
    assert snapshot["billing_record_id"] == "activity-123"
    billing = ledger.billing_snapshot(trial_id, route.value)
    assert billing["settled_attempts"] == billing["evidence_attempts"] == 1
    assert billing["missing_evidence_attempts"] == 0
    assert billing["by_currency"]["USD"] == {
        "evidence_attempts": 1,
        "gross_api_fee": "0.0125",
        "voucher_credit": "0.0125",
        "voucher_credit_partial": "0.0125",
        "voucher_credit_missing": 0,
        "cash_paid": "0",
        "cash_paid_partial": "0",
        "cash_paid_missing": 0,
    }
    ledger.close()


def test_non_usd_provider_billing_is_retained_without_guessed_conversion(tmp_path):
    plan = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    measured = LedgeredModelCallback(
        lambda _request, *, timeout_seconds: ModelReply(
            "ok",
            3,
            None,
            profile.model,
            2,
            1,
            profile,
            billing_evidence=ProviderBillingEvidence(
                gross_amount=Decimal("0.08"),
                currency="CNY",
                voucher_credit_amount=Decimal("0.08"),
                source="provider_invoice",
                record_id="invoice-456",
            ),
        ),
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _value: 5,
        route_remaining_seconds=lambda: 10,
    )

    reply = measured({"prompt": "bounded"})
    attempt = ledger._connection.execute("SELECT attempt_id FROM model_attempts").fetchone()
    snapshot = ledger.model_attempt_snapshot(attempt["attempt_id"])
    usage = ledger.usage_snapshot(trial_id, route.value)["primary"]

    assert reply.cost_usd is None
    assert reply.cost_source == "unknown"
    assert snapshot["billing_gross_amount"] == "0.08"
    assert snapshot["billing_currency"] == "CNY"
    assert snapshot["billing_cash_paid_amount"] is None
    assert snapshot["actual_cost_usd"] is None
    assert usage["actual_cost_complete"] is False
    billing = ledger.billing_snapshot(trial_id, route.value)
    assert billing["by_currency"]["CNY"]["gross_api_fee"] == "0.08"
    assert billing["by_currency"]["CNY"]["voucher_credit"] == "0.08"
    assert billing["by_currency"]["CNY"]["cash_paid"] is None
    ledger.close()


def test_provider_billing_rejects_unsupported_currency_and_inconsistent_usd_fee():
    with pytest.raises(ValueError, match="currency"):
        ProviderBillingEvidence(Decimal("1"), "US", source="provider_activity")

    evidence = ProviderBillingEvidence(
        Decimal("0.02"), "USD", source="provider_activity"
    )
    profile = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    ).profiles.primary
    with pytest.raises(ValueError, match="gross provider fee"):
        ModelReply(
            "ok",
            1,
            Decimal("0.01"),
            profile.model,
            1,
            0,
            profile,
            cost_source="provider_billed",
            billing_evidence=evidence,
        )


def test_verified_preflight_failure_releases_dispatched_reservation(tmp_path):
    plan = _plan_with_known_primary_prices()
    ledger, trial_id, route = _started_ledger(tmp_path, plan)
    profile = plan.profiles.primary
    calls = []

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        calls.append("preflight")
        raise PublicModelNotDispatched("request failed local validation")

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=profile,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda value: max(1, len(json.dumps(value).encode("utf-8"))),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(trial_id, route.value),
    )
    with pytest.raises(PublicModelNotDispatched):
        measured({"messages": []})

    attempt = ledger._connection.execute(
        "SELECT attempt_id,status FROM model_attempts"
    ).fetchone()
    assert calls == ["preflight"]
    assert attempt["status"] == "not_sent"
    assert ledger.route_status(trial_id, route.value) == "running"
    assert ledger.batch_snapshot()["qwen_requests"] == 0
    ledger.close()


def test_primary_model_applies_the_effective_deadline_to_the_http_client():
    from types import SimpleNamespace

    from decagent.models.primary import PrimaryModel

    class RootClient:
        def __init__(self, timeout):
            self.timeout = timeout
            self.chat = SimpleNamespace(completions=object())

        def with_options(self, *, timeout):
            return RootClient(timeout)

    class BoundChat:
        def __init__(self):
            self.request_timeout = 10
            self.root_client = RootClient(10)
            self.client = self.root_client.chat.completions
            self.observed_timeouts = []

        def bind_tools(self, _tools):
            return self

        @property
        def bound(self):
            return self

        def invoke(self, _messages):
            self.observed_timeouts.append(self.root_client.timeout)
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                response_metadata={"model_name": "qwen3.8-flash", "id": "req-1"},
            )

    client = BoundChat()
    model = PrimaryModel(client, requested_model="qwen3.8-flash")
    turn = model.respond(
        [{"role": "user", "content": "hello"}], timeout_seconds=1.25
    )

    assert turn.text == "ok"
    assert client.observed_timeouts == [1.25]
    assert client.root_client.timeout == 10

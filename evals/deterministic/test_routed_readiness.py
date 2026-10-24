"""Routed readiness uses durable, provider-separated authorization and cost limits."""

import sqlite3
from decimal import Decimal

import pytest

from decagent.evaluation.business.routed_readiness import (
    AUTHORIZATION_ID,
    DEEPSEEK_PRICE_SOURCE,
    MAX_CALLS,
    MAX_DEEPSEEK_COST,
    MAX_OPENROUTER_CALLS,
    MAX_QWEN_CALLS,
    MAX_QWEN_COST_CNY,
    MAX_TOTAL_TOKENS,
    MAX_WALL_SECONDS,
    SqliteRoutedDiagnosticLedger,
)
from decagent.storage.budget import BudgetExceeded


def _ledger(
    path,
    monkeypatch,
    *,
    fingerprint="a" * 64,
    fallback_price_digest="c" * 64,
    resume_known_not_sent=False,
):
    module = __import__(
        "decagent.evaluation.business.routed_readiness", fromlist=["unused"]
    )
    monkeypatch.setattr(module, "AUTHORIZATION_ANCHOR", path.parent / "anchor.sqlite")
    return SqliteRoutedDiagnosticLedger.open(
        path,
        diagnostic_fingerprint=fingerprint,
        primary_model="qwen3.8-flash",
        primary_endpoint="https://maas.qianwenaiapi.com/compatible-mode/v1",
        fallback_model="deepseek/deepseek-v4.1-flash",
        fallback_endpoint="https://openrouter.ai/api/v1",
        primary_price_digest="b" * 64,
        fallback_price_digest=fallback_price_digest,
        primary_currency="CNY",
        model_profile_digest="f" * 64,
        cohort_profiles_fingerprint="e" * 64,
        primary_price_version="qwen-price-v1",
        fallback_price_version="openrouter-price-v1",
        provider_request_profile_fingerprints={
            provider: {
                purpose: f"{digit}" * 64
                for purpose, digit in {
                    "connection": "1" if provider == "qwen" else "5",
                    "tool_contract": "2" if provider == "qwen" else "6",
                    "plan": "3" if provider == "qwen" else "7",
                    "stream": "4" if provider == "qwen" else "8",
                }.items()
            }
            for provider in ("qwen", "openrouter")
        },
        resume_known_not_sent=resume_known_not_sent,
    )


def _reserve(
    ledger,
    key,
    *,
    provider="qwen",
    model="qwen3.8-flash",
    tokens=100,
    cost="0",
    trial_cost_currency=None,
    **extra,
):
    metadata = {
        "provider_routing": (
            "openrouter_fireworks" if provider == "openrouter" else "direct_endpoint"
        ),
        "request_profile_fingerprint": "7" * 64 if provider == "openrouter" else "3" * 64,
    }
    metadata.update(extra)
    return ledger.reserve_model(
        key,
        1,
        provider,
        tokens,
        Decimal(cost),
        model_family="primary",
        purpose="plan",
        request_role="planning",
        requested_model=model,
        endpoint=(
            "https://openrouter.ai/api/v1"
            if provider == "openrouter"
            else "https://maas.qianwenaiapi.com/compatible-mode/v1"
        ),
        requested_output_tokens=8,
        trial_cost_currency=trial_cost_currency
        or ("USD" if provider == "openrouter" else "CNY"),
        **metadata,
    )


def test_unknown_qwen_is_held_and_other_provider_can_continue_independently(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    first = _reserve(ledger, "qwen-timeout")
    ledger.mark_dispatched(first)
    ledger.settle_model(
        first,
        None,
        None,
        response_state="unknown",
        error_code="provider_timeout",
        error_category="timeout",
    )

    with pytest.raises(BudgetExceeded, match="provider stopped"):
        _reserve(ledger, "unrelated-request")

    openrouter = _reserve(
        ledger,
        "deepseek-independent-probe",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        cost="0.1",
    )
    ledger.mark_dispatched(openrouter)
    ledger.settle_model(
        openrouter,
        15,
        Decimal("0.05"),
        response_state="responded",
        server_request_id="gen-dec-1234_abcd",
        resolved_model="deepseek/deepseek-v4.1-flash",
        latency_ms=40,
    )

    next_request = _reserve(
        ledger,
        "next-openrouter-phase",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        cost="0.1",
    )
    snapshot = ledger.snapshot()
    assert next_request != openrouter
    assert snapshot["used_calls"] == 3
    assert snapshot["held_or_actual_tokens"] == 215
    assert snapshot["deepseek_rate_estimate_usd"] == "0.15"
    assert snapshot["rate_estimate_basis"].startswith("frozen_standard_rates")
    assert snapshot["provider_billed_cost_available"] is False
    assert snapshot["attempts"][0]["status"] == "unknown"
    assert snapshot["attempts"][1]["recovery_of"] is None
    assert snapshot["attempts"][1]["server_request_id"] == "gen-dec-1234_abcd"


def _complete_request(ledger, receipt, *, tokens=10, cost):
    ledger.mark_dispatched(receipt)
    ledger.settle_model(receipt, tokens, Decimal(cost), response_state="responded")


def test_readiness_authorization_is_a_fresh_provider_capped_batch():
    assert AUTHORIZATION_ID == "primary-readiness-routed-v3-20261024"
    assert MAX_QWEN_CALLS == 6
    assert MAX_OPENROUTER_CALLS == 6
    assert MAX_CALLS == 12
    assert MAX_TOTAL_TOKENS == 600_000
    assert MAX_QWEN_COST_CNY == Decimal("1.62")
    assert MAX_DEEPSEEK_COST == Decimal("5")
    assert MAX_WALL_SECONDS == 2_700


def test_readiness_reservation_persists_route_and_request_profile_fingerprint(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    receipt = _reserve(ledger, "route-profile-bound")

    attempt = ledger.snapshot()["attempts"][0]

    assert attempt["receipt"] == receipt
    assert attempt["provider_routing"] == "direct_endpoint"
    assert attempt["request_profile_fingerprint"] == "3" * 64
    assert ledger.identity["model_profile_digest"] == "f" * 64
    assert ledger.identity["cohort_profiles_fingerprint"] == "e" * 64


@pytest.mark.parametrize(
    ("provider", "model", "metadata", "message"),
    [
        (
            "qwen",
            "qwen3.8-flash",
            {"provider_routing": "openrouter_fireworks"},
            "request route differs",
        ),
        (
            "qwen",
            "qwen3.8-flash",
            {"request_profile_fingerprint": "z" * 64},
            "request profile differs",
        ),
    ],
)
def test_readiness_reservation_rejects_route_and_profile_drift(
    tmp_path, monkeypatch, provider, model, metadata, message
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)

    with pytest.raises(BudgetExceeded, match=message):
        _reserve(ledger, "route-profile-drift", provider=provider, model=model, **metadata)


def test_readiness_rejects_unpinned_or_understated_qwen_price():
    from types import SimpleNamespace

    from decagent.evaluation.business.routed_readiness import _validate_profile
    from decagent.runtime.model_calls import TokenPrice

    settings = SimpleNamespace(
        main_model="qwen3.8-flash",
        fallback_model="deepseek/deepseek-v4.1-flash",
        main_api_key="configured",
        resolved_fallback_api_key="configured",
        main_base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        fallback_base_url="https://openrouter.ai/api/v1",
    )
    for price in (
        TokenPrice(
            "qwen3.8-flash-standard-cny-20261024-v1",
            Decimal("0.01"),
            Decimal("0.02"),
            currency="CNY",
        ),
        TokenPrice("unreviewed", Decimal("0.8"), Decimal("2.7"), currency="CNY"),
    ):
        config = type("Config", (), {"main_price": price})()
        with pytest.raises(ValueError, match="frozen Qwen price"):
            _validate_profile(settings, config)


def test_unknown_qwen_usage_stops_qwen_but_keeps_openrouter_probe_independent(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "provider-switch.sqlite3", monkeypatch)
    qwen = _reserve(ledger, "qwen-timeout")
    ledger.mark_dispatched(qwen)
    ledger.settle_model(
        qwen,
        None,
        None,
        response_state="unknown",
        error_code="provider_timeout",
        error_category="timeout",
    )

    openrouter = _reserve(
        ledger,
        "independent-openrouter-probe",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        cost="0.1",
    )
    ledger.mark_dispatched(openrouter)
    ledger.settle_model(
        openrouter,
        15,
        Decimal("0.05"),
        response_state="responded",
        server_request_id="gen-dec-1234_abcd",
        resolved_model="deepseek/deepseek-v4.1-flash",
        latency_ms=40,
    )

    with pytest.raises(BudgetExceeded, match="provider stopped"):
        _reserve(ledger, "qwen-must-not-resume")

    snapshot = ledger.snapshot()
    assert snapshot["attempts"][0]["status"] == "unknown"
    assert snapshot["attempts"][1]["recovery_of"] is None
    assert snapshot["attempts"][1]["provider"] == "openrouter"
    assert snapshot["used_calls"] == 2


def test_readiness_plan_passes_provider_and_deadline_policy_to_planner(monkeypatch):
    from types import SimpleNamespace

    from decagent.business.plans import planner as planner_module
    from decagent.business.plans.planner import plan_tool_definitions
    from decagent.config import Settings
    from decagent.evaluation.business.public_models import OneShotPrimaryModel
    from decagent.evaluation.business.routed_readiness import _diagnostic_actions
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    class Ledger:
        checked = 0

        def assert_within_wall_limit(self):
            self.checked += 1

        def request_timeout_seconds(self, model):
            return min(model.max_request_timeout, 13.0)

        def snapshot(self):
            return {"attempts": []}

    ledger = Ledger()
    captured = {}

    class Plan:
        digest = "d" * 64

        def to_mapping(self):
            return {
                "nodes": [
                    {"type": "read_tool", "tool": {"command": "directory"}}
                ]
            }

    class Planner:
        def plan(self, *_args, **_kwargs):
            return SimpleNamespace(plan=Plan(), repaired=False)

    def planner_factory(*_args, **kwargs):
        captured["settings"], captured["config"] = _args[:2]
        captured.update(kwargs)
        return Planner()

    monkeypatch.setattr(planner_module, "budgeted_planner", planner_factory)
    price = TokenPrice("readiness-v1", Decimal("0.6"), Decimal("2.4"))
    settings = Settings(fallback_api_key="configured")
    config = RuntimeConfig(fallback_price=price)
    actions = _diagnostic_actions(settings, config, ledger, provider="openrouter")
    actions["plan"]()

    assert captured["before_dispatch"] == ledger.assert_within_wall_limit
    assert captured["request_timeout"] == ledger.request_timeout_seconds
    assert captured["max_same_provider_attempts"] == 1
    assert captured["fallback_allowed"] is False
    assert captured["force_fallback"] is False
    assert captured["allow_format_repair"] is False
    assert isinstance(captured["model"], OneShotPrimaryModel)
    assert captured["model"].usage_include is True
    assert captured["model"].profile.provider_routing == "openrouter_fireworks"
    assert captured["model"].tool_definitions == plan_tool_definitions()
    assert captured["model"].profile.model == "deepseek/deepseek-v4.1-flash"
    assert captured["settings"].main_model == "deepseek/deepseek-v4.1-flash"
    assert captured["config"].main_provider == "openrouter"
    assert ledger.checked == 0  # The budgeted adapter invokes the guard before dispatch.


def test_readiness_uses_cancellable_one_shot_model_and_openrouter_usage(monkeypatch):
    from decagent.config import Settings
    from decagent.evaluation.business.public_models import OneShotPrimaryModel
    from decagent.evaluation.business.routed_readiness import _budgeted_adapter
    from decagent.runtime import model_factory
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return object()

    class Ledger:
        def assert_within_wall_limit(self):
            pass

        def request_timeout_seconds(self, model):
            return model.max_request_timeout

    monkeypatch.setattr(model_factory, "build_budgeted_primary", factory)
    settings = Settings(
        main_model="qwen3.8-flash",
        main_base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        main_api_key="qwen-secret",
        fallback_api_key="openrouter-secret",
    )
    config = RuntimeConfig(
        main_price=TokenPrice("qwen-v1", Decimal("0.8"), Decimal("2.7"), "CNY"),
        fallback_price=TokenPrice("openrouter-v1", Decimal("0.6"), Decimal("2.4")),
    )

    _budgeted_adapter(
        settings,
        config,
        Ledger(),
        purpose="stream",
        max_output_tokens=256,
        provider="openrouter",
    )

    model = captured["model"]
    assert isinstance(model, OneShotPrimaryModel)
    assert model.max_request_timeout == 60
    assert model.profile.model == "deepseek/deepseek-v4.1-flash"
    assert model.profile.endpoint == "https://openrouter.ai/api/v1"
    assert model.profile.provider_routing == "openrouter_fireworks"
    assert model.usage_include is True
    assert captured["max_same_provider_attempts"] == 1
    assert captured["fallback_allowed"] is False


def test_deadline_failure_after_dispatch_mark_releases_only_with_local_proof(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "post-dispatch-deadline.sqlite", monkeypatch)
    receipt = _reserve(ledger, "request-deadline")
    ledger.mark_dispatched(receipt)

    with pytest.raises(ValueError, match="cannot be released"):
        ledger.mark_not_sent(receipt, reason="request_deadline_exhausted")

    ledger.mark_not_sent(
        receipt,
        reason="request_deadline_exhausted",
        after_local_preflight=True,
    )

    attempt = ledger.snapshot()["attempts"][0]
    assert attempt["status"] == "settled"
    assert attempt["dispatch_state"] == "not_sent"
    assert attempt["actual_tokens"] == 0


def test_diagnostic_limits_reject_token_and_provider_fee_overflow(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)

    with pytest.raises(BudgetExceeded, match="input"):
        _reserve(ledger, "too-many-input-tokens", tokens=MAX_TOTAL_TOKENS + 1)

    for index in range(5):
        receipt = _reserve(
            ledger,
            f"deepseek-{index}",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            cost="0.9",
        )
        _complete_request(ledger, receipt, cost="0.9")
    with pytest.raises(BudgetExceeded, match="DeepSeek"):
        _reserve(
            ledger,
            "deepseek-sixth-over-cap",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            cost="0.6",
        )

    qwen = _ledger(tmp_path / "qwen" / "qwen-budget.sqlite", monkeypatch)
    for index in range(5):
        receipt = _reserve(qwen, f"qwen-{index}", cost="0.3")
        _complete_request(qwen, receipt, cost="0.3")
    with pytest.raises(BudgetExceeded, match="Qwen"):
        _reserve(qwen, "qwen-sixth-over-cap", cost="0.13")


def test_provider_cost_currency_must_match_the_frozen_profile(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "currency-ledger.sqlite", monkeypatch)

    with pytest.raises(BudgetExceeded, match="currency"):
        _reserve(ledger, "wrong-qwen-currency", trial_cost_currency="USD")


def test_new_authorization_refuses_to_mutate_the_historical_v1_ledger(
    tmp_path, monkeypatch
):
    path = tmp_path / "historical-v1.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE identity(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO identity VALUES(1, ?)",
            ('{"authorization_id":"primary-readiness-routed-v1"}',),
        )

    with pytest.raises(ValueError, match="different authorization"):
        _ledger(path, monkeypatch)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert tables == {"identity"}


def test_request_count_cap_and_resume_never_reopen_a_dispatched_unknown(tmp_path, monkeypatch):
    path = tmp_path / "new-batch.sqlite"
    ledger = _ledger(path, monkeypatch)
    first = None
    for index in range(MAX_QWEN_CALLS):
        receipt = _reserve(ledger, f"qwen-request-{index}", cost="0.01")
        _complete_request(ledger, receipt, cost="0.01")
        first = receipt
    with pytest.raises(BudgetExceeded, match="qwen readiness request cap"):
        _reserve(ledger, "qwen-seventh-request", cost="0.01")
    for index in range(MAX_OPENROUTER_CALLS):
        receipt = _reserve(
            ledger,
            f"openrouter-request-{index}",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            cost="0.1",
        )
        _complete_request(ledger, receipt, cost="0.1")
        first = receipt
    with pytest.raises(BudgetExceeded, match="exhausted"):
        _reserve(ledger, "thirteenth-request")
    ledger.close()

    restored = _ledger(path, monkeypatch)
    with pytest.raises(BudgetExceeded, match="exhausted"):
        _reserve(restored, "after-restart", provider="openrouter",
                 model="deepseek/deepseek-v4.1-flash", cost="0.1")
    assert restored.snapshot()["used_calls"] == 12
    assert first is not None


def test_total_token_cap_combines_both_providers(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path / "token-cap.sqlite", monkeypatch)
    request_tokens = 65_544
    for index in range(MAX_QWEN_CALLS):
        receipt = _reserve(
            ledger, f"qwen-large-{index}", tokens=request_tokens, cost="0.01"
        )
        _complete_request(ledger, receipt, tokens=request_tokens, cost="0.01")
    for index in range(3):
        receipt = _reserve(
            ledger,
            f"openrouter-large-{index}",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            tokens=request_tokens,
            cost="0.1",
        )
        _complete_request(ledger, receipt, tokens=request_tokens, cost="0.1")

    assert ledger.snapshot()["held_or_actual_tokens"] == 589_896
    with pytest.raises(BudgetExceeded, match="token cap"):
        _reserve(
            ledger,
            "openrouter-over-token-cap",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            tokens=request_tokens,
            cost="0.1",
        )


def test_expired_readiness_batch_never_dispatches_reserved_call(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    ledger = _ledger(tmp_path / "expired-batch.sqlite", monkeypatch)
    receipt = _reserve(ledger, "before-deadline", cost="0.01")
    expired = datetime.now(UTC) - timedelta(seconds=MAX_WALL_SECONDS + 1)
    with ledger._connect() as connection:
        connection.execute(
            "UPDATE batch_state SET started_at_utc=? WHERE id=1",
            (expired.isoformat(),),
        )

    with pytest.raises(BudgetExceeded, match="wall-clock"):
        ledger.mark_dispatched(receipt)
    ledger.mark_not_sent(receipt, reason="batch_deadline")
    assert ledger.snapshot()["used_calls"] == 0


def test_phase_journal_skips_passed_stages_and_never_replays_an_interrupted_stage(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    assert ledger.start_phase("connection") is None
    ledger.finish_phase("connection", "passed", {"response_complete": True})
    assert ledger.start_phase("connection") == {"response_complete": True}
    assert ledger.start_phase("plan") is None
    ledger.close()

    restored = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    with pytest.raises(BudgetExceeded, match="phase is already in progress"):
        restored.start_phase("plan")


def test_phase_records_and_final_result_do_not_accept_unknown_unrecovered_requests(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    receipt = _reserve(ledger, "unknown-phase")
    ledger.mark_dispatched(receipt)
    ledger.settle_model(
        receipt, None, None, response_state="unknown", error_code="provider_timeout",
        error_category="timeout",
    )
    with pytest.raises(BudgetExceeded, match="resolved attempt"):
        ledger.record_result({"status": "failed"})


def test_diagnostic_fingerprint_is_stable_sha256():
    from decagent.evaluation.business.routed_readiness import diagnostic_fingerprint

    value = diagnostic_fingerprint()

    assert len(value) == 64
    assert value == diagnostic_fingerprint()


def test_run_persists_profile_and_price_provenance_with_readiness_result(
    tmp_path, monkeypatch
):
    module = __import__(
        "decagent.evaluation.business.routed_readiness", fromlist=["unused"]
    )
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)
    monkeypatch.setattr(
        module,
        "_diagnostic_actions",
        lambda *_args, **_kwargs: {
            phase: (lambda phase=phase: {"phase": phase})
            for phase in ("connection", "tool_contract", "plan", "stream")
        },
    )
    profile_digest = ledger.identity["model_profile_digest"]
    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    settings = Settings(
        main_model="qwen3.8-flash",
        main_base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        main_api_key="qwen-key",
        fallback_api_key="openrouter-key",
    )
    config = RuntimeConfig(
        main_provider="qwen",
        main_price=TokenPrice("qwen-profile-v1", Decimal("0.8"), Decimal("2.7"), "CNY"),
        fallback_price=TokenPrice(
            "openrouter-profile-v1", Decimal("0.6"), Decimal("2.4"), "USD"
        ),
    )

    report = module.run_routed_readiness(
        settings,
        config,
        ledger,
        model_profile_digest=profile_digest,
        pricing_source=DEEPSEEK_PRICE_SOURCE,
    )

    assert report["model_profile_digest"] == profile_digest
    assert report["schema_version"] == "routed-model-readiness-evidence-v3"
    assert report["provider_profiles"]["qwen"]["provider_routing"] == "direct_endpoint"
    assert ledger.prior_result()["model_profile_digest"] == profile_digest
    assert ledger.prior_result()["pricing_source"] == DEEPSEEK_PRICE_SOURCE


def test_readiness_qualifies_one_provider_and_tries_openrouter_only_after_qwen_fails(
    monkeypatch,
):
    from decagent.evaluation.business.routed_readiness import (
        _readiness_request_profile_fingerprints,
        run_routed_readiness,
    )

    class PhaseLedger:
        identity = {
            "diagnostic_fingerprint": "a" * 64,
            "primary_price_source": "qwen-price-source",
        }

        def __init__(self):
            self.phases = {}
            self.result = None
            self.calls = []

        def ensure_resumable(self):
            pass

        def start_phase(self, phase):
            if phase in self.phases:
                status, report = self.phases[phase]
                return report if status == "passed" else None
            self.phases[phase] = ("started", None)
            return None

        def finish_phase(self, phase, status, report):
            self.phases[phase] = (status, report)

        def snapshot(self):
            return {
                "phases": [
                    {"phase": phase, "status": status, "report": report}
                    for phase, (status, report) in self.phases.items()
                ],
                "attempts": [],
            }

        def record_result(self, report):
            self.result = report

    ledger = PhaseLedger()

    def action_factory(_settings, _config, target_ledger, *, provider="qwen"):
        actions = {}
        for phase in ("connection", "tool_contract", "plan", "stream"):
            def run(phase=phase, provider=provider):
                target_ledger.calls.append(f"{provider}:{phase}")
                if provider == "qwen" and phase == "plan":
                    raise ValueError("Qwen plan does not compile")
                return {"provider": provider, "phase": phase}

            actions[phase] = run
        return actions

    monkeypatch.setattr(
        "decagent.evaluation.business.routed_readiness._diagnostic_actions",
        action_factory,
    )

    from decagent.config import Settings
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    settings = Settings(
        main_model="qwen3.8-flash",
        main_base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        main_api_key="qwen-key",
        fallback_model="deepseek/deepseek-v4.1-flash",
        fallback_base_url="https://openrouter.ai/api/v1",
        fallback_api_key="openrouter-key",
    )
    config = RuntimeConfig(
        main_provider="qwen",
        main_price=TokenPrice("qwen-price-v1", Decimal("0.8"), Decimal("2.7"), "CNY"),
        fallback_price=TokenPrice(
            "openrouter-price-v1", Decimal("0.6"), Decimal("2.4"), "USD"
        ),
    )
    report = run_routed_readiness(
        settings,
        config,
        ledger,
        model_profile_digest="d" * 64,
        pricing_source=DEEPSEEK_PRICE_SOURCE,
    )

    assert report["status"] == "passed"
    assert report["schema_version"] == "routed-model-readiness-evidence-v3"
    assert report["qualified_provider"] == "openrouter"
    assert report["provider_profiles"]["openrouter"] == {
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4.1-flash",
        "endpoint": "https://openrouter.ai/api/v1",
        "provider_routing": "openrouter_fireworks",
        "price_version": "openrouter-price-v1",
        "price_source": DEEPSEEK_PRICE_SOURCE,
        "price_currency": "USD",
        "input_price_per_million": "0.6",
        "output_price_per_million": "2.4",
        "request_profile_fingerprints": _readiness_request_profile_fingerprints(
            settings, config, provider="openrouter"
        ),
    }
    assert ledger.calls == [
        "qwen:connection",
        "qwen:tool_contract",
        "qwen:plan",
        "openrouter:connection",
        "openrouter:tool_contract",
        "openrouter:plan",
        "openrouter:stream",
    ]


def test_readiness_runtime_uses_the_qwen_accounting_provider_without_enabling_product_fallback():
    from decagent.evaluation.business.routed_readiness import _readiness_runtime_config
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice

    main_price = TokenPrice("main-v1", Decimal("0.2"), Decimal("0.4"))
    fallback_price = TokenPrice("fallback-v1", Decimal("0.6"), Decimal("2.4"))
    config = RuntimeConfig(
        main_provider="primary",
        main_price=main_price,
        fallback_price=fallback_price,
        main_fallback_enabled=False,
    )

    readiness = _readiness_runtime_config(config, fallback_price=fallback_price)

    assert readiness.main_provider == "qwen"
    assert readiness.main_fallback_enabled is False
    assert readiness.unknown_fallback_reservation is None


def test_known_not_sent_preflight_failure_can_resume_with_history_preserved(
    tmp_path, monkeypatch
):
    module = __import__(
        "decagent.evaluation.business.routed_readiness", fromlist=["unused"]
    )
    from decagent.runtime.model_calls import TokenPrice

    prior_price = module._price_digest(
        TokenPrice(
            "openrouter-models-api-2026-10-19-peak",
            Decimal("0.60"),
            Decimal("2.40"),
        )
    )
    current_price = module._price_digest(
        TokenPrice(
            module.DEEPSEEK_PRICE_VERSION,
            module.DEEPSEEK_PRICE["input_per_million"],
            module.DEEPSEEK_PRICE["output_per_million"],
        )
    )
    path = tmp_path / "new-batch.sqlite"
    first = _ledger(path, monkeypatch, fallback_price_digest=prior_price)
    local = _reserve(first, "known-not-sent-preflight")
    first.mark_not_sent(local, reason="before_dispatch_guard")
    first.start_phase("connection")
    first.finish_phase(
        "connection",
        "failed",
        {"error_type": "budget_stopped", "error_code": "unregistered_readiness_provider"},
    )
    first.record_result({"status": "failed", "ledger": {"used_calls": 0}})
    first.close()

    resumed = _ledger(
        path,
        monkeypatch,
        fingerprint="f" * 64,
        fallback_price_digest=current_price,
        resume_known_not_sent=True,
    )

    snapshot = resumed.snapshot()
    assert snapshot["diagnostic_fingerprint"] == "f" * 64
    assert snapshot["preflight_revisions"] == [
        {
            "prior_status": "failed",
            "prior_fingerprint": "a" * 64,
            "attempt_count": 1,
        }
    ]
    assert snapshot["used_calls"] == 0
    assert snapshot["attempts"][0]["dispatch_state"] == "not_sent"
    assert snapshot["attempts"][0]["status"] == "settled"
    assert snapshot["phases"] == []
    assert resumed.prior_result() is None
    assert resumed.start_phase("connection") is None


def test_preflight_resume_never_rebinds_a_dispatched_or_unknown_attempt(tmp_path, monkeypatch):
    path = tmp_path / "new-batch.sqlite"
    ledger = _ledger(path, monkeypatch)
    receipt = _reserve(ledger, "unknown-before-repair")
    ledger.mark_dispatched(receipt)
    ledger.settle_model(
        receipt,
        None,
        None,
        response_state="unknown",
        error_code="provider_timeout",
        error_category="timeout",
    )
    ledger.close()

    with pytest.raises(ValueError, match="only be rebound after known-not-sent"):
        _ledger(
            path, monkeypatch, fingerprint="f" * 64, resume_known_not_sent=True
        )

    preserved = _ledger(path, monkeypatch)
    assert preserved.snapshot()["attempts"][0]["status"] == "unknown"
    assert preserved.snapshot()["diagnostic_fingerprint"] == "a" * 64


def test_changed_readiness_identity_requires_explicit_known_not_sent_recovery(
    tmp_path, monkeypatch
):
    path = tmp_path / "new-batch.sqlite"
    first = _ledger(path, monkeypatch)
    first.close()

    with pytest.raises(ValueError, match="identity changed"):
        _ledger(path, monkeypatch, fingerprint="f" * 64)

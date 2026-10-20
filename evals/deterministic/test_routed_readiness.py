"""The new routed readiness allowance is durable and independent of old Qwen ledgers."""

from decimal import Decimal

import pytest

from decagent.evaluation.business.routed_readiness import (
    DEEPSEEK_PRICE_SOURCE,
    MAX_CALLS,
    MAX_DEEPSEEK_COST,
    MAX_TOTAL_TOKENS,
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
    **extra,
):
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
        **extra,
    )


def test_unknown_is_held_and_only_explicit_primary_recovery_can_continue(tmp_path, monkeypatch):
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

    with pytest.raises(BudgetExceeded, match="unresolved"):
        _reserve(ledger, "unrelated-request")

    retry = _reserve(
        ledger,
        "deepseek-recovery",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        cost="0.1",
        recovery_of=first,
    )
    ledger.mark_dispatched(retry)
    ledger.settle_model(
        retry,
        15,
        Decimal("0.05"),
        response_state="responded",
        server_request_id="gen-dec-1234_abcd",
        resolved_model="deepseek/deepseek-v4.1-flash",
        latency_ms=40,
    )

    next_request = _reserve(ledger, "next-phase")
    snapshot = ledger.snapshot()
    assert next_request != retry
    assert snapshot["used_calls"] == 3
    assert snapshot["held_or_actual_tokens"] == 215
    assert snapshot["deepseek_spend_usd"] == "0.05"
    assert snapshot["attempts"][0]["status"] == "unknown"
    assert snapshot["attempts"][1]["recovery_of"] == first
    assert snapshot["attempts"][1]["server_request_id"] == "gen-dec-1234_abcd"


def test_diagnostic_limits_reject_request_token_overflow_and_deepseek_spend_over_one_usd(
    tmp_path, monkeypatch
):
    ledger = _ledger(tmp_path / "new-batch.sqlite", monkeypatch)

    with pytest.raises(BudgetExceeded, match="input"):
        _reserve(ledger, "too-many-input-tokens", tokens=MAX_TOTAL_TOKENS + 1)

    one = _reserve(
        ledger,
        "deepseek-first",
        provider="openrouter",
        model="deepseek/deepseek-v4.1-flash",
        cost="0.6",
    )
    ledger.mark_dispatched(one)
    ledger.settle_model(
        one, 10, Decimal("0.6"), response_state="responded"
    )
    with pytest.raises(BudgetExceeded, match="DeepSeek"):
        _reserve(
            ledger,
            "deepseek-over-cap",
            provider="openrouter",
            model="deepseek/deepseek-v4.1-flash",
            cost="0.41",
        )
    assert MAX_DEEPSEEK_COST == Decimal("1")


def test_request_count_cap_and_resume_never_reopen_a_dispatched_unknown(tmp_path, monkeypatch):
    path = tmp_path / "new-batch.sqlite"
    ledger = _ledger(path, monkeypatch)
    first = None
    for index in range(MAX_CALLS):
        receipt = _reserve(ledger, f"request-{index}")
        ledger.mark_dispatched(receipt)
        ledger.settle_model(receipt, 10, Decimal(0), response_state="responded")
        first = receipt
    with pytest.raises(BudgetExceeded, match="exhausted"):
        _reserve(ledger, "ninth-request")
    ledger.close()

    restored = _ledger(path, monkeypatch)
    with pytest.raises(BudgetExceeded, match="exhausted"):
        _reserve(restored, "after-restart")
    assert restored.snapshot()["used_calls"] == 8
    assert first is not None


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
        lambda *_args: {
            phase: (lambda phase=phase: {"phase": phase})
            for phase in ("connection", "tool_contract", "plan", "stream")
        },
    )
    profile_digest = "d" * 64

    report = module.run_routed_readiness(
        object(),
        object(),
        ledger,
        model_profile_digest=profile_digest,
        pricing_source=DEEPSEEK_PRICE_SOURCE,
    )

    assert report["model_profile_digest"] == profile_digest
    assert ledger.prior_result()["model_profile_digest"] == profile_digest
    assert ledger.prior_result()["pricing_source"] == DEEPSEEK_PRICE_SOURCE


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

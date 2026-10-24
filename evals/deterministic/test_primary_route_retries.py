"""Bounded primary retries and cross-provider fallback reserve each request separately."""

from decimal import Decimal

import httpx
import pytest
from langchain_core.messages import AIMessage
from openai import APIStatusError, APITimeoutError

from decagent.models.primary import PrimaryModel
from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice


class AttemptLedger:
    def __init__(self, *, fail_after=None):
        self.rows = []
        self.fail_after = fail_after

    def reserve_model(self, key, attempt, provider, tokens, cost, **metadata):
        if self.fail_after is not None and len(self.rows) >= self.fail_after:
            from decagent.storage.budget import BudgetExceeded

            raise BudgetExceeded("test reservation limit")
        row = {
            "id": f"receipt-{len(self.rows) + 1}",
            "key": key,
            "attempt": attempt,
            "provider": provider,
            "tokens": tokens,
            "cost": cost,
            "metadata": metadata,
            "status": "reserved",
        }
        self.rows.append(row)
        return row["id"]

    def mark_dispatched(self, receipt):
        next(row for row in self.rows if row["id"] == receipt)["status"] = "dispatched"

    def mark_not_sent(self, receipt, *, reason, after_local_preflight=False):
        next(row for row in self.rows if row["id"] == receipt)["status"] = "not_sent"

    def settle_model(self, receipt, tokens, cost, *, response_state, error_code=None, **metadata):
        row = next(row for row in self.rows if row["id"] == receipt)
        row.update(
            status=response_state,
            actual_tokens=tokens,
            actual_cost=cost,
            error=error_code,
            **metadata,
        )


class SequenceClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _timeout():
    return APITimeoutError(request=httpx.Request("POST", "https://provider.example/chat"))


def _http_error(status, *, headers=None):
    request = httpx.Request("POST", "https://provider.example/chat")
    response = httpx.Response(status, request=request, headers=headers)
    return APIStatusError("failed", response=response, body={})


def _success(text="ready"):
    return AIMessage(
        content=text,
        tool_calls=[],
        invalid_tool_calls=[],
        usage_metadata={"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        response_metadata={"model_name": "fixture/model", "id": "provider-request-1"},
    )


def _routed(primary_client, fallback_client, *, ledger=None, slept=None):
    ledger = ledger or AttemptLedger()
    slept = slept if slept is not None else []
    primary = PrimaryModel(
        primary_client,
        requested_model="qwen3.8-flash",
        endpoint="https://maas.example/v1",
    )
    fallback = PrimaryModel(
        fallback_client,
        requested_model="deepseek/deepseek-v4.1-flash",
        endpoint="https://openrouter.ai/api/v1",
    )
    model = BudgetedPrimary(
        primary,
        ledger,
        price=TokenPrice("qwen-price", Decimal("1"), Decimal("2")),
        provider="qwen",
        max_output_tokens=128,
        fallback_model=fallback,
        fallback_provider="openrouter",
        fallback_price=TokenPrice("deepseek-price", Decimal("2"), Decimal("3")),
        retry_sleep=lambda delay: slept.append(delay),
    )
    return model, ledger, slept, primary_client, fallback_client


def test_qwen_timeout_retries_once_then_falls_back_with_independent_reservations():
    model, ledger, slept, primary, backup = _routed(
        SequenceClient(_timeout(), _timeout()), SequenceClient(_success("backup"))
    )

    result = model.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "backup"
    assert (primary.calls, backup.calls, slept) == (2, 1, [2])
    assert len(ledger.rows) == 3
    assert [row["provider"] for row in ledger.rows] == ["qwen", "qwen", "openrouter"]
    assert [row["metadata"].get("recovery_of") for row in ledger.rows] == [
        None,
        "receipt-1",
        "receipt-2",
    ]
    assert [row["status"] for row in ledger.rows] == ["unknown", "unknown", "responded"]
    assert all(row["cost"] > 0 for row in ledger.rows)
    assert all(row["metadata"]["requested_output_tokens"] == 128 for row in ledger.rows)
    backup = ledger.rows[-1]
    assert backup["metadata"]["requested_model"] == "deepseek/deepseek-v4.1-flash"
    assert backup["metadata"]["endpoint"] == "https://openrouter.ai/api/v1"
    assert backup["resolved_model"] == "fixture/model"
    assert backup["server_request_id"] == "provider-request-1"
    assert backup["actual_tokens"] == 11
    assert backup["actual_cost"] == Decimal("0.000024")
    assert backup["latency_ms"] >= 0


def test_primary_retry_success_never_dispatches_backup():
    model, ledger, slept, primary, backup = _routed(
        SequenceClient(_timeout(), _success("recovered")), SequenceClient(_success())
    )

    result = model.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "recovered"
    assert (primary.calls, backup.calls, slept) == (2, 0, [2])
    assert len(ledger.rows) == 2 and ledger.rows[1]["metadata"]["recovery_of"] == "receipt-1"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_provider_error_does_not_retry_or_fall_back(status):
    model, ledger, slept, primary, backup = _routed(
        SequenceClient(_http_error(status), _success()), SequenceClient(_success())
    )

    with pytest.raises(Exception) as caught:
        model.respond([{"role": "user", "content": "read only plan"}])

    assert getattr(caught.value, "retryable", None) is False
    assert (primary.calls, backup.calls, slept) == (1, 0, [])
    assert len(ledger.rows) == 1 and ledger.rows[0]["status"] == "unknown"


def test_budget_denial_after_unknown_primary_attempt_does_not_dispatch_retry_or_backup():
    ledger = AttemptLedger(fail_after=1)
    model, _, slept, primary, backup = _routed(
        SequenceClient(_timeout(), _success()), SequenceClient(_success()), ledger=ledger
    )

    from decagent.storage.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        model.respond([{"role": "user", "content": "read only plan"}])

    assert (primary.calls, backup.calls, slept) == (1, 0, [2])
    assert len(ledger.rows) == 1 and ledger.rows[0]["status"] == "unknown"


def test_retry_after_over_pause_threshold_stops_without_sleep_or_fallback():
    model, ledger, slept, primary, backup = _routed(
        SequenceClient(_http_error(429, headers={"retry-after": "31"}), _success()),
        SequenceClient(_success()),
    )

    with pytest.raises(Exception) as caught:
        model.respond([{"role": "user", "content": "read only plan"}])

    assert getattr(caught.value, "retry_after_seconds", None) == 31
    assert (primary.calls, backup.calls, slept) == (1, 0, [])
    assert len(ledger.rows) == 1 and ledger.rows[0]["status"] == "unknown"


def test_retry_after_within_pause_threshold_is_honored_on_retry():
    model, ledger, slept, primary, backup = _routed(
        SequenceClient(_http_error(429, headers={"retry-after": "5"}), _success("recovered")),
        SequenceClient(_success()),
    )

    result = model.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "recovered"
    assert (primary.calls, backup.calls, slept) == (2, 0, [5])
    assert len(ledger.rows) == 2


def test_provider_http_status_is_kept_in_the_attempt_record():
    model, ledger, _, primary, backup = _routed(
        SequenceClient(_http_error(503), _http_error(503)), SequenceClient(_success())
    )

    result = model.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "ready"
    assert primary.calls == 2 and backup.calls == 1
    assert ledger.rows[0]["provider_status"] == 503


def test_primary_native_currency_price_and_actual_usage_are_forwarded_to_trial_ledger():
    ledger = AttemptLedger()
    model = BudgetedPrimary(
        PrimaryModel(
            SequenceClient(_success()),
            requested_model="qwen3.8-flash",
            endpoint="https://maas.example/v1",
        ),
        ledger,
        price=TokenPrice("qwen-price-v1", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        max_output_tokens=128,
    )

    result = model.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "ready"
    assert ledger.rows[0]["metadata"]["trial_cost_currency"] == "CNY"
    assert ledger.rows[0]["metadata"]["trial_price_basis_id"] == "qwen-price-v1"
    settlement = ledger.rows[0]
    assert settlement["actual_input_tokens"] == 9
    assert settlement["actual_output_tokens"] == 2
    assert settlement["trial_actual_cost_native"] == Decimal("0.000013")
    assert settlement["trial_actual_cost_basis"] == "frozen_rate"


def test_readiness_timeout_policy_is_passed_to_each_provider_request():
    from types import SimpleNamespace

    class TimeoutAwareModel:
        requested_model = "qwen3.8-flash"
        endpoint = "https://maas.example/v1"
        schema_bytes = 0
        max_request_timeout = 60

        def __init__(self):
            self.timeouts = []

        def respond(self, messages, *, stream=False, timeout_seconds=None):
            self.timeouts.append(timeout_seconds)
            return SimpleNamespace(
                text="ready",
                usage=SimpleNamespace(input_tokens=9, output_tokens=2, total_tokens=11),
                model="fixture/model",
                server_request_id="request-1",
            )

    model = TimeoutAwareModel()
    budgeted = BudgetedPrimary(
        model,
        AttemptLedger(),
        price=TokenPrice("qwen-price", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        max_output_tokens=128,
        request_timeout=lambda _model: 7.5,
    )

    result = budgeted.respond([{"role": "user", "content": "read only plan"}])

    assert result.text == "ready"
    assert model.timeouts == [pytest.approx(7.5)]


def test_timeout_is_refreshed_after_dispatch_mark_and_budget_policy_latency_is_subtracted(
    monkeypatch,
):
    from decagent.runtime import model_calls

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(model_calls.time, "monotonic", clock.monotonic)
    ledger = AttemptLedger()

    class TimeoutAwareModel:
        requested_model = "qwen3.8-flash"
        endpoint = "https://maas.example/v1"
        schema_bytes = 0
        max_request_timeout = 60

        def __init__(self):
            self.timeouts = []

        def respond(self, messages, *, stream=False, timeout_seconds=None):
            self.timeouts.append(timeout_seconds)
            return SimpleNamespace(
                text="ready",
                usage=SimpleNamespace(input_tokens=9, output_tokens=2, total_tokens=11),
                model="fixture/model",
                server_request_id="request-1",
            )

    from types import SimpleNamespace

    model = TimeoutAwareModel()

    def request_timeout(_model):
        assert ledger.rows[0]["status"] == "dispatched"
        clock.now += 2.0
        return 10.0

    budgeted = BudgetedPrimary(
        model,
        ledger,
        price=TokenPrice("qwen-price", Decimal("1"), Decimal("2"), currency="CNY"),
        provider="qwen",
        max_output_tokens=128,
        request_timeout=request_timeout,
    )

    budgeted.respond([{"role": "user", "content": "read only plan"}])

    assert model.timeouts == [8.0]

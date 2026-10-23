"""Every provider attempt reserves durable tokens and fees before a network request."""

from __future__ import annotations

import inspect
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from decagent.business.guest_jev_pricing import (
    GUEST_JEV_ENDPOINT,
    GUEST_JEV_MAX_CONTEXT_TOKENS,
    GUEST_JEV_MODEL,
    GUEST_JEV_PRICE_BASIS_ID,
    GUEST_JEV_PROVIDER,
    GUEST_JEV_RESERVATION_USD,
)
from decagent.models.primary import (
    PrimaryProviderFailure,
    PrimaryRequestNotDispatched,
    PrimaryResponseRejected,
)
from decagent.storage.budget import BudgetExceeded


def _reserve(ledger, *args, **metadata):
    classified = getattr(ledger, "reserve_model", None)
    if not classified:
        return ledger.reserve(*args)
    parameters = inspect.signature(classified).parameters.values()
    if not any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
        supported = {item.name for item in parameters}
        metadata = {key: value for key, value in metadata.items() if key in supported}
    return classified(*args, **metadata)


def _settle(ledger, receipt, tokens, cost, *, response_state, error_code=None, **metadata):
    classified = getattr(ledger, "settle_model", None)
    if classified:
        parameters = inspect.signature(classified).parameters.values()
        if not any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters):
            supported = {item.name for item in parameters}
            metadata = {key: value for key, value in metadata.items() if key in supported}
        return classified(
            receipt, tokens, cost, response_state=response_state, error_code=error_code,
            **metadata,
        )
    return ledger.settle(receipt, tokens, cost)


def _not_sent(ledger, receipt, reason, *, after_local_preflight=False):
    release = getattr(ledger, "mark_not_sent", None)
    if release:
        return release(receipt, reason=reason, after_local_preflight=after_local_preflight)
    return ledger.settle(receipt, 0, Decimal("0"))


def _dispatched(ledger, receipt):
    mark = getattr(ledger, "mark_dispatched", None)
    if mark:
        mark(receipt)


@dataclass(frozen=True)
class TokenPrice:
    version: str
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str = "USD"

    def __post_init__(self):
        if not self.version or any(
            not rate.is_finite() or rate < 0
            for rate in (self.input_per_million, self.output_per_million)
        ):
            raise ValueError("Versioned, finite nonnegative token prices are required")
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise ValueError("Token price currency must be a three-letter uppercase code")

    def cost(self, input_tokens, output_tokens):
        return (
            self.input_per_million * input_tokens + self.output_per_million * output_tokens
        ) / Decimal(1_000_000)


class BudgetedPrimary:
    def __init__(
        self,
        model,
        ledger,
        *,
        price=None,
        max_output_tokens=2048,
        unknown_cost_reservation=None,
        before_dispatch=None,
        provider="primary",
        purpose="plan",
        request_role=None,
        plan_revision=None,
        node_id=None,
        node_ids=None,
        exception_reason=None,
        fallback_model=None,
        fallback_provider="openrouter",
        fallback_price=None,
        fallback_unknown_cost_reservation=None,
        fallback_requested_model=None,
        fallback_endpoint=None,
        max_same_provider_attempts=2,
        retry_sleep=time.sleep,
        max_retry_after_seconds=30,
        max_input_tokens=65_536,
    ):
        self.model, self.ledger, self.price = model, ledger, price
        self.max_output_tokens = max_output_tokens
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192:
            raise ValueError("Primary output limit must be between one and 8,192")
        if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= 65_536:
            raise ValueError("Primary input limit must be between one and 65,536")
        self.max_input_tokens = max_input_tokens
        self.unknown_cost_reservation = unknown_cost_reservation
        self.before_dispatch = before_dispatch
        self.provider = provider
        self.purpose, self.plan_revision = purpose, plan_revision
        self.request_role = request_role or purpose
        self.node_id, self.node_ids = node_id, node_ids
        self.exception_reason = exception_reason
        if type(max_same_provider_attempts) is not int or not 1 <= max_same_provider_attempts <= 2:
            raise ValueError("Primary model attempts must be between one and two")
        if type(max_retry_after_seconds) is not int or not 0 <= max_retry_after_seconds <= 30:
            raise ValueError("Retry-After pause threshold must be between zero and 30 seconds")
        self.max_same_provider_attempts = max_same_provider_attempts
        self.retry_sleep = retry_sleep
        self.max_retry_after_seconds = max_retry_after_seconds
        self.fallback_model = fallback_model
        self.fallback_provider = fallback_provider
        self.fallback_price = fallback_price
        self.fallback_unknown_cost_reservation = fallback_unknown_cost_reservation
        self.fallback_requested_model = fallback_requested_model
        self.fallback_endpoint = fallback_endpoint

    def respond_plan(self, messages, **attribution):
        """Planner transport hook; format repair remains an independent request."""
        return self.respond(messages, **attribution)

    def respond(
        self,
        messages,
        *,
        stream=False,
        purpose=None,
        plan_revision=None,
        node_id=None,
        node_ids=None,
        exception_reason=None,
        request_guard=None,
    ):
        # UTF-8 bytes conservatively bound textual input plus tool-schema framing.
        payload = json.dumps(messages, ensure_ascii=False).encode()
        request_key = uuid4().hex
        plan_revision = plan_revision if plan_revision is not None else self.plan_revision
        node_id = node_id if node_id is not None else self.node_id
        node_ids = node_ids if node_ids is not None else self.node_ids
        request_purpose = purpose or self.purpose
        request_exception = exception_reason or self.exception_reason
        receipt = None

        def call_route(
            model,
            *,
            provider,
            price,
            unknown_reservation,
            requested_model,
            endpoint,
            attempt,
            recovery_of,
            route_exception,
        ):
            nonlocal receipt
            input_bound = len(payload) + max(16384, getattr(model, "schema_bytes", 0) + 4096)
            if input_bound > self.max_input_tokens:
                raise BudgetExceeded(
                    "Model input exceeds the configured per-request limit",
                    code="model_input_limit_exceeded",
                )
            cost = (
                price.cost(input_bound, self.max_output_tokens)
                if price is not None
                else unknown_reservation
            )
            if cost is None:
                raise BudgetExceeded("Configure a price or explicit unknown-fee reservation")
            reservation_metadata = {
                "model_family": "primary",
                "purpose": request_purpose,
                "plan_revision": plan_revision,
                "node_id": node_id,
                "node_ids": node_ids,
                "exception_reason": route_exception,
                "request_role": self.request_role,
                "requested_model": requested_model,
                "endpoint": endpoint,
                "requested_output_tokens": self.max_output_tokens,
                "trial_cost_currency": price.currency if price is not None else None,
                "trial_cost_native": cost if price is not None else None,
                "trial_price_basis_id": price.version if price is not None else None,
            }
            if recovery_of is not None:
                if not getattr(self.ledger, "reserve_model", None):
                    raise BudgetExceeded(
                        "Ledger cannot safely attribute model recovery", code="unknown_model_cost"
                    )
                reservation_metadata["recovery_of"] = recovery_of
            receipt = _reserve(
                self.ledger,
                request_key,
                attempt,
                provider,
                input_bound + self.max_output_tokens,
                cost,
                **reservation_metadata,
            )
            try:
                if self.before_dispatch:
                    self.before_dispatch()
                if request_guard:
                    request_guard()
            except Exception:
                _not_sent(self.ledger, receipt, "before_dispatch_guard")
                raise
            try:
                _dispatched(self.ledger, receipt)
            except Exception:
                _not_sent(self.ledger, receipt, "dispatch_guard")
                raise
            started = time.monotonic()
            try:
                result = model.respond(messages, stream=stream)
            except PrimaryRequestNotDispatched as rejected:
                _not_sent(self.ledger, receipt, rejected.reason, after_local_preflight=True)
                raise
            except PrimaryResponseRejected as rejected:
                usage = rejected.usage
                actual_cost = (
                    price.cost(usage.input_tokens, usage.output_tokens)
                    if price is not None and usage is not None
                    else None
                )
                _settle(
                    self.ledger,
                    receipt,
                    usage.total_tokens if usage is not None else None,
                    actual_cost,
                    response_state="responded",
                    error_code=rejected.reason,
                    resolved_model=rejected.model,
                    server_request_id=rejected.server_request_id,
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    actual_input_tokens=usage.input_tokens if usage is not None else None,
                    actual_output_tokens=usage.output_tokens if usage is not None else None,
                    trial_actual_cost_native=actual_cost,
                    trial_actual_cost_basis="frozen_rate" if actual_cost is not None else None,
                )
                raise
            except PrimaryProviderFailure as failure:
                _settle(
                    self.ledger,
                    receipt,
                    None,
                    None,
                    response_state="unknown",
                    error_code=(
                        f"provider_http_{failure.status_code}"
                        if failure.status_code is not None
                        else f"provider_{failure.category}"
                    ),
                    server_request_id=failure.server_request_id,
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    error_category=failure.category,
                    provider_status=failure.status_code,
                )
                return None, failure, receipt
            except Exception:
                _settle(
                    self.ledger,
                    receipt,
                    None,
                    None,
                    response_state="unknown",
                    error_code="transport_error",
                    latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    error_category="transport_error",
                )
                raise
            usage = result.usage
            actual_cost = (
                price.cost(usage.input_tokens, usage.output_tokens) if price and usage else None
            )
            _settle(
                self.ledger,
                receipt,
                usage.total_tokens if usage else None,
                actual_cost,
                response_state="responded",
                resolved_model=getattr(result, "model", None),
                server_request_id=getattr(result, "server_request_id", None),
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                actual_input_tokens=usage.input_tokens if usage is not None else None,
                actual_output_tokens=usage.output_tokens if usage is not None else None,
                trial_actual_cost_native=actual_cost,
                trial_actual_cost_basis="frozen_rate" if actual_cost is not None else None,
            )
            return result, None, receipt

        recovery_of = None
        last_failure = None
        attempt = 0
        for provider_attempt in range(1, self.max_same_provider_attempts + 1):
            attempt += 1
            result, failure, receipt = call_route(
                self.model,
                provider=self.provider,
                price=self.price,
                unknown_reservation=self.unknown_cost_reservation,
                requested_model=getattr(self.model, "requested_model", None),
                endpoint=getattr(self.model, "endpoint", None),
                attempt=attempt,
                recovery_of=recovery_of,
                route_exception=(
                    request_exception or "provider_retry"
                    if provider_attempt > 1
                    else request_exception
                ),
            )
            if failure is None:
                return result
            last_failure = failure
            if not failure.retryable:
                raise failure
            if (
                failure.retry_after_seconds is not None
                and failure.retry_after_seconds > self.max_retry_after_seconds
            ):
                raise failure
            recovery_of = receipt
            if provider_attempt < self.max_same_provider_attempts:
                delay = max(2, round(failure.retry_after_seconds or 0))
                self.retry_sleep(delay)

        if self.fallback_model is not None and last_failure is not None and last_failure.retryable:
            if (
                last_failure.retry_after_seconds is not None
                and last_failure.retry_after_seconds > self.max_retry_after_seconds
            ):
                raise last_failure
            attempt += 1
            result, failure, _ = call_route(
                self.fallback_model,
                provider=self.fallback_provider,
                price=self.fallback_price,
                unknown_reservation=self.fallback_unknown_cost_reservation,
                requested_model=self.fallback_requested_model
                or getattr(self.fallback_model, "requested_model", None),
                endpoint=self.fallback_endpoint or getattr(self.fallback_model, "endpoint", None),
                attempt=attempt,
                recovery_of=recovery_of,
                route_exception=request_exception or "provider_fallback",
            )
            if failure is not None:
                raise failure
            return result
        raise last_failure


class LedgerAttemptBudget:
    """Bridge Jev's retry contract to the same task ledger as the primary model."""

    def __init__(
        self,
        ledger,
        *,
        reservation_usd,
        provider="jev",
        request_key=None,
        before_dispatch=None,
        purpose="decision",
        requested_model=None,
        endpoint=None,
        plan_revision=None,
        node_id=None,
        node_ids=None,
    ):
        self.ledger = ledger
        self.reservation_usd = Decimal(str(reservation_usd))
        if not self.reservation_usd.is_finite() or self.reservation_usd <= 0:
            raise ValueError("Positive Jev fee reservation required")
        self.provider, self.request_key = provider, request_key or uuid4().hex
        self.requested_model = requested_model
        self.endpoint = endpoint
        self._model_binding_valid = True
        self.receipts = {}
        self._active_key = None
        self.before_dispatch = before_dispatch
        self.purpose, self.plan_revision = purpose, plan_revision
        self.node_id, self.node_ids = node_id, node_ids

    def bind_model(self, *, model, provider, endpoint=None):
        """Bind reservation metadata to the same model and endpoint as the adapter."""
        requested_model = self.requested_model
        if provider == "openrouter" and requested_model == "jev-latest":
            requested_model = "~typesafe/jev-latest"
        if requested_model is not None and requested_model != model:
            self._model_binding_valid = False
        else:
            self.requested_model = model
        if self.provider not in {"jev", provider}:
            self._model_binding_valid = False
        else:
            self.provider = provider
        if self.endpoint is not None and self.endpoint != endpoint:
            self._model_binding_valid = False
        else:
            self.endpoint = endpoint

    def bind_request_context(self, *, plan_revision, node_ids, purpose="decision_batch"):
        """Called before each synchronous batch, without changing past receipts."""
        self.plan_revision, self.node_ids = plan_revision, tuple(node_ids)
        self.node_id = self.node_ids[0] if len(self.node_ids) == 1 else None
        self.purpose = purpose

    def reserve(self, request, attempt):
        if attempt == 1:
            self._active_key = f"{self.request_key}:{uuid4().hex}"
            self.receipts = {}
        model = self.requested_model or ""
        is_guest = getattr(self.ledger, "trial_budget", None) is not None
        if not self._model_binding_valid:
            return None
        if is_guest and (
            self.provider != GUEST_JEV_PROVIDER
            or self.endpoint != GUEST_JEV_ENDPOINT
            or model != GUEST_JEV_MODEL
            or self.reservation_usd < GUEST_JEV_RESERVATION_USD
        ):
            return None
        tokens = len(json.dumps(request.to_wire(model), ensure_ascii=False).encode()) + 4096
        if is_guest and tokens > GUEST_JEV_MAX_CONTEXT_TOKENS:
            return None
        if self.before_dispatch:
            self.before_dispatch()
        price_basis_id = GUEST_JEV_PRICE_BASIS_ID if is_guest else "jev-reservation-v1"
        try:
            receipt = _reserve(
                self.ledger,
                self._active_key,
                attempt,
                self.provider,
                tokens,
                self.reservation_usd,
                model_family="jev",
                purpose=self.purpose if attempt == 1 else "provider_retry",
                plan_revision=self.plan_revision,
                node_id=self.node_id,
                node_ids=self.node_ids,
                question_count=len(request.questions),
                trial_cost_currency="USD",
                trial_cost_native=self.reservation_usd,
                trial_price_basis_id=price_basis_id,
                requested_model=model or None,
                endpoint=self.endpoint,
            )
        except BudgetExceeded:
            return None
        self.receipts[attempt] = receipt
        try:
            if self.before_dispatch:
                self.before_dispatch()
        except Exception:
            _not_sent(self.ledger, receipt, "before_dispatch_guard")
            raise
        try:
            _dispatched(self.ledger, receipt)
        except Exception:
            _not_sent(self.ledger, receipt, "dispatch_guard")
            raise
        return float(self.reservation_usd)

    def settle(self, record):
        tokens = record.usage.input_tokens + record.usage.output_tokens if record.usage else None
        cost = Decimal(str(record.cost_usd)) if record.cost_usd is not None else None
        _settle(
            self.ledger,
            self.receipts[record.attempt],
            tokens,
            cost,
            response_state="responded" if record.status_code is not None else "unknown",
            error_code=record.error,
            actual_input_tokens=record.usage.input_tokens if record.usage else None,
            actual_output_tokens=record.usage.output_tokens if record.usage else None,
            trial_actual_cost_native=cost,
            trial_actual_cost_basis="provider_billed" if cost is not None else None,
        )

"""Every provider attempt reserves durable tokens and fees before a network request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from decagent.models.primary import PrimaryRequestNotDispatched, PrimaryResponseRejected
from decagent.storage.budget import BudgetExceeded


def _reserve(ledger, *args, **metadata):
    classified = getattr(ledger, "reserve_model", None)
    return classified(*args, **metadata) if classified else ledger.reserve(*args)


def _settle(ledger, receipt, tokens, cost, *, response_state, error_code=None):
    classified = getattr(ledger, "settle_model", None)
    if classified:
        return classified(
            receipt, tokens, cost, response_state=response_state, error_code=error_code
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

    def __post_init__(self):
        if not self.version or any(
            not rate.is_finite() or rate < 0
            for rate in (self.input_per_million, self.output_per_million)
        ):
            raise ValueError("Versioned, finite nonnegative token prices are required")

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
        plan_revision=None,
        node_id=None,
        node_ids=None,
        exception_reason=None,
    ):
        self.model, self.ledger, self.price = model, ledger, price
        self.max_output_tokens = max_output_tokens
        self.unknown_cost_reservation = unknown_cost_reservation
        self.before_dispatch = before_dispatch
        self.provider = provider
        self.purpose, self.plan_revision = purpose, plan_revision
        self.node_id, self.node_ids = node_id, node_ids
        self.exception_reason = exception_reason

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
        input_bound = len(payload) + max(16384, getattr(self.model, "schema_bytes", 0) + 4096)
        cost = (
            self.price.cost(input_bound, self.max_output_tokens)
            if self.price
            else (self.unknown_cost_reservation)
        )
        if cost is None:
            raise BudgetExceeded("Configure a price or explicit unknown-fee reservation")
        receipt = _reserve(
            self.ledger,
            uuid4().hex,
            1,
            self.provider,
            input_bound + self.max_output_tokens,
            cost,
            model_family="primary",
            purpose=purpose or self.purpose,
            plan_revision=plan_revision if plan_revision is not None else self.plan_revision,
            node_id=node_id if node_id is not None else self.node_id,
            node_ids=node_ids if node_ids is not None else self.node_ids,
            exception_reason=exception_reason or self.exception_reason,
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
        try:
            result = self.model.respond(messages, stream=stream)
        except PrimaryRequestNotDispatched as rejected:
            _not_sent(self.ledger, receipt, rejected.reason, after_local_preflight=True)
            raise
        except PrimaryResponseRejected as rejected:
            usage = rejected.usage
            actual_cost = (
                self.price.cost(usage.input_tokens, usage.output_tokens)
                if self.price is not None and usage is not None
                else None
            )
            _settle(
                self.ledger,
                receipt,
                usage.total_tokens if usage is not None else None,
                actual_cost,
                response_state="responded",
                error_code=rejected.reason,
            )
            raise
        except Exception:
            _settle(
                self.ledger,
                receipt,
                None,
                None,
                response_state="unknown",
                error_code="transport_error",
            )
            raise
        usage = result.usage
        actual_cost = (
            self.price.cost(usage.input_tokens, usage.output_tokens)
            if (self.price and usage)
            else None
        )
        _settle(
            self.ledger,
            receipt,
            usage.total_tokens if usage else None,
            actual_cost,
            response_state="responded",
        )
        return result


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
        plan_revision=None,
        node_id=None,
        node_ids=None,
    ):
        self.ledger = ledger
        self.reservation_usd = Decimal(str(reservation_usd))
        if not self.reservation_usd.is_finite() or self.reservation_usd <= 0:
            raise ValueError("Positive Jev fee reservation required")
        self.provider, self.request_key = provider, request_key or uuid4().hex
        self.receipts = {}
        self._active_key = None
        self.before_dispatch = before_dispatch
        self.purpose, self.plan_revision = purpose, plan_revision
        self.node_id, self.node_ids = node_id, node_ids

    def bind_request_context(self, *, plan_revision, node_ids, purpose="decision_batch"):
        """Called before each synchronous batch, without changing past receipts."""
        self.plan_revision, self.node_ids = plan_revision, tuple(node_ids)
        self.node_id = self.node_ids[0] if len(self.node_ids) == 1 else None
        self.purpose = purpose

    def reserve(self, request, attempt):
        if self.before_dispatch:
            self.before_dispatch()
        if attempt == 1:
            self._active_key = f"{self.request_key}:{uuid4().hex}"
            self.receipts = {}
        tokens = len(json.dumps(request.to_wire(""), ensure_ascii=False).encode()) + 4096
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
        )

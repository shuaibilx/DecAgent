"""Dedicated synchronous TypeSafe HTTP client with bounded, accounted attempts."""

import json
import math
import random
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

import httpx

from .contracts import (
    MODEL_ID,
    AttemptRecord,
    DecisionEvaluation,
    DecisionRequest,
    Usage,
    evaluation,
    parse_usage,
    validate_response,
)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "jev-1.13.0"


class AttemptBudget(Protocol):
    def reserve(self, request: DecisionRequest, attempt: int) -> float | None: ...

    def settle(self, record: AttemptRecord) -> None: ...


@dataclass
class CallBudget:
    """Explicit per-call spend cap. Unknown usage consumes the entire reservation."""

    max_usd: float
    reserve_usd: float
    committed_usd: float = field(default=0, init=False)
    _reserved: float = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        for value in (self.max_usd, self.reserve_usd):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Budget and per-attempt reservation must be finite and positive")

    def reserve(self, request: DecisionRequest, attempt: int) -> float | None:
        with self._lock:
            if self.committed_usd + self._reserved + self.reserve_usd > self.max_usd + 1e-12:
                return None
            self._reserved += self.reserve_usd
            return self.reserve_usd

    def settle(self, record: AttemptRecord) -> None:
        with self._lock:
            self._reserved -= record.reserved_usd
            cost = record.cost_usd if record.cost_usd is not None else record.reserved_usd
            self.committed_usd += cost
            if cost > record.reserved_usd + 1e-12:
                raise ValueError("Actual usage exceeded its reservation")


def _strict_json(response):
    def reject_constant(_):
        raise ValueError("Nonfinite JSON number")

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        response.content, parse_constant=reject_constant, object_pairs_hook=reject_duplicates
    )


class JevAdapter:
    name = "jev"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        provider: str = "typesafe",
        client: httpx.Client | None = None,
        budget: AttemptBudget | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        usage_hook: Callable[[AttemptRecord], None] | None = None,
        cost_estimator: Callable[[Usage], float] | None = None,
        cache_ttl: float = 300.0,
        max_cache_entries: int = 256,
    ):
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("Timeout must be positive and no more than ten seconds")
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("At most two retries are permitted")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("A requested model is required")
        if not math.isfinite(cache_ttl) or cache_ttl < 0 or max_cache_entries < 1:
            raise ValueError("Invalid cache bounds")
        if provider not in {"typesafe", "openrouter"}:
            raise ValueError("Unknown Jev provider")
        if provider == "openrouter":
            if model == "jev-latest":
                model = "~typesafe/jev-latest"
            if model != "~typesafe/jev-latest" and not re.fullmatch(
                r"typesafe/jev-\d+\.\d+(?:-\d{8})?", model
            ):
                raise ValueError("OpenRouter requires a TypeSafe Jev model identifier")
        self.provider = provider
        self.endpoint = OPENROUTER_ENDPOINT if provider == "openrouter" else ENDPOINT
        self.model = model
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=False)
        self.budget = budget
        bind_model = getattr(budget, "bind_model", None)
        if bind_model is not None:
            bind_model(model=self.model, provider=self.provider, endpoint=self.endpoint)
        self.timeout, self.max_retries = timeout, max_retries
        self.sleep, self.jitter, self.clock, self.utcnow = sleep, jitter, clock, utcnow
        self.usage_hook, self.cost_estimator = usage_hook, cost_estimator
        self.cache_ttl, self.max_cache_entries = cache_ttl, max_cache_entries
        self._cache = OrderedDict()
        self._resolved = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _cached(self, request, key):
        with self._cache_lock:
            actual = self._resolved.get(key)
            entry = self._cache.get((key, actual))
            if entry is None:
                return None
            saved_at, result = entry
            if self.clock() - saved_at >= self.cache_ttl:
                self._cache.pop((key, actual), None)
                self._resolved.pop(key, None)
                return None
            self._cache.move_to_end((key, actual))
            return replace(result, context=request.context, attempts=(), cached=True)

    def _save_cache(self, key, result):
        if self.cache_ttl == 0:
            return
        with self._cache_lock:
            self._resolved[key] = result.actual_model
            self._cache[(key, result.actual_model)] = (self.clock(), result)
            while len(self._cache) > self.max_cache_entries:
                (old_key, _), _entry = self._cache.popitem(last=False)
                self._resolved.pop(old_key, None)

    def evaluate(self, request: DecisionRequest) -> DecisionEvaluation:
        if not self._api_key:
            return evaluation(request, self.name, self.model, error="missing_key")
        if self.budget is None:
            return evaluation(request, self.name, self.model, error="missing_budget")
        key = request.cache_key(self.model)
        cached = self._cached(request, key)
        if cached is not None:
            return cached
        attempts = []
        for attempt in range(1, self.max_retries + 2):
            try:
                reserved = self.budget.reserve(request, attempt)
            except Exception:
                return evaluation(
                    request, self.name, self.model, attempts=attempts, error="budget_error"
                )
            if reserved is None:
                return evaluation(
                    request, self.name, self.model, attempts=attempts, error="budget_exceeded"
                )
            if isinstance(reserved, bool) or not math.isfinite(reserved) or reserved <= 0:
                return evaluation(
                    request,
                    self.name,
                    self.model,
                    attempts=attempts,
                    error="invalid_budget_reservation",
                )
            started, started_at = self.clock(), self.utcnow().isoformat()
            actual, answers, usage, status, error, retry, cost = (
                None,
                None,
                None,
                None,
                None,
                False,
                None,
            )
            response = None
            try:
                response = self._client.post(
                    self.endpoint,
                    json=request.to_wire(self.model),
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self.timeout,
                    follow_redirects=False,
                )
                status = response.status_code
                payload = None
                try:
                    payload = _strict_json(response)
                    usage = parse_usage(payload)
                    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
                        if MODEL_ID.fullmatch(payload["model"]):
                            actual = payload["model"]
                except (ValueError, UnicodeError):
                    pass
                if status != 200:
                    error = f"http_{status}"
                    retry = status in {429, 529} or 500 <= status <= 599
                else:
                    try:
                        actual, answers, usage = validate_response(payload, request, self.model)
                    except (ValueError, KeyError, TypeError):
                        error = "invalid_response"
                if (
                    self.provider == "openrouter"
                    and isinstance(payload, dict)
                    and isinstance(payload.get("usage"), dict)
                    and "cost" in payload["usage"]
                ):
                    cost = payload["usage"]["cost"]
                    if (
                        isinstance(cost, bool)
                        or not isinstance(cost, (int, float))
                        or not (math.isfinite(cost) and cost >= 0)
                    ):
                        cost, error, retry = None, "invalid_reported_cost", False
                if cost is None and usage is not None and self.cost_estimator is not None:
                    cost = self.cost_estimator(usage)
                    if isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
                        cost, error, retry = None, "invalid_cost_estimate", False
            except httpx.TransportError:
                # A lost response may already be charged; retain its reservation for review.
                error, retry = "network_error", False
            except Exception:
                # Do not persist provider bodies, request headers or exception strings.
                error, retry = "client_error", False
            finally:
                if response is not None:
                    response.close()
                record = AttemptRecord(
                    attempt,
                    self.model,
                    actual,
                    started_at,
                    max(0.0, self.clock() - started),
                    status,
                    error,
                    usage,
                    float(reserved),
                    cost,
                )
                attempts.append(record)
                try:
                    self.budget.settle(record)
                except Exception:
                    error, retry = "budget_error", False
                try:
                    if self.usage_hook is not None:
                        self.usage_hook(record)
                except Exception:
                    error, retry = "audit_error", False
            if error is None:
                result = evaluation(
                    request,
                    self.name,
                    self.model,
                    actual_model=actual,
                    answers=answers,
                    attempts=attempts,
                )
                self._save_cache(key, result)
                return result
            if not retry or attempt > self.max_retries:
                return evaluation(
                    request,
                    self.name,
                    self.model,
                    actual_model=actual,
                    attempts=attempts,
                    error=error,
                )
            # Bounded jitter prevents a custom source from removing the retry bound.
            random_part = min(1.0, max(0.0, float(self.jitter())))
            self.sleep(0.5 * 2 ** (attempt - 1) + 0.25 * random_part)
        raise AssertionError("Unreachable bounded retry loop")

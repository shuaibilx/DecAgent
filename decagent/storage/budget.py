"""Pre-request reservations. Unknown usage stays reserved until explicitly reconciled."""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4


class BudgetExceeded(RuntimeError):
    def __init__(self, message, *, code="budget_exceeded"):
        super().__init__(message)
        self.code = code


@dataclass
class Reservation:
    tokens: int
    cost: Decimal | None
    status: str = "reserved"
    actual_tokens: int | None = None
    actual_cost: Decimal | None = None


@dataclass
class Budget:
    max_calls: int = 30
    max_tokens: int = 100_000
    max_cost: Decimal | None = None
    max_active_seconds: float = 1800
    calls: int = 0
    spent_tokens: int = 0
    spent_cost: Decimal = Decimal("0")
    active_seconds: float = 0
    unknown_price_calls: int = 0
    reservations: dict[str, Reservation] = field(default_factory=dict)
    _active_since: float | None = None

    def __post_init__(self):
        if self.max_calls <= 0 or self.max_tokens <= 0 or self.max_active_seconds <= 0:
            raise ValueError("Budget bounds must be positive")
        if self.max_cost is not None and (not self.max_cost.is_finite() or self.max_cost <= 0):
            raise ValueError("Cost bound must be finite and positive")

    @property
    def reserved_cost(self):
        return sum(
            (
                max(r.cost or Decimal("0"), r.actual_cost or Decimal("0"))
                for r in self.reservations.values()
                if r.status != "settled"
            ),
            Decimal("0"),
        )

    @property
    def reserved_tokens(self):
        return sum(
            max(r.tokens, r.actual_tokens or 0)
            for r in self.reservations.values()
            if r.status != "settled"
        )

    def start_active(self):
        if self._active_since is None:
            self._active_since = time.monotonic()

    def pause_active(self):
        if self._active_since is not None:
            self.active_seconds += time.monotonic() - self._active_since
            self._active_since = None

    def check_time(self):
        current = self.active_seconds + (
            time.monotonic() - self._active_since if self._active_since is not None else 0
        )
        if current >= self.max_active_seconds:
            raise BudgetExceeded("Active task time budget exhausted")

    def reserve(self, tokens: int, cost: Decimal | None, allow_unknown_price=False):
        self.check_time()
        if tokens <= 0 or (cost is not None and (not cost.is_finite() or cost < 0)):
            raise ValueError("Invalid request reservation")
        if cost is None and not allow_unknown_price:
            raise BudgetExceeded("Unknown price requires explicit bounded-call authorization")
        if (
            self.calls >= self.max_calls
            or self.spent_tokens + self.reserved_tokens + tokens > self.max_tokens
        ):
            raise BudgetExceeded("Call/token budget exhausted before request")
        if (
            cost is not None
            and self.max_cost is not None
            and self.spent_cost + self.reserved_cost + cost > self.max_cost
        ):
            raise BudgetExceeded("Cost budget exhausted before request")
        self.calls += 1
        self.unknown_price_calls += int(cost is None)
        key = uuid4().hex
        self.reservations[key] = Reservation(tokens, cost)
        return key

    def settle(self, key: str, tokens: int | None, cost: Decimal | None):
        reservation = self.reservations[key]
        if reservation.status == "settled":
            raise ValueError("Reservation is already settled")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Invalid actual token usage")
        if cost is not None and (not cost.is_finite() or cost < 0):
            raise ValueError("Invalid actual usage")
        if tokens is not None:
            reservation.actual_tokens = max(tokens, reservation.actual_tokens or 0)
        if cost is not None:
            reservation.actual_cost = max(cost, reservation.actual_cost or Decimal("0"))
        if reservation.actual_tokens is None or reservation.actual_cost is None:
            reservation.status = "unknown"
            return
        self.spent_tokens += reservation.actual_tokens
        self.spent_cost += reservation.actual_cost
        reservation.status = "settled"

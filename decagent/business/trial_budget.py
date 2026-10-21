"""Persistent, atomic admission and budget reservations for anonymous trials.

This ledger is separate from the public benchmark ledger. It records provider
API charges before vouchers and never dispatches a model request itself.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from decagent.business.workspaces import parse_workspace_id

BEIJING = ZoneInfo("Asia/Shanghai")
FX_MAX_AGE_DAYS = 7
FX_SAFETY_MARGIN_BPS = 1000
USD_QUANTUM = Decimal("0.000001")
NATIVE_COST_QUANTUM = Decimal("0.000000000001")
FX_RATE_QUANTUM = Decimal("0.000001")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}\Z")
_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}\Z")


class BudgetPolicyError(ValueError):
    """A missing, stale, or invalid input makes budget enforcement unsafe."""


class TrialBudgetExceeded(RuntimeError):
    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


def _decimal(value, name, *, minimum=Decimal("0")):
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BudgetPolicyError(f"{name} must be a decimal value") from exc
    if not result.is_finite() or result < minimum:
        raise BudgetPolicyError(f"{name} must be finite and at least {minimum}")
    return result


def _ceil(value, quantum):
    return value.quantize(quantum, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FxSnapshot:
    source_date: date
    cny_per_usd: Decimal
    source_url: str
    safety_margin_bps: int = FX_SAFETY_MARGIN_BPS

    def __post_init__(self):
        if type(self.source_date) is not date:
            raise BudgetPolicyError("FX source date must be a calendar date")
        rate = _decimal(self.cny_per_usd, "FX rate", minimum=FX_RATE_QUANTUM).quantize(
            FX_RATE_QUANTUM, rounding=ROUND_DOWN
        )
        object.__setattr__(self, "cny_per_usd", rate)
        if (
            type(self.safety_margin_bps) is not int
            or not FX_SAFETY_MARGIN_BPS <= self.safety_margin_bps < 10_000
        ):
            raise BudgetPolicyError("FX safety margin must be at least 10% and less than 100%")
        parsed = urlsplit(self.source_url) if isinstance(self.source_url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "www.chinamoney.com.cn"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(self.source_url) > 2048
        ):
            raise BudgetPolicyError("FX source must be an HTTPS ChinaMoney/CFETS URL")

    def effective_rate(self, *, as_of: date):
        if type(as_of) is not date:
            raise BudgetPolicyError("FX evaluation date must be a calendar date")
        age = (as_of - self.source_date).days
        if age < 0 or age > FX_MAX_AGE_DAYS:
            raise BudgetPolicyError("FX quote is future-dated or older than seven days")
        factor = Decimal(10_000 - self.safety_margin_bps) / Decimal(10_000)
        result = (self.cny_per_usd * factor).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if result <= 0:
            raise BudgetPolicyError("FX safety margin leaves no usable rate")
        return result

    def cny_to_usd(self, amount, *, as_of: date):
        value = _decimal(amount, "CNY amount")
        return _ceil(value / self.effective_rate(as_of=as_of), USD_QUANTUM)


@dataclass(frozen=True)
class TrialBudgetPolicy:
    daily_cost_usd: Decimal = Decimal("5.00")
    daily_qwen_tokens: int = 5_000_000
    guest_daily_tasks: int = 30
    guest_daily_qwen_tokens: int = 500_000
    fx_snapshot: FxSnapshot | None = None

    def __post_init__(self):
        amount = _decimal(self.daily_cost_usd, "Daily USD budget", minimum=USD_QUANTUM)
        object.__setattr__(
            self, "daily_cost_usd", amount.quantize(USD_QUANTUM, rounding=ROUND_DOWN)
        )
        for name in ("daily_qwen_tokens", "guest_daily_tasks", "guest_daily_qwen_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise BudgetPolicyError(f"{name} must be a positive integer")
        if self.fx_snapshot is not None and not isinstance(self.fx_snapshot, FxSnapshot):
            raise BudgetPolicyError("FX snapshot must use the verified snapshot type")

    @classmethod
    def from_settings(cls, settings):
        snapshot = None
        if settings.guest_trial_fx_cny_per_usd is not None:
            snapshot = FxSnapshot(
                source_date=settings.guest_trial_fx_source_date,
                cny_per_usd=settings.guest_trial_fx_cny_per_usd,
                source_url=settings.guest_trial_fx_source_url,
                safety_margin_bps=settings.guest_trial_fx_safety_margin_bps,
            )
        return cls(
            daily_cost_usd=settings.guest_trial_daily_cost_usd,
            daily_qwen_tokens=settings.guest_trial_daily_qwen_tokens,
            guest_daily_tasks=settings.guest_trial_daily_tasks_per_guest,
            guest_daily_qwen_tokens=settings.guest_trial_daily_qwen_tokens_per_guest,
            fx_snapshot=snapshot,
        )


class TrialBudgetLedger:
    """Short PostgreSQL transactions for guest tasks and model-call reservations."""

    def __init__(self, database, policy: TrialBudgetPolicy, *, clock=None):
        self.database = database
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(BEIJING))

    def _budget_date(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise BudgetPolicyError("Budget clock must return an aware datetime")
        return now.astimezone(BEIJING).date()

    def _ensure_day(self, conn, budget_date):
        snapshot = self.policy.fx_snapshot
        fx_values = (None, None, None, FX_SAFETY_MARGIN_BPS, None)
        if snapshot is not None:
            # Persist even an old source quote for audit; CNY reservations validate freshness.
            factor = Decimal(10_000 - snapshot.safety_margin_bps) / Decimal(10_000)
            effective = (snapshot.cny_per_usd * factor).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            if effective <= 0:
                raise BudgetPolicyError("Configured FX rate is too small after safety margin")
            fx_values = (
                snapshot.source_date,
                snapshot.source_url,
                snapshot.cny_per_usd,
                snapshot.safety_margin_bps,
                effective,
            )
        conn.execute(
            "INSERT INTO business.guest_trial_budget_days "
            "(budget_date,daily_cost_limit_usd,daily_qwen_token_limit,fx_source_date,"
            "fx_source_url,fx_cny_per_usd,fx_margin_bps,fx_effective_cny_per_usd) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(budget_date) DO NOTHING",
            (
                budget_date,
                self.policy.daily_cost_usd,
                self.policy.daily_qwen_tokens,
                *fx_values,
            ),
        )
        row = conn.execute(
            "SELECT * FROM business.guest_trial_budget_days WHERE budget_date=%s FOR UPDATE",
            (budget_date,),
        ).fetchone()
        if (
            row["daily_cost_limit_usd"] != self.policy.daily_cost_usd
            or row["daily_qwen_token_limit"] != self.policy.daily_qwen_tokens
        ):
            raise BudgetPolicyError("Daily trial limits cannot change after the bucket is created")
        return row

    @staticmethod
    def _require_active_guest_workspace(conn, workspace_id):
        workspace_id = parse_workspace_id(workspace_id)
        workspace = conn.execute(
            "SELECT kind,active FROM business.workspaces WHERE id=%s FOR SHARE",
            (workspace_id,),
        ).fetchone()
        if not workspace or workspace["kind"] != "guest" or not workspace["active"]:
            raise BudgetPolicyError("An active guest workspace is required")
        return workspace_id

    def _ensure_workspace(self, conn, budget_date, workspace_id):
        workspace_id = self._require_active_guest_workspace(conn, workspace_id)
        conn.execute(
            "INSERT INTO business.guest_trial_workspace_usage "
            "(budget_date,workspace_id,task_limit,qwen_token_limit) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(budget_date,workspace_id) DO NOTHING",
            (
                budget_date,
                workspace_id,
                self.policy.guest_daily_tasks,
                self.policy.guest_daily_qwen_tokens,
            ),
        )
        row = conn.execute(
            "SELECT * FROM business.guest_trial_workspace_usage "
            "WHERE budget_date=%s AND workspace_id=%s FOR UPDATE",
            (budget_date, workspace_id),
        ).fetchone()
        if (
            row["task_limit"] != self.policy.guest_daily_tasks
            or row["qwen_token_limit"] != self.policy.guest_daily_qwen_tokens
        ):
            raise BudgetPolicyError("Per-guest limits cannot change after the bucket is created")
        return row

    @staticmethod
    def _lock_idempotency_key(conn, workspace_id, *, kind, key):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"guest-trial:{workspace_id}:{kind}:{key}",),
        )

    @staticmethod
    def _validate_ids(workspace_id, task_id):
        try:
            return parse_workspace_id(workspace_id), UUID(str(task_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise BudgetPolicyError("Invalid guest workspace or task identifier") from exc

    def admit_task(self, *, workspace_id, task_id):
        workspace_id, task_id = self._validate_ids(workspace_id, task_id)
        budget_date = self._budget_date()
        with self.database.connect() as conn:
            self._lock_idempotency_key(conn, workspace_id, kind="task", key=task_id)
            self._require_active_guest_workspace(conn, workspace_id)
            existing = conn.execute(
                "SELECT 1 FROM business.guest_trial_task_admissions "
                "WHERE workspace_id=%s AND task_id=%s",
                (workspace_id, task_id),
            ).fetchone()
            if existing:
                return False
            self._ensure_day(conn, budget_date)
            usage = self._ensure_workspace(conn, budget_date, workspace_id)
            if usage["task_count"] >= usage["task_limit"]:
                raise TrialBudgetExceeded("Daily guest task limit reached", code="guest_task_limit")
            conn.execute(
                "UPDATE business.guest_trial_workspace_usage SET task_count=task_count+1 "
                "WHERE budget_date=%s AND workspace_id=%s",
                (budget_date, workspace_id),
            )
            conn.execute(
                "INSERT INTO business.guest_trial_task_admissions "
                "(budget_date,workspace_id,task_id) VALUES(%s,%s,%s)",
                (budget_date, workspace_id, task_id),
            )
        return True

    def _cost_usd(self, day, currency, amount, budget_date):
        native = _decimal(amount, "Reserved native cost")
        native = _ceil(native, NATIVE_COST_QUANTUM)
        if currency == "USD":
            return native, _ceil(native, USD_QUANTUM)
        if currency != "CNY":
            raise BudgetPolicyError("Only USD and CNY charges are supported by trial budgets")
        if day["fx_source_date"] is None:
            raise BudgetPolicyError("No verified FX snapshot is frozen for this budget day")
        snapshot = FxSnapshot(
            source_date=day["fx_source_date"],
            cny_per_usd=day["fx_cny_per_usd"],
            source_url=day["fx_source_url"],
            safety_margin_bps=day["fx_margin_bps"],
        )
        return native, snapshot.cny_to_usd(native, as_of=budget_date)

    def reserve_request(
        self,
        *,
        workspace_id,
        task_id,
        request_key,
        provider,
        model,
        cost_currency,
        reserved_cost,
        reserved_qwen_tokens,
        price_basis_id,
    ):
        workspace_id, task_id = self._validate_ids(workspace_id, task_id)
        if not isinstance(request_key, str) or not _REQUEST_KEY.fullmatch(request_key):
            raise BudgetPolicyError("Invalid reservation request key")
        if (
            not isinstance(provider, str)
            or not _IDENTIFIER.fullmatch(provider)
            or not isinstance(model, str)
            or not _IDENTIFIER.fullmatch(model)
        ):
            raise BudgetPolicyError("Invalid provider or model identifier")
        if not isinstance(price_basis_id, str) or not _IDENTIFIER.fullmatch(price_basis_id):
            raise BudgetPolicyError("A verified price basis identifier is required")
        currency = cost_currency.upper() if isinstance(cost_currency, str) else ""
        if currency not in {"USD", "CNY"}:
            raise BudgetPolicyError("Only USD and CNY charges are supported by trial budgets")
        if type(reserved_qwen_tokens) is not int or reserved_qwen_tokens < 0:
            raise BudgetPolicyError("Qwen token reservation must be a nonnegative integer")
        if provider.casefold() != "qwen" and reserved_qwen_tokens:
            raise BudgetPolicyError("Only Qwen requests may reserve Qwen tokens")
        if reserved_cost is None:
            raise BudgetPolicyError("Unknown price has no reliable reservation upper bound")
        native_cost = _ceil(_decimal(reserved_cost, "Reserved native cost"), NATIVE_COST_QUANTUM)
        budget_date = self._budget_date()

        with self.database.connect() as conn:
            self._lock_idempotency_key(conn, workspace_id, kind="request", key=request_key)
            self._require_active_guest_workspace(conn, workspace_id)
            existing = conn.execute(
                "SELECT * FROM business.guest_trial_usage_reservations "
                "WHERE workspace_id=%s AND request_key=%s FOR UPDATE",
                (workspace_id, request_key),
            ).fetchone()
            if existing:
                expected = (
                    existing["task_id"] == task_id
                    and existing["provider"] == provider
                    and existing["model"] == model
                    and existing["cost_currency"] == currency
                    and existing["reserved_cost_native"] == native_cost
                    and existing["reserved_qwen_tokens"] == reserved_qwen_tokens
                    and existing["price_basis_id"] == price_basis_id
                )
                if not expected:
                    raise BudgetPolicyError(
                        "Request key is already bound to a different reservation"
                    )
                return existing

            day = self._ensure_day(conn, budget_date)
            self._ensure_workspace(conn, budget_date, workspace_id)
            native_cost, cost_usd = self._cost_usd(day, currency, native_cost, budget_date)

            updated_day = conn.execute(
                "UPDATE business.guest_trial_budget_days SET "
                "reserved_cost_usd=reserved_cost_usd+%s, "
                "qwen_tokens_reserved=qwen_tokens_reserved+%s "
                "WHERE budget_date=%s "
                "AND reserved_cost_usd+%s<=daily_cost_limit_usd "
                "AND qwen_tokens_reserved+%s<=daily_qwen_token_limit "
                "RETURNING budget_date",
                (cost_usd, reserved_qwen_tokens, budget_date, cost_usd, reserved_qwen_tokens),
            ).fetchone()
            if not updated_day:
                if day["reserved_cost_usd"] + cost_usd > day["daily_cost_limit_usd"]:
                    raise TrialBudgetExceeded(
                        "Daily trial cost limit reached", code="daily_cost_limit"
                    )
                raise TrialBudgetExceeded(
                    "Daily Qwen token limit reached", code="daily_qwen_token_limit"
                )

            updated_workspace = conn.execute(
                "UPDATE business.guest_trial_workspace_usage SET "
                "qwen_tokens_reserved=qwen_tokens_reserved+%s "
                "WHERE budget_date=%s AND workspace_id=%s "
                "AND qwen_tokens_reserved+%s<=qwen_token_limit "
                "RETURNING workspace_id",
                (reserved_qwen_tokens, budget_date, workspace_id, reserved_qwen_tokens),
            ).fetchone()
            if not updated_workspace:
                raise TrialBudgetExceeded(
                    "Daily guest Qwen token limit reached", code="guest_qwen_token_limit"
                )

            return conn.execute(
                "INSERT INTO business.guest_trial_usage_reservations "
                "(id,budget_date,workspace_id,task_id,request_key,provider,model,cost_currency,"
                "reserved_cost_native,reserved_cost_usd,reserved_qwen_tokens,price_basis_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    budget_date,
                    workspace_id,
                    task_id,
                    request_key,
                    provider,
                    model,
                    currency,
                    native_cost,
                    cost_usd,
                    reserved_qwen_tokens,
                    price_basis_id,
                ),
            ).fetchone()

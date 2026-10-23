"""Persistent, atomic admission and budget reservations for anonymous trials.

This ledger is separate from the public benchmark ledger. It records provider
API charges before vouchers and never dispatches a model request itself.
"""

import re
from contextlib import nullcontext
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
_TRIAL_ERROR_CATEGORIES = frozenset(
    {
        "network_error",
        "timeout",
        "rate_limited",
        "provider_error",
        "response_invalid",
        "usage_unknown",
        "unknown_error",
    }
)


def _trial_error_category(category, provider_status):
    """Map detailed provider transport failures to the bounded guest-ledger taxonomy."""
    if category is None:
        return category
    if type(category) is not str:
        return "unknown_error"
    if category in _TRIAL_ERROR_CATEGORIES:
        return category
    if category == "connection":
        return "network_error"
    if category == "transient_http":
        if provider_status == 408:
            return "timeout"
        if provider_status == 429:
            return "rate_limited"
        return "provider_error"
    if category in {"permanent_http", "provider_error"}:
        return "provider_error"
    return "unknown_error"


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

    def usage_status(self, *, workspace_id):
        """Return persisted trial usage without creating or changing a budget bucket."""
        budget_date = self._budget_date()
        workspace_id = parse_workspace_id(workspace_id)
        with self.database.connect() as conn:
            self._require_active_guest_workspace(conn, workspace_id)
            day = conn.execute(
                "SELECT daily_cost_limit_usd,reserved_cost_usd,daily_qwen_token_limit,"
                "qwen_tokens_reserved FROM business.guest_trial_budget_days "
                "WHERE budget_date=%s",
                (budget_date,),
            ).fetchone()
            usage = conn.execute(
                "SELECT task_limit,task_count,qwen_token_limit,qwen_tokens_reserved "
                "FROM business.guest_trial_workspace_usage "
                "WHERE budget_date=%s AND workspace_id=%s",
                (budget_date, workspace_id),
            ).fetchone()

        return {
            "budget_date": budget_date.isoformat(),
            "global": {
                "daily_cost_limit_usd": str(
                    day["daily_cost_limit_usd"] if day else self.policy.daily_cost_usd
                ),
                "cost_reserved_usd": str(day["reserved_cost_usd"] if day else Decimal("0")),
                "daily_qwen_token_limit": int(
                    day["daily_qwen_token_limit"] if day else self.policy.daily_qwen_tokens
                ),
                "qwen_tokens_reserved": int(day["qwen_tokens_reserved"] if day else 0),
            },
            "guest": {
                "daily_task_limit": int(
                    usage["task_limit"] if usage else self.policy.guest_daily_tasks
                ),
                "tasks_admitted": int(usage["task_count"] if usage else 0),
                "daily_qwen_token_limit": int(
                    usage["qwen_token_limit"] if usage else self.policy.guest_daily_qwen_tokens
                ),
                "qwen_tokens_reserved": int(usage["qwen_tokens_reserved"] if usage else 0),
            },
        }

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

    def admit_task(self, *, workspace_id, task_id, connection=None):
        workspace_id, task_id = self._validate_ids(workspace_id, task_id)
        budget_date = self._budget_date()
        with self._transaction(connection) as conn:
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
        attempt=1,
        provider,
        model,
        cost_currency,
        reserved_cost,
        reserved_qwen_tokens,
        price_basis_id,
        usage_record_id=None,
        connection=None,
    ):
        workspace_id, task_id = self._validate_ids(workspace_id, task_id)
        if not isinstance(request_key, str) or not _REQUEST_KEY.fullmatch(request_key):
            raise BudgetPolicyError("Invalid reservation request key")
        if type(attempt) is not int or attempt < 1:
            raise BudgetPolicyError("Attempt number must be a positive integer")
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
        if usage_record_id is not None:
            try:
                usage_record_id = UUID(str(usage_record_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise BudgetPolicyError("Invalid usage record identifier") from exc
        if provider.casefold() not in {"qwen", "dashscope"} and reserved_qwen_tokens:
            raise BudgetPolicyError("Only Qwen requests may reserve Qwen tokens")
        if reserved_cost is None:
            raise BudgetPolicyError("Unknown price has no reliable reservation upper bound")
        native_cost = _ceil(_decimal(reserved_cost, "Reserved native cost"), NATIVE_COST_QUANTUM)
        budget_date = self._budget_date()

        with self._transaction(connection) as conn:
            self._lock_idempotency_key(
                conn, workspace_id, kind="request", key=f"{request_key}:{attempt}"
            )
            self._require_active_guest_workspace(conn, workspace_id)
            existing = conn.execute(
                "SELECT * FROM business.guest_trial_usage_reservations "
                "WHERE workspace_id=%s AND request_key=%s AND attempt=%s FOR UPDATE",
                (workspace_id, request_key, attempt),
            ).fetchone()
            if existing:
                expected = (
                    existing["task_id"] == task_id
                    and existing["attempt"] == attempt
                    and existing["provider"] == provider
                    and existing["model"] == model
                    and existing["cost_currency"] == currency
                    and existing["reserved_cost_native"] == native_cost
                    and existing["reserved_qwen_tokens"] == reserved_qwen_tokens
                    and existing["price_basis_id"] == price_basis_id
                    and existing["usage_record_id"] == usage_record_id
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
                "(id,budget_date,workspace_id,task_id,request_key,attempt,provider,model,cost_currency,"
                "reserved_cost_native,reserved_cost_usd,reserved_qwen_tokens,price_basis_id,"
                "usage_record_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    budget_date,
                    workspace_id,
                    task_id,
                    request_key,
                    attempt,
                    provider,
                    model,
                    currency,
                    native_cost,
                    cost_usd,
                    reserved_qwen_tokens,
                    price_basis_id,
                    usage_record_id,
                ),
            ).fetchone()

    def _transaction(self, connection):
        return self.database.connect() if connection is None else nullcontext(connection)

    def mark_dispatched(self, reservation_id, *, connection=None):
        try:
            reservation_id = UUID(str(reservation_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise BudgetPolicyError("Invalid model reservation identifier") from exc
        with self._transaction(connection) as conn:
            row = conn.execute(
                "SELECT status FROM business.guest_trial_usage_reservations WHERE id=%s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if not row or row["status"] != "reserved":
                raise BudgetPolicyError("Only a reserved model attempt can be dispatched")
            return conn.execute(
                "UPDATE business.guest_trial_usage_reservations "
                "SET status='dispatched',dispatched_at=clock_timestamp() WHERE id=%s "
                "RETURNING *",
                (reservation_id,),
            ).fetchone()

    def settle_request(
        self,
        reservation_id,
        *,
        response_state,
        actual_input_tokens=None,
        actual_output_tokens=None,
        actual_cost_native=None,
        actual_cost_basis=None,
        voucher_credit_native=None,
        cash_paid_native=None,
        billing_evidence_source=None,
        server_request_id=None,
        latency_ms=None,
        error_category=None,
        provider_status=None,
        connection=None,
    ):
        try:
            reservation_id = UUID(str(reservation_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise BudgetPolicyError("Invalid model reservation identifier") from exc
        if response_state not in {"responded", "unknown"}:
            raise BudgetPolicyError("Invalid provider response state")
        for name, value in (
            ("Input tokens", actual_input_tokens),
            ("Output tokens", actual_output_tokens),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise BudgetPolicyError(f"{name} must be a nonnegative integer")
        if actual_cost_native is not None:
            actual_cost_native = _ceil(
                _decimal(actual_cost_native, "Actual native cost"), NATIVE_COST_QUANTUM
            )
            if actual_cost_basis not in {"provider_billed", "frozen_rate"}:
                raise BudgetPolicyError("Known cost requires a provider or frozen-rate basis")
        elif actual_cost_basis is not None:
            raise BudgetPolicyError("Unknown cost cannot claim a billing basis")
        for name, value in (
            ("Voucher credit", voucher_credit_native),
            ("Cash paid", cash_paid_native),
        ):
            if value is not None:
                value = _ceil(_decimal(value, name), NATIVE_COST_QUANTUM)
                if name == "Voucher credit":
                    voucher_credit_native = value
                else:
                    cash_paid_native = value
        if billing_evidence_source not in {None, "provider_response", "invoice"}:
            raise BudgetPolicyError("Invalid billing evidence source")
        if (voucher_credit_native is not None or cash_paid_native is not None) and (
            billing_evidence_source is None
        ):
            raise BudgetPolicyError("Voucher and cash figures require billing evidence")
        if server_request_id is not None and (
            not isinstance(server_request_id, str) or not _IDENTIFIER.fullmatch(server_request_id)
        ):
            raise BudgetPolicyError("Invalid server request identifier")
        if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
            raise BudgetPolicyError("Latency must be a nonnegative integer")
        if provider_status is not None and (
            type(provider_status) is not int or not 100 <= provider_status <= 599
        ):
            raise BudgetPolicyError("Invalid provider HTTP status")
        error_category = _trial_error_category(error_category, provider_status)

        with self._transaction(connection) as conn:
            row = conn.execute(
                "SELECT * FROM business.guest_trial_usage_reservations WHERE id=%s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if not row or row["status"] != "dispatched":
                raise BudgetPolicyError("Only a dispatched model attempt can be settled")
            actual_cost_usd = None
            if actual_cost_native is not None:
                day = conn.execute(
                    "SELECT * FROM business.guest_trial_budget_days "
                    "WHERE budget_date=%s FOR UPDATE",
                    (row["budget_date"],),
                ).fetchone()
                _, actual_cost_usd = self._cost_usd(
                    day, row["cost_currency"], actual_cost_native, row["budget_date"]
                )

            complete = (
                response_state == "responded"
                and actual_input_tokens is not None
                and actual_output_tokens is not None
                and actual_cost_native is not None
            )
            if not complete:
                return conn.execute(
                    "UPDATE business.guest_trial_usage_reservations SET status='unknown',"
                    "actual_input_tokens=COALESCE(%s,actual_input_tokens),"
                    "actual_output_tokens=COALESCE(%s,actual_output_tokens),"
                    "actual_cost_native=COALESCE(%s,actual_cost_native),"
                    "actual_cost_usd=COALESCE(%s,actual_cost_usd),"
                    "actual_cost_basis=COALESCE(%s,actual_cost_basis),"
                    "voucher_credit_native=COALESCE(%s,voucher_credit_native),"
                    "cash_paid_native=COALESCE(%s,cash_paid_native),"
                    "billing_evidence_source=COALESCE(%s,billing_evidence_source),"
                    "server_request_id=COALESCE(%s,server_request_id),"
                    "latency_ms=COALESCE(%s,latency_ms),"
                    "error_category=COALESCE(%s,error_category),settled_at=clock_timestamp() "
                    "WHERE id=%s RETURNING *",
                    (
                        actual_input_tokens,
                        actual_output_tokens,
                        actual_cost_native,
                        actual_cost_usd,
                        actual_cost_basis,
                        voucher_credit_native,
                        cash_paid_native,
                        billing_evidence_source,
                        server_request_id,
                        latency_ms,
                        error_category or "usage_unknown",
                        reservation_id,
                    ),
                ).fetchone()

            actual_qwen_tokens = (
                actual_input_tokens + actual_output_tokens
                if row["provider"].casefold() in {"qwen", "dashscope"}
                else 0
            )
            committed_cost_usd = actual_cost_usd
            cost_delta = committed_cost_usd - row["reserved_cost_usd"]
            token_delta = actual_qwen_tokens - row["reserved_qwen_tokens"]
            conn.execute(
                "UPDATE business.guest_trial_budget_days SET "
                "reserved_cost_usd=reserved_cost_usd+%s, "
                "qwen_tokens_reserved=qwen_tokens_reserved+%s WHERE budget_date=%s",
                (cost_delta, token_delta, row["budget_date"]),
            )
            if row["provider"].casefold() in {"qwen", "dashscope"}:
                conn.execute(
                    "UPDATE business.guest_trial_workspace_usage SET "
                    "qwen_tokens_reserved=qwen_tokens_reserved+%s "
                    "WHERE budget_date=%s AND workspace_id=%s",
                    (token_delta, row["budget_date"], row["workspace_id"]),
                )
            return conn.execute(
                "UPDATE business.guest_trial_usage_reservations SET status='settled',"
                "actual_input_tokens=%s,actual_output_tokens=%s,actual_cost_native=%s,"
                "actual_cost_usd=%s,actual_cost_basis=%s,voucher_credit_native=%s,"
                "cash_paid_native=%s,billing_evidence_source=%s,server_request_id=%s,"
                "latency_ms=%s,error_category=%s,reservation_exceeded=%s,"
                "settled_at=clock_timestamp() WHERE id=%s RETURNING *",
                (
                    actual_input_tokens,
                    actual_output_tokens,
                    actual_cost_native,
                    actual_cost_usd,
                    actual_cost_basis,
                    voucher_credit_native,
                    cash_paid_native,
                    billing_evidence_source,
                    server_request_id,
                    latency_ms,
                    error_category,
                    actual_cost_usd > row["reserved_cost_usd"]
                    or actual_qwen_tokens > row["reserved_qwen_tokens"],
                    reservation_id,
                ),
            ).fetchone()

    def mark_not_sent(
        self, reservation_id, *, reason, after_local_preflight=False, connection=None
    ):
        try:
            reservation_id = UUID(str(reservation_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise BudgetPolicyError("Invalid model reservation identifier") from exc
        if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
            raise BudgetPolicyError("Invalid local non-dispatch reason")
        if type(after_local_preflight) is not bool:
            raise BudgetPolicyError("Local dispatch proof must be explicit")
        with self._transaction(connection) as conn:
            row = conn.execute(
                "SELECT * FROM business.guest_trial_usage_reservations WHERE id=%s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            can_release = row and (
                row["status"] == "reserved"
                or (row["status"] == "dispatched" and after_local_preflight)
            )
            if not can_release:
                raise BudgetPolicyError(
                    "A dispatched reservation requires proof that no provider request was sent"
                )
            conn.execute(
                "UPDATE business.guest_trial_budget_days SET "
                "reserved_cost_usd=reserved_cost_usd-%s, "
                "qwen_tokens_reserved=qwen_tokens_reserved-%s WHERE budget_date=%s",
                (row["reserved_cost_usd"], row["reserved_qwen_tokens"], row["budget_date"]),
            )
            if row["reserved_qwen_tokens"]:
                conn.execute(
                    "UPDATE business.guest_trial_workspace_usage SET "
                    "qwen_tokens_reserved=qwen_tokens_reserved-%s "
                    "WHERE budget_date=%s AND workspace_id=%s",
                    (
                        row["reserved_qwen_tokens"],
                        row["budget_date"],
                        row["workspace_id"],
                    ),
                )
            return conn.execute(
                "UPDATE business.guest_trial_usage_reservations SET status='not_sent',"
                "actual_input_tokens=0,actual_output_tokens=0,not_sent_reason=%s,"
                "settled_at=clock_timestamp() WHERE id=%s RETURNING *",
                (reason, reservation_id),
            ).fetchone()


def make_task_usage_ledger(database, settings, task, *, clock=None):
    """Attach guest admission and spend controls to every business task ledger."""
    from decagent.storage.usage import UsageLedger

    task_id = task.get("id") if isinstance(task, dict) else task
    with database.connect() as conn:
        row = conn.execute(
            "SELECT t.workspace_id,w.kind,w.active FROM app.tasks AS t "
            "LEFT JOIN business.workspaces AS w ON w.id=t.workspace_id WHERE t.id=%s",
            (task_id,),
        ).fetchone()
    if row is None:
        raise KeyError("Task not found")
    if row["kind"] != "guest":
        return UsageLedger(database, task_id)

    trial_budget = TrialBudgetLedger(
        database, TrialBudgetPolicy.from_settings(settings), clock=clock
    )
    return UsageLedger(
        database,
        task_id,
        trial_budget=trial_budget,
        guest_workspace_id=row["workspace_id"],
        guest_dispatch_disabled=not settings.guest_trial_enabled or not row["active"],
    )

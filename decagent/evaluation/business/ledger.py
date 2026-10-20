"""One durable cumulative Jev cap across calibration, probes, and benchmarks."""

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from decagent.business.codec import strict_json

PRIOR_SPEND = Decimal("0.000253176")
CAP = Decimal("1")
GLOBAL_JEV_LEDGER_RELATIVE_PATH = Path(
    ".decagent/business-eval/global-jev-ledger.json"
)


def _amount(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Spend must be a finite nonnegative amount")
    return amount


def canonical_global_jev_ledger_path(root):
    """Return the sole project-scoped Jev ledger path used by public evaluation."""
    return Path(root).resolve() / GLOBAL_JEV_LEDGER_RELATIVE_PATH


def validate_global_jev_ledger_path(root, candidate):
    """Reject alternate paths and symlinked path components before any ledger action."""
    project_root = Path(root).resolve()
    canonical = canonical_global_jev_ledger_path(project_root)
    requested = Path(candidate)
    if not requested.is_absolute():
        requested = project_root / requested
    if requested.resolve(strict=False) != canonical:
        raise ValueError("Public evaluation requires the canonical project Jev ledger path")
    current = project_root
    for component in GLOBAL_JEV_LEDGER_RELATIVE_PATH.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("Public evaluation Jev ledger path cannot contain symlinks")
    return canonical


def open_existing_global_jev_ledger(root):
    """Open and validate the existing cumulative ledger without creating or resetting it."""
    path = validate_global_jev_ledger_path(
        root, canonical_global_jev_ledger_path(root)
    )
    if not path.exists():
        raise FileNotFoundError(
            "Existing project Jev ledger is required; initialization is forbidden"
        )
    if not path.is_file() or path.is_symlink():
        raise ValueError("Project Jev ledger must be an existing regular file")
    ledger = GlobalJevLedger(path)
    ledger.snapshot()
    return ledger


class GlobalJevLedger:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            yield
        finally:
            os.close(descriptor)
            lock.unlink()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(), self.path.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": "business-global-jev-ledger-1",
                    "cap_usd": str(CAP),
                    "prior_spend_usd": str(PRIOR_SPEND),
                    "attempts": [],
                },
                stream,
                indent=2,
            )

    def _read(self):
        value = strict_json(self.path.read_text(encoding="utf-8"))
        if (
            value.get("schema_version") != "business-global-jev-ledger-1"
            or _amount(value["cap_usd"]) != CAP
            or _amount(value["prior_spend_usd"]) != PRIOR_SPEND
            or not isinstance(value.get("attempts"), list)
        ):
            raise ValueError("Global Jev ledger identity or historical spend changed")
        return value

    def _write(self, value):
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    @staticmethod
    def _totals(value):
        spent, held = PRIOR_SPEND, Decimal(0)
        blocked = False
        for row in value["attempts"]:
            reserve = _amount(row["reserved_usd"])
            if row["status"] == "settled":
                spent += _amount(row["cost_usd"])
                blocked = blocked or row.get("reservation_exceeded", False)
            else:
                held += max(reserve, _amount(row.get("cost_usd") or 0))
                blocked = True
        return spent, held, blocked

    def snapshot(self):
        with self._locked():
            value = self._read()
        spent, held, blocked = self._totals(value)
        return {
            **value,
            "spent_usd": str(spent),
            "held_usd": str(held),
            "remaining_usd": str(max(Decimal(0), CAP - spent - held)),
            "blocked_unknown_or_pending": blocked,
            "within_cap": spent + held <= CAP,
        }

    def reserve(self, amount, attribution):
        amount = _amount(amount)
        if amount <= 0:
            raise ValueError("Positive per-attempt reservation required")
        with self._locked():
            value = self._read()
            spent, held, blocked = self._totals(value)
            if blocked or spent + held + amount > CAP:
                return None
            key = uuid4().hex
            value["attempts"].append(
                {
                    "id": key,
                    "status": "pending",
                    "reserved_usd": str(amount),
                    "cost_usd": None,
                    "tokens": None,
                    "actual_model": None,
                    "attribution": attribution,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write(value)
            return key

    def settle(self, key, *, tokens, cost, actual_model):
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Usage tokens must be a nonnegative integer")
        cost = _amount(cost) if cost is not None else None
        with self._locked():
            value = self._read()
            row = next((r for r in value["attempts"] if r["id"] == key), None)
            if row is None or row["status"] != "pending":
                raise ValueError("Unknown or already settled global reservation")
            row.update(
                tokens=tokens,
                cost_usd=str(cost) if cost is not None else None,
                actual_model=actual_model,
                status="settled" if cost is not None and tokens is not None else "unknown",
                reservation_exceeded=cost is not None and cost > _amount(row["reserved_usd"]),
            )
            self._write(value)
            if row["reservation_exceeded"]:
                raise ValueError("Actual Jev cost exceeded reserved bound; ledger preserved")

    def mark_unknown_if_pending(self, key):
        """Fail closed after a public-ledger owner takes over an interrupted Jev call."""
        with self._locked():
            value = self._read()
            row = next((item for item in value["attempts"] if item["id"] == key), None)
            if row is None:
                raise ValueError("Global Jev reservation is missing")
            if row["status"] == "pending":
                row.update(status="unknown", tokens=None, cost_usd=None, actual_model=None)
                self._write(value)
            return row["status"]


class GlobalAttemptBudget:
    """Compose the shared cap with the runtime task ledger, before HTTP dispatch."""

    def __init__(self, global_ledger, local_budget=None, *, reservation, attribution):
        self.global_ledger, self.local = global_ledger, local_budget
        self.reservation, self.attribution = _amount(reservation), attribution
        self.keys = {}

    def reserve(self, request, attempt):
        key = self.global_ledger.reserve(
            self.reservation,
            {
                **self.attribution,
                "attempt": attempt,
                "dataset": request.context.dataset_version,
                "request_hash": request.cache_key("global-cap"),
            },
        )
        if key is None:
            return None
        try:
            local = self.local.reserve(request, attempt) if self.local else float(self.reservation)
        except Exception:
            self.global_ledger.settle(key, tokens=0, cost=Decimal(0), actual_model=None)
            raise
        if local is None:
            self.global_ledger.settle(key, tokens=0, cost=Decimal(0), actual_model=None)
            return None
        self.keys[attempt] = key
        return float(self.reservation)

    def settle(self, record):
        tokens = record.usage.input_tokens + record.usage.output_tokens if record.usage else None
        try:
            self.global_ledger.settle(
                self.keys[record.attempt],
                tokens=tokens,
                cost=record.cost_usd,
                actual_model=record.actual_model,
            )
        finally:
            if self.local:
                self.local.settle(record)

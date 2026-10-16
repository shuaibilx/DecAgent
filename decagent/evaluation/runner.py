"""Durable budget reservations and experiment progress, independent of model transport.

The caller supplies trusted owner confirmation. Data in a manifest is not confirmation.
Each provider invocation, including retries, must use BudgetedCalls.call. A crashed or
timed-out request remains charged at its reservation until billing is reconciled.
"""

import json
import os
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from decagent.decisions.contracts import canonical_json

from .contracts import RunAuthorization, RunManifest, amount, money


class BudgetExhausted(RuntimeError):
    pass


@contextmanager
def file_lock(path):
    # The lock file is stable across atomic replacements of the progress file.
    with Path(str(path) + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".benchmark-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class CallTicket:
    attempt_id: str
    reserved_usd: str
    reserved_tokens: int
    requested_model: str
    expected_actual_model: str


@dataclass(frozen=True)
class CallOutcome:
    value: object
    actual_model: str | None
    cost_usd: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProgressStore:
    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def create(cls, path, manifest: RunManifest, *, authorization: RunAuthorization | None = None):
        if manifest.paid and (
            authorization is None or authorization.manifest_sha256 != manifest.sha256
        ):
            raise ValueError("Paid benchmark requires exact manifest authorization")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            if path.exists():
                raise FileExistsError("Progress exists; resume without overwriting it")
            atomic_json(
                path,
                {
                    "format_version": "1",
                    "manifest": manifest.to_dict(),
                    "manifest_sha256": manifest.sha256,
                    "authorization": asdict(authorization) if authorization else None,
                    "samples": {
                        f"{s}/{i}": {"status": "pending", "result": None}
                        for s in manifest.strategies
                        for i in manifest.sample_ids
                    },
                    "attempts": {},
                    "budget": {
                        "committed_usd": "0",
                        "committed_tokens": 0,
                        "calls": 0,
                        "exhausted": False,
                    },
                },
            )
        return cls(path)

    @classmethod
    def open(cls, path):
        store = cls(path)
        store.snapshot()
        return store

    def _read(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        manifest = RunManifest.from_dict(data["manifest"])
        if data.get("format_version") != "1" or manifest.sha256 != data["manifest_sha256"]:
            raise ValueError("Corrupt or changed experiment manifest")
        if manifest.paid:
            auth = RunAuthorization(**(data.get("authorization") or {}))
            if auth.manifest_sha256 != manifest.sha256:
                raise ValueError("Missing or changed experiment authorization")
        return data, manifest

    @contextmanager
    def _transaction(self):
        with file_lock(self.path):
            data, manifest = self._read()
            yield data, manifest
            atomic_json(self.path, data)

    def snapshot(self):
        with file_lock(self.path):
            data, _ = self._read()
            return data

    @staticmethod
    def _has_capacity(data, manifest):
        budget = data["budget"]
        return (
            not budget["exhausted"]
            and budget["calls"] < manifest.max_calls
            and budget["committed_tokens"] < manifest.max_tokens
            and (not manifest.paid or amount(budget["committed_usd"]) < amount(manifest.max_usd))
        )

    def start_sample(self, strategy, sample_id):
        with self._transaction() as (data, manifest):
            cell = data["samples"][f"{strategy}/{sample_id}"]
            if cell["status"] in {"unknown", "model_version_changed"}:
                raise ValueError("Interrupted sample requires reconciliation before reuse")
            if cell["status"] != "pending":
                raise ValueError("Sample already started or completed")
            if not self._has_capacity(data, manifest):
                raise BudgetExhausted("No budget for a new sample")
            cell["status"] = "running"

    def reserve(self, strategy, sample_id, role, max_input_tokens, max_output_tokens):
        if role not in {"primary", "judge", "extraction", "compression"}:
            raise ValueError("Unknown paid call role")
        for value in (max_input_tokens, max_output_tokens):
            if type(value) is not int or value < 0:
                raise ValueError("Reservation needs nonnegative bounded token counts")
        tokens = max_input_tokens + max_output_tokens
        if tokens == 0:
            raise ValueError("A request cannot reserve zero tokens")
        with self._transaction() as (data, manifest):
            cell = data["samples"][f"{strategy}/{sample_id}"]
            if cell["status"] != "running":
                raise ValueError("Requests require a running sample")
            budget = data["budget"]
            binding = manifest.judges[strategy] if role == "judge" else manifest.primary_model
            basis = (
                manifest.model_prices[binding.actual]
                if manifest.model_prices is not None
                else manifest.price
            )
            reserved = basis.reserve(max_input_tokens, max_output_tokens) if basis else amount("0")
            if (
                not self._has_capacity(data, manifest)
                or budget["committed_tokens"] + tokens > manifest.max_tokens
                or (
                    manifest.paid
                    and amount(budget["committed_usd"]) + reserved > amount(manifest.max_usd)
                )
            ):
                raise BudgetExhausted("Request reservation exceeds the run cap")
            ticket = CallTicket(
                uuid.uuid4().hex, money(reserved), tokens, binding.requested, binding.actual
            )
            data["attempts"][ticket.attempt_id] = {
                **asdict(ticket),
                "strategy": strategy,
                "sample_id": sample_id,
                "role": role,
                "status": "reserved",
                "cost_usd": None,
                "input_tokens": None,
                "output_tokens": None,
                "actual_model": None,
                "accounted_usd": ticket.reserved_usd,
                "accounted_tokens": tokens,
                "started_at": datetime.now(UTC).isoformat(),
            }
            budget["calls"] += 1
            budget["committed_tokens"] += tokens
            budget["committed_usd"] = money(amount(budget["committed_usd"]) + reserved)
            return ticket

    def settle(self, attempt_id, *, cost_usd, input_tokens, output_tokens, actual_model):
        values = {
            "cost_usd": money(cost_usd) if cost_usd is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "actual_model": actual_model,
        }
        for token in (input_tokens, output_tokens):
            if token is not None and (type(token) is not int or token < 0):
                raise ValueError("Invalid usage tokens")
        with self._transaction() as (data, manifest):
            attempt = data["attempts"][attempt_id]
            if attempt["status"] == "settled":
                if any(attempt[k] != v for k, v in values.items()):
                    raise ValueError("conflicting settlement cannot replace a real charge")
                return
            budget = data["budget"]
            charged = amount(attempt["reserved_usd"])
            if cost_usd is not None:
                charged = amount(cost_usd)
            budget["committed_usd"] = money(
                amount(budget["committed_usd"]) - amount(attempt["accounted_usd"]) + charged
            )
            attempt["accounted_usd"] = money(charged)
            if input_tokens is not None and output_tokens is not None:
                budget["committed_tokens"] += (
                    input_tokens + output_tokens - attempt["accounted_tokens"]
                )
                attempt["accounted_tokens"] = input_tokens + output_tokens
            attempt.update(values)
            attempt["status"] = "settled" if cost_usd is not None else "unknown"
            budget["exhausted"] = budget["committed_tokens"] > manifest.max_tokens or (
                manifest.paid and amount(budget["committed_usd"]) > amount(manifest.max_usd)
            )
            if actual_model is not None and actual_model != attempt["expected_actual_model"]:
                data["samples"][f"{attempt['strategy']}/{attempt['sample_id']}"]["status"] = (
                    "model_version_changed"
                )

    def mark_unknown(self, attempt_id):
        with self._transaction() as (data, _):
            attempt = data["attempts"][attempt_id]
            if attempt["status"] == "reserved":
                attempt["status"] = "unknown"

    def finish_sample(self, strategy, sample_id, result):
        status = result.get("status")
        if status not in {"completed", "failed", "skipped", "unknown", "succeeded"}:
            raise ValueError("Result requires an explicit completion status")
        with self._transaction() as (data, _):
            cell = data["samples"][f"{strategy}/{sample_id}"]
            if cell["status"] != "running":
                raise ValueError("Only running samples can finish")
            # Caller supplies already redacted observations; never provider exception text.
            canonical_json(result)
            cell.update(status=status, result=result)

    def recover_interrupted(self):
        with self._transaction() as (data, _):
            for cell in data["samples"].values():
                if cell["status"] == "running":
                    cell["status"] = "unknown"
            for attempt in data["attempts"].values():
                if attempt["status"] == "reserved":
                    attempt["status"] = "unknown"


class BudgetedCalls:
    def __init__(self, store: ProgressStore, strategy, sample_id):
        self.store, self.strategy, self.sample_id = store, strategy, sample_id

    def call(self, role, max_input_tokens, max_output_tokens, provider: Callable):
        ticket = self.store.reserve(
            self.strategy, self.sample_id, role, max_input_tokens, max_output_tokens
        )
        try:
            result = provider(ticket)
            if not isinstance(result, CallOutcome):
                raise ValueError("Provider must return an explicit usage outcome")
            self.store.settle(
                ticket.attempt_id,
                cost_usd=result.cost_usd,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                actual_model=result.actual_model,
            )
            return result.value
        except BaseException:
            self.store.mark_unknown(ticket.attempt_id)
            raise


def run_pending(store: ProgressStore, execute: Callable):
    """Execute pending cells with injected transport; no provider is loaded implicitly.

    Unknown cells are never retried here. Transport retries must call the supplied
    BudgetedCalls again, so every attempt is reserved and visible in the ledger.
    """
    for key, cell in store.snapshot()["samples"].items():
        if cell["status"] != "pending":
            continue
        strategy, sample_id = key.split("/", 1)
        try:
            store.start_sample(strategy, sample_id)
        except BudgetExhausted:
            break
        try:
            result = execute(strategy, sample_id, BudgetedCalls(store, strategy, sample_id))
        except BudgetExhausted:
            store.finish_sample(strategy, sample_id, {"status": "unknown", "reason": "budget_cap"})
            break
        except Exception as exc:
            store.finish_sample(
                strategy, sample_id, {"status": "failed", "reason": type(exc).__name__}
            )
            continue
        current = store.snapshot()["samples"][key]["status"]
        if current == "running":
            store.finish_sample(strategy, sample_id, result)
    return store.snapshot()

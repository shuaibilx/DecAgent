"""New, isolated and durable allowance for routed-model readiness diagnostics.

This ledger is separate from the earlier Qwen readiness authorization. It stores
only request metadata, usage and bounded error categories; prompts, responses and
credentials never enter the evidence store.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from decagent.decisions.contracts import canonical_json
from decagent.storage.budget import BudgetExceeded

AUTHORIZATION_ID = "primary-readiness-routed-v1"
AUTHORIZATION_ANCHOR = (
    Path(__file__).resolve().parents[3] / ".decagent" / "model-readiness-authorizations.sqlite"
)
MAX_CALLS = 8
MAX_TOTAL_TOKENS = 300_000
MAX_REQUEST_INPUT_TOKENS = 65_536
MAX_OUTPUT_TOKENS = 8_192
MAX_DEEPSEEK_COST = Decimal("1")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/_-]{0,254}\Z")
_RETRYABLE = frozenset({"timeout", "connection", "transient_http"})
DEEPSEEK_PRICE_VERSION = "openrouter-endpoints-api-2026-10-19-peak"
DEEPSEEK_PRICE_SOURCE = (
    "https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"
)
DEEPSEEK_PRICE_BOUND_PROVIDER = "BaseTen"
DEEPSEEK_PRICE = {
    "input_per_million": Decimal("0.60"),
    "output_per_million": Decimal("2.40"),
}
_LEGACY_DEEPSEEK_PRICE_VERSION = "openrouter-models-api-2026-10-19-peak"
_PRIMARY_ENDPOINT_HOST = "maas.qianwenaiapi.com"
_PRIMARY_ENDPOINT_PATH = "/compatible-mode/v1"
_PRIMARY_PROVIDER = "qwen"
_READINESS_CONTEXT = {
    "instruction": "Read the current service directory with the registered read-only "
    "directory capability, then complete only with the verified directory evidence. "
    "Propose a finite read-only plan; do not perform any business action."
}
_READINESS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"instruction": {"type": "string"}},
}
_PING_TOOL_NAME = "decagent_readiness_ping"
_PING_TOOL = {
    "type": "function",
    "function": {
        "name": _PING_TOOL_NAME,
        "description": "Return the fixed readiness contract value without executing tools.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ready"]}},
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid diagnostic endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Diagnostic endpoint must be a clean HTTPS URL")
    return value


def _safe_value(value, name):
    if type(value) is not str or _SAFE_VALUE.fullmatch(value) is None:
        raise ValueError(f"Invalid {name}")
    return value


class SqliteRoutedDiagnosticLedger:
    """Fixed batch caps, per-provider reservations and crash-safe unknown records."""

    def __init__(self, path, identity):
        self.path, self.identity = Path(path), identity
        self._closed = False

    @classmethod
    def open(
        cls,
        path,
        *,
        diagnostic_fingerprint,
        primary_model,
        primary_endpoint,
        fallback_model,
        fallback_endpoint,
        primary_price_digest,
        fallback_price_digest,
        primary_provider="qwen",
        resume_known_not_sent=False,
    ):
        if type(resume_known_not_sent) is not bool:
            raise ValueError("Known-not-sent recovery flag must be boolean")
        if (
            type(diagnostic_fingerprint) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", diagnostic_fingerprint)
        ):
            raise ValueError("Diagnostic source fingerprint must be SHA-256")
        _safe_value(primary_model, "primary model")
        _safe_value(fallback_model, "fallback model")
        if not isinstance(primary_provider, str) or _IDENTIFIER.fullmatch(primary_provider) is None:
            raise ValueError("Invalid primary provider")
        primary_endpoint, fallback_endpoint = _endpoint(primary_endpoint), _endpoint(
            fallback_endpoint
        )
        primary_url = urlsplit(primary_endpoint)
        if (
            primary_url.hostname != "maas.qianwenaiapi.com"
            or primary_url.path != "/compatible-mode/v1"
        ):
            raise ValueError("Readiness primary endpoint is outside the approved Qwen route")
        for value, label in (
            (primary_price_digest, "primary price digest"),
            (fallback_price_digest, "fallback price digest"),
        ):
            if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
                raise ValueError(f"{label} must be SHA-256")
        identity = {
            "schema_version": "routed-model-readiness-ledger-v1",
            "authorization_id": AUTHORIZATION_ID,
            "diagnostic_fingerprint": diagnostic_fingerprint,
            "primary_model": primary_model,
            "primary_provider": primary_provider,
            "primary_endpoint": primary_endpoint,
            "fallback_model": fallback_model,
            "fallback_provider": "openrouter",
            "fallback_endpoint": fallback_endpoint,
            "primary_price_digest": primary_price_digest,
            "fallback_price_digest": fallback_price_digest,
            "max_calls": MAX_CALLS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "max_request_input_tokens": MAX_REQUEST_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_deepseek_cost_usd": str(MAX_DEEPSEEK_COST),
        }
        target_input = Path(path).expanduser()
        anchor_input = Path(AUTHORIZATION_ANCHOR).expanduser()
        if target_input.is_symlink() or anchor_input.is_symlink():
            raise ValueError("Diagnostic ledger paths cannot be symbolic links")
        target = target_input.resolve()
        anchor = anchor_input.resolve()
        if target == anchor:
            raise ValueError(
                "Diagnostic ledger and fixed authorization anchor must be separate files"
            )
        anchor.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(anchor, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bindings "
                "(authorization_id TEXT PRIMARY KEY, ledger_path TEXT NOT NULL)"
            )
            prior = connection.execute(
                "SELECT ledger_path FROM bindings WHERE authorization_id=?", (AUTHORIZATION_ID,)
            ).fetchone()
            if prior is not None and Path(prior[0]) != target:
                raise ValueError("Routed readiness authorization is bound to another ledger")
            if prior is not None and not target.is_file():
                raise ValueError("Bound routed readiness ledger is missing; allowance stays spent")
            ledger = cls(target, identity)
            ledger._initialize(resume_known_not_sent=resume_known_not_sent)
            if prior is None:
                connection.execute(
                    "INSERT INTO bindings VALUES(?,?)", (AUTHORIZATION_ID, str(target))
                )
            connection.commit()
            return ledger
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _connect(self):
        if self._closed:
            raise ValueError("Routed diagnostic ledger is closed")
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self, *, resume_known_not_sent=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS identity(
                        id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS attempts(
                        receipt TEXT PRIMARY KEY,
                        request_digest TEXT NOT NULL UNIQUE,
                        attempt INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        requested_model TEXT NOT NULL,
                        resolved_model TEXT,
                        endpoint TEXT NOT NULL,
                        request_role TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        requested_tokens INTEGER NOT NULL,
                        requested_output_tokens INTEGER NOT NULL,
                        reserved_cost TEXT NOT NULL,
                        actual_tokens INTEGER,
                        actual_cost TEXT,
                        status TEXT NOT NULL CHECK(status IN ('reserved','unknown','settled')),
                        dispatch_state TEXT NOT NULL CHECK(
                          dispatch_state IN (
                            'reserved','dispatching','not_sent','responded','unknown'
                          )
                        ),
                        error_code TEXT,
                        error_category TEXT,
                        provider_status INTEGER,
                        latency_ms INTEGER,
                        server_request_id TEXT,
                        recovery_of TEXT REFERENCES attempts(receipt),
                        overrun INTEGER NOT NULL DEFAULT 0,
                        created_at_utc TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS readiness_single_recovery
                      ON attempts(recovery_of) WHERE recovery_of IS NOT NULL"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS result(
                        id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS phases(
                        phase TEXT PRIMARY KEY CHECK(phase IN (
                          'connection','tool_contract','plan','stream'
                        )),
                        status TEXT NOT NULL CHECK(
                          status IN ('started','passed','failed','skipped')
                        ),
                        report TEXT
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS preflight_revisions(
                        revision INTEGER PRIMARY KEY AUTOINCREMENT,
                        prior_identity TEXT NOT NULL,
                        prior_phases TEXT NOT NULL,
                        prior_result TEXT,
                        attempt_count INTEGER NOT NULL,
                        created_at_utc TEXT NOT NULL
                    )"""
                )
                row = connection.execute("SELECT value FROM identity WHERE id=1").fetchone()
                expected = canonical_json(self.identity)
                if row is None:
                    connection.execute("INSERT INTO identity VALUES(1,?)", (expected,))
                elif row["value"] != expected:
                    if not resume_known_not_sent:
                        raise ValueError(
                            "Routed readiness identity changed; original ledger preserved"
                        )
                    self._rebind_known_not_sent(connection, row["value"], expected)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _rebind_known_not_sent(self, connection, prior_identity, expected_identity):
        """Allow source-only repair after a proven local preflight failure, preserving history."""
        prior = json.loads(prior_identity)
        expected = json.loads(expected_identity)
        changed = {
            key for key in set(prior) | set(expected) if prior.get(key) != expected.get(key)
        }
        allowed_source_only = changed == {"diagnostic_fingerprint"}
        allowed_price_source_correction = changed == {
            "diagnostic_fingerprint",
            "fallback_price_digest",
        } and prior.get("fallback_price_digest") == _legacy_deepseek_price_digest()
        if allowed_price_source_correction:
            from decagent.runtime.model_calls import TokenPrice

            expected_price = _price_digest(
                TokenPrice(
                    DEEPSEEK_PRICE_VERSION,
                    DEEPSEEK_PRICE["input_per_million"],
                    DEEPSEEK_PRICE["output_per_million"],
                )
            )
            allowed_price_source_correction = (
                expected.get("fallback_price_digest") == expected_price
            )
        if (
            prior.get("authorization_id") != AUTHORIZATION_ID
            or not (allowed_source_only or allowed_price_source_correction)
        ):
            raise ValueError("Known-not-sent recovery cannot change the frozen model profile")
        attempts = self._rows(connection)
        if any(
            row["status"] != "settled"
            or row["dispatch_state"] != "not_sent"
            or row["actual_tokens"] != 0
            or row["actual_cost"] != "0"
            for row in attempts
        ):
            raise ValueError(
                "Routed readiness can only be rebound after known-not-sent attempts"
            )
        phases = [
            dict(row) for row in connection.execute("SELECT * FROM phases ORDER BY rowid")
        ]
        if any(phase["status"] not in {"failed", "skipped"} for phase in phases):
            raise ValueError("Only a failed local readiness preflight can be rebound")
        result_row = connection.execute("SELECT value FROM result WHERE id=1").fetchone()
        prior_result = result_row["value"] if result_row else None
        if prior_result is not None and json.loads(prior_result).get("status") != "failed":
            raise ValueError("Only a failed readiness result can be rebound")
        connection.execute(
            "INSERT INTO preflight_revisions(prior_identity,prior_phases,prior_result,"
            "attempt_count,created_at_utc) VALUES(?,?,?,?,?)",
            (
                prior_identity,
                canonical_json(phases),
                prior_result,
                len(attempts),
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute("DELETE FROM result")
        connection.execute("DELETE FROM phases")
        connection.execute("UPDATE identity SET value=? WHERE id=1", (expected_identity,))

    @contextmanager
    def _transaction(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT value FROM identity WHERE id=1").fetchone()
                if row is None or row["value"] != canonical_json(self.identity):
                    raise ValueError("Routed readiness identity changed")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _rows(connection):
        return [dict(row) for row in connection.execute("SELECT * FROM attempts ORDER BY rowid")]

    @staticmethod
    def _unresolved(rows):
        return [
            row for row in rows
            if row["status"] == "unknown" or row["dispatch_state"] == "dispatching"
        ]

    @staticmethod
    def _held_tokens(rows):
        return sum(
            0 if row["dispatch_state"] == "not_sent"
            else row["actual_tokens"] if row["status"] == "settled"
            else row["requested_tokens"]
            for row in rows
        )

    def _deepseek_spend(self, rows):
        model = self.identity["fallback_model"]
        return sum(
            (
                Decimal(row["actual_cost"])
                if row["status"] == "settled" and row["actual_cost"] is not None
                else Decimal(row["reserved_cost"])
                for row in rows
                if row["requested_model"] == model and row["dispatch_state"] != "not_sent"
            ),
            Decimal(0),
        )

    def _has_successful_recovery(self, row, rows):
        by_id = {item["receipt"]: item for item in rows}
        for candidate in rows:
            if candidate["status"] != "settled" or candidate["dispatch_state"] != "responded":
                continue
            if candidate["actual_tokens"] is None or candidate["actual_cost"] is None:
                continue
            current = candidate
            visited = set()
            while current["recovery_of"] is not None:
                parent = current["recovery_of"]
                if parent in visited:
                    break
                visited.add(parent)
                if parent == row["receipt"]:
                    return True
                current = by_id.get(parent)
                if current is None:
                    break
        return False

    def _valid_recovery(self, recovery_of, rows, *, recovery_receipt=None):
        by_id = {row["receipt"]: row for row in rows}
        current = by_id.get(str(recovery_of))
        if (
            current is None
            or current["status"] != "unknown"
            or current["dispatch_state"] != "unknown"
            or current["error_category"] not in _RETRYABLE
        ):
            return False
        ancestors = set()
        while current is not None:
            if current["receipt"] in ancestors or current["provider"] not in {
                self.identity["primary_provider"], "openrouter"
            }:
                return False
            ancestors.add(current["receipt"])
            current = by_id.get(current["recovery_of"]) if current["recovery_of"] else None
        children = [row for row in rows if row["recovery_of"] == str(recovery_of)]
        if children and not (
            len(children) == 1
            and recovery_receipt is not None
            and children[0]["receipt"] == recovery_receipt
            and children[0]["status"] == "reserved"
        ):
            return False
        unresolved = self._unresolved(rows)
        return bool(unresolved) and all(row["receipt"] in ancestors for row in unresolved)

    def _check_unresolved(self, rows, *, recovery_of=None, recovery_receipt=None):
        unresolved = self._unresolved(rows)
        if not unresolved:
            if recovery_of is not None:
                raise BudgetExceeded("Recovery source is resolved", code="unknown_model_cost")
            return
        if all(self._has_successful_recovery(row, rows) for row in unresolved):
            return
        if recovery_of is None or not self._valid_recovery(
            str(recovery_of), rows, recovery_receipt=recovery_receipt
        ):
            raise BudgetExceeded(
                "Routed diagnostic has unresolved usage", code="unknown_model_cost"
            )

    def assert_bound(self):
        with self._transaction() as connection:
            rows = self._rows(connection)
            if any(row["overrun"] for row in rows):
                raise BudgetExceeded("Routed diagnostic exceeded its reservation")
            unresolved = self._unresolved(rows)
            if unresolved:
                raise BudgetExceeded("Routed diagnostic has unresolved usage")

    def ensure_resumable(self):
        with self._transaction() as connection:
            rows = self._rows(connection)
            if any(
                row["status"] == "reserved"
                or row["overrun"]
                or row["dispatch_state"] == "dispatching"
                for row in rows
            ):
                raise BudgetExceeded("Routed diagnostic has pending or unknown usage")
            unresolved = self._unresolved(rows)
            if unresolved and not all(
                self._has_successful_recovery(row, rows) for row in unresolved
            ):
                raise BudgetExceeded("Routed diagnostic has unrecovered unknown usage")
            if connection.execute("SELECT 1 FROM phases WHERE status='started'").fetchone():
                raise BudgetExceeded("Routed diagnostic phase is already in progress")

    def start_phase(self, phase):
        if phase not in {"connection", "tool_contract", "plan", "stream"}:
            raise ValueError("Unsupported routed readiness phase")
        with self._transaction() as connection:
            rows = self._rows(connection)
            if any(row["overrun"] for row in rows):
                raise BudgetExceeded("Routed diagnostic exceeded its reservation")
            unresolved = self._unresolved(rows)
            if unresolved and not all(
                self._has_successful_recovery(row, rows) for row in unresolved
            ):
                raise BudgetExceeded("Routed diagnostic has unrecovered unknown usage")
            row = connection.execute(
                "SELECT status,report FROM phases WHERE phase=?", (phase,)
            ).fetchone()
            if row is not None:
                if row["status"] == "passed":
                    return json.loads(row["report"])
                if row["status"] == "started":
                    raise BudgetExceeded("Routed diagnostic phase is already in progress")
                raise BudgetExceeded("Routed diagnostic phase already ended; it cannot be replayed")
            connection.execute("INSERT INTO phases(phase,status) VALUES(?,'started')", (phase,))
            return None

    def finish_phase(self, phase, status, report):
        if phase not in {"connection", "tool_contract", "plan", "stream"}:
            raise ValueError("Unsupported routed readiness phase")
        if status not in {"passed", "failed", "skipped"}:
            raise ValueError("Invalid readiness phase status")
        safe = canonical_json(report)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM phases WHERE phase=?", (phase,)
            ).fetchone()
            if row is None or row["status"] != "started":
                raise ValueError("Readiness phase was not started exactly once")
            connection.execute(
                "UPDATE phases SET status=?,report=? WHERE phase=?",
                (status, safe, phase),
            )

    def reserve_model(
        self,
        request_key,
        attempt,
        provider,
        tokens,
        cost,
        *,
        model_family=None,
        purpose=None,
        request_role=None,
        requested_model=None,
        endpoint=None,
        requested_output_tokens=None,
        recovery_of=None,
        **_metadata,
    ):
        if (
            model_family != "primary"
            or type(attempt) is not int
            or attempt < 1
            or type(tokens) is not int
            or tokens <= 0
            or type(requested_output_tokens) is not int
            or not 1 <= requested_output_tokens <= MAX_OUTPUT_TOKENS
            or not isinstance(cost, Decimal)
            or not cost.is_finite()
            or cost < 0
        ):
            raise BudgetExceeded("Request is outside the routed readiness limits")
        if tokens - requested_output_tokens > MAX_REQUEST_INPUT_TOKENS:
            raise BudgetExceeded("Request input exceeds the diagnostic limit")
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise ValueError("Invalid diagnostic request identity")
        if purpose not in {"connection", "tool_contract", "plan", "format_repair", "stream"}:
            raise ValueError("Unsupported routed readiness phase")
        if request_role not in {"connection", "tool_contract", "planning", "stream"}:
            raise ValueError("Unsupported routed readiness role")
        requested_model = _safe_value(requested_model, "requested model")
        endpoint = _endpoint(endpoint)
        recovery_of = str(recovery_of) if recovery_of is not None else None
        if provider == self.identity["primary_provider"]:
            if requested_model != self.identity["primary_model"] or endpoint != self.identity[
                "primary_endpoint"
            ]:
                raise BudgetExceeded("Primary profile differs from the frozen readiness profile")
        elif provider == "openrouter":
            if requested_model != self.identity["fallback_model"] or endpoint != self.identity[
                "fallback_endpoint"
            ]:
                raise BudgetExceeded("Fallback profile differs from the frozen readiness profile")
        else:
            raise BudgetExceeded(
                "Unregistered readiness provider", code="unregistered_readiness_provider"
            )
        request_digest = hashlib.sha256(request_key.encode()).hexdigest()
        with self._transaction() as connection:
            rows = self._rows(connection)
            if any(row["overrun"] for row in rows):
                raise BudgetExceeded("Routed diagnostic exceeded its reservation")
            if recovery_of is not None and not self._valid_recovery(recovery_of, rows):
                raise BudgetExceeded(
                    "Unknown request is not eligible for recovery", code="unknown_model_cost"
                )
            self._check_unresolved(rows, recovery_of=recovery_of)
            counted_attempts = [row for row in rows if row["dispatch_state"] != "not_sent"]
            if len(counted_attempts) >= MAX_CALLS:
                raise BudgetExceeded("Routed readiness request cap exhausted")
            if self._held_tokens(rows) + tokens > MAX_TOTAL_TOKENS:
                raise BudgetExceeded("Routed readiness token cap exhausted")
            deepseek_spend = self._deepseek_spend(rows)
            if (
                requested_model == self.identity["fallback_model"]
                and deepseek_spend + cost > MAX_DEEPSEEK_COST
            ):
                raise BudgetExceeded("DeepSeek readiness budget exhausted")
            if connection.execute(
                "SELECT 1 FROM attempts WHERE request_digest=?", (request_digest,)
            ).fetchone():
                raise ValueError("Diagnostic request key cannot be reused")
            receipt = uuid4().hex
            connection.execute(
                "INSERT INTO attempts(receipt,request_digest,attempt,provider,requested_model,"
                "endpoint,request_role,purpose,requested_tokens,requested_output_tokens,reserved_cost,"
                "status,dispatch_state,recovery_of,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved','reserved',?,?)",
                (
                    receipt,
                    request_digest,
                    attempt,
                    provider,
                    requested_model,
                    endpoint,
                    request_role,
                    purpose,
                    tokens,
                    requested_output_tokens,
                    str(cost),
                    recovery_of,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return receipt

    def mark_dispatched(self, receipt):
        with self._transaction() as connection:
            rows = self._rows(connection)
            current = next((row for row in rows if row["receipt"] == str(receipt)), None)
            if (
                current is None
                or current["status"] != "reserved"
                or current["dispatch_state"] != "reserved"
            ):
                raise ValueError("Diagnostic receipt has no fresh reservation")
            self._check_unresolved(
                rows,
                recovery_of=current["recovery_of"],
                recovery_receipt=current["receipt"],
            )
            if (
                self._held_tokens(rows) > MAX_TOTAL_TOKENS
                or self._deepseek_spend(rows) > MAX_DEEPSEEK_COST
            ):
                raise BudgetExceeded("Routed readiness reservation exceeds its cap")
            if sum(row["dispatch_state"] != "not_sent" for row in rows) > MAX_CALLS:
                raise BudgetExceeded("Routed readiness request cap exhausted")
            connection.execute(
                "UPDATE attempts SET dispatch_state='dispatching' WHERE receipt=?", (str(receipt),)
            )

    def mark_not_sent(self, receipt, *, reason, after_local_preflight=False):
        if not isinstance(reason, str) or not _IDENTIFIER.fullmatch(reason):
            raise ValueError("Invalid non-dispatch reason")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (str(receipt),)
            ).fetchone()
            if row is None or row["status"] != "reserved" or row["dispatch_state"] != "reserved":
                raise ValueError("Dispatched diagnostic request cannot be released")
            connection.execute(
                "UPDATE attempts SET status='settled',dispatch_state='not_sent',"
                "actual_tokens=0,actual_cost='0',error_code=? WHERE receipt=?",
                (reason, str(receipt)),
            )

    def settle_model(
        self,
        receipt,
        tokens,
        cost,
        *,
        response_state,
        error_code=None,
        resolved_model=None,
        server_request_id=None,
        latency_ms=None,
        error_category=None,
        provider_status=None,
    ):
        if response_state not in {"responded", "unknown"}:
            raise ValueError("Invalid diagnostic response state")
        if error_code is not None:
            error_code = _safe_value(error_code, "error code")
        if error_category is not None and error_category not in {
            "timeout", "connection", "transient_http", "permanent_http", "provider_error",
            "transport_error", "usage_unknown",
        }:
            raise ValueError("Invalid provider error category")
        if resolved_model is not None:
            resolved_model = _safe_value(resolved_model, "resolved model")
        if server_request_id is not None:
            server_request_id = _safe_value(server_request_id, "server request id")
        if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
            raise ValueError("Invalid provider latency")
        if provider_status is not None and (
            type(provider_status) is not int or not 100 <= provider_status <= 599
        ):
            raise ValueError("Invalid provider status")
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Invalid provider usage")
        if cost is not None and (not isinstance(cost, Decimal) or not cost.is_finite() or cost < 0):
            raise ValueError("Invalid provider cost")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (str(receipt),)
            ).fetchone()
            if row is None or row["status"] != "reserved" or row["dispatch_state"] != "dispatching":
                raise ValueError("Diagnostic receipt has no active request")
            known = tokens is not None and cost is not None and response_state == "responded"
            overrun = (
                tokens is not None and tokens > row["requested_tokens"]
            ) or (
                cost is not None
                and cost > Decimal(row["reserved_cost"])
            )
            connection.execute(
                "UPDATE attempts SET actual_tokens=?,actual_cost=?,status=?,dispatch_state=?,"
                "error_code=?,error_category=?,provider_status=?,latency_ms=?,server_request_id=?,"
                "resolved_model=?,overrun=? WHERE receipt=?",
                (
                    tokens,
                    str(cost) if cost is not None else None,
                    "settled" if known else "unknown",
                    response_state,
                    error_code,
                    error_category,
                    provider_status,
                    latency_ms,
                    server_request_id,
                    resolved_model,
                    int(overrun),
                    str(receipt),
                ),
            )
            rows = self._rows(connection)
            total = self._held_tokens(rows)
            deepseek_cost = self._deepseek_spend(rows)
            if total > MAX_TOTAL_TOKENS or deepseek_cost > MAX_DEEPSEEK_COST or overrun:
                connection.execute(
                    "UPDATE attempts SET overrun=1 WHERE receipt=?", (str(receipt),)
                )
        if total > MAX_TOTAL_TOKENS or deepseek_cost > MAX_DEEPSEEK_COST or overrun:
            raise BudgetExceeded("Actual provider usage exceeded the routed readiness cap")

    def record_result(self, report):
        safe = canonical_json(report)
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM result").fetchone():
                raise ValueError("Readiness result is already frozen")
            rows = self._rows(connection)
            unresolved = self._unresolved(rows)
            if (
                any(
                    row["status"] == "reserved"
                    or row["dispatch_state"] == "dispatching"
                    or row["overrun"]
                    for row in rows
                )
                or (
                    unresolved
                    and not all(self._has_successful_recovery(row, rows) for row in unresolved)
                )
            ):
                raise BudgetExceeded("Readiness result requires resolved attempt usage")
            connection.execute("INSERT INTO result VALUES(1,?)", (safe,))

    def prior_result(self):
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM result WHERE id=1").fetchone()
        return json.loads(row["value"]) if row else None

    def snapshot(self):
        with self._transaction() as connection:
            rows = self._rows(connection)
            phases = [
                {"phase": row["phase"], "status": row["status"],
                 "report": json.loads(row["report"]) if row["report"] else None}
                for row in connection.execute("SELECT * FROM phases ORDER BY rowid")
            ]
            preflight_revisions = [
                {
                    "prior_status": (
                        json.loads(row["prior_result"]).get("status")
                        if row["prior_result"]
                        else None
                    ),
                    "prior_fingerprint": json.loads(row["prior_identity"])[
                        "diagnostic_fingerprint"
                    ],
                    "attempt_count": row["attempt_count"],
                }
                for row in connection.execute(
                    "SELECT * FROM preflight_revisions ORDER BY revision"
                )
            ]
        used_calls = sum(row["dispatch_state"] != "not_sent" for row in rows)
        held_tokens = self._held_tokens(rows)
        deepseek_spend = self._deepseek_spend(rows)
        attempts = []
        for row in rows:
            row["reserved_cost_usd"] = row.pop("reserved_cost")
            row["actual_cost_usd"] = row.pop("actual_cost")
            attempts.append(row)
        return {
            **self.identity,
            "used_calls": used_calls,
            "held_or_actual_tokens": held_tokens,
            "deepseek_spend_usd": str(deepseek_spend),
            "remaining_calls": MAX_CALLS - used_calls,
            "blocked": any(
                row["status"] in {"reserved", "unknown"} or row["overrun"] for row in rows
            ),
            "attempts": attempts,
            "phases": phases,
            "preflight_revisions": preflight_revisions,
        }

    def close(self):
        self._closed = True


def _price_digest(price):
    if price is None:
        raise ValueError("A versioned request price is required before diagnostic dispatch")
    return hashlib.sha256(
        canonical_json(
            {
                "version": price.version,
                "input_per_million": str(price.input_per_million),
                "output_per_million": str(price.output_per_million),
            }
        ).encode("utf-8")
    ).hexdigest()


def _legacy_deepseek_price_digest():
    from decagent.runtime.model_calls import TokenPrice

    return _price_digest(
        TokenPrice(
            _LEGACY_DEEPSEEK_PRICE_VERSION,
            DEEPSEEK_PRICE["input_per_million"],
            DEEPSEEK_PRICE["output_per_million"],
        )
    )


def _validate_profile(settings, config):
    if settings.main_model != "qwen3.8-flash":
        raise ValueError("Routed readiness is pinned to qwen3.8-flash")
    if settings.fallback_model != "deepseek/deepseek-v4.1-flash":
        raise ValueError("Routed readiness is pinned to DeepSeek V4.1 Flash")
    if not settings.main_api_key:
        raise ValueError("Primary model credentials are not configured")
    if not settings.resolved_fallback_api_key:
        raise ValueError("OpenRouter credentials are not configured")
    if config.main_price is None:
        raise ValueError("A versioned primary price or bounded reservation is required")
    primary_endpoint = _endpoint(settings.main_base_url)
    parsed = urlsplit(primary_endpoint)
    if parsed.hostname != _PRIMARY_ENDPOINT_HOST or parsed.path != _PRIMARY_ENDPOINT_PATH:
        raise ValueError("Primary readiness endpoint is outside the approved Qwen route")
    fallback_endpoint = _endpoint(settings.fallback_base_url)
    fallback = urlsplit(fallback_endpoint)
    if fallback.hostname != "openrouter.ai" or fallback.path != "/api/v1":
        raise ValueError("Fallback readiness endpoint is outside the approved OpenRouter route")
    from decagent.runtime.model_calls import TokenPrice

    deepseek_price = TokenPrice(
        DEEPSEEK_PRICE_VERSION,
        DEEPSEEK_PRICE["input_per_million"],
        DEEPSEEK_PRICE["output_per_million"],
    )
    return primary_endpoint, fallback_endpoint, deepseek_price


def _readiness_runtime_config(config, *, fallback_price):
    """Pin the diagnostic accounting provider without changing product settings."""
    return replace(
        config,
        main_provider=_PRIMARY_PROVIDER,
        fallback_price=fallback_price,
        unknown_fallback_reservation=None,
        main_fallback_enabled=False,
    )


def _primary_model(settings, *, max_output_tokens, tool_definitions=None, validator=None):
    from decagent.models.primary import PrimaryModel

    return PrimaryModel.from_profile(
        model=settings.main_model,
        base_url=settings.main_base_url,
        api_key=settings.main_api_key,
        max_output_tokens=max_output_tokens,
        tool_definitions=tool_definitions,
        validator=validator,
        known_secrets=(
            settings.main_api_key,
            settings.resolved_fallback_api_key,
            settings.jev_api_key,
            settings.github_token,
        ),
    )


def _budgeted_adapter(settings, config, ledger, *, purpose, max_output_tokens,
                      tool_definitions=None, validator=None):
    from decagent.runtime.model_factory import build_budgeted_primary

    model = _primary_model(
        settings,
        max_output_tokens=max_output_tokens,
        tool_definitions=tool_definitions,
        validator=validator,
    )
    return build_budgeted_primary(
        settings,
        config,
        ledger,
        purpose=purpose,
        request_role={"plan": "planning"}.get(purpose, purpose),
        max_output_tokens=max_output_tokens,
        model=model,
        tool_definitions=tool_definitions,
        validator=validator,
        fallback_allowed=True,
        force_fallback=True,
    )


def _validate_ping(name, params):
    if name != _PING_TOOL_NAME or params != {"status": "ready"}:
        raise ValueError("Readiness tool contract response differs")
    return {"status": "ready"}


def _attempt_models(ledger, phase):
    return sorted(
        {
            row["resolved_model"]
            for row in ledger.snapshot()["attempts"]
            if row["purpose"] == phase and row["resolved_model"]
        }
    )


def _diagnostic_actions(settings, config, ledger):
    def connection():
        adapter = _budgeted_adapter(
            settings, config, ledger, purpose="connection", max_output_tokens=256
        )
        turn = adapter.respond(
            [
                {"role": "system", "content": "Reply with one short readiness greeting."},
                {"role": "user", "content": "Connection check."},
            ]
        )
        if turn.tools or not turn.text.strip() or turn.usage is None:
            raise ValueError("Connection response was incomplete")
        return {
            "response_complete": True,
            "usage_complete": True,
            "resolved_models": _attempt_models(ledger, "connection"),
        }

    def tool_contract():
        adapter = _budgeted_adapter(
            settings,
            config,
            ledger,
            purpose="tool_contract",
            max_output_tokens=512,
            tool_definitions=[_PING_TOOL],
            validator=_validate_ping,
        )
        turn = adapter.respond(
            [
                {"role": "system", "content": "Call the readiness tool exactly once."},
                {"role": "user", "content": "Return the fixed readiness status."},
            ]
        )
        if len(turn.tools) != 1 or turn.tools[0].name != _PING_TOOL_NAME:
            raise ValueError("Tool response did not satisfy the minimal contract")
        return {
            "tool_name": _PING_TOOL_NAME,
            "parameter_contract_valid": True,
            "tool_executed": False,
            "resolved_models": _attempt_models(ledger, "tool_contract"),
        }

    def plan():
        from decagent.business.contracts import PluginSpec
        from decagent.business.plans.compiler import default_input_schema
        from decagent.business.plans.planner import budgeted_planner
        from decagent.business.tickets import builtin_manifest

        planner = budgeted_planner(
            settings,
            config,
            PluginSpec.from_mapping(builtin_manifest()),
            ledger,
            force_fallback=True,
        )
        proposal = planner.plan(
            _READINESS_CONTEXT, input_schema=default_input_schema()
        )
        plan = proposal.plan.to_mapping()
        commands = []
        for node in plan["nodes"]:
            if node["type"] == "governed_write":
                raise ValueError("Readiness plan unexpectedly contains a write node")
            if node["type"] == "read_tool":
                commands.append(node["tool"]["command"])
        if not commands or any(command != "directory" for command in commands):
            raise ValueError("Readiness plan must use only the registered directory read")
        return {
            "plan_compiled": True,
            "plan_digest": proposal.plan.digest,
            "node_count": len(plan["nodes"]),
            "read_only": True,
            "format_repair_used": proposal.repaired,
            "resolved_models": _attempt_models(ledger, "plan"),
        }

    def stream():
        adapter = _budgeted_adapter(
            settings, config, ledger, purpose="stream", max_output_tokens=256
        )
        turn = adapter.respond(
            [
                {"role": "system", "content": "Reply with a brief stream test."},
                {"role": "user", "content": "Streaming check."},
            ],
            stream=True,
        )
        if turn.tools or not turn.text.strip() or turn.usage is None:
            raise ValueError("Streaming response was incomplete")
        return {
            "response_complete": True,
            "usage_complete": True,
            "resolved_models": _attempt_models(ledger, "stream"),
        }

    return {
        "connection": connection,
        "tool_contract": tool_contract,
        "plan": plan,
        "stream": stream,
    }


def _failure_report(error):
    from decagent.models.primary import PrimaryProviderFailure, PrimaryResponseRejected
    from decagent.storage.budget import BudgetExceeded

    if isinstance(error, BudgetExceeded):
        return {"error_type": "budget_stopped", "error_code": error.code}
    if isinstance(error, PrimaryProviderFailure):
        return {
            "error_type": "provider_failure",
            "error_category": error.category,
            "provider_status": error.status_code,
        }
    if isinstance(error, PrimaryResponseRejected):
        return {"error_type": "response_rejected", "error_code": error.reason}
    return {"error_type": type(error).__name__}


def run_routed_readiness(
    settings,
    runtime_config,
    ledger,
    *,
    model_profile_digest,
    pricing_source,
):
    """Run or safely continue the four bounded phases without replaying completed calls."""
    ledger.ensure_resumable()
    phases = ("connection", "tool_contract", "plan", "stream")
    actions = _diagnostic_actions(settings, runtime_config, ledger)
    stop_reason = None
    for phase in phases:
        try:
            prior = ledger.start_phase(phase)
        except Exception as error:
            stop_reason = {"phase": phase, **_failure_report(error)}
            break
        if prior is not None:
            continue
        try:
            result = actions[phase]()
            ledger.finish_phase(phase, "passed", result)
        except Exception as error:
            failure = _failure_report(error)
            ledger.finish_phase(phase, "failed", failure)
            stop_reason = {"phase": phase, **failure}
            break
    snapshot = ledger.snapshot()
    passed = stop_reason is None and [row["status"] for row in snapshot["phases"]] == [
        "passed", "passed", "passed", "passed"
    ]
    report = {
        "schema_version": "routed-model-readiness-evidence-v1",
        "authorization_id": AUTHORIZATION_ID,
        "diagnostic_fingerprint": ledger.identity["diagnostic_fingerprint"],
        "model_profile_digest": model_profile_digest,
        "pricing_source": pricing_source,
        "status": "passed" if passed else "failed",
        "passed_criteria": (
            ["complete response and usage", "tool contract", "read-only production plan compiler"]
            if passed
            else []
        ),
        "stop_reason": stop_reason,
        "ledger": snapshot,
        "raw_prompts_responses_credentials_saved": False,
    }
    try:
        ledger.record_result(report)
    except BudgetExceeded:
        # Unknown provider usage remains durable in the attempt rows and intentionally
        # prevents a reusable completion record or automatic replay.
        pass
    return report


def diagnostic_fingerprint():
    """Bind the single authorization to the exact local prompts, schemas and adapters."""
    from decagent.business.tickets import builtin_manifest
    from decagent.decisions.contracts import canonical_json

    modules = (
        ("decagent.business.plans", (
            "planner.py", "compiler.py", "contracts.py", "plan-schema.json",
            "prompt_catalog.py", "bindings.py", "templates.py", "diagnostics.py",
        )),
        ("decagent.models", ("primary.py", "validation_feedback.py")),
        ("decagent.runtime", ("model_calls.py", "model_factory.py", "config.py")),
    )
    parts = []
    for package, names in modules:
        parts.extend(files(package).joinpath(name).read_bytes() for name in names)
    parts.extend(
        [
            Path(__file__).read_bytes(),
            canonical_json(builtin_manifest()).encode("utf-8"),
        ]
    )
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def _evidence_path(path):
    if path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = Path("evals/benchmarks/evidence") / f"routed-model-readiness-{stamp}.json"
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Readiness evidence output must be a new regular file")
    return target


def main(argv=None):
    import argparse
    import json

    from decagent.config import Settings
    from decagent.decisions.contracts import canonical_json
    from decagent.runtime.config import RuntimeConfig

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow the bounded new probe")
    parser.add_argument(
        "--resume-known-not-sent",
        action="store_true",
        help="Resume only a failed preflight whose ledger proves no request was dispatched",
    )
    parser.add_argument(
        "--ledger", type=Path, default=Path(".decagent/routed-model-readiness-v1.sqlite")
    )
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.resume_known_not_sent and not args.live:
        parser.error("--resume-known-not-sent requires --live")
    settings, config = Settings.from_env(), RuntimeConfig.from_env()
    primary_endpoint, fallback_endpoint, fallback_price = _validate_profile(settings, config)
    config = _readiness_runtime_config(config, fallback_price=fallback_price)
    identity = {
        "main_model": settings.main_model,
        "main_provider": config.main_provider,
        "main_endpoint": primary_endpoint,
        "main_price": {
            "version": config.main_price.version,
            "input_per_million": str(config.main_price.input_per_million),
            "output_per_million": str(config.main_price.output_per_million),
        },
        "fallback_model": settings.fallback_model,
        "fallback_provider": "openrouter",
        "fallback_endpoint": fallback_endpoint,
        "fallback_price": {
            "version": fallback_price.version,
            "input_per_million": str(fallback_price.input_per_million),
            "output_per_million": str(fallback_price.output_per_million),
            "source": DEEPSEEK_PRICE_SOURCE,
        },
    }
    profile_digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    if not args.live:
        print(json.dumps({
            "status": "dry_run",
            "profile_digest": profile_digest,
            "primary_model": settings.main_model,
            "fallback_model": settings.fallback_model,
            "fallback_peak_rates_usd_per_million": {
                key: str(value) for key, value in DEEPSEEK_PRICE.items()
            },
            "fallback_rate_bound_provider": DEEPSEEK_PRICE_BOUND_PROVIDER,
            "primary_provider": config.main_provider,
            "max_calls": MAX_CALLS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "max_deepseek_cost_usd": str(MAX_DEEPSEEK_COST),
            "live_requests": 0,
        }, ensure_ascii=False, indent=2))
        return 0

    evidence_path = _evidence_path(args.evidence)
    fingerprint = diagnostic_fingerprint()
    ledger = SqliteRoutedDiagnosticLedger.open(
        args.ledger,
        diagnostic_fingerprint=fingerprint,
        primary_model=settings.main_model,
        primary_endpoint=primary_endpoint,
        fallback_model=settings.fallback_model,
        fallback_endpoint=fallback_endpoint,
        primary_price_digest=_price_digest(config.main_price),
        fallback_price_digest=_price_digest(fallback_price),
        primary_provider=config.main_provider,
        resume_known_not_sent=args.resume_known_not_sent,
    )
    try:
        report = run_routed_readiness(
            settings,
            config,
            ledger,
            model_profile_digest=profile_digest,
            pricing_source=DEEPSEEK_PRICE_SOURCE,
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with evidence_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        print(json.dumps({
            "status": report["status"],
            "evidence": str(evidence_path),
            "used_calls": report["ledger"]["used_calls"],
            "used_tokens": report["ledger"]["held_or_actual_tokens"],
            "deepseek_spend_usd": report["ledger"]["deepseek_spend_usd"],
            "stop_reason": report["stop_reason"],
        }, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())

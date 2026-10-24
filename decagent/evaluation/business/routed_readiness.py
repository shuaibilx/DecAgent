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

AUTHORIZATION_ID = "primary-readiness-routed-v2-20261024"
AUTHORIZATION_ANCHOR = (
    Path(__file__).resolve().parents[3] / ".decagent" / "model-readiness-authorizations.sqlite"
)
MAX_CALLS = 12
MAX_QWEN_CALLS = 6
MAX_OPENROUTER_CALLS = 6
MAX_TOTAL_TOKENS = 600_000
MAX_REQUEST_INPUT_TOKENS = 65_536
MAX_OUTPUT_TOKENS = 8_192
MAX_QWEN_COST_CNY = Decimal("1.62")
MAX_DEEPSEEK_COST = Decimal("5")
MAX_WALL_SECONDS = 2_700
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/_-]{0,254}\Z")
DEEPSEEK_PRICE_VERSION = "openrouter-endpoints-api-2026-10-19-peak"
DEEPSEEK_PRICE_SOURCE = (
    "https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"
)
QWEN_PRICE_SOURCE = "https://platform.qianwenai.com/pricing/api"
DEEPSEEK_PRICE_BOUND_PROVIDER = "BaseTen"
DEEPSEEK_PRICE = {
    "input_per_million": Decimal("0.60"),
    "output_per_million": Decimal("2.40"),
}
QWEN_PRICE_VERSION = "qwen3.8-flash-standard-cny-20261024-v1"
QWEN_PRICE = {
    "input_per_million": Decimal("0.8"),
    "output_per_million": Decimal("2.7"),
}
_LEGACY_DEEPSEEK_PRICE_VERSION = "openrouter-models-api-2026-10-19-peak"
_PRIMARY_ENDPOINT_HOST = "maas.qianwenaiapi.com"
_PRIMARY_ENDPOINT_PATH = "/compatible-mode/v1"
_PRIMARY_PROVIDER = "qwen"
_DIAGNOSTIC_PHASES = ("connection", "tool_contract", "plan", "stream")
_PROVIDER_PHASES = tuple(
    f"{provider}_{phase}"
    for provider in ("qwen", "openrouter")
    for phase in _DIAGNOSTIC_PHASES
)
_READINESS_PHASES = frozenset((*_DIAGNOSTIC_PHASES, *_PROVIDER_PHASES))
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
        primary_currency="CNY",
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
        if primary_provider != "qwen" or primary_currency != "CNY":
            raise ValueError("Readiness is frozen to Qwen CNY billing")
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
            "schema_version": "routed-model-readiness-ledger-v2",
            "authorization_id": AUTHORIZATION_ID,
            "diagnostic_fingerprint": diagnostic_fingerprint,
            "primary_model": primary_model,
            "primary_provider": primary_provider,
            "primary_currency": primary_currency,
            "primary_endpoint": primary_endpoint,
            "fallback_model": fallback_model,
            "fallback_provider": "openrouter",
            "fallback_endpoint": fallback_endpoint,
            "primary_price_digest": primary_price_digest,
            "primary_price_source": QWEN_PRICE_SOURCE,
            "fallback_price_digest": fallback_price_digest,
            "fallback_price_source": DEEPSEEK_PRICE_SOURCE,
            "max_calls": MAX_CALLS,
            "max_qwen_calls": MAX_QWEN_CALLS,
            "max_openrouter_calls": MAX_OPENROUTER_CALLS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "max_request_input_tokens": MAX_REQUEST_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_deepseek_cost_usd": str(MAX_DEEPSEEK_COST),
            "max_qwen_cost_cny": str(MAX_QWEN_COST_CNY),
            "max_wall_seconds": MAX_WALL_SECONDS,
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
                row = connection.execute("SELECT value FROM identity WHERE id=1").fetchone()
                expected = canonical_json(self.identity)
                if row is not None:
                    prior = json.loads(row["value"])
                    if prior.get("authorization_id") != self.identity["authorization_id"]:
                        raise ValueError(
                            "Routed readiness ledger belongs to a different authorization"
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
                        cost_currency TEXT NOT NULL,
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
                phase_values = ",".join(
                    "'" + phase + "'" for phase in sorted(_READINESS_PHASES)
                )
                connection.execute(
                    f"""CREATE TABLE IF NOT EXISTS phases(
                        phase TEXT PRIMARY KEY CHECK(phase IN ({phase_values})),
                        status TEXT NOT NULL CHECK(
                          status IN ('started','passed','failed','skipped')
                        ),
                        report TEXT
                    )"""
                )
                phase_schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='phases'"
                ).fetchone()[0]
                if "'qwen_connection'" not in phase_schema:
                    connection.execute(
                        f"""CREATE TABLE phases_upgrade(
                            phase TEXT PRIMARY KEY CHECK(phase IN ({phase_values})),
                            status TEXT NOT NULL CHECK(
                              status IN ('started','passed','failed','skipped')
                            ),
                            report TEXT
                        )"""
                    )
                    connection.execute(
                        "INSERT INTO phases_upgrade(phase,status,report) "
                        "SELECT phase,status,report FROM phases"
                    )
                    connection.execute("DROP TABLE phases")
                    connection.execute("ALTER TABLE phases_upgrade RENAME TO phases")
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
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS batch_state("
                    "id INTEGER PRIMARY KEY CHECK(id=1),started_at_utc TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO batch_state(id,started_at_utc) VALUES(1,?)",
                    (datetime.now(UTC).isoformat(),),
                )
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

    @staticmethod
    def _provider_calls(rows, provider):
        return sum(
            row["provider"] == provider and row["dispatch_state"] != "not_sent"
            for row in rows
        )

    @staticmethod
    def _provider_spend(rows, *, provider, currency):
        return sum(
            (
                Decimal(row["actual_cost"])
                if row["status"] == "settled" and row["actual_cost"] is not None
                else Decimal(row["reserved_cost"])
                for row in rows
                if row["provider"] == provider
                and row["cost_currency"] == currency
                and row["dispatch_state"] != "not_sent"
            ),
            Decimal(0),
        )

    def _deepseek_spend(self, rows):
        return self._provider_spend(rows, provider="openrouter", currency="USD")

    def _qwen_spend(self, rows):
        return self._provider_spend(
            rows, provider=self.identity["primary_provider"], currency="CNY"
        )

    def _batch_started_at(self, connection):
        row = connection.execute(
            "SELECT started_at_utc FROM batch_state WHERE id=1"
        ).fetchone()
        if row is None:
            raise BudgetExceeded("Readiness batch clock is missing")
        try:
            started_at = datetime.fromisoformat(row["started_at_utc"])
        except (TypeError, ValueError) as exc:
            raise BudgetExceeded("Readiness batch clock is invalid") from exc
        if started_at.tzinfo is None:
            raise BudgetExceeded("Readiness batch clock must include a timezone")
        return started_at

    def _assert_within_wall_limit(self, connection):
        elapsed = (datetime.now(UTC) - self._batch_started_at(connection)).total_seconds()
        if elapsed >= MAX_WALL_SECONDS:
            raise BudgetExceeded("Readiness batch wall-clock limit exhausted")

    def assert_within_wall_limit(self):
        with self._transaction() as connection:
            self._assert_within_wall_limit(connection)

    def request_timeout_seconds(self, model):
        with self._transaction() as connection:
            self._assert_within_wall_limit(connection)
            started_at = self._batch_started_at(connection)
        remaining = MAX_WALL_SECONDS - (datetime.now(UTC) - started_at).total_seconds()
        provider_limit = getattr(model, "max_request_timeout", 60)
        timeout = min(float(provider_limit), remaining - 0.05)
        if timeout <= 0:
            raise BudgetExceeded("Readiness batch wall-clock limit exhausted")
        return timeout

    def _check_unresolved(self, rows, *, provider=None, recovery_of=None):
        unresolved = self._unresolved(rows)
        if not unresolved:
            if recovery_of is not None:
                raise BudgetExceeded("Recovery source is resolved", code="unknown_model_cost")
            return
        if recovery_of is not None:
            raise BudgetExceeded(
                "Readiness attempts cannot rewrite unknown usage as a recovery",
                code="unknown_model_cost",
            )
        if any(row["dispatch_state"] == "dispatching" for row in unresolved):
            raise BudgetExceeded(
                "Routed diagnostic has unresolved usage", code="unknown_model_cost"
            )
        unresolved_providers = {row["provider"] for row in unresolved}
        if (
            provider == "openrouter"
            and unresolved_providers == {self.identity["primary_provider"]}
        ):
            return
        raise BudgetExceeded(
            "Routed diagnostic provider stopped after unknown usage",
            code="unknown_model_cost",
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
            if any(row["provider"] != self.identity["primary_provider"] for row in unresolved):
                raise BudgetExceeded("OpenRouter readiness stopped after unknown usage")
            if connection.execute("SELECT 1 FROM phases WHERE status='started'").fetchone():
                raise BudgetExceeded("Routed diagnostic phase is already in progress")

    def start_phase(self, phase):
        if phase not in _READINESS_PHASES:
            raise ValueError("Unsupported routed readiness phase")
        with self._transaction() as connection:
            self._assert_within_wall_limit(connection)
            rows = self._rows(connection)
            if any(row["overrun"] for row in rows):
                raise BudgetExceeded("Routed diagnostic exceeded its reservation")
            provider = "openrouter" if phase.startswith("openrouter_") else "qwen"
            self._check_unresolved(rows, provider=provider)
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
        if phase not in _READINESS_PHASES:
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
        trial_cost_currency=None,
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
            expected_currency = self.identity["primary_currency"]
        elif provider == "openrouter":
            if requested_model != self.identity["fallback_model"] or endpoint != self.identity[
                "fallback_endpoint"
            ]:
                raise BudgetExceeded("Fallback profile differs from the frozen readiness profile")
            expected_currency = "USD"
        else:
            raise BudgetExceeded(
                "Unregistered readiness provider", code="unregistered_readiness_provider"
            )
        if trial_cost_currency != expected_currency:
            raise BudgetExceeded("Model request currency differs from the frozen provider profile")
        request_digest = hashlib.sha256(request_key.encode()).hexdigest()
        with self._transaction() as connection:
            self._assert_within_wall_limit(connection)
            rows = self._rows(connection)
            if any(row["overrun"] for row in rows):
                raise BudgetExceeded("Routed diagnostic exceeded its reservation")
            self._check_unresolved(
                rows,
                provider=provider,
                recovery_of=recovery_of,
            )
            counted_attempts = [row for row in rows if row["dispatch_state"] != "not_sent"]
            if len(counted_attempts) >= MAX_CALLS:
                raise BudgetExceeded("Routed readiness request cap exhausted")
            provider_cap = (
                MAX_QWEN_CALLS
                if provider == self.identity["primary_provider"]
                else MAX_OPENROUTER_CALLS
            )
            if self._provider_calls(rows, provider) >= provider_cap:
                raise BudgetExceeded(f"{provider} readiness request cap exhausted")
            if self._held_tokens(rows) + tokens > MAX_TOTAL_TOKENS:
                raise BudgetExceeded("Routed readiness token cap exhausted")
            deepseek_spend = self._deepseek_spend(rows)
            qwen_spend = self._qwen_spend(rows)
            qwen_reservation = (
                cost if provider == self.identity["primary_provider"] else Decimal(0)
            )
            if qwen_spend + qwen_reservation > MAX_QWEN_COST_CNY:
                raise BudgetExceeded("Qwen readiness budget exhausted")
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
                "cost_currency,"
                "status,dispatch_state,recovery_of,created_at_utc) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'reserved','reserved',?,?)",
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
                    trial_cost_currency,
                    recovery_of,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return receipt

    def mark_dispatched(self, receipt):
        with self._transaction() as connection:
            self._assert_within_wall_limit(connection)
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
                provider=current["provider"],
                recovery_of=current["recovery_of"],
            )
            if (
                self._provider_calls(rows, current["provider"])
                > (MAX_QWEN_CALLS if current["provider"] == self.identity["primary_provider"]
                   else MAX_OPENROUTER_CALLS)
                or self._held_tokens(rows) > MAX_TOTAL_TOKENS
                or self._deepseek_spend(rows) > MAX_DEEPSEEK_COST
                or self._qwen_spend(rows) > MAX_QWEN_COST_CNY
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
        if type(after_local_preflight) is not bool:
            raise ValueError("Local non-dispatch proof must be explicit")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (str(receipt),)
            ).fetchone()
            can_release = row and (
                row["dispatch_state"] == "reserved"
                or (row["dispatch_state"] == "dispatching" and after_local_preflight)
            )
            if row is None or row["status"] != "reserved" or not can_release:
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
            if response_state == "responded" and not known and error_category is None:
                error_category = "usage_unknown"
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
            qwen_cost = self._qwen_spend(rows)
            if (
                total > MAX_TOTAL_TOKENS
                or deepseek_cost > MAX_DEEPSEEK_COST
                or qwen_cost > MAX_QWEN_COST_CNY
                or overrun
            ):
                connection.execute(
                    "UPDATE attempts SET overrun=1 WHERE receipt=?", (str(receipt),)
                )
        if (
            total > MAX_TOTAL_TOKENS
            or deepseek_cost > MAX_DEEPSEEK_COST
            or qwen_cost > MAX_QWEN_COST_CNY
            or overrun
        ):
            raise BudgetExceeded("Actual provider usage exceeded the routed readiness cap")

    def record_result(self, report):
        safe = canonical_json(report)
        qualified_openrouter = (
            report.get("qualified_provider") == "openrouter"
            and report.get("providers", {}).get("openrouter", {}).get("status") == "passed"
        )
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM result").fetchone():
                raise ValueError("Readiness result is already frozen")
            rows = self._rows(connection)
            unresolved = self._unresolved(rows)
            unresolved_providers = {row["provider"] for row in unresolved}
            if (
                any(
                    row["status"] == "reserved"
                    or row["dispatch_state"] == "dispatching"
                    or row["overrun"]
                    for row in rows
                )
                or (
                    unresolved
                    and not (
                        qualified_openrouter
                        and unresolved_providers == {self.identity["primary_provider"]}
                    )
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
        qwen_spend = self._qwen_spend(rows)
        qwen_calls = self._provider_calls(rows, self.identity["primary_provider"])
        openrouter_calls = self._provider_calls(rows, "openrouter")
        with self._connect() as connection:
            started_at = self._batch_started_at(connection)
        elapsed_seconds = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
        attempts = []
        for row in rows:
            row["reserved_cost_amount"] = row.pop("reserved_cost")
            row["actual_cost_amount"] = row.pop("actual_cost")
            attempts.append(row)
        return {
            **self.identity,
            "used_calls": used_calls,
            "qwen_calls": qwen_calls,
            "openrouter_calls": openrouter_calls,
            "held_or_actual_tokens": held_tokens,
            "qwen_rate_estimate_cny": str(qwen_spend),
            "deepseek_rate_estimate_usd": str(deepseek_spend),
            "remaining_calls": MAX_CALLS - used_calls,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "remaining_wall_seconds": max(0.0, MAX_WALL_SECONDS - elapsed_seconds),
            "rate_estimate_basis": "frozen_standard_rates; Qwen input uses uncached upper bound",
            "provider_billed_cost_available": False,
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
                "currency": price.currency,
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
    if (
        config.main_price.version != QWEN_PRICE_VERSION
        or config.main_price.input_per_million != QWEN_PRICE["input_per_million"]
        or config.main_price.output_per_million != QWEN_PRICE["output_per_million"]
        or config.main_price.currency != "CNY"
    ):
        raise ValueError("Routed readiness requires the frozen Qwen price profile")
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


def _readiness_provider_profile(settings, config, provider):
    """Return an isolated single-provider profile for one readiness lane."""
    if provider == "qwen":
        return settings, replace(
            config,
            main_provider="qwen",
            fallback_price=None,
            unknown_fallback_reservation=None,
            main_fallback_enabled=False,
            max_primary_attempts=1,
        )
    if provider == "openrouter":
        provider_settings = replace(
            settings,
            main_model=settings.fallback_model,
            main_base_url=settings.fallback_base_url,
            main_api_key=settings.resolved_fallback_api_key,
        )
        return provider_settings, replace(
            config,
            main_provider="openrouter",
            main_price=config.fallback_price,
            fallback_price=None,
            unknown_main_reservation=None,
            unknown_fallback_reservation=None,
            main_fallback_enabled=False,
            max_primary_attempts=1,
        )
    raise ValueError("Unsupported readiness provider")


def _readiness_one_shot_model(settings, config, *, provider, max_output_tokens,
                              tool_definitions):
    """Build a process-cancellable client for one metered readiness request."""
    from decagent.evaluation.business.model_profiles import ModelProfile
    from decagent.evaluation.business.public_models import OneShotPrimaryModel

    price = config.main_price
    if price is None:
        raise ValueError("Routed readiness requires frozen prices before model creation")
    price_source = QWEN_PRICE_SOURCE if provider == "qwen" else DEEPSEEK_PRICE_SOURCE
    profile = ModelProfile(
        role="primary",
        provider=provider,
        model=settings.main_model,
        endpoint=settings.main_base_url,
        price_version=price.version,
        price_source=price_source,
        input_price_per_million=price.input_per_million,
        output_price_per_million=price.output_per_million,
        max_input_tokens=MAX_REQUEST_INPUT_TOKENS,
        max_output_tokens=max_output_tokens,
        fallback_allowed=False,
        max_provider_attempts=1,
        temperature=None,
        top_p=None,
        seed=None,
        unsupported_parameters=("temperature", "top_p", "seed"),
        response_mode="chat_tool_or_text",
        tool_choice="auto",
        timeout_seconds=60,
        provider_routing="direct_endpoint",
        price_currency=price.currency,
    )
    return OneShotPrimaryModel(
        profile,
        api_key=settings.main_api_key,
        tool_definitions=tool_definitions,
        usage_include=provider == "openrouter",
    )


def _budgeted_adapter(settings, config, ledger, *, purpose, max_output_tokens,
                      provider="qwen", tool_definitions=None, validator=None):
    from decagent.runtime.model_factory import build_budgeted_primary

    provider_settings, provider_config = _readiness_provider_profile(
        settings, config, provider
    )
    model = _readiness_one_shot_model(
        provider_settings,
        provider_config,
        provider=provider,
        max_output_tokens=max_output_tokens,
        tool_definitions=tool_definitions,
    )
    adapter = build_budgeted_primary(
        provider_settings,
        provider_config,
        ledger,
        purpose=purpose,
        request_role={"plan": "planning"}.get(purpose, purpose),
        max_output_tokens=max_output_tokens,
        model=model,
        tool_definitions=tool_definitions,
        validator=validator,
        before_dispatch=ledger.assert_within_wall_limit,
        fallback_allowed=False,
        force_fallback=False,
        max_same_provider_attempts=1,
        request_timeout=ledger.request_timeout_seconds,
    )
    return adapter


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


def _diagnostic_actions(settings, config, ledger, *, provider="qwen"):
    def connection():
        adapter = _budgeted_adapter(
            settings,
            config,
            ledger,
            purpose="connection",
            max_output_tokens=256,
            provider=provider,
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
            provider=provider,
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
        from decagent.business.plans.planner import (
            budgeted_planner,
            plan_tool_definitions,
        )
        from decagent.business.tickets import builtin_manifest

        provider_settings, provider_config = _readiness_provider_profile(
            settings, config, provider
        )
        model = _readiness_one_shot_model(
            provider_settings,
            provider_config,
            provider=provider,
            max_output_tokens=8192,
            tool_definitions=plan_tool_definitions(),
        )
        planner = budgeted_planner(
            provider_settings,
            provider_config,
            PluginSpec.from_mapping(builtin_manifest()),
            ledger,
            before_dispatch=ledger.assert_within_wall_limit,
            fallback_allowed=False,
            force_fallback=False,
            max_same_provider_attempts=1,
            request_timeout=ledger.request_timeout_seconds,
            allow_format_repair=False,
            model=model,
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
            settings,
            config,
            ledger,
            purpose="stream",
            max_output_tokens=256,
            provider=provider,
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
    """Qualify Qwen first, then OpenRouter only if Qwen does not qualify."""
    ledger.ensure_resumable()
    providers = {}
    qualified_provider = None
    for provider in ("qwen", "openrouter"):
        actions = _diagnostic_actions(
            settings, runtime_config, ledger, provider=provider
        )
        provider_failure = None
        for phase in _DIAGNOSTIC_PHASES:
            phase_key = f"{provider}_{phase}"
            prior_row = next(
                (
                    row for row in ledger.snapshot()["phases"]
                    if row["phase"] == phase_key
                ),
                None,
            )
            if prior_row and prior_row["status"] == "failed":
                provider_failure = {
                    "phase": phase,
                    **(prior_row["report"] or {"error_type": "previously_failed"}),
                }
                break
            try:
                prior = ledger.start_phase(phase_key)
            except Exception as error:
                provider_failure = {"phase": phase, **_failure_report(error)}
                break
            if prior is not None:
                continue
            try:
                result = actions[phase]()
                ledger.finish_phase(phase_key, "passed", result)
            except Exception as error:
                failure = _failure_report(error)
                ledger.finish_phase(phase_key, "failed", failure)
                provider_failure = {"phase": phase, **failure}
                break
        providers[provider] = {
            "status": "failed" if provider_failure else "passed",
            "failure": provider_failure,
        }
        if provider_failure is None:
            qualified_provider = provider
            break
    snapshot = ledger.snapshot()
    passed = qualified_provider is not None
    report = {
        "schema_version": "routed-model-readiness-evidence-v2",
        "authorization_id": AUTHORIZATION_ID,
        "diagnostic_fingerprint": ledger.identity["diagnostic_fingerprint"],
        "model_profile_digest": model_profile_digest,
        "primary_pricing_source": ledger.identity["primary_price_source"],
        "pricing_source": pricing_source,
        "status": "passed" if passed else "failed",
        "qualified_provider": qualified_provider,
        "providers": providers,
        "passed_criteria": (
            [
                "complete response and usage",
                "tool contract",
                "read-only production plan compiler",
                "streaming response and usage",
            ]
            if passed
            else []
        ),
        "stop_reason": None if passed else providers.get("openrouter", {}).get("failure"),
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
        ("decagent.evaluation.business", (
            "cancellable_model.py", "model_profiles.py", "public_models.py",
        )),
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
        "--ledger",
        type=Path,
        default=Path(".decagent/routed-model-readiness-v2-20261024.sqlite"),
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
            "currency": config.main_price.currency,
            "source": QWEN_PRICE_SOURCE,
        },
        "fallback_model": settings.fallback_model,
        "fallback_provider": "openrouter",
        "fallback_endpoint": fallback_endpoint,
        "fallback_price": {
            "version": fallback_price.version,
            "input_per_million": str(fallback_price.input_per_million),
            "output_per_million": str(fallback_price.output_per_million),
            "currency": fallback_price.currency,
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
            "qwen_rates_cny_per_million": {
                "input_uncached": str(config.main_price.input_per_million),
                "output": str(config.main_price.output_per_million),
            },
            "fallback_rate_bound_provider": DEEPSEEK_PRICE_BOUND_PROVIDER,
            "primary_provider": config.main_provider,
            "primary_currency": config.main_price.currency,
            "max_calls": MAX_CALLS,
            "max_qwen_calls": MAX_QWEN_CALLS,
            "max_openrouter_calls": MAX_OPENROUTER_CALLS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "max_qwen_cost_cny": str(MAX_QWEN_COST_CNY),
            "max_deepseek_cost_usd": str(MAX_DEEPSEEK_COST),
            "max_wall_seconds": MAX_WALL_SECONDS,
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
        primary_currency=config.main_price.currency,
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
            "qwen_rate_estimate_cny": report["ledger"]["qwen_rate_estimate_cny"],
            "deepseek_rate_estimate_usd": report["ledger"]["deepseek_rate_estimate_usd"],
            "stop_reason": report["stop_reason"],
        }, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())

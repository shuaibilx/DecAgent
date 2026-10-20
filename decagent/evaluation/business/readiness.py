"""Isolated, durable owner authorization for a bounded Qwen planner diagnostic.

Nothing here runs a business tool or creates a production database. The SQLite
port is consumed by the existing BudgetedPrimary, including its repair calls.
The old diagnostic authorizations and unknown receipts are never read or edited.
This new authorization fixes three requests, one repair, 65,536 input tokens and
8,192 output tokens per request, within 100,000 cumulative input + output tokens.
Unknown dispatch or usage stops subsequent requests, including format repair.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from decagent.business.contracts import PluginSpec
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.planner import (
    ConditionalPlanner,
    PlanProposalInvalid,
    plan_tool_definitions,
    validate_plan_tool,
)
from decagent.business.tickets import builtin_manifest
from decagent.decisions.contracts import canonical_json
from decagent.models.primary import PrimaryModel, PrimaryResponseRejected
from decagent.models.validation_feedback import ValidationIssue, bounded_validation_issues
from decagent.runtime.model_calls import BudgetedPrimary, TokenPrice
from decagent.storage.budget import BudgetExceeded

AUTHORIZATION_ID = "qwen-readiness-20261019-plan-execution-v1"
# This diagnostic belongs to this checkout. Environment variables, Settings and
# CLI paths must never select its authorization anchor or create a second cap.
AUTHORIZATION_ANCHOR = (
    Path(__file__).resolve().parents[3] / ".decagent" / "qwen-readiness-authorizations.sqlite"
)
MODEL = "qwen3.8-flash"
ENDPOINT_HOST = "maas.qianwenaiapi.com"
PRICE_VERSION = "owner-confirmed-free-quota-20261011"
MAX_CALLS = 3
MAX_FORMAT_REPAIRS = 1
MAX_REQUEST_INPUT_TOKENS = 65536
# Fixed before the first request under this new authorization. The historical
# 65,536-token diagnostic remains separate and is never reopened or enlarged.
MAX_TOTAL_TOKENS = 100000
MAX_OUTPUT_TOKENS = 8192
_ERRORS = frozenset(
    {
        "tool_schema",
        "tool_identity",
        "invalid_tool_calls",
        "content_type",
        "usage_invalid",
        "usage_missing",
        "unsafe_tool_proposal",
        "malformed_response",
        "empty_stream",
        "transport_error",
        "before_dispatch_guard",
        "dispatch_guard",
        "context_limit",
        "input_invalid",
    }
)
_INPUT_SCHEMA = {"type": "object", "properties": {"instruction": {"type": "string"}}}
_CONTEXT = {
    "instruction": "Read the current service directory using directory, then complete "
    "with its verified directory evidence. Propose a read-only plan; perform no writes."
}
_HISTORICAL_EVIDENCE = [
    "evals/benchmarks/evidence/qwen-primary-readiness-20261019.json",
    "evals/benchmarks/evidence/qwen-plan-readiness-20261019.json",
    "evals/benchmarks/evidence/qwen-plan-format-repair-20261019.json",
    "evals/benchmarks/evidence/primary-request-reconciliation-20261019.json",
]


def diagnostic_fingerprint():
    """Fingerprint the exact local diagnostic and repaired validation contracts."""
    parts = [
        files("decagent.business.plans").joinpath(name).read_bytes()
        for name in (
            "planner.py",
            "diagnostics.py",
            "examples.json",
            "plan-schema.json",
            "compiler.py",
            "contracts.py",
            "prompt_catalog.py",
            "bindings.py",
            "templates.py",
            "schema_tools.py",
        )
    ]
    parts.extend(
        files("decagent.models").joinpath(name).read_bytes()
        for name in ("primary.py", "validation_feedback.py")
    )
    parts.extend([Path(__file__).read_bytes(), canonical_json(builtin_manifest()).encode()])
    parts.append(files("decagent.runtime").joinpath("model_calls.py").read_bytes())
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def _check_fingerprint(expected):
    if expected != diagnostic_fingerprint():
        raise ValueError("Readiness diagnostic fingerprint does not match actual sources")


def _open_bound_ledger(settings, path, fingerprint):
    """Bind this authorization to one absolute path in fixed checkout state.

    The anchor commits before any request reservation. Losing its bound ledger
    fails closed even when no request had been attempted. A different CLI path
    cannot create a fresh allowance for the same authorization. Configured data
    directories are irrelevant to the fixed anchor derived from this module.
    """
    target = Path(path).resolve()
    anchor = AUTHORIZATION_ANCHOR
    anchor.parent.mkdir(parents=True, exist_ok=True)
    if target == anchor:
        raise ValueError("Readiness ledger path cannot be its authorization anchor")
    if anchor.is_symlink():
        raise ValueError("Readiness authorization anchor requires a regular file")
    connection = sqlite3.connect(anchor, timeout=30, isolation_level=None)
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA journal_mode=WAL")
    ledger = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS bindings "
            "(authorization_id TEXT PRIMARY KEY, ledger_path TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT ledger_path FROM bindings WHERE authorization_id=?", (AUTHORIZATION_ID,)
        ).fetchone()
        if row is not None:
            if Path(row[0]) != target:
                raise ValueError("Readiness authorization is bound to a different ledger path")
            if not target.is_file():
                raise ValueError("Bound readiness ledger is missing; authorization remains spent")
        ledger = SqliteReadinessLedger.open(
            target,
            model=settings.main_model,
            endpoint_host=_endpoint(settings),
            diagnostic_fingerprint=fingerprint,
        )
        if row is None:
            connection.execute("INSERT INTO bindings VALUES(?,?)", (AUTHORIZATION_ID, str(target)))
        connection.commit()
        return ledger
    except BaseException:
        connection.rollback()
        if ledger is not None:
            ledger.close()
        raise
    finally:
        connection.close()


class SqliteReadinessLedger:
    """One immutable authorization per persistent file; no implicit reconciliation.

    Pending reservations also stop subsequent requests. A process crash cannot
    make a reservation reusable. BEGIN IMMEDIATE serializes every budget change.
    """

    def __init__(self, path, identity):
        self.path, self.identity = Path(path), identity
        self._closed = False

    @classmethod
    def open(cls, path, *, model, endpoint_host, diagnostic_fingerprint):
        if model != MODEL or endpoint_host != ENDPOINT_HOST:
            raise ValueError("Readiness identity is outside the owner authorization")
        if type(diagnostic_fingerprint) is not str or not re.fullmatch(
            "[0-9a-f]{64}", diagnostic_fingerprint
        ):
            raise ValueError("Readiness diagnostic fingerprint must be SHA-256")
        identity = {
            "schema_version": "qwen-readiness-ledger-v1",
            "authorization_id": AUTHORIZATION_ID,
            "model": model,
            "endpoint_host": endpoint_host,
            "diagnostic_fingerprint": diagnostic_fingerprint,
            "max_calls": MAX_CALLS,
            "max_format_repairs": MAX_FORMAT_REPAIRS,
            "max_request_input_tokens": MAX_REQUEST_INPUT_TOKENS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "total_tokens_include_input_and_output": True,
            "price_version": PRICE_VERSION,
        }
        path = Path(path)
        if path.is_symlink():
            raise ValueError("Readiness ledger requires a regular file")
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = cls(path, identity)
        with ledger._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS identity "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS attempts ("
                    "receipt TEXT PRIMARY KEY, request_digest TEXT NOT NULL UNIQUE,"
                    "purpose TEXT NOT NULL, requested_tokens INTEGER NOT NULL,"
                    "status TEXT NOT NULL, dispatch_state TEXT NOT NULL,"
                    "actual_tokens INTEGER, error_code TEXT, validation_issues TEXT NOT NULL,"
                    "created_at_utc TEXT NOT NULL, reservation_exceeded INTEGER NOT NULL DEFAULT 0)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS result "
                    "(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)"
                )
                row = connection.execute("SELECT value FROM identity WHERE id=1").fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO identity VALUES(1,?)", (canonical_json(identity),)
                    )
                elif row["value"] != canonical_json(identity):
                    raise ValueError("Readiness identity changed; original ledger preserved")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return ledger

    @contextmanager
    def _connect(self):
        if self._closed:
            raise ValueError("Readiness ledger is closed")
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    def _identity(self, connection):
        row = connection.execute("SELECT value FROM identity WHERE id=1").fetchone()
        if row is None or row["value"] != canonical_json(self.identity):
            raise ValueError("Readiness identity changed; original ledger preserved")

    @contextmanager
    def _transaction(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._identity(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _rows(connection):
        return [dict(row) for row in connection.execute("SELECT * FROM attempts ORDER BY rowid")]

    @staticmethod
    def _blocked(rows):
        return any(
            row["status"] in {"reserved", "unknown"} or row["reservation_exceeded"] for row in rows
        )

    def assert_bound(self):
        """The before_dispatch hook rechecks the persisted immutable authorization."""
        with self._transaction() as connection:
            if any(
                row["status"] == "unknown" or row["reservation_exceeded"]
                for row in self._rows(connection)
            ):
                raise BudgetExceeded("Readiness has unresolved usage")

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
        **_metadata,
    ):
        if (
            provider != "primary"
            or model_family != "primary"
            or attempt != 1
            or purpose not in {"plan", "format_repair"}
            or Decimal(str(cost)) != 0
            or type(tokens) is not int
            or tokens <= 0
            or tokens - MAX_OUTPUT_TOKENS > MAX_REQUEST_INPUT_TOKENS
        ):
            raise BudgetExceeded("Request is outside the readiness authorization")
        if type(request_key) is not str or not request_key or len(request_key) > 256:
            raise ValueError("Invalid readiness request identity")
        with self._transaction() as connection:
            rows = self._rows(connection)
            used = sum(
                row["actual_tokens"] if row["status"] == "settled" else row["requested_tokens"]
                for row in rows
            )
            if (
                self._blocked(rows)
                or len(rows) >= MAX_CALLS
                or used + tokens > MAX_TOTAL_TOKENS
                or connection.execute("SELECT 1 FROM result").fetchone() is not None
            ):
                raise BudgetExceeded("Readiness authorization exhausted or unresolved")
            if purpose == "format_repair" and (
                not rows
                or rows[-1]["status"] != "settled"
                or any(row["purpose"] == "format_repair" for row in rows)
            ):
                raise BudgetExceeded("Only one known-response format repair is authorized")
            key = hashlib.sha256(request_key.encode()).hexdigest()
            if connection.execute(
                "SELECT 1 FROM attempts WHERE request_digest=?", (key,)
            ).fetchone():
                raise ValueError("Duplicate readiness request identity")
            receipt = uuid4().hex
            connection.execute(
                "INSERT INTO attempts(receipt,request_digest,purpose,requested_tokens,status,"
                "dispatch_state,validation_issues,created_at_utc) VALUES(?,?,?,?,'reserved',"
                "'reserved','[]',?)",
                (receipt, key, purpose, tokens, datetime.now(UTC).isoformat()),
            )
            return receipt

    def mark_dispatched(self, receipt):
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (receipt,)
            ).fetchone()
            if row is None or row["status"] != "reserved" or row["dispatch_state"] != "reserved":
                raise ValueError("Readiness receipt has no new reservation")
            connection.execute(
                "UPDATE attempts SET dispatch_state='dispatching' WHERE receipt=?", (receipt,)
            )

    def mark_not_sent(self, receipt, *, reason, after_local_preflight=False):
        if reason not in _ERRORS:
            raise ValueError("Unsupported readiness non-dispatch classification")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (receipt,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "reserved"
                or (row["dispatch_state"] != "reserved" and not after_local_preflight)
            ):
                raise ValueError("Dispatched readiness request cannot be released")
            connection.execute(
                "UPDATE attempts SET status='settled',dispatch_state='not_sent',"
                "actual_tokens=0,error_code=? WHERE receipt=?",
                (reason, receipt),
            )

    def settle_model(self, receipt, tokens, cost, *, response_state, error_code=None):
        if (
            response_state not in {"responded", "unknown"}
            or error_code is not None
            and error_code not in _ERRORS
            or tokens is not None
            and (type(tokens) is not int or tokens < 0)
            or cost is not None
            and Decimal(str(cost)) != 0
        ):
            raise ValueError("Unsupported readiness settlement metadata")
        exceeded = False
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE receipt=?", (receipt,)
            ).fetchone()
            if row is None or row["status"] != "reserved" or row["dispatch_state"] != "dispatching":
                raise ValueError("Readiness receipt has no active dispatch")
            known = tokens is not None and cost is not None and response_state == "responded"
            exceeded = tokens is not None and tokens > row["requested_tokens"]
            connection.execute(
                "UPDATE attempts SET status=?,dispatch_state=?,actual_tokens=?,"
                "error_code=?,reservation_exceeded=? WHERE receipt=?",
                (
                    "settled" if known else "unknown",
                    response_state,
                    tokens,
                    error_code,
                    int(exceeded),
                    receipt,
                ),
            )
        if exceeded:
            raise BudgetExceeded("Readiness actual usage exceeded its reserved bound; preserved")

    def record_validation_issues(self, issues, *, before_settle=False):
        safe = [issue.to_mapping() for issue in bounded_validation_issues(issues)]
        if not safe:
            return
        with self._transaction() as connection:
            rows = self._rows(connection)
            allowed = {"responded", "dispatching"} if before_settle else {"responded"}
            if not rows or rows[-1]["dispatch_state"] not in allowed:
                raise ValueError("Readiness validation needs a completed response")
            connection.execute(
                "UPDATE attempts SET validation_issues=? WHERE receipt=?",
                (canonical_json(safe), rows[-1]["receipt"]),
            )

    def snapshot(self):
        with self._transaction() as connection:
            rows = self._rows(connection)
        for row in rows:
            row["validation_issues"] = json.loads(row["validation_issues"])
            row["reservation_exceeded"] = bool(row["reservation_exceeded"])
            row["actual_cost_usd"] = "0" if row["status"] == "settled" else None
            row["reserved_cost_usd"] = "0"
        known_actual_tokens = sum(
            row["actual_tokens"] for row in rows if row["actual_tokens"] is not None
        )
        actual_tokens_complete = all(row["actual_tokens"] is not None for row in rows)
        return {
            **self.identity,
            "attempts": rows,
            "blocked": self._blocked(rows),
            "actual_tokens": known_actual_tokens if actual_tokens_complete else None,
            "known_actual_tokens": known_actual_tokens,
            "actual_tokens_complete": actual_tokens_complete,
            "remaining_calls": MAX_CALLS - len(rows),
        }

    def passed_result(self):
        with self._transaction() as connection:
            row = connection.execute("SELECT value FROM result WHERE id=1").fetchone()
            return json.loads(row["value"]) if row else None

    def save_passed_result(self, report):
        with self._transaction() as connection:
            connection.execute("INSERT INTO result VALUES(1,?)", (canonical_json(report),))

    def close(self):
        self._closed = True


class _DiagnosticModel:
    """Persist safe completed-rejection diagnostics before settlement can stop us."""

    def __init__(self, model, ledger):
        self.model, self.ledger = model, ledger
        self.schema_bytes = getattr(model, "schema_bytes", 0)

    def respond(self, messages, *, stream=False):
        try:
            return self.model.respond(messages, stream=stream)
        except PrimaryResponseRejected as error:
            self.ledger.record_validation_issues(error.validation_issues, before_settle=True)
            raise


class _DiagnosticTransport(BudgetedPrimary):
    def respond_plan(self, messages, **attribution):
        if attribution.get("purpose") == "format_repair":
            try:
                entries = json.loads(messages[-1]["content"])["validation_issues"]
                issues = tuple(
                    ValidationIssue(item["code"], tuple(item["path"])) for item in entries[:8]
                )
                self.ledger.record_validation_issues(issues)
            except (ValueError, KeyError, TypeError):
                pass
        return super().respond_plan(messages, **attribution)


def _endpoint(settings):
    endpoint = urlsplit(settings.main_base_url)
    if (
        settings.main_model != MODEL
        or endpoint.scheme != "https"
        or endpoint.hostname != ENDPOINT_HOST
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.port not in {None, 443}
    ):
        raise ValueError("Readiness identity is outside the owner authorization")
    return endpoint.hostname


def run_readiness(settings, *, ledger_path, diagnostic_fingerprint, live=False, evidence_path=None):
    """Compile one directory plan; live=True is the only provider-dispatch entry.

    Callers must review the frozen diagnostic before enabling live. All transport
    exceptions are reduced to closed codes; raw responses and secrets are omitted.
    """
    base = {
        "schema_version": "qwen-readiness-evidence-v1",
        "authorization_id": AUTHORIZATION_ID,
        "status": "offline",
        "business_tool_calls": 0,
        "raw_response_persisted": False,
        "historical_evidence": _HISTORICAL_EVIDENCE,
        "historical_unknown_replayed_or_reset": False,
        "limits": {
            "max_calls": MAX_CALLS,
            "max_format_repairs": MAX_FORMAT_REPAIRS,
            "max_request_input_tokens": MAX_REQUEST_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "total_tokens_include_input_and_output": True,
        },
    }
    if not live:
        return base
    if evidence_path is not None and Path(evidence_path).exists():
        raise FileExistsError("Readiness evidence already exists; no request dispatched")
    _check_fingerprint(diagnostic_fingerprint)
    _endpoint(settings)
    ledger = _open_bound_ledger(settings, ledger_path, diagnostic_fingerprint)
    try:
        previous = ledger.passed_result()
        if previous:
            return {**previous, "reused_passed_evidence": True}
        report = {
            **base,
            "status": "stopped",
            "error_code": "readiness_failed",
            "plan_node_types": [],
            "plan_commands": [],
            "format_repair_used": False,
            "compiled_plan": None,
            "plan_read_only": False,
        }
        try:
            ledger.assert_bound()
            model = PrimaryModel.from_settings(
                settings,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                tool_definitions=plan_tool_definitions(),
                validator=validate_plan_tool,
            )

            def guard():
                _endpoint(settings)
                _check_fingerprint(diagnostic_fingerprint)
                ledger.assert_bound()

            transport = _DiagnosticTransport(
                _DiagnosticModel(model, ledger),
                ledger,
                price=TokenPrice(PRICE_VERSION, Decimal(0), Decimal(0)),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                before_dispatch=guard,
            )
            spec = PluginSpec.from_mapping(builtin_manifest())
            planner = ConditionalPlanner(
                transport, spec, max_request_tokens=MAX_REQUEST_INPUT_TOKENS
            )
            proposal = planner.plan(_CONTEXT, input_schema=_INPUT_SCHEMA)
            plan = compile_plan(proposal.plan, spec, input_schema=_INPUT_SCHEMA).plan
            mapping = plan.to_mapping()
            types = [node["type"] for node in mapping["nodes"]]
            commands = [
                node["tool"]["command"] for node in mapping["nodes"] if node["type"] == "read_tool"
            ]
            if (
                set(types) != {"read_tool", "complete"}
                or set(commands) != {"directory"}
                or any(
                    node["tool"]["plugin_id"] != "service-tickets"
                    for node in mapping["nodes"]
                    if node["type"] == "read_tool"
                )
            ):
                raise ValueError("Readiness requires a directory-only read plan")
            report.update(
                status="passed",
                error_code=None,
                plan_node_types=types,
                plan_commands=commands,
                format_repair_used=proposal.repaired,
                compiled_plan=mapping,
                plan_read_only=True,
                plan_sha256=plan.digest,
            )
        except PrimaryResponseRejected as error:
            report["error_code"] = error.reason if error.reason in _ERRORS else "primary_rejected"
        except PlanProposalInvalid as error:
            ledger.record_validation_issues(error.validation_issues)
            report["error_code"] = "invalid_plan"
        except BudgetExceeded:
            report["error_code"] = "authorization_budget_stopped"
        except Exception:
            report["error_code"] = "readiness_failed"
        report["ledger"] = ledger.snapshot()
        report["format_repair_used"] = any(
            row["purpose"] == "format_repair" for row in report["ledger"]["attempts"]
        )
        if report["ledger"]["blocked"]:
            report["status"] = "stopped"
        if report["status"] == "passed":
            ledger.save_passed_result(report)
        if evidence_path is not None:
            target = Path(evidence_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        return report
    finally:
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Explicitly dispatch owner-approved probe"
    )
    parser.add_argument("--ledger", type=Path, default=Path(".decagent/qwen-readiness-v1.sqlite"))
    parser.add_argument("--evidence", type=Path)
    arguments = parser.parse_args(argv)
    from decagent.config import Settings

    report = run_readiness(
        Settings.from_env(),
        ledger_path=arguments.ledger,
        diagnostic_fingerprint=diagnostic_fingerprint(),
        live=arguments.live,
        evidence_path=arguments.evidence,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"offline", "passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

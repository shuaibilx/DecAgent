"""Persistent identity guard for the approved public-evaluation budget window."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

PUBLIC_EVALUATION_BUDGET_ID = "sop-pilot-20261024-v1"
PUBLIC_EVALUATION_BUDGET_EXPIRES_UTC = datetime(2026, 11, 1, tzinfo=UTC)
_PHASES = frozenset({"diagnostic", "pilot"})


def _repository_identity(root):
    root = Path(root).resolve()
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return root.as_posix()
    remote = result.stdout.strip()
    return remote or root.as_posix()


def canonical_public_weekly_budget_path(root, budget_id=PUBLIC_EVALUATION_BUDGET_ID):
    """Use a stable per-user location so changing a checkout cannot reset budgets."""
    if budget_id != PUBLIC_EVALUATION_BUDGET_ID:
        raise ValueError("Public evaluation budget ID is not authorized")
    identity = hashlib.sha256(
        f"{_repository_identity(root)}\0{budget_id}".encode()
    ).hexdigest()
    return (
        Path.home()
        / ".decagent"
        / "evaluation"
        / "authorizations"
        / f"{identity}.sqlite3"
    )


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Weekly evaluation authorization cannot be a symlink")
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS budget_metadata(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            budget_id TEXT NOT NULL,
            repository_digest TEXT NOT NULL,
            project_root TEXT NOT NULL,
            expires_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS phases(
            phase TEXT PRIMARY KEY CHECK(phase IN ('diagnostic','pilot')),
            candidate_sha TEXT NOT NULL,
            cohort_digest TEXT NOT NULL,
            ledger_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','completed')),
            report_sha256 TEXT,
            phase_round INTEGER NOT NULL DEFAULT 1 CHECK(phase_round IN (1,2))
        );
        CREATE TABLE IF NOT EXISTS phase_history(
            phase TEXT NOT NULL CHECK(phase IN ('diagnostic','pilot')),
            phase_round INTEGER NOT NULL CHECK(phase_round IN (1,2)),
            candidate_sha TEXT NOT NULL,
            cohort_digest TEXT NOT NULL,
            ledger_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='completed'),
            report_sha256 TEXT NOT NULL,
            PRIMARY KEY(phase,phase_round)
        );
        CREATE TABLE IF NOT EXISTS superseded_phase_candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase TEXT NOT NULL,
            previous_candidate_sha TEXT NOT NULL,
            previous_cohort_digest TEXT NOT NULL,
            previous_ledger_path TEXT NOT NULL,
            successor_candidate_sha TEXT NOT NULL,
            successor_cohort_digest TEXT NOT NULL,
            successor_ledger_path TEXT NOT NULL,
            original_started_at_utc TEXT NOT NULL,
            proof_sha256 TEXT NOT NULL,
            superseded_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS openrouter_diagnostic_continuation(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            parent_report_sha256 TEXT NOT NULL,
            candidate_sha TEXT NOT NULL,
            cohort_digest TEXT NOT NULL,
            ledger_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','completed')),
            report_sha256 TEXT
        );
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(phases)")
    }
    if "phase_round" not in columns:
        connection.execute(
            "ALTER TABLE phases ADD COLUMN phase_round INTEGER NOT NULL DEFAULT 1 "
            "CHECK(phase_round IN (1,2))"
        )
    connection.execute(
        "INSERT OR IGNORE INTO phase_history(phase,phase_round,candidate_sha,cohort_digest,"
        "ledger_path,status,report_sha256) SELECT phase,phase_round,candidate_sha,cohort_digest,"
        "ledger_path,status,report_sha256 FROM phases WHERE status='completed' "
        "AND report_sha256 IS NOT NULL"
    )
    return connection


def _validate_identity(phase, candidate_sha, cohort_digest, ledger_path):
    if phase not in _PHASES:
        raise ValueError("Only the approved diagnostic and pilot phases are authorized")
    if type(candidate_sha) is not str or re.fullmatch(
        r"(?:[a-f0-9]{40}|[a-f0-9]{64})", candidate_sha
    ) is None:
        raise ValueError("A verified candidate SHA is required for weekly authorization")
    if type(cohort_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", cohort_digest) is None:
        raise ValueError("A frozen cohort digest is required for weekly authorization")
    return str(Path(ledger_path).resolve(strict=False))


def _inspect_unspent_diagnostic_ledger(
    ledger_path, *, candidate_sha, cohort_digest
):
    """Prove that a diagnostic candidate can be superseded without resetting use."""
    path = Path(ledger_path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Diagnostic successor requires its original ledger")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Diagnostic successor ledger path cannot contain symlinks")
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=5
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "metadata",
            "batch_control",
            "route_trials",
            "model_attempts",
            "tool_attempts",
            "machine_time_attempts",
            "tau_step_attempts",
        }
        if not required <= tables:
            raise ValueError("Diagnostic successor cannot verify the original ledger")
        metadata = {
            key: json.loads(value)
            for key, value in connection.execute("SELECT key,value FROM metadata")
        }
        if (
            metadata.get("candidate_sha") != candidate_sha
            or metadata.get("cohort_digest") != cohort_digest
            or metadata.get("mode") != "smoke"
        ):
            raise ValueError("Diagnostic successor ledger identity differs")

        routes = list(
            connection.execute(
                "SELECT status,score_status,score_digest,score_evidence_json,"
                "cleanup_status,cleanup_evidence_digest,result_digest,error_category "
                "FROM route_trials"
            )
        )
        if len(routes) != 8 or any(
            tuple(row)
            != ("pending", "not_attempted", None, None, "pending", None, None, None)
            for row in routes
        ):
            raise ValueError("Diagnostic successor cannot replay a started route")
        for table in (
            "model_attempts",
            "tool_attempts",
            "machine_time_attempts",
            "tau_step_attempts",
        ):
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
                raise ValueError("Diagnostic successor cannot reset a dispatched attempt")

        batch = connection.execute(
            "SELECT started_at_utc,deadline_at_utc,stopped_reason "
            "FROM batch_control WHERE singleton=1"
        ).fetchone()
        if batch is None or batch[2] is not None:
            raise ValueError("Diagnostic successor cannot reset a stopped batch")
        started_value = datetime.fromisoformat(batch[0])
        deadline_value = datetime.fromisoformat(batch[1])
        if (
            started_value.tzinfo is None
            or started_value.utcoffset() != timedelta(0)
            or deadline_value.tzinfo is None
            or deadline_value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Diagnostic successor batch clock must be UTC-aware")
        started_at = started_value.astimezone(UTC)
        deadline = deadline_value.astimezone(UTC)
        if deadline <= datetime.now(UTC) or deadline - started_at > timedelta(minutes=45):
            raise ValueError("Diagnostic successor has no remaining wall-clock budget")
        proof = {
            "candidate_sha": candidate_sha,
            "cohort_digest": cohort_digest,
            "started_at_utc": started_at.isoformat(),
            "deadline_at_utc": deadline.isoformat(),
            "pending_route_count": len(routes),
            "model_attempts": 0,
            "tool_attempts": 0,
            "machine_time_attempts": 0,
            "tau_step_attempts": 0,
        }
        return {
            "started_at_utc": started_at.isoformat(),
            "deadline_at_utc": deadline.isoformat(),
            "proof_sha256": hashlib.sha256(
                json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    except (sqlite3.Error, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Diagnostic successor cannot verify the original ledger") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _read_diagnostic_batch_clock(
    ledger_path, *, candidate_sha, cohort_digest, now=None, require_unexpired=True
):
    """Read the immutable absolute clock from an existing Qwen diagnostic ledger."""
    path = Path(ledger_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("The completed Qwen diagnostic ledger is unavailable")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("The Qwen diagnostic ledger cannot contain symlinks")
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=5
        )
        metadata = {
            key: json.loads(value)
            for key, value in connection.execute("SELECT key,value FROM metadata")
        }
        batch = connection.execute(
            "SELECT started_at_utc,deadline_at_utc FROM batch_control WHERE singleton=1"
        ).fetchone()
        if (
            metadata.get("candidate_sha") != candidate_sha
            or metadata.get("cohort_digest") != cohort_digest
            or metadata.get("mode") != "smoke"
            or batch is None
        ):
            raise ValueError("The Qwen diagnostic ledger identity or clock differs")
        started_value = datetime.fromisoformat(batch[0])
        deadline_value = datetime.fromisoformat(batch[1])
        if (
            started_value.tzinfo is None
            or started_value.utcoffset() != timedelta(0)
            or deadline_value.tzinfo is None
            or deadline_value.utcoffset() != timedelta(0)
        ):
            raise ValueError("Qwen diagnostic clock must be timezone-aware UTC")
        started_at = started_value.astimezone(UTC)
        deadline = deadline_value.astimezone(UTC)
        if deadline <= started_at or deadline - started_at > timedelta(minutes=45):
            raise ValueError("Qwen diagnostic clock exceeds the authorized 45-minute window")
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() != timedelta(0):
            raise ValueError("Qwen diagnostic clock check requires timezone-aware UTC")
        if require_unexpired and current_time.astimezone(UTC) >= deadline:
            raise ValueError("The original 45-minute diagnostic deadline has expired")
        return {
            "batch_started_at_utc": started_at.isoformat(),
            "batch_deadline_at_utc": deadline.isoformat(),
        }
    except (sqlite3.Error, json.JSONDecodeError, TypeError, IndexError) as exc:
        raise ValueError("The Qwen diagnostic ledger clock cannot be verified") from exc
    finally:
        if "connection" in locals():
            connection.close()


def openrouter_diagnostic_parent_clock(
    root, *, parent_report_sha256, now=None, require_unexpired=True
):
    """Resolve and verify the original Qwen phase clock bound to its report hash."""
    authorization_path = canonical_public_weekly_budget_path(root)
    if not authorization_path.is_file() or authorization_path.is_symlink():
        raise ValueError("OpenRouter continuation requires the existing weekly authorization")
    try:
        connection = sqlite3.connect(
            f"file:{authorization_path.resolve()}?mode=ro", uri=True, timeout=5
        )
        row = connection.execute(
            "SELECT candidate_sha,cohort_digest,ledger_path FROM phase_history "
            "WHERE phase='diagnostic' AND phase_round=2 AND status='completed' "
            "AND report_sha256=?",
            (parent_report_sha256,),
        ).fetchone()
        if row is None:
            raise ValueError("OpenRouter continuation must bind the completed Qwen round-2 report")
        ledger_path = Path(row[2]).resolve(strict=False)
        root = Path(root).resolve()
        if not ledger_path.is_relative_to(root):
            raise ValueError("Qwen diagnostic ledger must belong to this project")
        return _read_diagnostic_batch_clock(
            ledger_path,
            candidate_sha=row[0],
            cohort_digest=row[1],
            now=now,
            require_unexpired=require_unexpired,
        )
    except (sqlite3.Error, TypeError) as exc:
        raise ValueError("The original Qwen diagnostic clock cannot be verified") from exc
    finally:
        if "connection" in locals():
            connection.close()


def authorize_public_evaluation_phase(
    root,
    *,
    phase,
    phase_round=1,
    candidate_sha,
    cohort_digest,
    ledger_path,
    diagnostic_report_sha256=None,
    finalize_candidate_successor=False,
):
    """Bind this budget window to one candidate and one diagnostic/pilot pair.

    The per-phase ledgers retain the detailed resource reservations. This external
    state prevents another candidate/cohort, checkout path, or deleted local ledger
    from obtaining a fresh copy of the authorized ceilings.
    """
    root = Path(root).resolve()
    ledger_identity = _validate_identity(phase, candidate_sha, cohort_digest, ledger_path)
    if type(phase_round) is not int or phase_round not in {1, 2}:
        raise ValueError("Only the frozen first or one-time second diagnostic round is allowed")
    if phase != "diagnostic" and phase_round != 1:
        raise ValueError("Only diagnostic phases may have a round number")
    if not Path(ledger_identity).is_relative_to(root):
        raise ValueError("Weekly authorization ledger must belong to this project")
    authorization_path = canonical_public_weekly_budget_path(root)
    repository_digest = hashlib.sha256(
        _repository_identity(root).encode("utf-8")
    ).hexdigest()
    now = datetime.now(UTC)
    if now >= PUBLIC_EVALUATION_BUDGET_EXPIRES_UTC:
        raise ValueError("The approved public-evaluation budget window has expired")
    connection = _connect(authorization_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT budget_id,repository_digest,project_root,expires_at_utc "
            "FROM budget_metadata WHERE singleton=1"
        ).fetchone()
        expected = (
            PUBLIC_EVALUATION_BUDGET_ID,
            repository_digest,
            str(root),
            PUBLIC_EVALUATION_BUDGET_EXPIRES_UTC.isoformat(),
        )
        if current is None:
            connection.execute(
                "INSERT INTO budget_metadata VALUES(1,?,?,?,?)", expected
            )
        elif tuple(current) != expected:
            raise ValueError(
                "Weekly evaluation authorization is already bound to another project or budget"
            )

        if phase == "diagnostic" and phase_round == 2:
            current_diagnostic = connection.execute(
                "SELECT candidate_sha,cohort_digest,ledger_path,status,report_sha256,phase_round "
                "FROM phases WHERE phase='diagnostic'"
            ).fetchone()
            if current_diagnostic is None:
                raise ValueError("Second diagnostic round requires a completed first round")
            same_identity = tuple(current_diagnostic[:3]) == (
                candidate_sha,
                cohort_digest,
                ledger_identity,
            )
            if current_diagnostic[5] == 2:
                if current_diagnostic[3] == "completed":
                    raise ValueError("The one-time second diagnostic round is already complete")
                if not same_identity:
                    raise ValueError("Open second diagnostic cannot change candidate or ledger")
                if not Path(ledger_identity).is_file():
                    raise ValueError("Authorized second diagnostic ledger is missing")
            else:
                if current_diagnostic[3] != "completed" or not current_diagnostic[4]:
                    raise ValueError("Second diagnostic round requires a completed first round")
                if connection.execute(
                    "SELECT 1 FROM phases WHERE phase='pilot'"
                ).fetchone() is not None:
                    raise ValueError("Second diagnostic round cannot follow pilot dispatch")
                if connection.execute(
                    "SELECT 1 FROM phase_history WHERE phase='diagnostic' AND phase_round=2"
                ).fetchone() is not None:
                    raise ValueError("The one-time second diagnostic round was already used")
                if same_identity or Path(ledger_identity).exists():
                    raise ValueError(
                        "Second diagnostic requires a new candidate, cohort and ledger"
                    )
                connection.execute(
                    "UPDATE phases SET candidate_sha=?,cohort_digest=?,ledger_path=?,"
                    "status='open',report_sha256=NULL,phase_round=2 WHERE phase='diagnostic'",
                    (candidate_sha, cohort_digest, ledger_identity),
                )
            connection.execute("COMMIT")
            return {
                "authorization_path": authorization_path,
                "batch_started_at_utc": None,
                "candidate_successor_pending": False,
                "phase_round": 2,
                "previous_report_sha256": current_diagnostic[4],
            }
        if phase == "diagnostic" and phase_round != 1:
            raise ValueError("Diagnostic round is unsupported")

        if phase == "pilot":
            if (
                type(diagnostic_report_sha256) is not str
                or re.fullmatch(r"[a-f0-9]{64}", diagnostic_report_sha256) is None
            ):
                raise ValueError("Pilot requires the completed diagnostic report digest")
            diagnostic = connection.execute(
                "SELECT candidate_sha,status,report_sha256 FROM phases "
                "WHERE phase='diagnostic'"
            ).fetchone()
            if (
                diagnostic is None
                or diagnostic[0] != candidate_sha
                or diagnostic[1] != "completed"
                or diagnostic[2] != diagnostic_report_sha256
            ):
                raise ValueError(
                    "Pilot is not bound to the completed diagnostic in this budget window"
                )

        existing = connection.execute(
            "SELECT candidate_sha,cohort_digest,ledger_path,status "
            "FROM phases WHERE phase=?",
            (phase,),
        ).fetchone()
        active_round = connection.execute(
            "SELECT phase_round FROM phases WHERE phase=?", (phase,)
        ).fetchone()
        if existing is not None and active_round is not None:
            if active_round[0] != phase_round:
                raise ValueError("Requested evaluation round differs from the authorized phase")
        expected_phase = (candidate_sha, cohort_digest, ledger_identity)
        preserved_start = None
        if existing is None:
            connection.execute(
                "INSERT INTO phases(phase,candidate_sha,cohort_digest,ledger_path,status) "
                "VALUES(?,?,?,?, 'open')",
                (phase, *expected_phase),
            )
        else:
            if existing[3] == "completed":
                raise ValueError("This weekly evaluation phase is already complete")
            if tuple(existing[:3]) == expected_phase:
                if not Path(ledger_identity).is_file():
                    raise ValueError(
                        "Authorized public-evaluation ledger is missing; budget cannot be reset"
                    )
            else:
                previous_candidate, previous_cohort, previous_path = existing[:3]
                previous_ledger = Path(previous_path)
                if (
                    phase != "diagnostic"
                    or not previous_ledger.is_relative_to(root)
                    or not previous_ledger.is_file()
                ):
                    raise ValueError(
                        "Weekly budget cannot reset for another candidate, cohort, or state path"
                    )
                try:
                    previous_state = _inspect_unspent_diagnostic_ledger(
                        previous_ledger,
                        candidate_sha=previous_candidate,
                        cohort_digest=previous_cohort,
                    )
                except ValueError as exc:
                    raise ValueError(
                        "Weekly budget cannot reset for another candidate, cohort, or state path"
                    ) from exc
                preserved_start = previous_state["started_at_utc"]

                successor_ledger = Path(ledger_identity)
                if successor_ledger.exists():
                    successor_state = _inspect_unspent_diagnostic_ledger(
                        successor_ledger,
                        candidate_sha=candidate_sha,
                        cohort_digest=cohort_digest,
                    )
                    if successor_state["started_at_utc"] != preserved_start:
                        raise ValueError(
                            "Diagnostic successor must preserve the original batch start time"
                        )
                elif finalize_candidate_successor:
                    raise ValueError(
                        "Diagnostic successor ledger must exist before authorization transfer"
                    )

                if finalize_candidate_successor:
                    connection.execute(
                        "INSERT INTO superseded_phase_candidates("
                        "phase,previous_candidate_sha,previous_cohort_digest,"
                        "previous_ledger_path,successor_candidate_sha,"
                        "successor_cohort_digest,successor_ledger_path,"
                        "original_started_at_utc,proof_sha256,superseded_at_utc) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            phase,
                            previous_candidate,
                            previous_cohort,
                            previous_path,
                            candidate_sha,
                            cohort_digest,
                            ledger_identity,
                            preserved_start,
                            previous_state["proof_sha256"],
                            now.isoformat(),
                        ),
                    )
                    connection.execute(
                        "UPDATE phases SET candidate_sha=?,cohort_digest=?,ledger_path=?,"
                        "status='open',report_sha256=NULL WHERE phase=?",
                        (candidate_sha, cohort_digest, ledger_identity, phase),
                    )
        connection.execute("COMMIT")
        return {
            "authorization_path": authorization_path,
            "batch_started_at_utc": preserved_start,
            "candidate_successor_pending": (
                existing is not None
                and tuple(existing[:3]) != expected_phase
                and not finalize_candidate_successor
            ),
        }
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def complete_public_evaluation_phase(
    root,
    *,
    phase,
    phase_round=1,
    candidate_sha,
    cohort_digest,
    ledger_path,
    report_sha256,
    diagnostic_report_sha256=None,
):
    """Seal one phase report so the next phase can consume its remaining budget."""
    root = Path(root).resolve()
    ledger_identity = _validate_identity(phase, candidate_sha, cohort_digest, ledger_path)
    if type(phase_round) is not int or phase_round not in {1, 2}:
        raise ValueError("Evaluation phase round is invalid")
    if phase != "diagnostic" and phase_round != 1:
        raise ValueError("Only diagnostic phases may have a round number")
    if type(report_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", report_sha256) is None:
        raise ValueError("A finalized live evaluation report digest is required")
    if not Path(ledger_identity).is_file():
        raise ValueError("The authorized public-evaluation ledger is missing")
    authorization_path = canonical_public_weekly_budget_path(root)
    connection = _connect(authorization_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT candidate_sha,cohort_digest,ledger_path,status,phase_round "
            "FROM phases WHERE phase=?",
            (phase,),
        ).fetchone()
        if existing != (
            candidate_sha,
            cohort_digest,
            ledger_identity,
            "open",
            phase_round,
        ):
            raise ValueError("Live report does not match the open weekly budget phase")
        if phase == "pilot":
            diagnostic = connection.execute(
                "SELECT report_sha256 FROM phases WHERE phase='diagnostic'"
            ).fetchone()
            if (
                diagnostic is None
                or diagnostic[0] != diagnostic_report_sha256
            ):
                raise ValueError("Pilot report diagnostic binding differs from authorization")
        connection.execute(
            "UPDATE phases SET status='completed',report_sha256=? WHERE phase=?",
            (report_sha256, phase),
        )
        connection.execute(
            "INSERT INTO phase_history(phase,phase_round,candidate_sha,cohort_digest,"
            "ledger_path,status,report_sha256) VALUES(?,?,?,?,?,'completed',?)",
            (
                phase,
                phase_round,
                candidate_sha,
                cohort_digest,
                ledger_identity,
                report_sha256,
            ),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def authorize_openrouter_diagnostic_continuation(
    root,
    *,
    parent_report_sha256,
    candidate_sha,
    cohort_digest,
    ledger_path,
):
    """Authorize one separately accounted OpenRouter probe bound to Qwen round 2."""
    root = Path(root).resolve()
    ledger_identity = _validate_identity(
        "diagnostic", candidate_sha, cohort_digest, ledger_path
    )
    if (
        type(parent_report_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", parent_report_sha256) is None
    ):
        raise ValueError("OpenRouter continuation requires the Qwen round-2 report digest")
    if not Path(ledger_identity).is_relative_to(root):
        raise ValueError("OpenRouter continuation ledger must belong to this project")
    if datetime.now(UTC) >= PUBLIC_EVALUATION_BUDGET_EXPIRES_UTC:
        raise ValueError("The approved public-evaluation budget window has expired")

    authorization_path = canonical_public_weekly_budget_path(root)
    repository_digest = hashlib.sha256(
        _repository_identity(root).encode("utf-8")
    ).hexdigest()
    connection = _connect(authorization_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        metadata = connection.execute(
            "SELECT budget_id,repository_digest,project_root,expires_at_utc "
            "FROM budget_metadata WHERE singleton=1"
        ).fetchone()
        expected_metadata = (
            PUBLIC_EVALUATION_BUDGET_ID,
            repository_digest,
            str(root),
            PUBLIC_EVALUATION_BUDGET_EXPIRES_UTC.isoformat(),
        )
        if metadata is None:
            raise ValueError(
                "OpenRouter continuation requires the existing weekly Qwen authorization"
            )
        if tuple(metadata) != expected_metadata:
            raise ValueError(
                "Weekly evaluation authorization is bound to another project or budget"
            )

        parent = connection.execute(
            "SELECT report_sha256,candidate_sha,cohort_digest,ledger_path FROM phase_history "
            "WHERE phase='diagnostic' AND phase_round=2 AND status='completed'"
        ).fetchone()
        if parent is None or parent[0] != parent_report_sha256:
            raise ValueError(
                "OpenRouter continuation must bind the completed Qwen round-2 report"
            )
        parent_clock = _read_diagnostic_batch_clock(
            parent[3], candidate_sha=parent[1], cohort_digest=parent[2]
        )

        existing = connection.execute(
            "SELECT parent_report_sha256,candidate_sha,cohort_digest,ledger_path,status "
            "FROM openrouter_diagnostic_continuation WHERE singleton=1"
        ).fetchone()
        expected = (
            parent_report_sha256,
            candidate_sha,
            cohort_digest,
            ledger_identity,
        )
        ledger = Path(ledger_identity)
        if existing is None:
            if ledger.exists():
                raise ValueError("OpenRouter continuation requires a new independent ledger")
            connection.execute(
                "INSERT INTO openrouter_diagnostic_continuation("
                "singleton,parent_report_sha256,candidate_sha,cohort_digest,ledger_path,status) "
                "VALUES(1,?,?,?,?, 'open')",
                expected,
            )
        elif tuple(existing[:4]) == expected:
            if existing[4] == "completed":
                raise ValueError("OpenRouter diagnostic continuation is already complete")
            if not ledger.is_file():
                raise ValueError("Authorized OpenRouter ledger is missing; budget cannot be reset")
            child_clock = _read_diagnostic_batch_clock(
                ledger,
                candidate_sha=candidate_sha,
                cohort_digest=cohort_digest,
            )
            if child_clock != parent_clock:
                raise ValueError("OpenRouter continuation must preserve the Qwen batch deadline")
        else:
            raise ValueError(
                "OpenRouter continuation cannot change its candidate, cohort or ledger"
            )
        connection.execute("COMMIT")
        return {
            "authorization_path": authorization_path,
            "parent_report_sha256": parent_report_sha256,
            **parent_clock,
        }
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def complete_openrouter_diagnostic_continuation(
    root,
    *,
    parent_report_sha256,
    candidate_sha,
    cohort_digest,
    ledger_path,
    report_sha256,
):
    """Seal the OpenRouter probe without mutating Qwen phase history."""
    root = Path(root).resolve()
    ledger_identity = _validate_identity(
        "diagnostic", candidate_sha, cohort_digest, ledger_path
    )
    if (
        type(parent_report_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", parent_report_sha256) is None
        or type(report_sha256) is not str
        or re.fullmatch(r"[a-f0-9]{64}", report_sha256) is None
    ):
        raise ValueError("OpenRouter report and parent report digests are required")
    if not Path(ledger_identity).is_file():
        raise ValueError("The authorized OpenRouter evaluation ledger is missing")
    parent_clock = openrouter_diagnostic_parent_clock(
        root,
        parent_report_sha256=parent_report_sha256,
        require_unexpired=False,
    )
    child_clock = _read_diagnostic_batch_clock(
        ledger_identity,
        candidate_sha=candidate_sha,
        cohort_digest=cohort_digest,
        require_unexpired=False,
    )
    if child_clock != parent_clock:
        raise ValueError("OpenRouter continuation must preserve the Qwen batch deadline")
    authorization_path = canonical_public_weekly_budget_path(root)
    connection = _connect(authorization_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT parent_report_sha256,candidate_sha,cohort_digest,ledger_path,status "
            "FROM openrouter_diagnostic_continuation WHERE singleton=1"
        ).fetchone()
        if existing != (
            parent_report_sha256,
            candidate_sha,
            cohort_digest,
            ledger_identity,
            "open",
        ):
            raise ValueError("OpenRouter report does not match the authorized continuation")
        connection.execute(
            "UPDATE openrouter_diagnostic_continuation SET status='completed',report_sha256=? "
            "WHERE singleton=1",
            (report_sha256,),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

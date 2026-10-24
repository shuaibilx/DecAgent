"""Persistent identity guard for the approved public-evaluation budget window."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
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
            report_sha256 TEXT
        );
        """
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


def authorize_public_evaluation_phase(
    root,
    *,
    phase,
    candidate_sha,
    cohort_digest,
    ledger_path,
    diagnostic_report_sha256=None,
):
    """Bind this budget window to one candidate and one diagnostic/pilot pair.

    The per-phase ledgers retain the detailed resource reservations. This external
    state prevents another candidate/cohort, checkout path, or deleted local ledger
    from obtaining a fresh copy of the authorized ceilings.
    """
    root = Path(root).resolve()
    ledger_identity = _validate_identity(phase, candidate_sha, cohort_digest, ledger_path)
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
        expected_phase = (candidate_sha, cohort_digest, ledger_identity)
        if existing is None:
            connection.execute(
                "INSERT INTO phases(phase,candidate_sha,cohort_digest,ledger_path,status) "
                "VALUES(?,?,?,?, 'open')",
                (phase, *expected_phase),
            )
        else:
            if tuple(existing[:3]) != expected_phase:
                raise ValueError(
                    "Weekly budget cannot reset for another candidate, cohort, or state path"
                )
            if existing[3] == "completed":
                raise ValueError("This weekly evaluation phase is already complete")
            if not Path(ledger_identity).is_file():
                raise ValueError(
                    "Authorized public-evaluation ledger is missing; budget cannot be reset"
                )
        connection.execute("COMMIT")
        return authorization_path
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
    candidate_sha,
    cohort_digest,
    ledger_path,
    report_sha256,
    diagnostic_report_sha256=None,
):
    """Seal one phase report so the next phase can consume its remaining budget."""
    root = Path(root).resolve()
    ledger_identity = _validate_identity(phase, candidate_sha, cohort_digest, ledger_path)
    if type(report_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", report_sha256) is None:
        raise ValueError("A finalized live evaluation report digest is required")
    if not Path(ledger_identity).is_file():
        raise ValueError("The authorized public-evaluation ledger is missing")
    authorization_path = canonical_public_weekly_budget_path(root)
    connection = _connect(authorization_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT candidate_sha,cohort_digest,ledger_path,status FROM phases WHERE phase=?",
            (phase,),
        ).fetchone()
        if existing != (candidate_sha, cohort_digest, ledger_identity, "open"):
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
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from decagent.evaluation.business import public_weekly_budget as budget
from decagent.evaluation.business.public_trials import (
    PublicTrialLedger,
    load_public_run_plan,
)
from evals.benchmarks.pilot_gate import build_bounded_diagnostic_plan


def _ledger(root, name):
    path = root / ".decagent" / "business-eval" / "public-trials" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def _write_batch_clock(path, *, candidate_sha, cohort_digest, started_at, deadline_at):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE batch_control(singleton INTEGER PRIMARY KEY,"
            "started_at_utc TEXT NOT NULL,deadline_at_utc TEXT NOT NULL);"
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [
                ("candidate_sha", json.dumps(candidate_sha)),
                ("cohort_digest", json.dumps(cohort_digest)),
                ("mode", json.dumps("smoke")),
            ],
        )
        connection.execute(
            "INSERT INTO batch_control(singleton,started_at_utc,deadline_at_utc) "
            "VALUES(1,?,?)",
            (started_at.isoformat(), deadline_at.isoformat()),
        )


def test_weekly_authorization_binds_one_diagnostic_and_its_pilot(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    candidate = "a" * 40
    diagnostic_digest = "b" * 64
    diagnostic_ledger = _ledger(root, "diagnostic.sqlite3")

    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=candidate,
        cohort_digest=diagnostic_digest,
        ledger_path=diagnostic_ledger,
    )
    with pytest.raises(ValueError, match="completed diagnostic"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="pilot",
            candidate_sha=candidate,
            cohort_digest="c" * 64,
            ledger_path=_ledger(root, "pilot.sqlite3"),
            diagnostic_report_sha256="d" * 64,
        )

    budget.complete_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=candidate,
        cohort_digest=diagnostic_digest,
        ledger_path=diagnostic_ledger,
        report_sha256="d" * 64,
    )
    pilot_ledger = _ledger(root, "pilot.sqlite3")
    budget.authorize_public_evaluation_phase(
        root,
        phase="pilot",
        candidate_sha=candidate,
        cohort_digest="c" * 64,
        ledger_path=pilot_ledger,
        diagnostic_report_sha256="d" * 64,
    )
    budget.complete_public_evaluation_phase(
        root,
        phase="pilot",
        candidate_sha=candidate,
        cohort_digest="c" * 64,
        ledger_path=pilot_ledger,
        report_sha256="e" * 64,
        diagnostic_report_sha256="d" * 64,
    )

    with pytest.raises(ValueError, match="already complete"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="pilot",
            candidate_sha=candidate,
            cohort_digest="c" * 64,
            ledger_path=pilot_ledger,
            diagnostic_report_sha256="d" * 64,
        )


@pytest.mark.parametrize(
    "candidate,cohort,name",
    [
        ("f" * 40, "b" * 64, "diagnostic.sqlite3"),
        ("a" * 40, "f" * 64, "diagnostic.sqlite3"),
        ("a" * 40, "b" * 64, "alternate.sqlite3"),
    ],
)
def test_weekly_authorization_rejects_candidate_cohort_or_state_reset(
    tmp_path, monkeypatch, candidate, cohort, name
):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    ledger = _ledger(root, "diagnostic.sqlite3")
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha="a" * 40,
        cohort_digest="b" * 64,
        ledger_path=ledger,
    )

    with pytest.raises(ValueError, match="another candidate, cohort, or state path"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="diagnostic",
            candidate_sha=candidate,
            cohort_digest=cohort,
            ledger_path=_ledger(root, name),
        )


def test_missing_open_ledger_does_not_recreate_its_weekly_budget(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    ledger = _ledger(root, "diagnostic.sqlite3")
    arguments = {
        "phase": "diagnostic",
        "candidate_sha": "a" * 40,
        "cohort_digest": "b" * 64,
        "ledger_path": ledger,
    }
    budget.authorize_public_evaluation_phase(root, **arguments)
    ledger.unlink()

    with pytest.raises(ValueError, match="ledger is missing"):
        budget.authorize_public_evaluation_phase(root, **arguments)


def _diagnostic_plan_for_root(root):
    source_root = Path(__file__).resolve().parents[2]
    plan = load_public_run_plan(
        source_root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    return replace(build_bounded_diagnostic_plan(plan), root=root)


def test_unspent_diagnostic_candidate_successor_preserves_deadline_and_audit(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    original = _diagnostic_plan_for_root(root)
    successor = replace(original, manifest_digest="frozen-successor-plan")
    old_candidate, new_candidate = "a" * 40, "b" * 40
    old_ledger_path = root / ".decagent" / "business-eval" / "old.sqlite3"
    new_ledger_path = root / ".decagent" / "business-eval" / "new.sqlite3"
    original_start = datetime.now(UTC) - timedelta(seconds=20)

    old_ledger = PublicTrialLedger.create(
        old_ledger_path, original, batch_started_at=original_start
    )
    old_ledger.bind_candidate_sha(old_candidate)
    old_ledger.close()
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=old_candidate,
        cohort_digest=original.cohort_digest,
        ledger_path=old_ledger_path,
    )

    staged = budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=new_candidate,
        cohort_digest=successor.cohort_digest,
        ledger_path=new_ledger_path,
    )
    assert staged["candidate_successor_pending"] is True
    assert staged["batch_started_at_utc"] == original_start.isoformat()

    new_ledger = PublicTrialLedger.create(
        new_ledger_path,
        successor,
        batch_started_at=original_start,
    )
    new_ledger.bind_candidate_sha(new_candidate)
    new_ledger.close()
    finalized = budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=new_candidate,
        cohort_digest=successor.cohort_digest,
        ledger_path=new_ledger_path,
        finalize_candidate_successor=True,
    )

    assert finalized["candidate_successor_pending"] is False
    assert finalized["batch_started_at_utc"] == original_start.isoformat()
    assert old_ledger_path.is_file()
    with sqlite3.connect(auth_path) as connection:
        active = connection.execute(
            "SELECT candidate_sha,cohort_digest,ledger_path,status FROM phases "
            "WHERE phase='diagnostic'"
        ).fetchone()
        history_count = connection.execute(
            "SELECT COUNT(*) FROM superseded_phase_candidates"
        ).fetchone()[0]
    assert active == (
        new_candidate,
        successor.cohort_digest,
        str(new_ledger_path.resolve()),
        "open",
    )
    assert history_count == 1


def test_started_diagnostic_cannot_be_rebound_to_a_successor(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    original = _diagnostic_plan_for_root(root)
    old_ledger_path = root / ".decagent" / "business-eval" / "started.sqlite3"
    old_candidate = "a" * 40
    ledger = PublicTrialLedger.create(old_ledger_path, original)
    ledger.bind_candidate_sha(old_candidate)
    trial = original.trials[0]
    ledger.start_route(trial["trial_id"], original.routes[0])
    ledger.close()
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha=old_candidate,
        cohort_digest=original.cohort_digest,
        ledger_path=old_ledger_path,
    )

    with pytest.raises(ValueError, match="another candidate, cohort, or state path"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="diagnostic",
            candidate_sha="b" * 40,
            cohort_digest="c" * 64,
            ledger_path=root / ".decagent" / "business-eval" / "successor.sqlite3",
        )


def test_one_explicit_second_diagnostic_round_preserves_the_first_report(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    first = {
        "phase": "diagnostic",
        "candidate_sha": "a" * 40,
        "cohort_digest": "b" * 64,
        "ledger_path": _ledger(root, "first-diagnostic.sqlite3"),
    }
    budget.authorize_public_evaluation_phase(root, **first)
    first_report = "c" * 64
    budget.complete_public_evaluation_phase(
        root, **first, report_sha256=first_report
    )

    second = {
        "phase": "diagnostic",
        "phase_round": 2,
        "candidate_sha": "d" * 40,
        "cohort_digest": "e" * 64,
        "ledger_path": root / ".decagent" / "business-eval" / "second-diagnostic.sqlite3",
    }
    authorized = budget.authorize_public_evaluation_phase(root, **second)
    assert authorized["phase_round"] == 2
    assert authorized["previous_report_sha256"] == first_report
    assert authorized["batch_started_at_utc"] is None

    second["ledger_path"].touch()
    second_report = "f" * 64
    budget.complete_public_evaluation_phase(
        root, **second, report_sha256=second_report
    )

    with sqlite3.connect(auth_path) as connection:
        history = connection.execute(
            "SELECT phase_round,candidate_sha,cohort_digest,report_sha256 "
            "FROM phase_history WHERE phase='diagnostic' ORDER BY phase_round"
        ).fetchall()
    assert history == [
        (1, "a" * 40, "b" * 64, first_report),
        (2, "d" * 40, "e" * 64, second_report),
    ]
    with pytest.raises(ValueError, match="already complete"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="diagnostic",
            phase_round=2,
            candidate_sha="d" * 40,
            cohort_digest="e" * 64,
            ledger_path=second["ledger_path"],
        )


def test_second_diagnostic_round_requires_completed_first_round_and_new_state(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )

    with pytest.raises(ValueError, match="completed first round"):
        budget.authorize_public_evaluation_phase(
            root,
            phase="diagnostic",
            phase_round=2,
            candidate_sha="a" * 40,
            cohort_digest="b" * 64,
            ledger_path=_ledger(root, "second.sqlite3"),
        )


def test_openrouter_continuation_is_independent_and_bound_to_qwen_round_two(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    first_ledger = _ledger(root, "qwen-round1.sqlite3")
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha="a" * 40,
        cohort_digest="b" * 64,
        ledger_path=first_ledger,
    )
    first_report = "c" * 64
    budget.complete_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha="a" * 40,
        cohort_digest="b" * 64,
        ledger_path=first_ledger,
        report_sha256=first_report,
    )
    second_ledger = root / ".decagent" / "business-eval" / "qwen-round2.sqlite3"
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        phase_round=2,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        ledger_path=second_ledger,
    )
    second_ledger.touch()
    original_start = datetime.now(UTC) - timedelta(minutes=1)
    original_deadline = original_start + timedelta(minutes=45)
    _write_batch_clock(
        second_ledger,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        started_at=original_start,
        deadline_at=original_deadline,
    )
    qwen_report = "f" * 64
    budget.complete_public_evaluation_phase(
        root,
        phase="diagnostic",
        phase_round=2,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        ledger_path=second_ledger,
        report_sha256=qwen_report,
    )

    openrouter_ledger = root / ".decagent" / "business-eval" / "openrouter.sqlite3"
    identity = {
        "parent_report_sha256": qwen_report,
        "candidate_sha": "1" * 40,
        "cohort_digest": "2" * 64,
        "ledger_path": openrouter_ledger,
    }
    authorized = budget.authorize_openrouter_diagnostic_continuation(root, **identity)
    assert authorized["parent_report_sha256"] == qwen_report
    assert authorized["batch_started_at_utc"] == original_start.isoformat()
    assert authorized["batch_deadline_at_utc"] == original_deadline.isoformat()
    with pytest.raises(ValueError, match="Qwen round-2 report"):
        budget.authorize_openrouter_diagnostic_continuation(
            root, **{**identity, "parent_report_sha256": "3" * 64}
        )

    openrouter_ledger.touch()
    _write_batch_clock(
        openrouter_ledger,
        candidate_sha=identity["candidate_sha"],
        cohort_digest=identity["cohort_digest"],
        started_at=original_start,
        deadline_at=original_deadline,
    )
    budget.authorize_openrouter_diagnostic_continuation(root, **identity)
    or_report = "4" * 64
    budget.complete_openrouter_diagnostic_continuation(
        root, **identity, report_sha256=or_report
    )
    with sqlite3.connect(auth_path) as connection:
        qwen_history = connection.execute(
            "SELECT phase_round,report_sha256 FROM phase_history "
            "WHERE phase='diagnostic' ORDER BY phase_round"
        ).fetchall()
        continuation = connection.execute(
            "SELECT parent_report_sha256,candidate_sha,cohort_digest,status,report_sha256 "
            "FROM openrouter_diagnostic_continuation WHERE singleton=1"
        ).fetchone()
    assert qwen_history == [(1, first_report), (2, qwen_report)]
    assert continuation == (qwen_report, "1" * 40, "2" * 64, "completed", or_report)
    with pytest.raises(ValueError, match="already complete"):
        budget.authorize_openrouter_diagnostic_continuation(root, **identity)


def test_openrouter_continuation_rejects_expired_parent_deadline(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    auth_path = tmp_path / "user-state" / "weekly.sqlite3"
    monkeypatch.setattr(
        budget, "canonical_public_weekly_budget_path", lambda *_args: auth_path
    )
    first_ledger = _ledger(root, "qwen-round1.sqlite3")
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha="a" * 40,
        cohort_digest="b" * 64,
        ledger_path=first_ledger,
    )
    budget.complete_public_evaluation_phase(
        root,
        phase="diagnostic",
        candidate_sha="a" * 40,
        cohort_digest="b" * 64,
        ledger_path=first_ledger,
        report_sha256="c" * 64,
    )
    second_ledger = root / ".decagent" / "business-eval" / "qwen-round2.sqlite3"
    budget.authorize_public_evaluation_phase(
        root,
        phase="diagnostic",
        phase_round=2,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        ledger_path=second_ledger,
    )
    second_ledger.touch()
    original_start = datetime.now(UTC) - timedelta(minutes=46)
    original_deadline = original_start + timedelta(minutes=45)
    _write_batch_clock(
        second_ledger,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        started_at=original_start,
        deadline_at=original_deadline,
    )
    qwen_report = "f" * 64
    budget.complete_public_evaluation_phase(
        root,
        phase="diagnostic",
        phase_round=2,
        candidate_sha="d" * 40,
        cohort_digest="e" * 64,
        ledger_path=second_ledger,
        report_sha256=qwen_report,
    )

    with pytest.raises(ValueError, match="original 45-minute diagnostic deadline has expired"):
        budget.authorize_openrouter_diagnostic_continuation(
            root,
            parent_report_sha256=qwen_report,
            candidate_sha="1" * 40,
            cohort_digest="2" * 64,
            ledger_path=root / ".decagent" / "business-eval" / "openrouter.sqlite3",
        )

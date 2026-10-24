import pytest

from decagent.evaluation.business import public_weekly_budget as budget


def _ledger(root, name):
    path = root / ".decagent" / "business-eval" / "public-trials" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


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

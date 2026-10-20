import multiprocessing
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest


def _hold_supervisor_lock(path, acquired, release):
    from decagent.evaluation.business.public_supervisor import PublicSupervisor

    with PublicSupervisor(path):
        acquired.set()
        release.wait(10)


def _paused_dispatched_route(root, selected, ledger_path, lock_path, global_jev_path,
                             ready, resume, results):
    from decagent.evaluation.business.ledger import GlobalJevLedger
    from decagent.evaluation.business.public_supervisor import PublicSupervisor
    from decagent.evaluation.business.public_trials import PublicTrialLedger

    supervisor = PublicSupervisor(lock_path)
    ledger = None
    try:
        supervisor.acquire()
        ledger = PublicTrialLedger.create(
            ledger_path,
            selected,
            global_jev_ledger=GlobalJevLedger(global_jev_path),
            lease_seconds=2,
        )
        trial, route = selected.trials[0], selected.routes[0]
        ledger.start_route(trial["trial_id"], route)
        attempt = ledger.reserve_model_attempt(
            trial_id=trial["trial_id"],
            route=route,
            role="primary",
            estimated_input_tokens=100,
            max_output_tokens=32,
            reserved_cost_usd=None,
            profile_digest=selected.profiles.primary.fingerprint,
        )
        ledger.mark_model_dispatched(attempt)
        ledger._heartbeat_stop.set()
        ledger._heartbeat_thread.join(timeout=3)
        results.put(("paused", attempt))
        ready.set()
        if not resume.wait(10):
            results.put(("old_owner_error", "resume signal timed out"))
            return
        try:
            ledger.mark_model_dispatched(attempt)
        except ValueError as exc:
            results.put(("old_owner_fenced", "lease was lost" in str(exc)))
        else:
            results.put(("old_owner_fenced", False))
    except Exception as exc:
        results.put(("old_owner_error", type(exc).__name__))
    finally:
        if ledger is not None:
            ledger.close()
        supervisor.release()


def _try_supervisor_takeover(lock_path, results):
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        PublicSupervisorBusy,
    )

    try:
        with PublicSupervisor(lock_path, acquire_timeout=0.1):
            results.put("acquired")
    except PublicSupervisorBusy:
        results.put("blocked")


def test_live_supervisor_lock_excludes_another_process_until_exit(tmp_path):
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        PublicSupervisorBusy,
    )

    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    lock_path = tmp_path / "public-evaluation.lock"
    process = context.Process(
        target=_hold_supervisor_lock,
        args=(str(lock_path), acquired, release),
    )
    process.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(PublicSupervisorBusy):
            PublicSupervisor(lock_path, acquire_timeout=0.1).acquire()
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0

    with PublicSupervisor(lock_path, acquire_timeout=0.5) as supervisor:
        assert supervisor.held


def test_supervisor_wait_is_bounded(tmp_path):
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        PublicSupervisorBusy,
    )

    path = tmp_path / "public-evaluation.lock"
    with PublicSupervisor(path):
        started = time.monotonic()
        with pytest.raises(PublicSupervisorBusy):
            PublicSupervisor(path, acquire_timeout=0.05).acquire()
        assert time.monotonic() - started < 1


def test_expired_ledger_lease_cannot_transfer_dispatch_while_old_process_holds_lock(tmp_path):
    from decagent.evaluation.business.ledger import (
        GlobalJevLedger,
        canonical_global_jev_ledger_path,
    )
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        canonical_public_supervisor_lock_path,
    )
    from decagent.evaluation.business.public_trials import (
        canonical_public_trial_ledger_path,
        load_public_run_plan,
        open_first_wave_public_trial_ledger,
    )

    root = Path(__file__).resolve().parents[2]
    project_root = tmp_path / "project"
    selected = replace(
        load_public_run_plan(
            root,
            mode="smoke",
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=project_root,
    )
    global_jev_path = canonical_global_jev_ledger_path(project_root)
    GlobalJevLedger(global_jev_path).initialize()
    ledger_path = canonical_public_trial_ledger_path(project_root)
    lock_path = canonical_public_supervisor_lock_path(project_root)

    context = multiprocessing.get_context("spawn")
    ready, resume = context.Event(), context.Event()
    results = context.Queue()
    old_owner = context.Process(
        target=_paused_dispatched_route,
        args=(
            str(project_root),
            selected,
            str(ledger_path),
            str(lock_path),
            str(global_jev_path),
            ready,
            resume,
            results,
        ),
    )
    old_owner.start()
    try:
        assert ready.wait(10)
        paused = results.get(timeout=1)
        assert paused[0] == "paused"
        attempt_id = paused[1]
        with sqlite3.connect(ledger_path) as connection:
            connection.execute(
                "UPDATE execution_lease SET expires_at_utc=? WHERE lease_name=?",
                ("2000-01-01T00:00:00+00:00", "public-evaluation"),
            )

        contender_results = context.Queue()
        contender = context.Process(
            target=_try_supervisor_takeover,
            args=(str(lock_path), contender_results),
        )
        contender.start()
        contender.join(5)
        assert contender.exitcode == 0
        assert contender_results.get(timeout=1) == "blocked"

        resume.set()
        assert results.get(timeout=5) == ("old_owner_fenced", True)
        old_owner.join(5)
        assert old_owner.exitcode == 0
    finally:
        resume.set()
        if old_owner.is_alive():
            old_owner.terminate()
            old_owner.join(5)

    with PublicSupervisor(lock_path) as supervisor:
        resumed = open_first_wave_public_trial_ledger(
            project_root, selected, supervisor=supervisor, lease_seconds=2
        )
        trial, route = selected.trials[0], selected.routes[0]
        assert resumed.route_status(trial["trial_id"], route) == "unknown"
        assert resumed.attempt_status(attempt_id) == "unknown"
        with pytest.raises(ValueError, match="cannot be replayed"):
            resumed.start_route(trial["trial_id"], route)
        resumed.close()

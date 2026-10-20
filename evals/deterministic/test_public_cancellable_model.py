"""Process cancellation primitives for one-shot public model dispatches."""

import json
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _return_value():
    return {"status": "ok", "usage": {"input": 3, "output": 2}}


def _wait_forever():
    while True:
        time.sleep(1)


def _signal_then_wait(release, started):
    started.set()
    if not release.wait(5):
        raise TimeoutError("test worker release was not signaled")
    return {"status": "released"}


def _mark_called(event):
    event.set()
    return {"status": "called"}


def _enter_worker_with_dead_parent(connection, target_called):
    from decagent.evaluation.business import cancellable_model

    class DeadParent:
        @staticmethod
        def is_alive():
            return False

    cancellable_model.multiprocessing.parent_process = lambda: DeadParent()
    cancellable_model._process_entry(connection, _mark_called, (target_called,), None)


def _spawn_descendant_and_wait(pid_queue):
    process = multiprocessing.get_context("spawn").Process(target=_wait_forever)
    process.start()
    pid_queue.put(process.pid)
    while True:
        time.sleep(1)


def _process_is_alive(pid):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancellable_process_returns_a_bounded_result():
    from decagent.evaluation.business.cancellable_model import run_cancellable_process

    result = run_cancellable_process(_return_value, timeout_seconds=3)

    assert result == {"status": "ok", "usage": {"input": 3, "output": 2}}


def test_one_shot_worker_will_not_run_provider_target_after_parent_exit():
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    target_called = context.Event()
    process = context.Process(
        target=_enter_worker_with_dead_parent,
        args=(child_connection, target_called),
    )
    process.start()
    child_connection.close()
    try:
        frame = json.loads(parent_connection.recv_bytes().decode("utf-8"))
        process.join(5)
        assert process.exitcode == 0
        assert frame == {
            "ok": False,
            "stage": "isolation_setup",
            "exception_type": "RuntimeError",
        }
        assert not target_called.is_set()
    finally:
        parent_connection.close()
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()


def test_cancellable_process_deadline_reaps_worker_and_descendant():
    from decagent.evaluation.business.cancellable_model import (
        CancellableProcessTimeout,
        run_cancellable_process,
    )

    context = multiprocessing.get_context("spawn")
    descendants = context.Queue()
    started = time.monotonic()
    try:
        with pytest.raises(CancellableProcessTimeout):
            run_cancellable_process(
                _spawn_descendant_and_wait,
                (descendants,),
                timeout_seconds=0.5,
            )
        descendant_pid = descendants.get(timeout=2)
        assert time.monotonic() - started < 5
        assert not _process_is_alive(descendant_pid), "A child of the timed-out worker leaked"
    finally:
        descendants.close()
        descendants.join_thread()


def test_posix_process_tree_termination_sweeps_descendant_started_during_shutdown(
    monkeypatch,
):
    from types import SimpleNamespace

    from decagent.evaluation.business import cancellable_model

    descendant_alive = True
    kill_attempts = []

    def kill_group(group_id, signum):
        nonlocal descendant_alive
        kill_attempts.append((group_id, signum))
        if len(kill_attempts) > 1:
            descendant_alive = False

    class Process:
        pid = 12345

        def __init__(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout == 2
            self.alive = False

        def kill(self):
            self.alive = False

    monkeypatch.setattr(
        cancellable_model,
        "os",
        SimpleNamespace(name="posix", killpg=kill_group),
    )
    monkeypatch.setattr(cancellable_model, "signal", SimpleNamespace(SIGKILL=9))

    cancellable_model._terminate_process_tree(Process())

    assert kill_attempts == [(12345, cancellable_model.signal.SIGKILL)] * 2
    assert not descendant_alive


def test_active_one_shot_worker_blocks_supervisor_takeover_after_owner_lock_release(
    tmp_path,
):
    from decagent.evaluation.business.cancellable_model import run_cancellable_process
    from decagent.evaluation.business.public_supervisor import (
        PublicSupervisor,
        PublicSupervisorBusy,
        canonical_public_worker_guard_path,
    )

    context = multiprocessing.get_context("spawn")
    release, started = context.Event(), context.Event()
    root = tmp_path / "project"
    supervisor_lock = root / ".decagent" / "business-eval" / "public-evaluation.lock"
    guard_path = canonical_public_worker_guard_path(root)
    owner = PublicSupervisor(supervisor_lock)
    owner.acquire()
    outcome = {}

    def invoke():
        try:
            outcome["result"] = run_cancellable_process(
                _signal_then_wait,
                (release, started),
                timeout_seconds=4,
                worker_guard_path=guard_path,
            )
        except BaseException as exc:  # surfaced in the test thread below
            outcome["error"] = exc

    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert started.wait(5)
        # Simulate a dead/suspended supervisor whose main lock no longer
        # fences a still-live provider subprocess.
        owner.release()
        with pytest.raises(PublicSupervisorBusy, match="worker"):
            PublicSupervisor(supervisor_lock).acquire()
    finally:
        release.set()
        caller.join(8)
        owner.release()

    assert not caller.is_alive()
    assert "error" not in outcome
    assert outcome["result"] == {"status": "released"}
    with PublicSupervisor(supervisor_lock):
        pass


def test_public_primary_response_is_decoded_from_a_one_shot_child(monkeypatch):
    from decagent.business.plans.planner import plan_tool_definitions
    from decagent.evaluation.business import cancellable_model
    from decagent.evaluation.business.public_models import (
        OneShotPrimaryModel,
        _one_shot_primary_dispatch,
    )
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    profile = load_public_run_plan(
        root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    ).profiles.primary
    model = OneShotPrimaryModel(
        profile,
        api_key="test-secret",
        tool_definitions=plan_tool_definitions(),
    )
    captured = {}

    def dispatch(target, args, *, timeout_seconds, worker_guard_path=None):
        assert target is _one_shot_primary_dispatch
        captured["args"] = args
        captured["timeout_seconds"] = timeout_seconds
        captured["worker_guard_path"] = worker_guard_path
        return {
            "kind": "turn",
            "turn": {
                "text": "",
                "tools": [
                    {
                        "id": "call-1",
                        "name": "decagent_submit_plan",
                        "params": {"nodes": []},
                    }
                ],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
                "model": profile.model,
                "server_request_id": "provider-request-1",
            },
        }

    monkeypatch.setattr(cancellable_model, "run_cancellable_process", dispatch)
    turn = model.respond(
        [{"role": "user", "content": "produce a plan"}],
        timeout_seconds=0.25,
    )

    assert turn.usage.total_tokens == 10
    assert turn.tools[0].name == "decagent_submit_plan"
    assert captured["timeout_seconds"] == 0.25
    assert captured["worker_guard_path"] is None
    assert captured["args"][1] == "test-secret"
    assert captured["args"][2] == ("test-secret",)
    assert not hasattr(model, "client")
    assert not hasattr(model, "known_secrets")
    model.close()


def test_tau_public_model_uses_one_shot_primary_factory_by_default():
    from decagent.evaluation.business.public_models import (
        PublicTauModelCallback,
        _one_shot_primary_factory,
        public_tau_model_callback_factory,
    )
    from decagent.evaluation.business.public_trials import load_public_run_plan

    root = Path(__file__).resolve().parents[2]
    profiles = load_public_run_plan(
        root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    ).profiles
    factory = public_tau_model_callback_factory(
        profiles.user_simulator,
        role="user_simulator",
        api_key="test-secret",
    )
    callback = factory(
        SimpleNamespace(),
        SimpleNamespace(),
        "react_skill_mcp",
    )

    assert isinstance(callback, PublicTauModelCallback)
    assert callback.primary_factory is _one_shot_primary_factory
    assert callback._clients == {}


def test_timed_out_provider_child_marks_attempt_unknown_and_cannot_replay(tmp_path):
    from decagent.evaluation.business.cancellable_model import (
        run_cancellable_process,
    )
    from decagent.evaluation.business.public_resources import (
        LedgeredModelCallback,
        PublicModelOutcomeUnknown,
    )
    from decagent.evaluation.business.public_trials import (
        PublicTrialLedger,
        load_public_run_plan,
    )

    root = Path(__file__).resolve().parents[2]
    plan = load_public_run_plan(
        root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    ledger = PublicTrialLedger.create(tmp_path / "public.sqlite3", plan)
    trial_id = plan.trials[0]["trial_id"]
    route = "react_skill_mcp"
    ledger.start_route(trial_id, route)
    reservation = ledger.reserve_machine_time(
        trial_id=trial_id, route=route, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    descendants = multiprocessing.get_context("spawn").Queue()
    calls = []

    def dispatch(_request, *, timeout_seconds):
        calls.append(timeout_seconds)
        return run_cancellable_process(
            _spawn_descendant_and_wait,
            (descendants,),
            timeout_seconds=timeout_seconds,
        )

    measured = LedgeredModelCallback(
        dispatch,
        ledger,
        profile=plan.profiles.primary,
        trial_id=trial_id,
        route=route,
        input_token_upper_bound=lambda _request: 100,
        route_remaining_seconds=lambda: 0.5,
    )
    try:
        with pytest.raises(PublicModelOutcomeUnknown):
            measured({"messages": [{"role": "user", "content": "wait"}]})
        child_pid = descendants.get(timeout=2)
        assert ledger.route_status(trial_id, route) == "unknown"
        assert ledger.model_attempts_snapshot(trial_id, route)[0]["status"] == "unknown"
        assert not _process_is_alive(child_pid)
        with pytest.raises(ValueError, match="unresolved or unknown model usage"):
            measured({"messages": [{"role": "user", "content": "do not replay"}]})
        assert calls == [0.5]
    finally:
        descendants.close()
        descendants.join_thread()
        ledger.close()

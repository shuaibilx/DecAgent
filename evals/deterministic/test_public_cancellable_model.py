"""Process cancellation primitives for one-shot public model dispatches."""

import json
import multiprocessing
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest


def _return_value():
    return {"status": "ok", "usage": {"input": 3, "output": 2}}


def _return_unencodable_value(kind):
    if kind == "nonfinite":
        return {"value": float("nan")}
    if kind == "surrogate":
        return {"value": "\ud800"}
    raise ValueError("Unknown test fixture")


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
    if sys.platform.startswith("linux"):
        try:
            stat = Path("/proc", str(pid), "stat").read_text(encoding="ascii")
        except FileNotFoundError:
            return False
        except OSError:
            stat = None
        if stat is not None:
            closing_paren = stat.rfind(")")
            fields = stat[closing_paren + 2 :].split() if closing_paren >= 0 else []
            if fields and fields[0] in {"Z", "X"}:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancellable_process_returns_a_bounded_result():
    from decagent.evaluation.business.cancellable_model import run_cancellable_process

    result = run_cancellable_process(_return_value, timeout_seconds=3)

    assert result == {"status": "ok", "usage": {"input": 3, "output": 2}}


@pytest.mark.parametrize(
    ("kind", "exception_type"),
    [("nonfinite", "ValueError"), ("surrogate", "UnicodeEncodeError")],
)
def test_unencodable_child_result_is_classified_as_response_failure(kind, exception_type):
    from decagent.evaluation.business.cancellable_model import (
        CancellableProcessError,
        run_cancellable_process,
    )

    with pytest.raises(CancellableProcessError) as raised:
        run_cancellable_process(
            _return_unencodable_value,
            (kind,),
            timeout_seconds=3,
        )

    assert raised.value.stage == "response"
    assert raised.value.exception_type == exception_type


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


def test_linux_process_group_scan_waits_for_active_members_not_zombies(tmp_path):
    from decagent.evaluation.business.cancellable_model import (
        _linux_process_group_has_active_members,
    )

    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    target_group = 4242

    def write_stat(pid, comm, state, process_group):
        stat_path = proc_root / str(pid) / "stat"
        stat_path.parent.mkdir()
        stat_path.write_text(
            f"{pid} ({comm}) {state} 1 {process_group} {process_group} 0 0 0\n",
            encoding="ascii",
        )
        return stat_path

    active = write_stat(100, "python (worker)", "D", target_group)
    write_stat(101, "python-zombie", "Z", target_group)
    write_stat(102, "other-group", "R", target_group + 1)

    assert _linux_process_group_has_active_members(target_group, proc_root=proc_root)

    active.write_text(
        f"100 (python (worker)) Z 1 {target_group} {target_group} 0 0 0\n",
        encoding="ascii",
    )
    assert not _linux_process_group_has_active_members(target_group, proc_root=proc_root)


def test_linux_process_group_scan_fails_closed_when_inventory_is_incomplete(
    monkeypatch, tmp_path
):
    from decagent.evaluation.business.cancellable_model import (
        _linux_process_group_has_active_members,
    )

    proc_root = tmp_path / "proc"
    process_stat = proc_root / "100" / "stat"
    process_stat.parent.mkdir(parents=True)
    process_stat.write_text("100 (worker) R 1 4242 4242 0 0 0\n", encoding="ascii")
    original_read_text = Path.read_text

    def unreadable_stat(path, *args, **kwargs):
        if path == process_stat:
            raise PermissionError("simulated incomplete proc entry")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_stat)

    assert _linux_process_group_has_active_members(4242, proc_root=proc_root) is None


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


@pytest.mark.parametrize(
    ("worker_stage", "expected_stage"),
    [("target", "model_worker_target"), ("response", "model_worker_response")],
)
def test_public_primary_exposes_only_allowlisted_worker_diagnostics(
    monkeypatch, worker_stage, expected_stage
):
    from decagent.business.plans.planner import plan_tool_definitions
    from decagent.evaluation.business import cancellable_model
    from decagent.evaluation.business.public_models import (
        ModelWorkerOutcomeUnknown,
        OneShotPrimaryModel,
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

    def dispatch(*_args, **_kwargs):
        raise cancellable_model.CancellableProcessError(
            stage=worker_stage, exception_type="ConnectTimeout"
        )

    monkeypatch.setattr(cancellable_model, "run_cancellable_process", dispatch)
    with pytest.raises(ModelWorkerOutcomeUnknown) as raised:
        model.respond([{"role": "user", "content": "safe test"}])

    assert raised.value.failure_stage == expected_stage
    assert raised.value.diagnostic_exception_type == "ConnectTimeout"
    assert "test-secret" not in str(raised.value)
    assert "ConnectTimeout" not in str(raised.value)
    model.close()


def test_one_shot_primary_client_setup_error_is_verified_not_dispatched(monkeypatch):
    from decagent.evaluation.business import public_models

    class BrokenFactory:
        @staticmethod
        def from_evaluation_profile(*_args, **_kwargs):
            raise ValueError("private setup detail and credential")

    monkeypatch.setattr(public_models, "PrimaryModel", BrokenFactory)
    result = public_models._one_shot_primary_dispatch(
        None, "test-secret", (), None, [], 1.0
    )

    assert result == {"kind": "not_dispatched", "reason": "input_invalid"}
    assert "private" not in repr(result)
    assert "test-secret" not in repr(result)


def test_one_shot_primary_model_call_error_stays_unknown_with_safe_stage(monkeypatch):
    from decagent.evaluation.business import public_models

    class BrokenClient:
        closed = False

        def respond(self, *_args, **_kwargs):
            raise ValueError("private provider response and credential")

        def close(self):
            self.closed = True

    client = BrokenClient()

    class TestFactory:
        @staticmethod
        def from_evaluation_profile(*_args, **_kwargs):
            return client

    monkeypatch.setattr(public_models, "PrimaryModel", TestFactory)
    result = public_models._one_shot_primary_dispatch(
        None, "test-secret", (), None, [], 1.0
    )

    assert result == {
        "kind": "worker_outcome_unknown",
        "worker_stage": "model_call",
        "exception_type": "ValueError",
    }
    assert client.closed
    assert "private" not in repr(result)
    assert "test-secret" not in repr(result)


@pytest.mark.parametrize(
    ("response_status", "expected_stage"),
    [(None, "request_pre_send"), (502, "provider_response")],
)
def test_one_shot_primary_records_only_safe_http_boundary_diagnostics(
    monkeypatch, response_status, expected_stage
):
    from decagent.evaluation.business import public_models

    class BrokenClient:
        closed = False

        def respond(self, *_args, **_kwargs):
            raise ValueError("private provider body and credential")

        def close(self):
            self.closed = True

    client = BrokenClient()

    class TestFactory:
        @staticmethod
        def from_evaluation_profile(*_args, **kwargs):
            hooks = kwargs["http_client"].event_hooks
            hooks["request"][0](object())
            if response_status is not None:
                hooks["response"][0](type("Response", (), {"status_code": response_status})())
            return client

    monkeypatch.setattr(public_models, "PrimaryModel", TestFactory)
    result = public_models._one_shot_primary_dispatch(
        None, "test-secret", (), None, [], 1.0
    )

    expected = {
        "kind": "worker_outcome_unknown",
        "worker_stage": expected_stage,
        "exception_type": "ValueError",
    }
    if response_status is not None:
        expected["http_status"] = response_status
    assert result == expected
    assert client.closed
    assert "private" not in repr(result)
    assert "test-secret" not in repr(result)


def test_primary_transport_observer_uses_httpx_hooks_without_retaining_content():
    import httpx2

    from decagent.evaluation.business.public_models import _PrimaryTransportObservation

    observation = _PrimaryTransportObservation()
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(503)),
        event_hooks={
            "request": [observation.on_request],
            "response": [observation.on_response],
        },
    )
    try:
        response = client.post("https://provider.invalid/chat", content="private prompt")
    finally:
        client.close()

    assert response.status_code == 503
    assert observation.request_prepared is True
    assert observation.response_status == 503
    assert set(vars(observation)) == {"request_prepared", "response_status"}
    assert "private prompt" not in repr(vars(observation))


def test_one_shot_primary_response_framing_error_has_response_stage(monkeypatch):
    from decagent.evaluation.business import public_models

    class ClientWithUnserializableTurn:
        closed = False

        def respond(self, *_args, **_kwargs):
            return object()

        def close(self):
            self.closed = True

    client = ClientWithUnserializableTurn()

    class TestFactory:
        @staticmethod
        def from_evaluation_profile(*_args, **_kwargs):
            return client

    monkeypatch.setattr(public_models, "PrimaryModel", TestFactory)
    result = public_models._one_shot_primary_dispatch(
        None, "test-secret", (), None, [], 1.0
    )

    assert result == {
        "kind": "worker_outcome_unknown",
        "worker_stage": "response",
        "exception_type": "AttributeError",
    }
    assert client.closed
    assert "test-secret" not in repr(result)


def test_one_shot_primary_decodes_model_call_unknown_stage(monkeypatch):
    from decagent.evaluation.business import cancellable_model
    from decagent.evaluation.business.public_models import (
        ModelWorkerOutcomeUnknown,
        OneShotPrimaryModel,
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
    model = OneShotPrimaryModel(profile, api_key="test-secret")

    def dispatch(*_args, **_kwargs):
        return {
            "kind": "worker_outcome_unknown",
            "worker_stage": "model_call",
            "exception_type": "ValueError",
        }

    monkeypatch.setattr(cancellable_model, "run_cancellable_process", dispatch)
    with pytest.raises(ModelWorkerOutcomeUnknown) as raised:
        model.respond([{"role": "user", "content": "safe test"}])

    assert raised.value.failure_stage == "model_worker_model_call"
    assert raised.value.diagnostic_exception_type == "ValueError"
    assert "test-secret" not in str(raised.value)
    from decagent.evaluation.business.public_diagnostics import validate_diagnostic

    validate_diagnostic(
        failure_stage=raised.value.failure_stage,
        exception_type=raised.value.diagnostic_exception_type,
    )
    model.close()


def test_one_shot_primary_decodes_received_http_response_diagnostics(monkeypatch):
    from decagent.evaluation.business import cancellable_model
    from decagent.evaluation.business.public_diagnostics import (
        safe_failure_stage,
        safe_http_status,
        validate_diagnostic,
    )
    from decagent.evaluation.business.public_models import (
        ModelWorkerOutcomeUnknown,
        OneShotPrimaryModel,
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
    model = OneShotPrimaryModel(profile, api_key="test-secret")

    def dispatch(*_args, **_kwargs):
        return {
            "kind": "worker_outcome_unknown",
            "worker_stage": "provider_response",
            "exception_type": "ValueError",
            "http_status": 502,
        }

    monkeypatch.setattr(cancellable_model, "run_cancellable_process", dispatch)
    with pytest.raises(ModelWorkerOutcomeUnknown) as raised:
        model.respond([{"role": "user", "content": "safe test"}])

    assert safe_failure_stage(raised.value) == "model_worker_provider_response"
    assert safe_http_status(raised.value) == 502
    validate_diagnostic(
        failure_stage=raised.value.failure_stage,
        exception_type=raised.value.diagnostic_exception_type,
        http_status=raised.value.status_code,
    )
    assert "test-secret" not in str(raised.value)
    model.close()


def test_one_shot_primary_records_local_provider_response_without_leaking_content():
    from dataclasses import replace

    from decagent.evaluation.business.public_diagnostics import validate_diagnostic
    from decagent.evaluation.business.public_models import (
        ModelWorkerOutcomeUnknown,
        OneShotPrimaryModel,
    )
    from decagent.evaluation.business.public_trials import load_public_run_plan

    class MalformedResponseHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            # Return an invalid response body without reading or retaining the
            # request body. This test must never expose prompt data or a secret.
            body = b"{malformed"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), MalformedResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = None
    try:
        root = Path(__file__).resolve().parents[2]
        profile = load_public_run_plan(
            root,
            mode="smoke",
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ).profiles.primary
        # ModelProfile correctly rejects non-HTTPS provider endpoints. The
        # post-validation override is confined to this test's localhost server.
        profile = replace(profile)
        object.__setattr__(
            profile,
            "endpoint",
            f"http://127.0.0.1:{server.server_port}/v1",
        )
        model = OneShotPrimaryModel(profile, api_key="test-secret")

        with pytest.raises(ModelWorkerOutcomeUnknown) as raised:
            model.respond(
                [{"role": "user", "content": "offline diagnostic fixture"}],
                timeout_seconds=10,
            )

        error = raised.value
        assert error.failure_stage == "model_worker_provider_response"
        assert error.status_code == 200
        validate_diagnostic(
            failure_stage=error.failure_stage,
            exception_type=error.diagnostic_exception_type,
            http_status=error.status_code,
        )
        assert "test-secret" not in str(error)
        assert "offline diagnostic fixture" not in str(error)
    finally:
        if model is not None:
            model.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


def test_tau_user_simulator_callback_round_trips_registered_tool_over_local_http():
    from decagent.evaluation.business.public_models import (
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
    requests = []

    class OpenAICompatibleHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            body = json.dumps(
                {
                    "id": "chatcmpl-tau-loopback-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": profiles.user_simulator.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-user-1",
                                        "type": "function",
                                        "function": {
                                            "name": "provide_booking_reference",
                                            "arguments": '{"reference":"R-123"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 17,
                        "completion_tokens": 5,
                        "total_tokens": 22,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", "request-tau-loopback-1")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAICompatibleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    callback = None
    try:
        profile = profiles.user_simulator
        object.__setattr__(
            profile, "endpoint", f"http://127.0.0.1:{server.server_port}/v1"
        )
        callback = public_tau_model_callback_factory(
            profile,
            role="user_simulator",
            api_key="local-loopback-secret",
        )(SimpleNamespace(), SimpleNamespace(), "react_skill_mcp")
        tool_schema = {
            "type": "function",
            "function": {
                "name": "provide_booking_reference",
                "description": "Provide the reference supplied by the user.",
                "parameters": {
                    "type": "object",
                    "properties": {"reference": {"type": "string"}},
                    "required": ["reference"],
                    "additionalProperties": False,
                },
            },
        }

        result = callback(
            {
                "model": profile.model,
                "messages": [{"role": "user", "content": "The reference is R-123."}],
                "tools": [tool_schema],
            },
            timeout_seconds=5,
        )

        assert result.value == {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-user-1",
                    "name": "provide_booking_reference",
                    "arguments": {"reference": "R-123"},
                }
            ],
        }
        assert result.input_tokens == 17
        assert result.output_tokens == 5
        assert result.model == profile.model
        assert result.provider_request_id == "request-tau-loopback-1"
        assert result.generation_id is None
        assert requests[0]["model"] == profile.model
        assert requests[0]["tools"][0]["function"]["name"] == "provide_booking_reference"
    finally:
        if callback is not None:
            callback.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tau_user_simulator_callback_accepts_empty_tool_list_over_local_http():
    from decagent.evaluation.business.public_models import (
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
    requests = []

    class OpenAICompatibleHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(
                {
                    "id": "chatcmpl-tau-text-only-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": profiles.user_simulator.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "I can help with that.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 6,
                        "total_tokens": 19,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-request-id", "request-tau-text-only-1")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAICompatibleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    callback = None
    try:
        profile = profiles.user_simulator
        object.__setattr__(
            profile, "endpoint", f"http://127.0.0.1:{server.server_port}/v1"
        )
        callback = public_tau_model_callback_factory(
            profile,
            role="user_simulator",
            api_key="local-loopback-secret",
        )(SimpleNamespace(), SimpleNamespace(), "react_skill_mcp")

        result = callback(
            {
                "model": profile.model,
                "messages": [{"role": "user", "content": "I need help."}],
                "tools": [],
            },
            timeout_seconds=5,
        )

        assert result.value == {
            "content": "I can help with that.",
            "tool_calls": [],
        }
        assert result.input_tokens == 13
        assert result.output_tokens == 6
        assert result.provider_request_id == "request-tau-text-only-1"
        assert len(requests) == 1
        assert requests[0].get("tools") in (None, [])
    finally:
        if callback is not None:
            callback.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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

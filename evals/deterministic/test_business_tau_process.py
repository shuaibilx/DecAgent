"""Process isolation and original-tool forwarding, without model requests."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


def bridge():
    assert importlib.util.find_spec("decagent.evaluation.business.tau_process") is not None, (
        "locked tau runtime subprocess bridge is not implemented"
    )
    from decagent.evaluation.business import tau_process

    return tau_process


def test_tau_failure_stage_allowlists_match_worker_and_public_diagnostics():
    module = bridge()
    worker_path = Path(module.__file__).with_name("tau_worker.py")
    spec = importlib.util.spec_from_file_location("_tau_worker_stage_contract", worker_path)
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    from decagent.evaluation.business.public_diagnostics import FAILURE_STAGES

    assert module.TAU_WORKER_FAILURE_STAGES == worker._FAILURE_STAGES
    assert worker._FAILURE_STAGES <= FAILURE_STAGES
    assert module.TauBridgeError("invalid stage", failure_stage=[]).failure_stage is None


def test_tau_mcp_environment_uses_official_environment_policy_when_unspecified():
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    policy = "<main_policy>verified upstream policy</main_policy>"

    class Environment:
        closed = False

        def get_policy(self):
            return policy

        def get_tools(self):
            return []

        def close(self):
            self.closed = True

        @property
        def cleanup_verified(self):
            return self.closed

    environment = Environment()
    with TauMCPEnvironment(
        lambda: environment,
        system_prompt=None,
        user_prompt="Use the frozen business workflow.",
    ) as session:
        assert session.model_input()["system_prompt"] == policy
    assert session.cleanup_verified is True
    assert environment.closed is True


@pytest.fixture
def worker(tmp_path):
    script = tmp_path / "protocol_fixture.py"
    script.write_text(
        """import json, os, subprocess, sys, time
for line in sys.stdin.buffer:
    request = json.loads(line)
    method = request["method"]
    if method == "timeout":
        time.sleep(20)
    if method == "exit":
        sys.exit(3)
    if method == "oversize":
        print("x" * 20000, flush=True)
        continue
    if method == "malformed":
        print("not-json", flush=True)
        continue
    value = {"params": request["params"], "pid": os.getpid(),
             "secret_inherited": bool(os.environ.get("DECAGENT_JEV_API_KEY"))}
    if method == "spawn-child":
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        value["child_pid"] = child.pid
    print(json.dumps({"id": request["id"] + (1 if method == "wrong_id" else 0),
                      "ok": True, "result": value}), flush=True)
""",
        encoding="utf-8",
    )
    return script


def channel(worker, **kwargs):
    module = bridge()
    return module._StdioChannel(
        [sys.executable, "-I", str(worker)],
        cwd=worker.parent,
        environment=module._worker_environment(worker.parent),
        timeout=kwargs.get("timeout", 3),
        max_frame=kwargs.get("max_frame", 4096),
    )


def test_user_tool_batch_is_validated_and_forwarded_as_one_simulator_input():
    environment_type = bridge().TauProcessEnvironment
    environment = object.__new__(environment_type)

    class Channel:
        timeout = 15

        def __init__(self):
            self.requests = []

        def request(
            self,
            method,
            params,
            *,
            on_callback,
            timeout_seconds=None,
            timeout_cap_seconds=None,
        ):
            self.requests.append((method, params, on_callback))
            return {"message": {"role": "user", "content": "Done."}, "stop": False}

    channel = Channel()
    environment._channel = channel
    batch = {
        "role": "tool",
        "requestor": "user",
        "tool_messages": [
            {
                "id": "user-1",
                "role": "tool",
                "requestor": "user",
                "content": "first result",
                "error": False,
            },
            {
                "id": "user-2",
                "role": "tool",
                "requestor": "user",
                "content": "second result",
                "error": False,
            },
        ],
    }

    result = environment.user_turn(
        batch,
        model="qwen-test",
        on_model_call=lambda _event, *, timeout_seconds: {"content": "unused"},
    )

    assert result["message"]["content"] == "Done."
    assert len(channel.requests) == 1
    assert channel.requests[0][0:2] == (
        "user_turn",
        {"message": batch, "model": "qwen-test"},
    )
    assert channel.timeout == 15


@pytest.mark.parametrize(
    "batch",
    [
        {"role": "tool", "requestor": "assistant", "tool_messages": []},
        {
            "role": "tool",
            "requestor": "user",
            "tool_messages": [
                {
                    "id": "user-1",
                    "role": "tool",
                    "requestor": "assistant",
                    "content": "result",
                    "error": False,
                }
            ],
        },
        {
            "role": "tool",
            "requestor": "user",
            "tool_messages": [
                {
                    "id": "user-1",
                    "role": "tool",
                    "requestor": "user",
                    "content": "result",
                    "error": False,
                }
            ]
            * 9,
        },
    ],
)
def test_invalid_user_tool_batch_is_rejected_before_process_dispatch(batch):
    environment = object.__new__(bridge().TauProcessEnvironment)

    class Channel:
        timeout = 15
        requests = 0

        def request(self, *_args, **_kwargs):
            self.requests += 1
            raise AssertionError("Invalid tool batch must not reach the owned process")

    channel = Channel()
    environment._channel = channel
    with pytest.raises(ValueError, match="user-tool message batch"):
        environment.user_turn(
            batch,
            model="qwen-test",
            on_model_call=lambda _event, *, timeout_seconds: {"content": "unused"},
        )
    assert channel.requests == 0


def test_process_channel_preserves_json_values_and_removes_credentials(worker, monkeypatch):
    monkeypatch.setenv("DECAGENT_JEV_API_KEY", "fixture-secret")
    with channel(worker) as client:
        values = {"value": "7", "missing": None, "extra": False, "中文": [1, "2"]}
        result = client.request("echo", values)
        assert result["params"] == values
        assert result["secret_inherited"] is False
        process = client.process
    assert process.poll() is not None


def test_process_channel_round_trips_evaluator_owned_model_callbacks(tmp_path):
    bridge()
    script = tmp_path / "callback_worker.py"
    script.write_text(
        "import json, sys\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'event':'parent_call','request_id':request['id'],"
        "'name':'user_simulator','payload':{'messages':[{'role':'system',"
        "'content':'private'}]}}), flush=True)\n"
        "reply=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'id':request['id'],'ok':True,'result':reply['result']}), flush=True)\n",
        encoding="utf-8",
    )
    observed = []
    with channel(script) as client:
        result = client.request(
            "simulate_user_turn",
            {"assistant_message": "Hello"},
            on_callback=lambda event, *, timeout_seconds: observed.append(event)
            or {"content": "I need help."},
        )
    assert observed == [
        {
            "event": "parent_call",
            "request_id": 1,
            "name": "user_simulator",
            "payload": {"messages": [{"role": "system", "content": "private"}]},
        }
    ]
    assert result == {"content": "I need help."}


def test_process_channel_passes_remaining_deadline_to_timeout_aware_callback(tmp_path):
    bridge()
    script = tmp_path / "deadline_callback_worker.py"
    script.write_text(
        "import json, sys\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'event':'parent_call','request_id':request['id'],"
        "'name':'user_simulator','payload':{}}), flush=True)\n"
        "reply=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'id':request['id'],'ok':True,'result':reply['result']}), flush=True)\n",
        encoding="utf-8",
    )
    observed = []

    def callback(event, *, timeout_seconds):
        observed.append(timeout_seconds)
        return {"content": "bounded"}

    with channel(script, timeout=1.5) as client:
        result = client.request("simulate_user_turn", {}, on_callback=callback)

    assert result == {"content": "bounded"}
    assert len(observed) == 1
    assert 0 < observed[0] <= 1.5


def test_transport_failure_identifies_method_and_cause_type_without_payload(tmp_path):
    module = bridge()
    script = tmp_path / "callback_failure_worker.py"
    script.write_text(
        "import json, sys\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'event':'parent_call','request_id':request['id'],"
        "'name':'user_simulator','payload':{}}), flush=True)\n",
        encoding="utf-8",
    )

    def fail(_event, *, timeout_seconds):
        raise ValueError("private context must not be copied to diagnostics")

    with channel(script) as client:
        with pytest.raises(module.TauBridgeError) as failure:
            client.request("simulate_user_turn", {}, on_callback=fail)
    assert "simulate_user_turn" in str(failure.value)
    assert "ValueError" in str(failure.value)
    assert "private context" not in str(failure.value)
    assert failure.value.failure_stage == "tau_worker_user_model_callback"
    assert failure.value.diagnostic_exception_type == "ValueError"


def test_parent_callback_deadline_is_attributed_to_model_callback(tmp_path):
    module = bridge()
    script = tmp_path / "slow_callback_worker.py"
    script.write_text(
        "import json, sys\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'event':'parent_call','request_id':request['id'],"
        "'name':'user_simulator','payload':{}}), flush=True)\n"
        "sys.stdin.buffer.readline()\n",
        encoding="utf-8",
    )

    def delayed(_event, *, timeout_seconds):
        time.sleep(0.25)
        return {}

    with channel(script) as client:
        with pytest.raises(module.TauBridgeError) as failure:
            client.request(
                "simulate_user_turn",
                {},
                on_callback=delayed,
                timeout_seconds=0.1,
            )

    assert failure.value.failure_stage == "tau_worker_user_model_callback"
    assert failure.value.diagnostic_exception_type == "TauBridgeError"


def test_worker_error_reports_allowlisted_stage_without_exception_message(tmp_path):
    module = bridge()
    worker_path = Path(module.__file__).with_name("tau_worker.py")
    script = tmp_path / "worker_failure_stage_fixture.py"
    script.write_text(
        "import runpy\n"
        + "worker = runpy.run_path("
        + repr(str(worker_path))
        + ")\n"
        + "def fail(*args, on_stage=None, **kwargs):\n"
        + "    on_stage('tau_worker_user_input_parse')\n"
        + "    raise ValueError('private prompt and task content')\n"
        + "worker['main'].__globals__['dispatch'] = fail\nworker['main']()\n",
        encoding="utf-8",
    )

    with module._StdioChannel(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        environment=module._worker_environment(tmp_path),
    ) as client:
        with pytest.raises(module.TauBridgeError) as failure:
            client.request("user_turn", {})

    assert failure.value.failure_stage == "tau_worker_user_input_parse"
    assert failure.value.diagnostic_exception_type == "ValueError"
    assert "private prompt" not in str(failure.value)


def test_process_channel_rejects_unbounded_synchronous_callback(tmp_path):
    module = bridge()
    script = tmp_path / "unbounded_callback_worker.py"
    script.write_text(
        "import json, sys\n"
        "request=json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'event':'parent_call','request_id':request['id'],"
        "'name':'user_simulator','payload':{}}), flush=True)\n"
        "sys.stdin.buffer.readline()\n",
        encoding="utf-8",
    )
    called = []

    with channel(script, timeout=0.2) as client:
        process = client.process
        with pytest.raises(module.TauBridgeError) as failure:
            client.request(
                "simulate_user_turn", {}, on_callback=lambda _event: called.append(True)
            )

    assert called == []
    assert "must accept timeout_seconds" in str(failure.value.__cause__)
    assert process.poll() is not None


@pytest.mark.parametrize("method", ["timeout", "exit", "oversize", "malformed", "wrong_id"])
def test_broken_transport_closes_process_and_never_retries(worker, method):
    module = bridge()
    with channel(worker, timeout=0.3) as client:
        process = client.process
        with pytest.raises(module.TauBridgeError):
            client.request(method, {})
        assert process.poll() is not None
        with pytest.raises(module.TauBridgeError, match="closed"):
            client.request("echo", {})


def test_trial_channels_are_fresh_processes(worker):
    with channel(worker) as first:
        first_pid = first.request("echo", {})["pid"]
    with channel(worker) as second:
        assert second.request("echo", {})["pid"] != first_pid


def test_oversized_request_is_rejected_without_transmission(worker):
    module = bridge()
    with channel(worker) as client:
        with pytest.raises(module.TauBridgeError):
            client.request("echo", {"too_large": "x" * 4096})
        assert client.process.poll() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows interpreter process-tree regression")
def test_close_reclaims_owned_interpreter_descendants_on_windows(worker):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    with channel(worker) as client:
        child = client.request("spawn-child", {})["child_pid"]
        handle = kernel.OpenProcess(0x00100000, False, child)
        assert handle
    try:
        assert kernel.WaitForSingleObject(handle, 1000) == 0, "Owned interpreter child leaked"
    finally:
        kernel.CloseHandle(handle)


def test_request_timeout_also_bounds_a_worker_that_never_reads_stdin(tmp_path):
    module = bridge()
    with module._StdioChannel(
        [sys.executable, "-I", "-c", "import time; time.sleep(20)"],
        cwd=tmp_path,
        environment=module._worker_environment(tmp_path),
        timeout=0.3,
    ) as client:
        with pytest.raises(module.TauBridgeError):
            client.request("blocked-write", {"payload": "x" * (2 * 1024 * 1024)})
        assert client.process.poll() is not None


def test_request_specific_deadline_tightens_process_timeout(tmp_path):
    module = bridge()
    with module._StdioChannel(
        [sys.executable, "-I", "-c", "import time; time.sleep(20)"],
        cwd=tmp_path,
        environment=module._worker_environment(tmp_path),
        timeout=3,
    ) as client:
        process = client.process
        started = time.monotonic()
        with pytest.raises(module.TauBridgeError):
            client.request("slow", {}, timeout_seconds=0.15)
        elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert process.poll() is not None


def test_request_timeout_cap_is_scoped_to_one_process_request(worker):
    module = bridge()
    with module._StdioChannel(
        [sys.executable, "-I", str(worker)],
        cwd=worker.parent,
        environment=module._worker_environment(worker.parent),
        timeout=3,
    ) as client:
        process = client.process
        started = time.monotonic()
        with pytest.raises(module.TauBridgeError):
            client.request("timeout", {}, timeout_cap_seconds=0.15)
        elapsed = time.monotonic() - started
        assert client.timeout == 3

    assert elapsed < 1.0
    assert process.poll() is not None


def test_tau_process_environment_forwards_tool_deadline_to_child_channel():
    environment = object.__new__(bridge().TauProcessEnvironment)

    class Channel:
        def __init__(self):
            self.arguments = None

        def request(self, method, params, *, timeout_seconds=None):
            self.arguments = (method, params, timeout_seconds)
            return {"content": "ok", "error": False}

    channel = Channel()
    environment._channel = channel
    response = environment.get_response(
        SimpleNamespace(id="tool-1", name="lookup", arguments={"id": 1}),
        timeout_seconds=2.5,
    )

    assert response.content == "ok"
    assert channel.arguments[0] == "call"
    assert channel.arguments[2] == 2.5


def test_tau_user_and_judge_callbacks_receive_enclosing_deadline():
    environment = object.__new__(bridge().TauProcessEnvironment)

    class Channel:
        timeout = 15

        def __init__(self):
            self.requests = []

        def request(
            self,
            method,
            params,
            *,
            on_callback=None,
            timeout_seconds=None,
            timeout_cap_seconds=None,
        ):
            self.requests.append((method, timeout_seconds, timeout_cap_seconds))
            if method == "user_turn":
                return {
                    "message": {"role": "user", "content": "Ready"},
                    "stop": False,
                }
            return {
                "scored": True,
                "reward": 1,
                "scorer": "tau2-bench-original-evaluate-simulation",
            }

    channel = Channel()
    environment._channel = channel
    environment._started_at = "2026-10-20T00:00:00+00:00"
    environment.user_turn(
        {"role": "assistant", "content": "Hello"},
        model="qwen-test",
        on_model_call=lambda _event, *, timeout_seconds: {"content": "unused"},
        timeout_seconds=2.25,
    )
    environment.score_simulation(
        messages=[],
        termination_reason="agent_stop",
        duration=0.1,
        judge_model="qwen-test",
        on_model_call=lambda _event, *, timeout_seconds: {"content": "unused"},
        timeout_seconds=1.5,
    )

    assert channel.requests == [
        ("user_turn", 2.25, 90),
        ("score_simulation", 1.5, 120),
    ]
    assert channel.timeout == 15


def test_worker_network_audit_blocks_connect_before_any_external_request(tmp_path):
    module = bridge()
    worker_path = Path(module.__file__).with_name("tau_worker.py")
    script = tmp_path / "audit_fixture.py"
    script.write_text(
        "import runpy, socket\n"
        + "worker = runpy.run_path("
        + repr(str(worker_path))
        + ")\n"
        + "def attempt(*args, **kwargs):\n    socket.create_connection(('127.0.0.1', 9))\n"
        + "worker['main'].__globals__['dispatch'] = attempt\nworker['main']()\n",
        encoding="utf-8",
    )
    with module._StdioChannel(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        environment=module._worker_environment(tmp_path),
        timeout=3,
    ) as client:
        with pytest.raises(module.TauBridgeError) as failure:
            client.request("network-probe", {})
        assert "RuntimeError" in str(failure.value.__cause__)
        assert client.process.poll() is not None


def test_pinned_configuration_rejects_unknown_domain_and_case():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(sys.executable))
    with pytest.raises(ValueError, match="domain"):
        runtime.open("arbitrary", "0")
    with pytest.raises(ValueError, match="task"):
        runtime.open("retail", "not-in-official-split")


def test_pinned_tau_runtime_accepts_linux_inventory_line_endings(monkeypatch):
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (root / "evals/benchmarks/manifests/source_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    inventory_path = (root / manifest["task_inventory"]["path"]).resolve()
    original_read_bytes = Path.read_bytes
    windows_bytes = original_read_bytes(inventory_path)
    linux_bytes = windows_bytes.replace(b"\r\n", b"\n")

    def read_linux_checkout(path):
        if path.resolve() == inventory_path:
            return linux_bytes
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_linux_checkout)

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(sys.executable))

    assert runtime.case_ids("retail", split="train")


def test_tau_runtime_verifier_help_works_from_isolated_python():
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    root = Path(__file__).resolve().parents[2]
    script = root / "evals/benchmarks/verify_tau_runtime.py"
    result = subprocess.run(
        [configured, "-X", "utf8", str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--output" in result.stdout


def test_pinned_runtime_separates_train_smoke_from_frozen_test_cases():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(sys.executable))
    for domain in ("retail", "telecom"):
        train = runtime.case_ids(domain, split="train")
        test = runtime.case_ids(domain)
        assert len(train) == 2
        assert len(test) == 40
        assert not set(train) & set(test)
        with pytest.raises(ValueError, match="requested split"):
            runtime.open(domain, train[0])
        with pytest.raises(ValueError, match="requested split"):
            runtime.open(domain, test[0], split="train")


def test_pinned_tau_user_simulator_receives_multi_tool_results_in_one_turn():
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    callbacks = []
    batch = {
        "role": "tool",
        "requestor": "user",
        "tool_messages": [
            {
                "id": "user-lookup-order",
                "role": "tool",
                "requestor": "user",
                "content": "Order A-1 status: shipped.",
                "error": False,
            },
            {
                "id": "user-lookup-shipment",
                "role": "tool",
                "requestor": "user",
                "content": "Shipment A-1 carrier: Example Express.",
                "error": False,
            },
        ],
    }
    case_id = runtime.case_ids("retail", split="train")[0]
    with runtime.open("retail", case_id, split="train") as environment:
        result = environment.user_turn(
            batch,
            model="qwen3.8-flash",
            on_model_call=lambda event, *, timeout_seconds: callbacks.append(event)
            or {
                "content": "Thanks, I received both results.",
                "tool_calls": [],
                "tau_usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "cost_usd": 0,
            },
        )

    assert result["message"]["role"] == "user"
    assert len(callbacks) == 1
    tool_messages = [
        item for item in callbacks[0]["payload"]["messages"] if item.get("role") == "tool"
    ]
    assert [item["content"] for item in tool_messages[-2:]] == [
        "Order A-1 status: shipped.",
        "Shipment A-1 carrier: Example Express.",
    ]


def test_real_pinned_tau_process_crosses_mcp_and_preserves_errors():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    case_id = runtime.case_ids("retail")[0]
    states = []
    for _ in range(2):
        with TauMCPEnvironment(
            lambda: runtime.open("retail", case_id), system_prompt="Public", user_prompt="Public"
        ) as client:
            environment = client.environment
            process = environment.process
            states.append([environment.get_db_hash(), environment.get_user_db_hash()])
            tools = client.model_input()["tools"]
            calculate = next(tool for tool in tools if tool["name"] == "calculate")
            assert calculate["annotations"]["readOnlyHint"] is True
            assert any(tool["annotations"]["readOnlyHint"] is False for tool in tools)
            direct = environment.get_response(
                SimpleNamespace(id="direct", name="calculate", arguments={"expression": "2+2"})
            )
            actual = client.call_tool("calculate", {"expression": "2+2"})
            assert actual["content"][0]["text"] == direct.content == "4.0"
            user_turn = client.user_turn(
                {"role": "assistant", "content": "Hi! How can I help you today?"},
                model="qwen3.8-flash",
                on_model_call=lambda _event, *, timeout_seconds: {
                    "content": "I need help with my order.",
                    "tool_calls": [],
                    "tau_usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    "cost_usd": 0,
                },
            )
            assert user_turn["message"]["role"] == "user"
            assert user_turn["usage"]["requests"] == 1
            assert client.auxiliary_usage["user_simulator"]["tokens"] == 8
            assert client.auxiliary_usage["user_simulator"]["cost_usd"] == 0
            scored = client.score_simulation(
                messages=[
                    {"role": "assistant", "content": "Hi! How can I help you today?"},
                    user_turn["message"],
                    {"role": "assistant", "content": "I can help with that."},
                ],
                termination_reason="agent_stop",
                duration=0.1,
                judge_model="qwen3.8-flash",
                on_model_call=lambda _event, *, timeout_seconds: {
                    "content": "{\"results\": []}"
                },
            )
            assert scored["scored"] is True
            assert scored["scorer"] == "tau2-bench-original-evaluate-simulation"
            assert client.auxiliary_usage["official_judge"]["requests"] == 0
            invalid = client.call_tool("calculate", {"expression": "bad"})
            assert invalid["isError"] is True
            assert "Invalid characters" in invalid["content"][0]["text"]
            # No adapter casting: upstream raises for an integer expression.
            raw = client.call_tool("calculate", {"expression": 7})
            assert raw["isError"] is True
            direct_error = environment.get_response(
                SimpleNamespace(id="direct", name="calculate", arguments={"expression": 7})
            )
            assert raw["content"][0]["text"] == direct_error.content
            assert "int" in direct_error.content
            assert "evaluation_criteria" not in json.dumps(client.model_input())
        assert process.poll() is not None
    assert states[0] == states[1]


def test_pinned_tau_process_exposes_only_opaque_task_fingerprint():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    case_id = runtime.case_ids("retail", split="train")[0]

    with runtime.open("retail", case_id, split="train") as environment:
        assert len(environment.task_digest) == 64
        assert all(character in "0123456789abcdef" for character in environment.task_digest)
        assert not hasattr(environment, "task")
        assert not hasattr(environment, "target_state")


def test_real_pinned_tau_user_simulator_uses_parent_model_callback_without_network():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    environment = runtime.open("retail", runtime.case_ids("retail")[0])
    calls = []
    before = [environment.get_db_hash(), environment.get_user_db_hash()]
    try:
        result = environment.user_turn(
            "Hello, how can I help?",
            model="qwen3.8-flash",
            on_model_call=lambda event, *, timeout_seconds: calls.append(event)
            or {
                "content": "I need help with my order.",
                "tool_calls": [],
                "usage": {"input_tokens": 21, "output_tokens": 8},
                "cost_usd": 0,
            },
        )
        assert len(calls) == 1
        assert calls[0]["name"] == "tau_user_simulator"
        assert calls[0]["payload"]["model"] == "qwen3.8-flash"
        assert result["message"]["role"] == "user"
        assert result["message"]["content"] == "I need help with my order."
        assert result["usage"] == {
            "requests": 1,
            "input_tokens": 21,
            "output_tokens": 8,
            "cost_usd": 0,
        }
        assert "user_scenario" not in result
        assert [environment.get_db_hash(), environment.get_user_db_hash()] == before
    finally:
        environment.close()


def test_real_pinned_tau_train_smoke_initializes_official_user_simulator():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    environment = runtime.open(
        "retail", runtime.case_ids("retail", split="train")[0], split="train"
    )
    try:
        result = environment.user_turn(
            "Hi! How can I help you today?",
            model="qwen3.8-flash",
            on_model_call=lambda _event, *, timeout_seconds: {
                "content": "I need help with a recent order.",
                "tool_calls": [],
                "tau_usage": {"prompt_tokens": 13, "completion_tokens": 5},
                "cost_usd": 0,
            },
        )
        assert result["message"]["role"] == "user"
        assert result["usage"]["requests"] == 1
        assert "user_scenario" not in json.dumps(result)
    finally:
        environment.close()


def test_real_pinned_tau_official_scorer_keeps_task_labels_in_worker():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    environment = runtime.open("retail", runtime.case_ids("retail")[0])
    calls = []
    try:
        result = environment.score_simulation(
            messages=[
                {"role": "assistant", "content": "Hi! How can I help you today?"},
                {"role": "user", "content": "I need help with my order."},
                {"role": "assistant", "content": "I can help with that."},
            ],
            termination_reason="agent_stop",
            duration=1.25,
            judge_model="qwen3.8-flash",
            on_model_call=lambda event, *, timeout_seconds: calls.append(event)
            or {
                "content": "{\"results\": []}",
                "tau_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "cost_usd": 0,
            },
        )
        assert result["scored"] is True
        assert result["scorer"] == "tau2-bench-original-evaluate-simulation"
        assert 0 <= result["reward"] <= 1
        assert result["judge_usage"]["requests"] == len(calls)
        assert "user_scenario" not in json.dumps(result)
        assert not any("expected" in key.lower() for key in result)
        process = environment.process
        with pytest.raises(module.TauBridgeError):
            environment.score_simulation(
                messages=[],
                termination_reason="not-an-upstream-termination",
                duration=0,
                judge_model="qwen3.8-flash",
                on_model_call=lambda _event, *, timeout_seconds: {"content": "{}"},
            )
        assert process.poll() is not None, "Invalid scorer input must retire its worker"
    finally:
        environment.close()


@pytest.mark.parametrize("domain", ("retail", "telecom"))
def test_real_pinned_tau_route_runs_user_turn_clarification_and_original_evaluator(
    tmp_path, domain
):
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")

    from decagent.decisions.contracts import digest
    from decagent.evaluation.business.comparison import ComparisonTrial, Route
    from decagent.evaluation.business.public_session import tau_session_factory
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    case_id = runtime.case_ids(domain, split="train")[0]
    safe_input = {"instruction": "Help the τ-Bench customer using the available tools."}
    public_skill = "Use only the available customer-service tools; ask when information is missing."

    def open_environment(_trial):
        return runtime.open(domain, case_id, split="train")

    def boundary(environment, trial):
        public = environment.model_input()
        process = environment.environment
        return {
            "initial_state_digest": digest(
                {
                    "agent_state": process.get_db_hash(),
                    "user_state": process.get_user_db_hash(),
                }
            ),
            "goal_digest": digest({"task_input": safe_input, "skill": public_skill}),
            "actor_profile_digest": digest({"domain": domain, "case_id": trial.case_id}),
            "permission_digest": digest(
                {"tools": public["tools"], "policy": process.get_policy()}
            ),
            "tool_adapter_digest": digest(
                {
                "commit": runtime.source_commit,
                    "package_version": runtime.package_version,
                    "tools": public["tools"],
                    "mcp_protocol": "in-memory",
                }
            ),
        }

    with TauMCPEnvironment(
        lambda: open_environment(None),
        system_prompt=public_skill,
        user_prompt=safe_input["instruction"],
    ) as preview:
        facts = boundary(preview, SimpleNamespace(case_id=case_id))

    trial = ComparisonTrial(
        dataset="tau-bench",
        dataset_version=runtime.source_commit,
        dataset_checksum=digest(
            {"commit": runtime.source_commit, "split": "train", "domain": domain}
        ),
        case_id=case_id,
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )
    session_factory = tau_session_factory(
        open_environment,
        dataset_version=runtime.source_commit,
        tool_catalog_version=runtime.package_version,
        journal_root=tmp_path,
        task_input_factory=lambda _trial: safe_input,
        skill_factory=lambda _trial: public_skill,
        boundary_factory=boundary,
        scorer_factory=lambda _environment, _trial: pytest.fail(
            "The real τ session must use the original official evaluator"
        ),
    )
    user_turns = []
    judge_calls = []
    react_requests = []

    def model_reply(value):
        return ModelReply(
            value,
            tokens=8,
            cost_usd=Decimal("0"),
            model="scripted-offline",
            input_tokens=5,
            output_tokens=3,
        )

    def user_simulator(request, *, timeout_seconds=None):
        assert timeout_seconds is not None and timeout_seconds > 0
        user_turns.append(request)
        content = {
            1: "I need help with a service issue.",
            2: "The reference number is 1453.",
        }.get(len(user_turns), "###STOP###")
        return model_reply({"content": content})

    def official_judge(request, *, timeout_seconds=None):
        assert timeout_seconds is not None and timeout_seconds > 0
        judge_calls.append(request)
        return model_reply({"content": "{\"results\": []}"})

    result = ControlledRoutes(session_factory).run(
        Route.REACT_SKILL_MCP,
        trial,
        react=lambda request: react_requests.append(request)
        or model_reply(
            {"ask_user": "What can I help you with?"}
            if len(react_requests) == 1
            else {"finish": True, "message": "I can help with that issue."}
        ),
        user_model="scripted-offline",
        user_simulator=user_simulator,
        judge_model="scripted-offline",
        official_judge=official_judge,
    )

    assert result.status == "complete", (
        f"{result.error}; terminal={result.terminal}; user requests={len(user_turns)}; "
        f"agent requests={result.usage.primary_requests}; "
        f"tool calls={[(call.command, call.error) for call in result.tool_calls]}"
    )
    assert result.score is not None and result.score.official is True
    assert result.run is not None and result.run.benchmark_score is not None
    assert len(react_requests) == 2
    assert len(user_turns) == 3
    assert result.tau_steps == 5
    assert result.run.user_simulator_requests == 3
    assert result.run.user_simulator_tokens == 24
    assert result.run.judge_requests == len(judge_calls)
    assert result.error is None
    assert result.cleanup_verified is True
    assert result.cleanup_evidence == {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
    }


@pytest.mark.parametrize("domain", ("retail", "telecom"))
def test_real_pinned_tau_plan_route_uses_simulator_for_clarification(
    tmp_path, monkeypatch, domain
):
    """Protocol smoke only: scripted replies, original tools and original scorer."""
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")

    from decagent.decisions.contracts import digest
    from decagent.evaluation.business.comparison import ComparisonTrial, Route
    from decagent.evaluation.business.public_session import tau_session_factory
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    score_inputs = []
    original_score_simulation = TauMCPEnvironment.score_simulation

    def capture_score_simulation(environment, **kwargs):
        score_inputs.append(json.loads(json.dumps(kwargs["messages"])))
        return original_score_simulation(environment, **kwargs)

    monkeypatch.setattr(TauMCPEnvironment, "score_simulation", capture_score_simulation)

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    case_id = runtime.case_ids(domain, split="train")[0]
    if domain == "retail":
        retail_db = json.loads(
            (runtime.source / "data/tau2/domains/retail/db.json").read_text(encoding="utf-8")
        )
        lookup_tool = "get_order_details"
        lookup_argument = "order_id"
        fixture_lookup_value = next(iter(retail_db["orders"]))
        clarification = "Which order should I check?"
        first_user_reply = "I need help with a recent order."
    else:
        import tomllib

        telecom_db = tomllib.loads(
            (runtime.source / "data/tau2/domains/telecom/db.toml").read_text(encoding="utf-8")
        )
        lookup_tool = "get_customer_by_phone"
        lookup_argument = "phone_number"
        fixture_lookup_value = telecom_db["customers"][0]["phone_number"]
        clarification = "What phone number should I look up?"
        first_user_reply = "I need help with my mobile account."
    task_input = {"instruction": "Help the τ-Bench customer using the available tools."}
    skill = "Use the available customer-service tools; clarify missing information."

    def open_environment(_trial):
        return runtime.open(domain, case_id, split="train")

    def boundary(environment, trial):
        public = environment.model_input()
        process = environment.environment
        return {
            "initial_state_digest": digest(
                {"agent_state": process.get_db_hash(), "user_state": process.get_user_db_hash()}
            ),
            "goal_digest": digest({"task_input": task_input, "skill": skill}),
            "actor_profile_digest": digest({"domain": domain, "case_id": trial.case_id}),
            "permission_digest": digest(
                {"tools": public["tools"], "policy": process.get_policy()}
            ),
            "tool_adapter_digest": digest(
                {
                    "commit": runtime.source_commit,
                    "package_version": runtime.package_version,
                    "tools": public["tools"],
                    "mcp_protocol": "in-memory",
                }
            ),
        }

    with TauMCPEnvironment(
        lambda: open_environment(None), system_prompt=skill, user_prompt=task_input["instruction"]
    ) as preview:
        facts = boundary(preview, SimpleNamespace(case_id=case_id))

    trial = ComparisonTrial(
        dataset="tau-bench",
        dataset_version=runtime.source_commit,
        dataset_checksum=digest(
            {"commit": runtime.source_commit, "split": "train", "domain": domain}
        ),
        case_id=case_id,
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )
    session_factory = tau_session_factory(
        open_environment,
        dataset_version=runtime.source_commit,
        tool_catalog_version=runtime.package_version,
        journal_root=tmp_path,
        task_input_factory=lambda _trial: task_input,
        skill_factory=lambda _trial: skill,
        boundary_factory=boundary,
        scorer_factory=lambda _environment, _trial: pytest.fail(
            "The real τ session must use the original official evaluator"
        ),
    )
    user_turns = []
    judge_calls = []

    def measured(value):
        return ModelReply(
            value,
            tokens=8,
            cost_usd=Decimal("0"),
            model="scripted-offline",
            input_tokens=5,
            output_tokens=3,
        )

    def planner(request):
        return measured(
            {
                "schema_version": "1",
                "plan_id": "tau-real-plan-clarification",
                "revision": 1,
                "registry_fingerprint": request["registry_fingerprint"],
                "entry": "clarify",
                "goal": {
                    "description": "Clarify and retain the order identifier.",
                    "evidence": [
                        {
                            "id": "order_details",
                            "kind": "tool_result",
                    "description": f"The official {domain} lookup returned a result.",
                            "source": {
                                "source": "node_output",
                                "node_id": "lookup",
                                "path": [],
                                "value_type": "object",
                                "nullable": False,
                                "provenance": "tool_result",
                            },
                        }
                    ],
                },
                "nodes": [
                    {
                        "id": "clarify",
                        "depends_on": [],
                        "on_missing": {"stop": "missing_input"},
                        "on_error": {"stop": "error"},
                        "type": "ask_user",
                        "prompt": clarification,
                        "answer": {"value_type": "string", "nullable": False},
                        "next": ["lookup"],
                    },
                    {
                        "id": "lookup",
                        "depends_on": ["clarify"],
                        "on_missing": {"stop": "missing_input"},
                        "on_error": {"stop": "error"},
                        "type": "read_tool",
                        "tool": {
                            "plugin_id": "evaluation-tools",
                            "command": lookup_tool,
                        },
                        "inputs": {
                            lookup_argument: {
                                "source": "node_output",
                                "node_id": "clarify",
                                "path": [],
                                "value_type": "string",
                                "nullable": False,
                                "provenance": "user_answer",
                            }
                        },
                        "next": ["done"],
                    },
                    {
                        "id": "done",
                        "depends_on": ["lookup"],
                        "on_missing": {"stop": "missing_input"},
                        "on_error": {"stop": "error"},
                        "type": "complete",
                        "evidence": ["order_details"],
                    },
                ],
            }
        )

    def user_simulator(request, *, timeout_seconds=None):
        assert timeout_seconds is not None and timeout_seconds > 0
        user_turns.append(request)
        content = {
            1: first_user_reply,
            2: fixture_lookup_value,
        }.get(len(user_turns), "###STOP###")
        return measured({"content": content})

    def official_judge(request, *, timeout_seconds=None):
        assert timeout_seconds is not None and timeout_seconds > 0
        judge_calls.append(request)
        return measured({"content": "{\"results\": []}"})

    result = ControlledRoutes(session_factory).run(
        Route.PLAN_MCP,
        trial,
        planner=planner,
        user_model="scripted-offline",
        user_simulator=user_simulator,
        judge_model="scripted-offline",
        official_judge=official_judge,
    )

    assert result.status == "complete", (
        f"{result.error}; terminal={result.terminal}; user requests={len(user_turns)}; "
        f"agent requests={result.usage.primary_requests}; "
        f"tool calls={[(call.command, call.error) for call in result.tool_calls]}"
    )
    assert result.score is not None and result.score.official is True
    assert result.run.route is Route.PLAN_MCP
    assert result.run is not None and result.run.benchmark_score is not None
    assert result.run.primary_requests == 1
    assert [call.command for call in result.tool_calls] == [lookup_tool]
    assert result.tool_calls[0].error is None
    assert result.run.user_simulator_requests == len(user_turns) == 3
    assert result.run.user_simulator_tokens == 24
    assert result.tau_steps == 7
    assert result.run.judge_requests == len(judge_calls)
    assert result.error is None
    journal_paths = list(tmp_path.rglob("*.sqlite3"))
    assert len(journal_paths) == 1
    with sqlite3.connect(journal_paths[0]) as connection:
        row = connection.execute(
            "SELECT messages,termination_reason,duration FROM simulation_trajectory WHERE id=1"
        ).fetchone()
    assert row is not None
    persisted_messages = json.loads(row[0])
    assert score_inputs == [persisted_messages]
    assert row[1] in {"agent_stop", "user_stop"}
    assert row[2] >= 0
    assert result.cleanup_verified is True
    assert result.cleanup_evidence == {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
    }


def test_real_pinned_worker_rejects_modified_source_archive(monkeypatch):
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    runtime = replace(
        module.PinnedTauRuntime.from_manifest(root, Path(configured)), archive_sha256="0" * 64
    )
    processes = []
    original_popen = module.subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if str(args[0][0]) == str(runtime.python):
            processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", record_process)
    with pytest.raises(module.TauBridgeError):
        runtime.open("retail", runtime.case_ids("retail")[0])
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_real_telecom_process_preserves_original_error_and_initial_state():
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    with TauMCPEnvironment(
        lambda: runtime.open("telecom", runtime.case_ids("telecom")[0]),
        system_prompt="Public protocol regression",
        user_prompt="Public protocol regression",
    ) as client:
        native = client.environment
        before = [native.get_db_hash(), native.get_user_db_hash()]
        arguments = {"phone_number": "not-a-phone"}
        expected = native.get_response(
            SimpleNamespace(
                id="direct",
                name="get_customer_by_phone",
                arguments=arguments,
            )
        )
        actual = client.call_tool("get_customer_by_phone", arguments)
        assert actual["content"][0]["text"] == expected.content
        assert actual["isError"] == expected.error is True
        assert [native.get_db_hash(), native.get_user_db_hash()] == before


def test_production_plan_binds_original_tau_response_into_next_process_tool(tmp_path):
    """A synthetic protocol regression, never an official task success score."""
    module = bridge()
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("DECAGENT_TEST_TAU_PYTHON")
    if not configured:
        pytest.skip("opt-in pinned tau subprocess runtime is not configured")
    from decagent.business.plans.contracts import Plan
    from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
    from decagent.business.plans.runtime import NodeResult, PlanRuntime
    from decagent.decisions.contracts import digest
    from decagent.evaluation.business.comparison import ComparisonTrial
    from decagent.evaluation.business.public_session import EvaluationPlanSession
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment
    from evals.deterministic.test_business_evaluation_catalog import catalog_plan

    runtime = module.PinnedTauRuntime.from_manifest(root, Path(configured))
    # An explicit synthetic regression input from public fixture data, unrelated
    # to the selected task's goal, private user scenario or expected actions.
    database = json.loads((runtime.source / "data/tau2/domains/retail/db.json").read_text())
    user = next(iter(database["users"].values()))
    task_input = {"instruction": "Protocol regression: find this public fixture user's details."}
    case_id = runtime.case_ids("retail")[0]
    with (
        TauMCPEnvironment(
            lambda: runtime.open("retail", case_id),
            system_prompt="Synthetic regression, no official task target.",
            user_prompt=task_input["instruction"],
        ) as environment,
        EvaluationScope("tau-bench", runtime.source_commit, case_id) as scope,
    ):
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            environment.model_input()["tools"],
            benchmark_version=runtime.source_commit,
            result_contract_version="tau2-mcp-observation-v1",
        )
        raw = catalog_plan(catalog).to_mapping()
        raw.pop("content_digest", None)
        lookup = raw["nodes"][0]
        lookup["tool"]["command"] = "find_user_id_by_email"
        lookup["inputs"] = {
            "email": {
                "source": "literal",
                "value": user["email"],
                "value_type": "string",
                "nullable": False,
                "provenance": "plan_literal",
            }
        }
        lookup["next"] = ["details"]
        details = {
            **lookup,
            "id": "details",
            "depends_on": ["lookup"],
            "next": ["done"],
            "tool": {"plugin_id": catalog.id, "command": "get_user_details"},
            "inputs": {
                "user_id": {
                    "source": "node_output",
                    "node_id": "lookup",
                    "path": ["value"],
                    "value_type": "string",
                    "nullable": False,
                    "provenance": "tool_result",
                }
            },
        }
        raw["nodes"].insert(1, details)
        raw["nodes"][-1]["depends_on"] = ["details"]
        raw["goal"]["evidence"][0]["source"].update(node_id="details", path=["value", "user_id"])
        plan = Plan.from_mapping(raw)
        facts = {
            key: digest(key)
            for key in (
                "initial_state_digest",
                "goal_digest",
                "actor_profile_digest",
                "permission_digest",
                "tool_adapter_digest",
            )
        }
        trial = ComparisonTrial(
            dataset="tau-bench",
            dataset_version=runtime.source_commit,
            dataset_checksum=digest("protocol-regression-only"),
            case_id=case_id,
            branch_kind="structured",
            memory_mode="disabled",
            workflow_mode="cold",
            **facts,
        )

        def no_score(_environment):
            raise AssertionError("Protocol regression must not invoke or fake the official scorer")

        session = EvaluationPlanSession(
            environment,
            catalog,
            trial=trial,
            isolation_id="process-binding-regression",
            journal_root=tmp_path,
            actor_id="eval:protocol",
            task_input=task_input,
            skill="Protocol regression",
            boundary_facts=facts,
            scorer=no_score,
        )
        try:
            repository = session.publish_plan(plan, task_input)

            def execute(node, inputs, context, _saved):
                assert node["type"] == "read_tool"
                observed = session.call(node["tool"]["command"], inputs)
                return NodeResult(session._node_record(node, inputs, context, observed))

            interpreter = PlanRuntime(
                repository, execute, completion_verifier=session.evaluation_execution_finished
            )
            for _ in range(5):
                terminal = interpreter.step()
                if terminal["status"] == "complete":
                    break
            assert terminal["status"] == "complete", terminal
            assert terminal["completion"]["business_goal_verified"] is False
            calls = repository.external_calls()
            assert len(calls) == 2
            details_row = next(row for row in repository.nodes() if row["node_id"] == "details")
            assert details_row["inputs"]["user_id"] == user["user_id"]
            assert all(row["status"] == "succeeded" for row in calls)
        finally:
            session.close()

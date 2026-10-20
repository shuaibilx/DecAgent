"""Evaluator-owned bridge to a separately locked, offline original tau2 runtime.

The application never imports tau2. Only trusted benchmark code can construct
this adapter; no command, source path or process operation is an MCP tool.
"""

import inspect
import json
import os
import queue
import signal
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

MAX_FRAME = 4 * 1024 * 1024


class TauBridgeError(RuntimeError):
    """The trial transport is unusable; a dispatched write may be unknown."""


def _worker_environment(source):
    # Explicit allowlist: never forward provider keys, proxy credentials,
    # PYTHONPATH, application .env settings or automatic tracing configuration.
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        LOGURU_LEVEL="ERROR",
        TAU2_DATA_DIR=str(Path(source) / "data"),
    )
    return environment


class _StdioChannel:
    """Ordered, bounded JSON transport. Any failure retires the process."""

    def __init__(self, command, *, cwd, environment, timeout=15, max_frame=MAX_FRAME):
        if not 0 < timeout <= 120 or not 256 <= max_frame <= MAX_FRAME:
            raise ValueError("Invalid tau bridge bounds")
        self.timeout, self.max_frame = timeout, max_frame
        self._responses = queue.Queue(maxsize=2)
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        self._reader = threading.Thread(target=self._read, daemon=True, name="tau-frame-reader")
        self._reader.start()

    def _read(self):
        try:
            while not self._closed:
                line = self.process.stdout.readline(self.max_frame + 1)
                if not line or len(line) > self.max_frame or not line.endswith(b"\n"):
                    self._responses.put_nowait(TauBridgeError("tau worker frame or EOF failure"))
                    return
                self._responses.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            # The request timeout also covers an unsolicited-message overflow.
            return

    def request(
        self,
        method,
        params,
        *,
        on_callback=None,
        timeout_seconds=None,
        timeout_cap_seconds=None,
    ):
        for name, value in (
            ("deadline", timeout_seconds),
            ("timeout cap", timeout_cap_seconds),
        ):
            if value is not None and (
                type(value) not in {int, float}
                or not isfinite(value)
                or value <= 0
                or value > 1_800
            ):
                raise ValueError(f"A positive bounded τ {name} is required")
        with self._lock:
            if self._closed:
                raise TauBridgeError("tau bridge is closed")
            self._sequence += 1
            try:
                timeout_limits = [self.timeout]
                if timeout_seconds is not None:
                    timeout_limits.append(float(timeout_seconds))
                if timeout_cap_seconds is not None:
                    timeout_limits.append(float(timeout_cap_seconds))
                effective_timeout = min(timeout_limits)
                deadline = time.monotonic() + effective_timeout

                def send(message):
                    try:
                        frame = (
                            json.dumps(message, ensure_ascii=False, allow_nan=False).encode(
                                "utf-8"
                            )
                            + b"\n"
                        )
                        if len(frame) > self.max_frame:
                            raise TauBridgeError("tau request exceeds frame limit")
                        sent = queue.Queue(maxsize=1)

                        def write():
                            try:
                                # Raw unbuffered pipes may perform partial writes.
                                remaining = memoryview(frame)
                                while remaining:
                                    count = self.process.stdin.write(remaining)
                                    if not count:
                                        raise BrokenPipeError("tau worker stdin closed")
                                    remaining = remaining[count:]
                                sent.put(None)
                            except (OSError, ValueError) as exc:
                                sent.put(exc)

                        writer = threading.Thread(
                            target=write, daemon=True, name="tau-frame-writer"
                        )
                        writer.start()
                        failure = sent.get(timeout=max(0, deadline - time.monotonic()))
                        if failure is not None:
                            raise TauBridgeError("tau worker write failed") from failure
                    except queue.Empty as exc:
                        raise TauBridgeError("tau worker request timed out") from exc

                send({"id": self._sequence, "method": method, "params": params})
                callback_count = 0
                while True:
                    response = self._responses.get(timeout=max(0, deadline - time.monotonic()))
                    if isinstance(response, Exception):
                        raise response
                    result = json.loads(response)
                    if (
                        isinstance(result, dict)
                        and result.get("event") == "parent_call"
                    ):
                        if (
                            type(result.get("request_id")) is not int
                            or result["request_id"] != self._sequence
                            or not isinstance(result.get("name"), str)
                            or not isinstance(result.get("payload"), dict)
                            or on_callback is None
                        ):
                            raise TauBridgeError("tau worker callback protocol failure")
                        callback_count += 1
                        if callback_count > 16:
                            raise TauBridgeError("tau worker callback limit exceeded")
                        callback_timeout = deadline - time.monotonic()
                        if callback_timeout <= 0:
                            raise TauBridgeError("tau worker callback timed out")
                        try:
                            parameters = inspect.signature(on_callback).parameters.values()
                        except (TypeError, ValueError):
                            parameters = ()
                        accepts_timeout = any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            or parameter.name == "timeout_seconds"
                            and parameter.kind
                            in {
                                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                inspect.Parameter.KEYWORD_ONLY,
                            }
                            for parameter in parameters
                        )
                        if not accepts_timeout:
                            raise TauBridgeError(
                                "tau callback must accept timeout_seconds for its deadline"
                            )
                        callback_result = on_callback(
                            result, timeout_seconds=callback_timeout
                        )
                        if time.monotonic() >= deadline:
                            raise TauBridgeError("tau worker callback timed out")
                        send(
                            {
                                "event": "parent_result",
                                "request_id": self._sequence,
                                "result": callback_result,
                            }
                        )
                        continue
                    if (
                        isinstance(result, dict)
                        and result.get("ok") is False
                        and result.get("id") == self._sequence
                    ):
                        error_type = result.get("error_type", "UnknownError")
                        if not isinstance(error_type, str) or not error_type.isidentifier():
                            error_type = "UnknownError"
                        raise TauBridgeError("tau worker rejected request: " + error_type)
                    if (
                        not isinstance(result, dict)
                        or type(result.get("id")) is not int
                        or result["id"] != self._sequence
                        or result.get("ok") is not True
                        or "result" not in result
                    ):
                        raise TauBridgeError("tau worker response protocol failure")
                    return result["result"]
            except Exception as exc:
                self.close()
                raise TauBridgeError(
                    f"tau {method} failed ({type(exc).__name__}); no automatic replay"
                ) from exc

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            if os.name == "nt":
                # Windows venv python.exe is a redirector that can own a second
                # interpreter. Kill only this owned live PID's process tree.
                taskkill = Path(os.environ["SYSTEMROOT"]) / "System32" / "taskkill.exe"
                try:
                    subprocess.run(
                        [str(taskkill), "/PID", str(self.process.pid), "/T", "/F"],
                        check=False,
                        timeout=3,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self._reader.join(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()

    @property
    def cleanup_verified(self):
        return self._closed and self.process.poll() is not None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@dataclass(frozen=True)
class PinnedTauRuntime:
    """Trusted harness configuration read from the frozen source manifest."""

    python: Path
    source: Path
    archive: Path
    archive_sha256: str
    package_version: str
    source_commit: str
    domains: dict
    smoke_domains: dict

    @classmethod
    def from_manifest(cls, root, python):
        root, python = Path(root).resolve(), Path(python).resolve()
        if not python.is_file():
            raise ValueError("Dedicated tau Python executable is missing")
        manifest = json.loads(
            (root / "evals/benchmarks/manifests/source_manifest.json").read_text()
        )
        record = manifest["tau2_bench"]["source_repository"]
        inventory_path = (root / manifest["task_inventory"]["path"]).resolve()
        if (
            not inventory_path.is_relative_to(root)
            or sha256(inventory_path.read_bytes()).hexdigest()
            != manifest["task_inventory"]["sha256"]
        ):
            raise ValueError("Frozen tau task inventory differs")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        domains = inventory["resource_subsets"]["tau2_bench_supplement"]["domains"]
        smoke_domains = inventory["resource_subsets"]["tau2_bench_smoke"]["domains"]
        source = (root / record["local_path"]).resolve()
        archive = (root / record["archive"]["path"]).resolve()
        if not source.is_relative_to(root) or not archive.is_relative_to(root):
            raise ValueError("Pinned tau source path escapes the project")
        return cls(
            python,
            source,
            archive,
            record["archive"]["sha256"],
            record["package_version"],
            record["commit"],
            deepcopy(domains),
            deepcopy(smoke_domains),
        )

    def case_ids(self, domain, *, split="test"):
        key = "tau2-bench/" + domain
        selections = {"test": self.domains, "train": self.smoke_domains}
        if split not in selections:
            raise ValueError(
                "Only the frozen train smoke and supplementary test splits are available"
            )
        if key not in selections[split]:
            raise ValueError("Unknown pinned tau domain")
        selection = selections[split][key]
        if selection["official_split"] != split:
            raise ValueError("Pinned tau split metadata differs")
        return tuple(selection["task_ids"])

    def open(self, domain, case_id, *, split="test"):
        if case_id not in self.case_ids(domain, split=split):
            raise ValueError("Unknown pinned tau task for the requested split")
        return TauProcessEnvironment(self, domain, case_id, split)


class TauProcessEnvironment:
    """One original Environment in one owned process, compatible with TauToolSession."""

    def __init__(self, runtime, domain, case_id, split="test"):
        self._channel = _StdioChannel(
            [
                str(runtime.python),
                "-I",
                "-X",
                "utf8",
                str(Path(__file__).with_name("tau_worker.py").resolve()),
            ],
            cwd=runtime.source,
            environment=_worker_environment(runtime.source),
            timeout=60,
        )
        self.process = self._channel.process
        self._started_at = datetime.now(UTC).isoformat()
        try:
            public = self._channel.request(
                "initialize",
                {
                    "source": str(runtime.source),
                    "archive": str(runtime.archive),
                    "archive_sha256": runtime.archive_sha256,
                    "version": runtime.package_version,
                    "domain": domain,
                    "case_id": case_id,
                    "split": split,
                },
            )
            self._tools = deepcopy(public["tools"])
            self._mutations = dict(public["mutations"])
            self._policy = public["policy"]
            task_digest = public.get("task_digest")
            if (
                type(task_digest) is not str
                or len(task_digest) != 64
                or any(character not in "0123456789abcdef" for character in task_digest)
            ):
                raise TauBridgeError("tau worker returned an invalid opaque task digest")
            self.task_digest = task_digest
            self.tools = self
            self._channel.timeout = 15
        except BaseException:
            self.close()
            raise

    def get_tools(self):
        return [SimpleNamespace(openai_schema=deepcopy(item)) for item in self._tools]

    @property
    def cleanup_verified(self):
        return self._channel.cleanup_verified

    def tool_mutates_state(self, name):
        return self._mutations.get(name, True) is not False

    def get_policy(self):
        return self._policy

    def get_response(self, request, *, timeout_seconds=None):
        timeout_kwargs = (
            {"timeout_seconds": timeout_seconds}
            if timeout_seconds is not None
            else {}
        )
        result = self._channel.request(
            "call",
            {
                "id": request.id,
                "name": request.name,
                "arguments": request.arguments,
                "requestor": getattr(request, "requestor", "assistant"),
            },
            **timeout_kwargs,
        )
        return SimpleNamespace(content=result["content"], error=result["error"])

    def user_turn(self, message, *, model, on_model_call, timeout_seconds=None):
        """Advance the pinned official user simulator through a parent model callback.

        The user scenario and simulator state stay inside the worker. The callback
        receives only the original simulator's model request and must return a
        single normalized completion; this bridge never retries it.
        """
        if not callable(on_model_call):
            raise TypeError("An evaluator-owned user simulator model callback is required")
        if type(model) is not str or not model.strip() or len(model) > 256:
            raise ValueError("A bounded evaluator model identifier is required")
        if isinstance(message, str):
            message = {"role": "assistant", "content": message}
        if type(message) is not dict or message.get("role") not in {"assistant", "tool"}:
            raise ValueError("A supported assistant or user-tool message is required")
        if message["role"] == "tool":
            tool_messages = message.get("tool_messages")
            if tool_messages is None:
                tool_messages = [message]
            if (
                message.get("requestor") != "user"
                or type(tool_messages) is not list
                or not 1 <= len(tool_messages) <= 8
                or any(
                    type(item) is not dict
                    or item.get("role") != "tool"
                    or item.get("requestor") != "user"
                    or type(item.get("id")) is not str
                    or not item["id"].strip()
                    or type(item.get("content")) is not str
                    or type(item.get("error")) is not bool
                    for item in tool_messages
                )
            ):
                raise ValueError("A bounded user-tool message batch is required")
        return self._channel.request(
            "user_turn",
            {"message": deepcopy(message), "model": model},
            on_callback=on_model_call,
            timeout_seconds=timeout_seconds,
            timeout_cap_seconds=90,
        )

    def call_user_tool(self, request, *, timeout_seconds=None):
        """Execute an original tau2 user-side tool in the same trial environment."""
        if getattr(request, "requestor", "user") != "user":
            raise ValueError("User-side calls must retain the upstream user requestor")
        timeout_kwargs = (
            {"timeout_seconds": timeout_seconds}
            if timeout_seconds is not None
            else {}
        )
        return self._channel.request(
            "call",
            {
                "id": request.id,
                "name": request.name,
                "arguments": deepcopy(request.arguments),
                "requestor": "user",
            },
            **timeout_kwargs,
        )

    def score_simulation(
        self,
        *,
        messages,
        termination_reason,
        duration,
        judge_model,
        on_model_call,
        timeout_seconds=None,
    ):
        """Call the pinned upstream evaluator without returning private labels.

        Natural-language criteria, when present, are judged through the supplied
        evaluator callback and their requests are returned as separate usage.
        """
        if not callable(on_model_call):
            raise TypeError("An evaluator-owned official scorer callback is required")
        if type(messages) is not list or len(messages) > 2048:
            raise ValueError("Official scoring trajectory exceeds its message bound")
        if type(termination_reason) is not str or len(termination_reason) > 64:
            raise ValueError("A bounded tau2 termination reason is required")
        if (
            type(duration) not in {int, float}
            or not 0 <= duration <= 1800
        ):
            raise ValueError("Official scoring duration is outside the trial budget")
        if type(judge_model) is not str or not judge_model.strip() or len(judge_model) > 256:
            raise ValueError("A bounded evaluator model identifier is required")
        return self._channel.request(
            "score_simulation",
            {
                "messages": deepcopy(messages),
                "termination_reason": termination_reason,
                "duration": float(duration),
                "simulation_id": str(uuid4()),
                "start_time": self._started_at,
                "judge_model": judge_model,
            },
            on_callback=on_model_call,
            timeout_seconds=timeout_seconds,
            timeout_cap_seconds=120,
        )

    def get_db_hash(self):
        return self._channel.request("state", {})["agent"]

    def get_user_db_hash(self):
        return self._channel.request("state", {})["user"]

    def close(self):
        self._channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

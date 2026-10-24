"""Standalone stdlib/τ2 worker; run as a file in the pinned upstream environment.

Do not import DecAgent or MCP here. stdout is a private bounded JSON protocol;
upstream diagnostics go to stderr. The original simulator stays evaluator-side
in this process; only model requests cross the parent callback channel.
"""

import contextlib
import importlib
import importlib.metadata
import json
import math
import platform
import sys
import tarfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path, PurePosixPath

MAX_FRAME = 4 * 1024 * 1024
_FAILURE_STAGES = frozenset(
    {
        "tau_worker_runtime_initialize",
        "tau_worker_tool_execution",
        "tau_worker_user_input_parse",
        "tau_worker_user_model_callback",
        "tau_worker_user_response_parse",
        "tau_worker_user_simulator_step",
        "tau_worker_user_result_encode",
        "tau_worker_score_input_parse",
        "tau_worker_official_judge_callback",
        "tau_worker_score_execution",
        "tau_worker_score_result_encode",
        "tau_worker_state_read",
        "tau_worker_protocol",
    }
)
_METHOD_STAGES = {
    "initialize": "tau_worker_runtime_initialize",
    "call": "tau_worker_tool_execution",
    "user_turn": "tau_worker_user_input_parse",
    "score_simulation": "tau_worker_score_input_parse",
    "state": "tau_worker_state_read",
}
_SAFE_REWARD_COMPONENTS = frozenset(
    {"DB", "ENV_ASSERTION", "ACTION", "COMMUNICATE", "NL_ASSERTION"}
)


def _safe_reward_breakdown(value):
    """Expose only upstream aggregate reward categories, never private checks."""
    if type(value) is not dict:
        return {}
    result = {}
    for key, score in value.items():
        name = getattr(key, "value", key)
        if (
            type(name) is str
            and name in _SAFE_REWARD_COMPONENTS
            and type(score) in {int, float}
            and math.isfinite(score)
            and 0 <= score <= 1
        ):
            result[name] = float(score)
    return dict(sorted(result.items()))


def verify_source(params):
    source, archive = Path(params["source"]).resolve(), Path(params["archive"]).resolve()
    if sha256(archive.read_bytes()).hexdigest() != params["archive_sha256"]:
        raise ValueError("tau archive checksum differs")
    checked = 0
    with tarfile.open(archive) as package:
        for member in package:
            if not member.isfile():
                continue
            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
            name = str(relative)
            if not (
                (name.startswith("src/tau2/") and name.endswith(".py"))
                or name in {"pyproject.toml", "uv.lock"}
                or name.startswith(("data/tau2/domains/retail/", "data/tau2/domains/telecom/"))
            ):
                continue
            target = (source / name).resolve()
            if not target.is_relative_to(source):
                raise ValueError("tau source escapes root")
            stream = package.extractfile(member)
            if (
                stream is None
                or sha256(stream.read()).digest() != sha256(target.read_bytes()).digest()
            ):
                raise ValueError("tau source or data differs")
            checked += 1
    if checked < 10:
        raise ValueError("tau source verification incomplete")
    return source


def initialize(params):
    if params["domain"] not in {"retail", "telecom"} or params["split"] not in {
        "train",
        "test",
    }:
        raise ValueError("Only registered supplementary domains/splits are allowed")
    if platform.python_version_tuple()[:2] != ("3", "12"):
        raise ValueError("Pinned offline tau runtime requires Python 3.12")
    source = verify_source(params)
    package = importlib.import_module("tau2")
    if not Path(package.__file__).resolve().is_relative_to(source / "src"):
        raise ValueError("tau import is not the pinned source")
    if importlib.metadata.version("tau2") != params["version"]:
        raise ValueError("tau package version differs")
    module = importlib.import_module("tau2.domains." + params["domain"] + ".environment")
    tasks = [task for task in module.get_tasks(params["split"]) if task.id == params["case_id"]]
    if len(tasks) != 1:
        raise ValueError("Official task is missing or ambiguous")
    task = tasks[0]
    environment = module.get_environment()
    initial = deepcopy(task.initial_state)
    environment.set_state(
        initialization_data=initial.initialization_data if initial else None,
        initialization_actions=initial.initialization_actions if initial else None,
        message_history=(initial.message_history or []) if initial else [],
    )
    from tau2.runner.build import build_user

    simulator = build_user(
        "user_simulator",
        environment,
        task,
        llm="evaluator-callback",
    )
    return (
        environment,
        simulator,
        simulator.get_init_state(
            message_history=deepcopy(initial.message_history or []) if initial else []
        ),
        task,
    )


def _plain_message(message):
    return message.model_dump(mode="json", exclude_none=True)


def _task_digest(task):
    """Bind private scorer/task metadata without returning it to the parent."""
    dump = getattr(task, "model_dump", None)
    value = dump(mode="json") if callable(dump) else task
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def dispatch(
    environment,
    simulator,
    user_state,
    task,
    method,
    params,
    parent_call=None,
    on_stage=None,
):
    def set_stage(name):
        if name not in _FAILURE_STAGES:
            raise ValueError("Unknown evaluator diagnostic stage")
        if callable(on_stage):
            on_stage(name)

    if method == "initialize":
        set_stage("tau_worker_runtime_initialize")
        if environment is not None:
            raise ValueError("One fresh environment per process")
        environment, simulator, user_state, task = initialize(params)
        tools = environment.get_tools()
        result = {
            "tools": [tool.openai_schema for tool in tools],
            "mutations": {
                tool.name: environment.tools.tool_mutates_state(tool.name) for tool in tools
            },
            "policy": environment.get_policy(),
            "task_digest": _task_digest(task),
        }
        return environment, simulator, user_state, task, result
    if environment is None:
        raise ValueError("tau environment is not initialized")
    if method == "call":
        set_stage("tau_worker_tool_execution")
        from tau2.data_model.message import ToolCall

        response = environment.get_response(
            ToolCall(
                id=params["id"],
                name=params["name"],
                arguments=params["arguments"],
                requestor=params.get("requestor", "assistant"),
            )
        )
        return environment, simulator, user_state, task, {
            "id": response.id,
            "role": response.role,
            "requestor": response.requestor,
            "content": response.content,
            "error": response.error,
        }
    if method == "user_turn":
        set_stage("tau_worker_user_input_parse")
        if simulator is None or user_state is None or parent_call is None:
            raise ValueError("Official user simulator callback is unavailable")
        from tau2.data_model.message import (
            AssistantMessage,
            MultiToolMessage,
            ToolCall,
            ToolMessage,
        )

        incoming = params["message"]
        if incoming.get("role") == "assistant" and isinstance(
            incoming.get("content"), str
        ):
            message = AssistantMessage.text(incoming["content"])
        elif incoming.get("role") == "tool" and incoming.get("requestor") == "user":
            tool_messages = incoming.get("tool_messages")
            if tool_messages is None:
                message = ToolMessage.model_validate(incoming)
            else:
                if not isinstance(tool_messages, list) or not 1 <= len(tool_messages) <= 8:
                    raise ValueError("User tool result batch is outside its fixed bound")
                parsed = [ToolMessage.model_validate(item) for item in tool_messages]
                if any(item.requestor != "user" for item in parsed):
                    raise ValueError("User tool result batch lost its original requestor")
                message = (
                    parsed[0]
                    if len(parsed) == 1
                    else MultiToolMessage(role="tool", tool_messages=parsed)
                )
        else:
            raise ValueError("Unsupported official user simulator input message")

        user_module = importlib.import_module("tau2.user.user_simulator")
        original_generate = user_module.generate
        callback_usage = []

        def callback_generate(*, model, messages, tools=None, call_name=None, **_kwargs):
            set_stage("tau_worker_user_model_callback")
            payload = {
                "model": params["model"],
                "call_name": call_name,
                "messages": importlib.import_module(
                    "tau2.utils.llm_utils"
                ).to_litellm_messages(messages),
                "tools": [tool.openai_schema for tool in (tools or [])],
            }
            response = parent_call("tau_user_simulator", payload)
            set_stage("tau_worker_user_response_parse")
            if not isinstance(response, dict) or not isinstance(
                response.get("content"), (str, type(None))
            ):
                raise ValueError("Evaluator returned an invalid simulator response")
            reported_usage = response.get("tau_usage", response.get("usage"))
            if isinstance(reported_usage, dict):
                input_tokens = reported_usage.get(
                    "prompt_tokens", reported_usage.get("input_tokens")
                )
                output_tokens = reported_usage.get(
                    "completion_tokens", reported_usage.get("output_tokens")
                )
            else:
                input_tokens = output_tokens = None
            valid_usage = (
                type(input_tokens) is int
                and input_tokens >= 0
                and type(output_tokens) is int
                and output_tokens >= 0
            )
            cost = response.get("cost_usd")
            valid_cost = (
                type(cost) in {int, float}
                and math.isfinite(cost)
                and cost >= 0
            )
            callback_usage.append(
                {
                    "input_tokens": input_tokens if valid_usage else None,
                    "output_tokens": output_tokens if valid_usage else None,
                    "cost_usd": cost if valid_cost else None,
                }
            )
            tool_calls = []
            for item in response.get("tool_calls", []):
                if not isinstance(item, dict):
                    raise ValueError("Evaluator returned an invalid simulator tool call")
                tool_calls.append(
                    ToolCall(
                        id=item["id"],
                        name=item["name"],
                        arguments=item["arguments"],
                        requestor="assistant",
                    )
                )
            return AssistantMessage(
                role="assistant",
                content=response.get("content"),
                tool_calls=tool_calls or None,
                usage=(
                    {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                    }
                    if valid_usage
                    else None
                ),
                cost=cost if valid_cost else None,
                generation_time_seconds=response.get("generation_time_seconds"),
            )

        user_module.generate = callback_generate
        try:
            set_stage("tau_worker_user_simulator_step")
            user_message, user_state = simulator.generate_next_message(message, user_state)
        finally:
            user_module.generate = original_generate
        set_stage("tau_worker_user_result_encode")
        known_input = [item["input_tokens"] for item in callback_usage]
        known_output = [item["output_tokens"] for item in callback_usage]
        known_cost = [item["cost_usd"] for item in callback_usage]
        usage = {
            "requests": len(callback_usage),
            "input_tokens": sum(known_input)
            if all(v is not None for v in known_input)
            else None,
            "output_tokens": sum(known_output)
            if all(v is not None for v in known_output)
            else None,
            "cost_usd": sum(known_cost) if all(v is not None for v in known_cost) else None,
        }
        return environment, simulator, user_state, task, {
            "message": _plain_message(user_message),
            "stop": simulator.is_stop(user_message),
            "usage": usage,
        }
    if method == "score_simulation":
        set_stage("tau_worker_score_input_parse")
        if task is None or parent_call is None:
            raise ValueError("Official tau task scorer is unavailable")
        from tau2.data_model.simulation import SimulationRun, TerminationReason
        from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
        from tau2.orchestrator.modes import CommunicationMode
        from tau2.utils.llm_utils import to_tau2_messages
        from tau2.utils.utils import get_now

        raw_messages = params["messages"]
        if not isinstance(raw_messages, list) or len(raw_messages) > 2048:
            raise ValueError("Official scoring trajectory exceeds its message bound")
        flattened = []
        for item in raw_messages:
            if not isinstance(item, dict):
                raise ValueError("Invalid official scoring message")
            if "tool_messages" in item:
                tool_messages = item["tool_messages"]
                if not isinstance(tool_messages, list) or len(tool_messages) > 32:
                    raise ValueError("Invalid official multi-tool message")
                flattened.extend(tool_messages)
            else:
                flattened.append(item)
        trajectory = to_tau2_messages(flattened)
        reason = TerminationReason(params["termination_reason"])
        duration = params["duration"]
        if (
            type(duration) not in {int, float}
            or not math.isfinite(duration)
            or not 0 <= duration <= 1800
        ):
            raise ValueError("Official scoring duration is outside the trial budget")
        if type(params["simulation_id"]) is not str or len(params["simulation_id"]) > 128:
            raise ValueError("Invalid official simulation identifier")
        if type(params["start_time"]) is not str or len(params["start_time"]) > 64:
            raise ValueError("Invalid official simulation start time")
        simulation = SimulationRun(
            id=params["simulation_id"],
            task_id=task.id,
            start_time=params["start_time"],
            end_time=get_now(),
            duration=duration,
            termination_reason=reason,
            messages=trajectory,
        )

        nl_module = importlib.import_module("tau2.evaluator.evaluator_nl_assertions")
        original_generate = nl_module.generate
        judge_usage = []

        def callback_judge(*, model, messages, tools=None, call_name=None, **_kwargs):
            set_stage("tau_worker_official_judge_callback")
            payload = {
                "model": params["judge_model"],
                "call_name": call_name,
                "messages": importlib.import_module(
                    "tau2.utils.llm_utils"
                ).to_litellm_messages(messages),
                "tools": [tool.openai_schema for tool in (tools or [])],
            }
            response = parent_call("tau_official_nl_judge", payload)
            set_stage("tau_worker_score_execution")
            if not isinstance(response, dict) or not isinstance(
                response.get("content"), str
            ):
                raise ValueError("Evaluator returned an invalid official judge response")
            reported_usage = response.get("tau_usage", response.get("usage"))
            if isinstance(reported_usage, dict):
                input_tokens = reported_usage.get(
                    "prompt_tokens", reported_usage.get("input_tokens")
                )
                output_tokens = reported_usage.get(
                    "completion_tokens", reported_usage.get("output_tokens")
                )
            else:
                input_tokens = output_tokens = None
            valid_usage = (
                type(input_tokens) is int
                and input_tokens >= 0
                and type(output_tokens) is int
                and output_tokens >= 0
            )
            cost = response.get("cost_usd")
            valid_cost = (
                type(cost) in {int, float}
                and math.isfinite(cost)
                and cost >= 0
            )
            judge_usage.append(
                {
                    "input_tokens": input_tokens if valid_usage else None,
                    "output_tokens": output_tokens if valid_usage else None,
                    "cost_usd": cost if valid_cost else None,
                }
            )
            message = importlib.import_module("tau2.data_model.message").AssistantMessage
            return message(
                role="assistant",
                content=response["content"],
                usage=(
                    {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                    }
                    if valid_usage
                    else None
                ),
                cost=cost if valid_cost else None,
                generation_time_seconds=response.get("generation_time_seconds"),
            )

        nl_module.generate = callback_judge
        try:
            set_stage("tau_worker_score_execution")
            reward_info = evaluate_simulation(
                simulation=simulation,
                task=task,
                evaluation_type=EvaluationType.ALL,
                solo_mode=False,
                domain=environment.get_domain_name(),
                mode=CommunicationMode.HALF_DUPLEX,
                strict_replay=True,
            )
        finally:
            nl_module.generate = original_generate

        set_stage("tau_worker_score_result_encode")
        input_counts = [item["input_tokens"] for item in judge_usage]
        output_counts = [item["output_tokens"] for item in judge_usage]
        costs = [item["cost_usd"] for item in judge_usage]
        reward = reward_info.reward
        if type(reward) not in {int, float} or not math.isfinite(reward) or not 0 <= reward <= 1:
            raise ValueError("Original tau2 scorer returned an invalid reward")
        return environment, simulator, user_state, task, {
            "scored": True,
            "scorer": "tau2-bench-original-evaluate-simulation",
            "reward": reward,
            "score_components": _safe_reward_breakdown(
                getattr(reward_info, "reward_breakdown", None)
            ),
            "judge_usage": {
                "requests": len(judge_usage),
                "input_tokens": sum(input_counts)
                if all(value is not None for value in input_counts)
                else None,
                "output_tokens": sum(output_counts)
                if all(value is not None for value in output_counts)
                else None,
                "cost_usd": sum(costs) if all(value is not None for value in costs) else None,
            },
        }
    if method == "state":
        set_stage("tau_worker_state_read")
        return environment, simulator, user_state, task, {
            "agent": environment.get_db_hash(),
            "user": environment.get_user_db_hash(),
        }
    raise ValueError("Unknown tau bridge method")


def main():
    # Python audit hook also covers connect_ex; no provider HTTP can leave this
    # worker even if eager upstream imports attempt telemetry or model metadata.
    def deny_network(event, _args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Network/model access is forbidden in the tau tool worker")

    sys.addaudithook(deny_network)
    environment = simulator = user_state = task = None

    protocol_stdout = sys.stdout.buffer

    def request_parent(request_id, name, payload):
        if not isinstance(name, str) or not isinstance(payload, dict):
            raise ValueError("Invalid evaluator callback request")
        event = {
            "event": "parent_call",
            "request_id": request_id,
            "name": name,
            "payload": payload,
        }
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_FRAME:
            raise ValueError("Evaluator callback exceeds frame limit")
        # dispatch() redirects ordinary upstream stdout to stderr so its logs
        # cannot corrupt the protocol. Keep this original pipe for callbacks.
        protocol_stdout.write(encoded)
        protocol_stdout.flush()
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise ValueError("Evaluator callback response is missing or oversized")
        reply = json.loads(line)
        if (
            not isinstance(reply, dict)
            or reply.get("event") != "parent_result"
            or reply.get("request_id") != request_id
            or not isinstance(reply.get("result"), dict)
        ):
            raise ValueError("Evaluator callback response protocol failure")
        return reply["result"]

    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line:
            return 0
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            return 2
        request = {}
        stage = "tau_worker_protocol"
        try:
            request = json.loads(line)
            if type(request["id"]) is not int or not isinstance(request["params"], dict):
                raise ValueError("Invalid request envelope")
            stage = _METHOD_STAGES.get(request.get("method"), "tau_worker_protocol")

            def record_stage(name):
                nonlocal stage
                stage = name if name in _FAILURE_STAGES else "tau_worker_protocol"

            with contextlib.redirect_stdout(sys.stderr):
                def callback(name, payload, request_id=request["id"]):
                    return request_parent(request_id, name, payload)

                environment, simulator, user_state, task, result = dispatch(
                    environment,
                    simulator,
                    user_state,
                    task,
                    request["method"],
                    request["params"],
                    parent_call=callback,
                    on_stage=record_stage,
                )
            response = {"id": request["id"], "ok": True, "result": result}
        except Exception as exc:
            response = {
                "id": request.get("id"),
                "ok": False,
                "error_type": type(exc).__name__,
                "error_stage": stage,
            }
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(encoded) > MAX_FRAME:
            return 2
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        if not response["ok"]:
            return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Isolated benchmark session adapter for the production conditional interpreter."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from decagent.business.contracts import validate_schema
from decagent.business.plans.compiler import default_input_schema
from decagent.business.plans.evaluation_catalog import (
    ENTERPRISE_MCP_OBSERVATION_CONTRACT_VERSION,
    EvaluationCatalog,
    EvaluationScope,
)
from decagent.business.plans.runtime import NodeFailure, NodePending, NodeResult
from decagent.decisions.contracts import canonical_json, digest

from .comparison import ComparisonTrial, Route, TauStepLimitReached
from .enterprise_environment import EnterpriseEnvironment
from .journal import EvaluationJournalRepository
from .routes import TaskScore, TrialBoundary


class _ToolOutputValidationFailure(ValueError):
    """A benchmark result failed local parsing or its frozen output contract."""

    failure_stage = "tool_output_validation"


class EvaluationPlanSession:
    """One evaluator-owned upstream environment and its durable local journal.

    The evaluator supplies a fresh upstream environment, its original tool list,
    and fingerprints derived from the pinned task/environment. This adapter
    preserves original tool names and inputs, records effects locally, and uses
    the same production compiler, bindings and PlanRuntime as service-tickets.
    """

    def __init__(
        self,
        environment,
        catalog,
        *,
        trial,
        isolation_id,
        journal_root,
        actor_id,
        task_input,
        skill,
        boundary_facts,
        scorer,
        input_schema=None,
        memory_fixture=None,
        private_initial_state_fingerprint=None,
    ):
        if type(catalog) is not EvaluationCatalog:
            raise TypeError("Isolated public evaluation catalog required")
        catalog.assert_active()
        if not isinstance(trial, ComparisonTrial):
            raise TypeError("A frozen benchmark comparison trial is required")
        if type(boundary_facts) is not dict or set(boundary_facts) != {
            "initial_state_digest",
            "goal_digest",
            "actor_profile_digest",
            "permission_digest",
            "tool_adapter_digest",
        }:
            raise ValueError("Measured trial boundary fingerprints are required")
        if not callable(scorer) and not callable(
            getattr(scorer, "score_with_model_response", None)
        ):
            raise TypeError("The original benchmark verifier must be provided")
        self.environment = environment
        self.spec = catalog
        self.trial = trial
        self.isolation_id = isolation_id
        self.actor_id = actor_id
        self.task_input = deepcopy(task_input)
        self.input_schema = deepcopy(input_schema)
        self.skill = skill
        self._boundary_facts = deepcopy(boundary_facts)
        self._scorer = scorer
        self._private_initial_state_fingerprint = deepcopy(
            private_initial_state_fingerprint
        )
        self._private_score_evidence = None
        self._memory_fixture = deepcopy(memory_fixture or {})
        self._calls = 0
        self._official_tool_calls = []
        self._final_response = None
        self._route = None
        self._closed = False
        self._trajectory = []
        self._conversation_started = None
        self._user_model = None
        self._user_model_call = None
        self._judge_model = None
        self._judge_model_call = None
        self._deadline_provider = None
        self._tau_step_recorder = None
        self._tau_steps = 0
        self._termination_reason = "agent_stop"
        self._user_stopped = False
        self.repository = None
        self.task_id = isolation_id
        route_key = digest(
            {
                "dataset": trial.dataset,
                "version": trial.dataset_version,
                "case_id": trial.case_id,
                "isolation_id": isolation_id,
            }
        )
        self.journal_path = Path(journal_root) / route_key[:2] / (route_key + ".sqlite3")
        self.repository = EvaluationJournalRepository.create(
            self.journal_path,
            catalog,
            actor_id=actor_id,
            task_input=self.task_input,
            input_schema=self.input_schema,
        )
        self.task_id = self.repository.task_id
        self.gateway = self

    @classmethod
    def from_enterprise(
        cls,
        environment: EnterpriseEnvironment,
        scope: EvaluationScope,
        trial: ComparisonTrial,
        isolation_id,
        *,
        journal_root,
        scorer,
    ):
        public = environment.model_input()
        spec = environment.spec
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            public["tools"],
            benchmark_version=spec.image,
            result_contract_version=ENTERPRISE_MCP_OBSERVATION_CONTRACT_VERSION,
        )
        from .enterprise_scoring import bounded_state_fingerprint

        initial_state_fingerprint = bounded_state_fingerprint(environment.state_fingerprint())
        if initial_state_fingerprint["digest"] != trial.initial_state_digest:
            raise ValueError("Fresh EnterpriseOps state does not match the frozen trial")
        task_input = {"instruction": public["user_prompt"]}
        boundary_facts = {
            "initial_state_digest": initial_state_fingerprint["digest"],
            "goal_digest": digest(
                {"system_prompt": public["system_prompt"], "user_prompt": public["user_prompt"]}
            ),
            "actor_profile_digest": digest({"context_identity": spec.context}),
            "permission_digest": digest(
                {
                    "selected_tools": sorted(spec.selected_tools),
                    "restricted_tools": sorted(spec.restricted_tools),
                    "effective_tools": sorted(tool["name"] for tool in public["tools"]),
                }
            ),
            "tool_adapter_digest": digest(
                {
                    "image": spec.image,
                    "platform": spec.platform,
                    "mcp_protocol": "2024-11-05",
                    "catalog_digest": catalog.digest,
                }
            ),
        }
        actor_id = "eval:" + digest({"case_id": trial.case_id})[:32]
        return cls.from_upstream(
            environment,
            scope,
            trial=trial,
            isolation_id=isolation_id,
            journal_root=journal_root,
            actor_id=actor_id,
            task_input=task_input,
            skill=public["system_prompt"],
            boundary_facts=boundary_facts,
            scorer=scorer,
            benchmark_version=spec.image,
            result_contract_version=ENTERPRISE_MCP_OBSERVATION_CONTRACT_VERSION,
            private_initial_state_fingerprint=initial_state_fingerprint,
        )

    @classmethod
    def from_upstream(
        cls,
        environment,
        scope,
        *,
        trial,
        isolation_id,
        journal_root,
        actor_id,
        task_input,
        skill,
        boundary_facts,
        scorer,
        benchmark_version,
        result_contract_version,
        input_schema=None,
        memory_fixture=None,
        private_initial_state_fingerprint=None,
    ):
        public = environment.model_input()
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            public["tools"],
            benchmark_version=benchmark_version,
            result_contract_version=result_contract_version,
        )
        return cls(
            environment,
            catalog,
            trial=trial,
            isolation_id=isolation_id,
            journal_root=journal_root,
            actor_id=actor_id,
            task_input=task_input,
            skill=skill,
            boundary_facts=boundary_facts,
            scorer=scorer,
            input_schema=input_schema,
            memory_fixture=memory_fixture,
            private_initial_state_fingerprint=private_initial_state_fingerprint,
        )

    def reset(self):
        self._active()
        return not self._calls and not self.repository.nodes()

    def boundary(self):
        self._active()
        # Re-read the original upstream catalog so any in-session schema drift
        # changes its digest and fails TrialBoundary.check before another call.
        if hasattr(self.environment, "model_input"):
            current_tools = self.environment.model_input()["tools"]
            if digest(current_tools) != digest(self.spec.raw_tools):
                raise ValueError("Evaluation upstream tool catalog changed during a trial")
        return TrialBoundary(**self._boundary_facts)

    def verified_memories(self):
        self._active()
        return deepcopy(self._memory_fixture)

    def begin_route(self, route):
        self._active()
        self._route = Route(route)

    def load_workflow(self):
        self._active()
        raise ValueError("No verified public benchmark workflow is published for this trial")

    def validate_workflow(self, plan):
        self._active()
        from decagent.business.plans.compiler import compile_plan

        compile_plan(plan, self.spec, input_schema=self.input_schema)
        return True

    def publish_plan(self, plan, task_input):
        self._active()
        self.repository.publish_plan(plan, task_input)
        return self.repository

    def bind_deadline_provider(self, provider):
        self._active()
        if not callable(provider):
            raise TypeError("A public route deadline provider is required")
        self._deadline_provider = provider
        bind = getattr(self.environment, "set_deadline_provider", None)
        if callable(bind):
            bind(provider)

    def bind_tau_step_recorder(self, recorder):
        self._active()
        if not callable(recorder):
            raise TypeError("A public τ-step recorder is required")
        if self._tau_steps:
            raise ValueError("τ-step recording must be bound before the first conversation turn")
        self._tau_step_recorder = recorder

    @property
    def tau_steps(self):
        return self._tau_steps

    def record_tau_step(self, kind):
        self._active()
        if not self.supports_user_simulation:
            return 0
        if type(kind) is not str or kind not in {"agent", "user", "environment"}:
            raise ValueError("τ-step kind must be agent, user, or environment")
        if not callable(self._tau_step_recorder):
            raise ValueError("A τ-step recorder must be bound before simulation")
        admitted = self._tau_step_recorder(kind)
        if admitted is False:
            raise TauStepLimitReached("The frozen τ-Bench conversation-step limit was reached")
        self._tau_steps += 1
        return self._tau_steps

    def _request_timeout(self, supplied=None):
        values = []
        if supplied is not None:
            values.append(supplied)
        if self._deadline_provider is not None:
            values.append(self._deadline_provider())
        for value in values:
            if (
                type(value) not in {int, float}
                or not isfinite(value)
                or value <= 0
            ):
                raise TimeoutError("Public route or batch deadline expired")
        return min(values) if values else None

    def call(self, command, params, *, timeout_seconds=None):
        self._active()
        timeout_seconds = self._request_timeout(timeout_seconds)
        self.spec.validate(command, params)
        command_spec = self.spec.commands[command]
        call_id = self.repository.begin_external_call(command, params)
        self._calls += 1
        trajectory_call_id = uuid4().hex
        if self._conversation_started is not None:
            self._trajectory.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": trajectory_call_id,
                            "name": command,
                            "arguments": deepcopy(params),
                            "requestor": "assistant",
                        }
                    ],
                }
            )
        try:
            call_kwargs = (
                {"timeout_seconds": timeout_seconds}
                if timeout_seconds is not None
                else {}
            )
            raw = self.environment.call_tool(
                command, deepcopy(params), **call_kwargs
            )
            try:
                observed = self.spec.observe(command, raw)
            except ValueError as exc:
                raise _ToolOutputValidationFailure(
                    "Benchmark tool output failed its frozen result contract"
                ) from exc
            self._official_tool_calls.append(
                {"name": command, "args": deepcopy(params)}
            )
            self.repository.finish_external_call(
                call_id, "failed" if observed["is_error"] else "succeeded", observed
            )
            if self._conversation_started is not None:
                trajectory_result = raw
                if observed["normalizations"]:
                    trajectory_result = {
                        "content": [
                            {
                                "type": "text",
                                "text": canonical_json(observed["value"]),
                            }
                        ],
                        "isError": observed["is_error"],
                    }
                self._trajectory.append(
                    self._tool_message(trajectory_call_id, trajectory_result)
                )
            return observed
        except Exception as exc:
            self.repository.finish_external_call(
                call_id,
                "unknown" if command_spec.effect == "write" else "failed",
            )
            if self._conversation_started is not None:
                self._trajectory.append(
                    {
                        "id": trajectory_call_id,
                        "role": "tool",
                        "requestor": "assistant",
                        "content": "Evaluation tool call failed: " + type(exc).__name__,
                        "error": True,
                    }
                )
            raise NodeFailure("Original benchmark tool failed: " + type(exc).__name__) from exc

    @staticmethod
    def _tool_message(call_id, raw):
        if not isinstance(raw, dict):
            content = str(raw)
            is_error = False
        else:
            blocks = raw.get("content", [])
            content = (
                "\n".join(
                    item["text"]
                    for item in blocks
                    if isinstance(item, dict)
                    and item.get("type") == "text"
                    and isinstance(item.get("text"), str)
                )
                if isinstance(blocks, list)
                else str(blocks)
            )
            is_error = raw.get("isError") is True
        return {
            "id": call_id,
            "role": "tool",
            "requestor": "assistant",
            "content": content,
            "error": is_error,
        }

    @property
    def supports_user_simulation(self):
        capability = getattr(self.environment, "supports_user_simulation", None)
        if type(capability) is bool:
            return capability
        return all(
            callable(getattr(self.environment, name, None))
            for name in ("user_turn", "call_user_tool", "score_simulation")
        )

    @property
    def requires_official_judge(self):
        return (
            not self.supports_user_simulation
            and getattr(self._scorer, "requires_judge", False) is True
        )

    def configure_official_judge(self, *, model, callback):
        """Attach a route-ledgered judge to an Enterprise response-check scorer."""
        self._active()
        if not self.requires_official_judge:
            raise ValueError("This isolated session does not require an Enterprise judge")
        if getattr(self._scorer, "judge_client_configured", False) is True:
            raise ValueError(
                "Controlled response checks require a scorer without a preconfigured judge"
            )
        if type(model) is not str or not model.strip() or len(model) > 256:
            raise ValueError("A bounded official judge model identity is required")
        if not callable(callback):
            raise TypeError("A measured, ledgered official judge callback is required")
        from .enterprise_scoring import BudgetedEnterpriseJudge

        bridge = _LedgeredEnterpriseJudgeBridge(callback, model)
        self._scorer.configure_judge_client(
            BudgetedEnterpriseJudge(bridge, bridge.usage_snapshot)
        )

    @property
    def user_stopped(self):
        return self._user_stopped

    @property
    def conversation(self):
        return deepcopy(self._trajectory)

    def configure_conversation(
        self, *, user_model, user_model_call, judge_model, judge_model_call
    ):
        self._active()
        if not self.supports_user_simulation:
            raise ValueError("This isolated benchmark environment has no official user simulator")
        for value, name in ((user_model, "user model"), (judge_model, "official judge model")):
            if type(value) is not str or not value.strip() or len(value) > 256:
                raise ValueError("A bounded " + name + " identifier is required")
        if not callable(user_model_call) or not callable(judge_model_call):
            raise ValueError("Measured user simulator and official judge callbacks are required")
        self._user_model = user_model
        self._user_model_call = user_model_call
        self._judge_model = judge_model
        self._judge_model_call = judge_model_call

    def start_conversation(self):
        self._active()
        if not self.supports_user_simulation or self._user_model_call is None:
            raise ValueError("The official τ user simulator was not configured")
        if self._conversation_started is not None:
            raise ValueError("An isolated benchmark conversation can only start once")
        self._conversation_started = perf_counter()
        greeting = {"role": "assistant", "content": "Hi! How can I help you today?"}
        self._trajectory.append(greeting)
        message = self._send_to_user(greeting)
        content = message.get("content")
        if not isinstance(content, str) and not self._user_stopped:
            raise ValueError("The official user simulator did not provide an initial user message")
        if isinstance(content, str):
            updated = deepcopy(self.task_input)
            updated["user_message"] = content
            validate_schema(self.input_schema, updated)
            self.repository.save_context({"task_input": updated})
            self.task_input = updated
        return deepcopy(message)

    def _send_to_user(self, message):
        self.record_tau_step("user")
        timeout_seconds = self._request_timeout()
        call_kwargs = {
            "model": self._user_model,
            "on_model_call": self._user_model_call,
        }
        if timeout_seconds is not None:
            call_kwargs["timeout_seconds"] = timeout_seconds
        result = self.environment.user_turn(deepcopy(message), **call_kwargs)
        if type(result) is not dict or type(result.get("message")) is not dict:
            raise ValueError("The isolated user simulator returned an invalid message")
        incoming = deepcopy(result["message"])
        if (
            incoming.get("role") != "user"
            or type(result.get("stop")) is not bool
            or (
                incoming.get("content") is not None
                and (
                    type(incoming.get("content")) is not str
                    or len(incoming["content"]) > 16_384
                )
            )
        ):
            raise ValueError("The isolated simulator must return a user message")
        self._trajectory.append(incoming)
        self._user_stopped = result.get("stop") is True
        if self._user_stopped:
            self._termination_reason = "user_stop"
        message = incoming
        for _ in range(16):
            calls = message.get("tool_calls") or []
            if not calls:
                return message
            if type(calls) is not list or len(calls) > 8:
                raise ValueError("The τ user simulator exceeded its bounded tool call count")
            if any(
                type(call) is not dict
                or type(call.get("id")) is not str
                or not call["id"].strip()
                or type(call.get("name")) is not str
                or not call["name"].strip()
                or type(call.get("arguments")) is not dict
                or call.get("requestor", "user") != "user"
                for call in calls
            ):
                raise ValueError("The τ user simulator returned an invalid user tool call")
            self.record_tau_step("environment")
            tool_messages = []
            for call in calls:
                timeout_seconds = self._request_timeout()
                call_kwargs = (
                    {"timeout_seconds": timeout_seconds}
                    if timeout_seconds is not None
                    else {}
                )
                result = self.environment.call_user_tool(
                    SimpleNamespace(
                        id=call["id"],
                        name=call["name"],
                        arguments=deepcopy(call["arguments"]),
                        requestor="user",
                    ),
                    **call_kwargs,
                )
                if type(result) is not dict or result.get("requestor") != "user":
                    raise ValueError("A τ user tool response lost its user requestor")
                tool_messages.append(deepcopy(result))
            simulator_input = (
                tool_messages[0]
                if len(tool_messages) == 1
                else {
                    "role": "tool",
                    "requestor": "user",
                    "tool_messages": deepcopy(tool_messages),
                }
            )
            self._trajectory.append(deepcopy(simulator_input))
            self.record_tau_step("user")
            timeout_seconds = self._request_timeout()
            call_kwargs = {
                "model": self._user_model,
                "on_model_call": self._user_model_call,
            }
            if timeout_seconds is not None:
                call_kwargs["timeout_seconds"] = timeout_seconds
            next_turn = self.environment.user_turn(simulator_input, **call_kwargs)
            if (
                type(next_turn) is not dict
                or type(next_turn.get("message")) is not dict
                or type(next_turn.get("stop")) is not bool
            ):
                raise ValueError("The isolated user simulator returned an invalid tool response")
            message = deepcopy(next_turn["message"])
            if message.get("role") != "user":
                raise ValueError("The isolated simulator must return a user message")
            self._trajectory.append(message)
            self._user_stopped = next_turn["stop"]
            if self._user_stopped:
                self._termination_reason = "user_stop"
                return message
        raise ValueError("The τ user simulator exceeded its bounded tool cycle")

    def ask_user(self, prompt):
        self._active()
        if self._conversation_started is None or type(prompt) is not str or not prompt.strip():
            raise NodeFailure("A bounded official user conversation is required")
        assistant = {"role": "assistant", "content": prompt}
        self._trajectory.append(assistant)
        message = self._send_to_user(assistant)
        answer = message.get("content")
        if self._user_stopped:
            raise NodePending(
                {"kind": "user_stop", "reason": "The official τ user simulator ended the task"}
            )
        if not isinstance(answer, str) or not answer.strip():
            raise NodeFailure("The official τ user simulator returned no clarification text")
        return answer

    def finish_conversation(self, message):
        self._active()
        if self._conversation_started is None:
            return
        if type(message) is not str or not message.strip() or len(message) > 16_384:
            raise ValueError("A bounded final assistant message is required for τ scoring")
        assistant = {"role": "assistant", "content": message}
        self._trajectory.append(assistant)
        if self._user_stopped:
            return
        self._send_to_user(assistant)
        if not self._user_stopped:
            self._termination_reason = "agent_stop"

    def set_final_response(self, message):
        """Retain a route-supplied actual final response for private scoring."""
        self._active()
        if type(message) is not str or not message.strip() or len(message) > 16_384:
            raise ValueError("A bounded actual final response is required")
        if self._final_response is not None and self._final_response != message:
            raise ValueError("The official final response is immutable")
        self.repository.save_context({"final_response": message})
        self._final_response = message

    def prepare_final_response(self, policy):
        from .final_response import prepare_response

        self._active()
        observations = []
        for call in self.repository.external_calls():
            observed = call["result_snapshot"]
            if call["status"] not in {"succeeded", "failed"} or observed is None:
                raise ValueError("Final response cannot infer an unresolved tool outcome")
            observations.append({
                "call_id": call["call_id"], "tool": call["command"], "status": call["status"],
                "observation": deepcopy(observed["raw"]),
            })
        return prepare_response(
            policy, observations, task_input=self.task_input, skill=self.skill,
        )

    @property
    def private_initial_state_fingerprint(self):
        return deepcopy(self._private_initial_state_fingerprint)

    @property
    def private_score_evidence(self):
        return deepcopy(self._private_score_evidence)

    def scoring_model_usage(self):
        """Measured Enterprise judge usage, including unsuccessful dispatches."""
        return deepcopy(getattr(self._scorer, "observed_judge_usage", None))

    def _official_model_response(self):
        response = {
            "content": self._final_response if self._final_response is not None else "",
            "tool_calls": deepcopy(self._official_tool_calls),
        }
        if len(canonical_json(response).encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("Official model response projection exceeds its bound")
        return response

    def before_node(self, node, _context, saved):
        self._active()
        if not saved:
            return
        if node["type"] == "governed_write" and (
            saved.get("dispatch_attempts", 0) > 0
            or saved.get("status") in {"unknown", "waiting", "running"}
        ):
            request = (saved.get("output_snapshot") or {}).get("request") or {
                "kind": "evaluation_tool_outcome_unknown",
                "node_id": node["id"],
                "command": node["command"]["command"],
                "reason": "A dispatched benchmark write has no verified final result",
            }
            raise NodePending(request, unknown=True)

    def handle_node(self, node, inputs, context, saved, gateway):
        self._active()
        if node["type"] == "ask_user":
            answer = self.ask_user(node["prompt"])
            return NodeResult(self._node_record(node, inputs, context, answer))
        if node["type"] != "governed_write":
            raise NodeFailure("Only original upstream writes use this evaluation handler")
        command = node["command"]["command"]
        if saved.get("status") != "running" or saved.get("dispatch_attempts", 0):
            raise NodePending(
                {
                    "kind": "evaluation_tool_outcome_unknown",
                    "node_id": node["id"],
                    "command": command,
                    "reason": "A benchmark write dispatch cannot be replayed",
                },
                unknown=True,
            )
        self.repository.guard()
        self.repository.dispatch(
            node["id"], max_attempts=1, generation=saved["execution_generation"]
        )
        try:
            observed = gateway.call(command, inputs)
        except Exception as exc:
            raise NodePending(
                {
                    "kind": "evaluation_tool_outcome_unknown",
                    "node_id": node["id"],
                    "command": command,
                    "reason": (
                        "The upstream write may have taken effect; automatic retry is disabled"
                    ),
                    "error_type": type(exc).__name__,
                },
                unknown=True,
            ) from exc
        if observed["is_error"]:
            raise NodeFailure("Original benchmark tool returned isError")
        return NodeResult(self._node_record(node, inputs, context, observed))

    def handle_react_write(self, command, params, gateway):
        self._active()
        if self.repository.has_unknown_external_write():
            raise NodePending(
                {
                    "kind": "evaluation_tool_outcome_unknown",
                    "command": command,
                    "reason": "An earlier upstream write has no verified result",
                },
                unknown=True,
            )
        try:
            return gateway.call(command, params)
        except Exception as exc:
            raise NodePending(
                {
                    "kind": "evaluation_tool_outcome_unknown",
                    "command": command,
                    "reason": (
                        "The upstream write may have taken effect; automatic retry is disabled"
                    ),
                    "error_type": type(exc).__name__,
                },
                unknown=True,
            ) from exc

    def accept_decision(self, node, question, value, context, _saved):
        self._active()
        from decagent.evaluation.business.semantic_templates import (
            CSM_KNOWLEDGE_TEMPLATE_VERSION,
            csm_knowledge_tool_is_supported,
        )

        if (
            type(self.spec) is not EvaluationCatalog
            or node.get("question", {}).get("question_id") != "csm_knowledge_article"
            or question.type != "choice"
            or question.version != CSM_KNOWLEDGE_TEMPLATE_VERSION
            or not csm_knowledge_tool_is_supported(self.spec)
            or type(value) is not dict
        ):
            raise NodeFailure("The isolated catalog has no matching reviewed semantic template")
        selected = value.get("selected_value")
        if selected != value.get("choice") or selected not in question.criteria:
            raise NodeFailure("CSM choice does not match a registered candidate")
        accepted_value = deepcopy(value)
        if selected != "no_match":
            prefix = "article:"
            if not selected.startswith(prefix) or not selected[len(prefix) :].isdecimal():
                raise NodeFailure("CSM choice does not name a returned knowledge article")
            knowledge_id = int(selected[len(prefix) :])
            if knowledge_id < 1:
                raise NodeFailure("CSM knowledge article ID is invalid")
            binding = node.get("inputs", {}).get("articles", {})
            source = context.node_outputs.get(binding.get("node_id"))
            candidates = (
                source.get("data", {}).get("value") if isinstance(source, dict) else None
            )
            if (
                not isinstance(source, dict)
                or source.get("verified") is not True
                or source.get("kind") != "read_tool"
                or not isinstance(candidates, list)
                or not any(
                    isinstance(article, dict)
                    and article.get("knowledge_id") == knowledge_id
                    and article.get("state") == "published"
                    for article in candidates
                )
            ):
                raise NodeFailure("CSM choice is outside the verified published result")
            accepted_value["knowledge_id"] = knowledge_id
        return NodeResult(
            {
                "kind": "decide",
                "accepted": True,
                "value": accepted_value,
                "selection_origin": "reviewed_semantic_evaluation",
                "employee_id": context.employee_id,
                "scope_revision": context.scope_revision,
                "account_revision": context.account_revision,
                "grants_write_authority": False,
            },
            no_match=selected == "no_match",
        )

    def evaluation_execution_finished(self, compiled, node, context):
        self._active()
        evidence = {item["id"]: item for item in compiled.plan.goal["evidence"]}
        from decagent.business.plans.bindings import resolve_binding

        observed = []
        for evidence_id in node["evidence"]:
            item = evidence[evidence_id]
            resolve_binding(item["source"], context)
            observed.append(evidence_id)
        return {
            "kind": "evaluation_execution_finished",
            "valid": bool(observed),
            "business_goal_verified": False,
            "observed_evidence_count": len(observed),
        }

    def score(self):
        self._active()
        if self.supports_user_simulation:
            if self._conversation_started is None or self._judge_model_call is None:
                raise ValueError(
                    "The official τ conversation and judge callbacks were not configured"
                )
            duration = min(1800.0, max(0.0, perf_counter() - self._conversation_started))
            self.repository.save_simulation_trajectory(
                self.conversation,
                termination_reason=self._termination_reason,
                duration=duration,
            )
            simulation = self.repository.simulation_trajectory()
            if simulation is None:
                raise ValueError("The official τ simulation trajectory was not persisted")
            timeout_seconds = self._request_timeout()
            call_kwargs = dict(
                messages=simulation["messages"],
                termination_reason=simulation["termination_reason"],
                duration=simulation["duration"],
                judge_model=self._judge_model,
                on_model_call=self._judge_model_call,
            )
            if timeout_seconds is not None:
                call_kwargs["timeout_seconds"] = timeout_seconds
            result = self.environment.score_simulation(**call_kwargs)
            if (
                type(result) is not dict
                or result.get("scored") is not True
                or result.get("scorer") != "tau2-bench-original-evaluate-simulation"
                or type(result.get("reward")) not in {int, float}
            ):
                raise ValueError("The pinned τ-Bench official evaluator returned an invalid score")
            result = TaskScore(
                success=float(result["reward"]) == 1.0,
                forbidden_side_effects=None,
                benchmark_score=float(result["reward"]),
                official=True,
            )
        else:
            score_with_response = getattr(self._scorer, "score_with_model_response", None)
            if callable(score_with_response):
                try:
                    result = score_with_response(
                        self.environment,
                        self._official_model_response(),
                        initial_state_fingerprint=self._private_initial_state_fingerprint,
                    )
                finally:
                    evidence = getattr(self._scorer, "private_audit_evidence", None)
                    if evidence is not None:
                        self._private_score_evidence = deepcopy(evidence)
                        self.repository.record_private_scoring_evidence(evidence)
            else:
                result = self._scorer(self.environment)
        if not isinstance(result, TaskScore):
            raise TypeError("Original benchmark verifier must return TaskScore")
        auxiliary = getattr(self.environment, "auxiliary_usage", None)
        if isinstance(auxiliary, dict):
            simulator = auxiliary.get("user_simulator", {})
            judge = auxiliary.get("official_judge", {})
            if simulator.get("requests", 0) or judge.get("requests", 0):
                result = replace(
                    result,
                    user_simulator_cost_usd=(
                        simulator.get("cost_usd")
                        if simulator.get("requests", 0)
                        else result.user_simulator_cost_usd
                    ),
                    user_simulator_requests=(
                        simulator["requests"]
                        if simulator.get("requests", 0)
                        else result.user_simulator_requests
                    ),
                    user_simulator_tokens=(
                        simulator.get("tokens")
                        if simulator.get("requests", 0)
                        else result.user_simulator_tokens
                    ),
                    simulator_seconds=(
                        simulator.get("seconds", 0.0)
                        if simulator.get("requests", 0)
                        else result.simulator_seconds
                    ),
                    judge_cost_usd=(
                        judge.get("cost_usd")
                        if judge.get("requests", 0)
                        else result.judge_cost_usd
                    ),
                    judge_requests=(
                        judge["requests"]
                        if judge.get("requests", 0)
                        else result.judge_requests
                    ),
                    judge_tokens=(
                        judge.get("tokens")
                        if judge.get("requests", 0)
                        else result.judge_tokens
                    ),
                    judge_seconds=(
                        judge.get("seconds", 0.0)
                        if judge.get("requests", 0)
                        else result.judge_seconds
                    ),
                )
        return result

    def _node_record(self, node, inputs, context, observed):
        return {
            "kind": node["type"],
            "verified": True,
            "data": deepcopy(observed),
            "inputs": deepcopy(inputs),
            "employee_id": context.employee_id,
            "scope_revision": context.scope_revision,
            "account_revision": context.account_revision,
        }

    def _active(self):
        if self._closed:
            raise ValueError("Evaluation session is closed")
        self.spec.assert_active()

    def close(self):
        if self._closed:
            return
        if self.repository is not None:
            self.repository.close()
        self._closed = True

    def cleanup_evidence(self):
        """Report only verified session/environment teardown facts."""
        environment_closed = getattr(self.environment, "cleanup_verified", False) is True
        return {
            "verified": self._closed and environment_closed,
            "session_closed": self._closed,
            "environment_closed": environment_closed,
        }


class _LedgeredEnterpriseJudgeBridge:
    """Adapt verifier messages to the frozen, ledgered public judge callback."""

    def __init__(self, callback, model):
        self.callback = callback
        self.model = model
        ledger = getattr(callback, "ledger", None)
        self.trial_id = getattr(callback, "trial_id", None)
        self.route = getattr(callback, "route", None)
        if (
            not callable(getattr(ledger, "model_attempts_snapshot", None))
            or type(self.trial_id) is not str
            or type(self.route) is not str
        ):
            raise TypeError("Enterprise judge callback must expose its durable attempt ledger")
        self.ledger = ledger

    @staticmethod
    def _messages(messages):
        if type(messages) not in {list, tuple} or not 1 <= len(messages) <= 8:
            raise ValueError("Enterprise judge messages exceed the frozen request contract")
        role_names = {
            "system": "system",
            "human": "user",
            "user": "user",
            "ai": "assistant",
            "assistant": "assistant",
            "tool": "tool",
        }
        result = []
        for message in messages:
            if type(message) is dict:
                role, content = message.get("role"), message.get("content")
            else:
                role = getattr(message, "type", None)
                content = getattr(message, "content", None)
            role = role_names.get(role)
            if role is None or type(content) is not str or len(content) > 32_768:
                raise ValueError("Enterprise judge messages must be bounded text messages")
            result.append({"role": role, "content": content})
        return result

    async def ainvoke(self, messages):
        """Call the sync HTTP adapter in its current ledger-owning thread."""
        from .routes import ModelReply

        reply = self.callback(
            {"model": self.model, "messages": self._messages(messages)}
        )
        if (
            not isinstance(reply, ModelReply)
            or type(reply.value) is not dict
            or type(reply.value.get("content")) is not str
            or reply.value.get("tool_calls") != []
        ):
            raise ValueError("Enterprise judge callback returned an invalid text-only response")
        return SimpleNamespace(content=reply.value["content"])

    def usage_snapshot(self):
        rows = self.ledger.model_attempts_snapshot(self.trial_id, self.route)
        counted = [
            row for row in rows
            if row.get("role") == "judge" and row.get("status") != "not_sent"
        ]
        if any(row.get("status") not in {"settled", "settled_overrun"} for row in counted):
            from .enterprise_scoring import EnterpriseScoringError

            raise EnterpriseScoringError("Enterprise judge dispatch usage is unresolved")
        input_tokens, output_tokens = 0, 0
        seconds = 0.0
        costs = []
        for row in counted:
            prompt = row.get("actual_input_tokens")
            completion = row.get("actual_output_tokens")
            elapsed = row.get("attempt_seconds")
            if (
                type(prompt) is not int or prompt < 0
                or type(completion) is not int or completion < 0
                or type(elapsed) not in {int, float} or elapsed < 0
            ):
                from .enterprise_scoring import EnterpriseScoringError

                raise EnterpriseScoringError("Enterprise judge usage is incomplete")
            input_tokens += prompt
            output_tokens += completion
            seconds += float(elapsed)
            amount = row.get("actual_cost_usd")
            if amount is None:
                costs.append(None)
            else:
                try:
                    costs.append(Decimal(str(amount)))
                except (InvalidOperation, ValueError) as exc:
                    from .enterprise_scoring import EnterpriseScoringError

                    raise EnterpriseScoringError(
                        "Enterprise judge cost evidence is invalid"
                    ) from exc
        cost = None if any(item is None for item in costs) else sum(costs, Decimal("0"))
        return {
            "requests": len(counted),
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "cost_usd": cost,
            "seconds": seconds,
        }


def enterprise_session_factory(
    spec, dataset_version, journal_root, scorer_factory=None, *, judge_client=None
):
    """Build fresh Docker/MCP/session objects for each controlled route run.

    ``scorer_factory`` receives the live EnterpriseEnvironment and pinned task
    spec, and must delegate to the upstream official VerifierEngine.
    """

    @contextmanager
    def factory(trial, isolation_id):
        if trial.case_id != spec.task_id or trial.dataset_version != dataset_version:
            raise ValueError("Frozen comparison trial does not match the pinned Enterprise task")
        with EnterpriseEnvironment(spec) as environment:
            with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
                if scorer_factory is None:
                    from .enterprise_scoring import EnterpriseOfficialScorer

                    scorer = EnterpriseOfficialScorer(spec, judge_client=judge_client)
                else:
                    scorer = scorer_factory(environment, spec)
                session = EvaluationPlanSession.from_enterprise(
                    environment,
                    scope,
                    trial,
                    isolation_id,
                    journal_root=journal_root,
                    scorer=scorer,
                )
                try:
                    yield session
                finally:
                    session.close()

    return factory


def tau_session_factory(
    environment_factory,
    *,
    dataset_version,
    tool_catalog_version,
    journal_root,
    task_input_factory,
    skill_factory,
    boundary_factory,
    scorer_factory,
    input_schema_factory=None,
):
    """Build the same MCP session over a freshly configured tau2 Environment.

    The injected factories belong to the pinned upstream harness: they create
    the official task/user simulation, derive public task input and frozen
    environment fingerprints, and delegate scoring to tau2's original judge.
    This module does not import tau2's optional model/provider dependencies.
    """
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment

    @contextmanager
    def factory(trial, isolation_id):
        if trial.dataset_version != dataset_version:
            raise ValueError("Frozen τ-Bench trial does not match the pinned source revision")
        task_input = task_input_factory(trial)
        skill = skill_factory(trial)
        with TauMCPEnvironment(
            lambda: environment_factory(trial),
            system_prompt=skill,
            user_prompt=task_input["instruction"],
        ) as environment:
            with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
                boundary_facts = boundary_factory(environment, trial)
                session = EvaluationPlanSession.from_upstream(
                    environment,
                    scope,
                    trial=trial,
                    isolation_id=isolation_id,
                    journal_root=journal_root,
                    actor_id="eval:" + digest({"case_id": trial.case_id})[:32],
                    task_input=task_input,
                    skill=skill,
                    boundary_facts=boundary_facts,
                    scorer=lambda _environment: scorer_factory(environment.environment, trial),
                    benchmark_version=tool_catalog_version,
                    result_contract_version="tau2-mcp-observation-v1",
                    input_schema=_tau_input_schema(
                        input_schema_factory(trial) if input_schema_factory else None
                    ),
                )
                try:
                    yield session
                finally:
                    session.close()

    return factory


def _tau_input_schema(schema):
    """Add the real first user turn as an explicit plan-bindable task field."""
    value = deepcopy(schema) if schema is not None else default_input_schema()
    properties = value.get("properties") if isinstance(value, dict) else None
    if not isinstance(properties, dict):
        raise ValueError("τ plan input schema must declare object properties")
    current = properties.get("user_message")
    if current is not None and current != {"type": "string", "maxLength": 8192}:
        raise ValueError("τ user_message must use the fixed bounded text schema")
    properties["user_message"] = {"type": "string", "maxLength": 8192}
    return value

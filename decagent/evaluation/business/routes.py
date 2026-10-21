"""Executable controlled routes over the production conditional-plan interpreter.

This module is transport independent, not a second plan engine. A session adapter
must reset an isolated environment, publish a fresh application PlanRepository,
and supply the same employee-bound gateway and governance handlers to all routes.
The factory is entered anew for EVERY run, with a new isolation ID. Adapters must
discard learned memory/model caches; only the explicit frozen memory fixture and
published workflow supplied by the session may survive. No trial results are fed
back to subsequent runs.

The business-context-memory requirement "Generic planner receives no cross-task
memory by default" applies to both planner and ReAct requests. Only a validated
published hot workflow may consume copied memory through explicit stable slots.

Model callbacks are single dispatches (no hidden retries or response caches).
They return measured tokens/cost/model version, including known failed requests
via ModelCallError. Unknown usage prevents conversion to a reportable RouteRun.
Production budget reservations remain the responsibility of the model adapter;
these runners neither open network connections nor bypass the cumulative ledger.

There is deliberately no purported business-2 baseline: that requires a frozen
legacy service adapter. Public Tau/EnterpriseOps tools use an evaluator-owned
EvaluationCatalog and isolated journal; their raw MCP envelopes never become
production PluginSpecs. Official scores still come only from upstream verifiers.
"""

import inspect
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from math import isfinite
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from decagent.business.contracts import PluginSpec, validate_schema
from decagent.business.decision_policy import normalize_answer
from decagent.business.mcp_contract import FORBIDDEN_INPUT_KEYS
from decagent.business.plans.compiler import compile_plan, default_input_schema
from decagent.business.plans.contracts import Plan
from decagent.business.plans.evaluation_catalog import EvaluationCatalog
from decagent.business.plans.runtime import NodeFailure, NodePending, NodeResult, PlanRuntime
from decagent.business.plans.templates import QuestionRegistry
from decagent.decisions.contracts import digest, json_value
from decagent.evaluation.business.public_resources import (
    PublicModelOutcomeUnknown,
    PublicModelProfileDrift,
    PublicToolOutcomeUnknown,
)

from .comparison import ComparisonTrial, Route, RouteRun, TauStepLimitReached
from .final_response import DEFAULT_RESPONSE_POLICY, FinalResponsePolicy, accept_summary
from .model_profiles import ModelProfile
from .public_diagnostics import safe_exception_type


def _money(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("Cost must be a finite nonnegative Decimal")


@dataclass(frozen=True)
class ProviderBillingEvidence:
    """Original per-request billing facts, kept separate from voucher and cash.

    Only values present in a provider activity or invoice record belong here.
    Account-window totals must not be divided across requests.
    """

    gross_amount: Decimal
    currency: str
    voucher_credit_amount: Decimal | None = None
    cash_paid_amount: Decimal | None = None
    source: str = "provider_activity"
    record_id: str | None = None

    def __post_init__(self):
        _money(self.gross_amount)
        if type(self.currency) is not str or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Provider billing currency must be an uppercase ISO-style code")
        for name, amount in (
            ("voucher credit", self.voucher_credit_amount),
            ("cash paid", self.cash_paid_amount),
        ):
            if amount is not None:
                try:
                    _money(amount)
                except ValueError as exc:
                    raise ValueError("Provider " + name + " must be nonnegative") from exc
        if self.source not in {"provider_activity", "provider_invoice"}:
            raise ValueError("Provider billing evidence source is not supported")
        if self.record_id is not None and (
            type(self.record_id) is not str
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", self.record_id)
        ):
            raise ValueError("Provider billing record ID must be a bounded safe identifier")


def _validate_tool(spec, command, params):
    if not isinstance(command, str) or command not in spec.commands or type(params) is not dict:
        raise NodeFailure("Unregistered tool or invalid arguments")
    if type(spec) is not EvaluationCatalog and any(
        "".join(c for c in key.lower() if c.isalnum()) in FORBIDDEN_INPUT_KEYS for key in params
    ):
        raise NodeFailure("Tool arguments cannot replace program-owned identity")
    spec.validate(command, params)


def _record_tau_step(session, kind):
    if not getattr(session, "supports_user_simulation", False):
        return
    recorder = getattr(session, "record_tau_step", None)
    if not callable(recorder):
        raise ValueError("τ sessions must expose the official conversation-step recorder")
    admitted = recorder(kind)
    if admitted is False:
        raise TauStepLimitReached("The frozen τ-Bench conversation-step limit was reached")


def _validate_memories(memories):
    # Session owns source-evidence/ACL verification against the restored fixture.
    # Never expose an obviously unverified/inactive record even to the planner.
    if not isinstance(memories, dict):
        raise ValueError("Frozen verified memory fixture must be a mapping")
    for record in memories.values():
        if (
            not isinstance(record, dict)
            or record.get("verified") is not True
            or record.get("active") is not True
            or record.get("expired") is not False
            or record.get("stale", False) is not False
            or not record.get("evidence_id")
        ):
            raise ValueError("Frozen memory requires current verified source evidence")


def _uses_memory(value):
    if isinstance(value, dict):
        return value.get("source") == "verified_memory" or any(
            _uses_memory(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_uses_memory(item) for item in value)
    return False


@dataclass(frozen=True)
class TrialBoundary:
    """Observed digests, computed from the restored environment, not copied labels."""

    initial_state_digest: str
    goal_digest: str
    actor_profile_digest: str
    permission_digest: str
    tool_adapter_digest: str

    def check(self, trial, *, initial=True):
        for name, value in asdict(self).items():
            if not initial and name == "initial_state_digest":
                continue
            if value != getattr(trial, name):
                raise ValueError("Trial boundary changed: " + name)


class ToolGateway(Protocol):
    """Same authenticated tool transport for every strategy; returns raw tool data."""

    def call(
        self, command: str, params: dict, *, timeout_seconds: float | None = None
    ) -> dict: ...


class TrialSession(Protocol):
    """Application adapter port; factory context exit closes all per-trial clients.

    reset() returns True only after database, task journals, caches and learned
    memories are reset. boundary() independently measures snapshot/goal/actor/
    permissions/tool implementation+catalog identity. The adapter must bind the
    registered spec to that catalog digest. publish_plan creates a new, leased,
    empty application repository. Its ID equals the freshly allocated session
    task_id (or isolation_id for adapters using that ID); no old node, decision,
    action, approval or model journal may be reused. Application task IDs may be
    allocated by Database.create_business_task independently of isolation_id.

    handle_node must use existing governed write/ask-user services and the given
    observed gateway (never treat a semantic answer as approval). accept_decision
    applies the SAME frozen gate/normalization policy to either semantic model;
    it returns a current NodeResult or raises NodePending for human intervention.
    score is private and never exposed to model callbacks. Hot workflow loading
    and validation are deterministic; prior compilation/model preparation costs
    belong to the separately retained workflow publication evidence.
    """

    isolation_id: str
    spec: PluginSpec | EvaluationCatalog
    gateway: ToolGateway
    task_input: dict
    skill: str

    def reset(self) -> bool: ...
    def boundary(self) -> TrialBoundary: ...
    def verified_memories(self) -> dict: ...
    def load_workflow(self) -> Plan | dict: ...
    def validate_workflow(self, plan: Plan) -> bool: ...
    def publish_plan(self, plan: Plan, task_input: dict): ...
    def handle_node(self, node, inputs, context, saved, gateway) -> NodeResult: ...
    def accept_decision(self, node, question, value, context, saved) -> NodeResult: ...
    def score(self) -> "TaskScore": ...


@dataclass(frozen=True)
class ModelReply:
    value: object
    tokens: int
    cost_usd: Decimal | None
    model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    profile: ModelProfile | None = None
    cost_source: str | None = None
    provider: str | None = None
    provider_request_id: str | None = None
    generation_id: str | None = None
    http_status: int | None = None
    billing_evidence: ProviderBillingEvidence | None = None
    cached_input_tokens: int | None = None

    def __post_init__(self):
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("Measured model tokens must be a nonnegative integer")
        if self.cost_usd is not None:
            _money(self.cost_usd)
        if self.model is not None and (type(self.model) is not str or not self.model.strip()):
            raise ValueError("Measured upstream model version must be nonempty when supplied")
        if self.profile is not None and not isinstance(self.profile, ModelProfile):
            raise ValueError("Measured model profile must use the frozen profile contract")
        if self.billing_evidence is not None and not isinstance(
            self.billing_evidence, ProviderBillingEvidence
        ):
            raise ValueError("Provider billing evidence must use its validated contract")
        if self.cost_source is None:
            object.__setattr__(
                self, "cost_source", "provider_billed" if self.cost_usd is not None else "unknown"
            )
        if self.cost_source not in {"provider_billed", "frozen_profile", "unknown"}:
            raise ValueError(
                "Model cost source must be provider billed, frozen profile, or unknown"
            )
        non_usd_bill = (
            self.billing_evidence is not None
            and self.billing_evidence.currency != "USD"
        )
        if non_usd_bill and self.cost_source == "unknown":
            object.__setattr__(self, "cost_source", "provider_billed")
        if (self.cost_usd is None) != (self.cost_source == "unknown") and not non_usd_bill:
            raise ValueError("Unknown cost source and missing cost must be recorded together")
        if self.billing_evidence is not None and self.billing_evidence.currency == "USD":
            if self.cost_usd is None:
                object.__setattr__(self, "cost_usd", self.billing_evidence.gross_amount)
                object.__setattr__(self, "cost_source", "provider_billed")
            elif (
                self.cost_source != "provider_billed"
                or self.cost_usd != self.billing_evidence.gross_amount
            ):
                raise ValueError("USD model cost must equal the gross provider fee evidence")
        elif non_usd_bill and (
            self.cost_usd is not None or self.cost_source != "provider_billed"
        ):
            raise ValueError("Non-USD provider billing must remain in its original currency")
        for name, value in (
            ("provider", self.provider),
            ("provider request ID", self.provider_request_id),
            ("generation ID", self.generation_id),
        ):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError("Measured " + name + " must be nonempty text when supplied")
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("Measured HTTP status must be a valid status code")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("Auxiliary token splits must be reported together")
        if self.input_tokens is not None and (
            type(self.input_tokens) is not int
            or self.input_tokens < 0
            or type(self.output_tokens) is not int
            or self.output_tokens < 0
            or self.tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("Measured token split must match the total token count")
        if self.cached_input_tokens is not None and (
            self.input_tokens is None
            or type(self.cached_input_tokens) is not int
            or not 0 <= self.cached_input_tokens <= self.input_tokens
        ):
            raise ValueError("Cached input tokens must be bounded by measured input tokens")


class ModelCallError(Exception):
    """An unsuccessful dispatch with known usage; omit reply when still unknown."""

    def __init__(self, message, *, reply=None):
        super().__init__(message)
        if reply is not None and not isinstance(reply, ModelReply):
            raise TypeError("Failed-call usage must be a ModelReply")
        self.reply = reply


@dataclass(frozen=True)
class ModelAttempt:
    role: str
    model: str | None
    tokens: int | None
    cost_usd: Decimal | None
    seconds: float
    error: str | None = None


@dataclass
class ObservedUsage:
    primary_requests: int = 0
    jev_requests: int = 0
    primary_tokens: int = 0
    jev_tokens: int = 0
    primary_cost_usd: Decimal | None = Decimal("0")
    jev_cost_usd: Decimal | None = Decimal("0")
    user_simulator_requests: int = 0
    judge_requests: int = 0
    user_simulator_tokens: int = 0
    judge_tokens: int = 0
    user_simulator_cost_usd: Decimal | None = Decimal("0")
    judge_cost_usd: Decimal | None = Decimal("0")
    complete: bool = True
    cost_complete: bool = True
    actual_models: list = field(default_factory=list)
    actual_profiles: list = field(default_factory=list)
    attempts: list = field(default_factory=list)

    def record_scoring_usage(self, observed):
        """Merge evaluator-owned judge accounting without inventing unknown totals."""
        if (
            type(observed) is not dict
            or set(observed) != {"requests", "tokens", "cost_usd", "seconds", "complete"}
            or type(observed["requests"]) is not int or observed["requests"] < 0
            or type(observed["complete"]) is not bool
            or type(observed["seconds"]) not in {int, float}
            or not isfinite(observed["seconds"]) or observed["seconds"] < 0
        ):
            raise ValueError("Invalid measured scorer usage")
        if observed["complete"]:
            if type(observed["tokens"]) is not int or observed["tokens"] < 0:
                raise ValueError("Invalid measured scorer tokens")
            if observed["cost_usd"] is not None:
                _money(observed["cost_usd"])
        elif observed["tokens"] is not None or observed["cost_usd"] is not None:
            raise ValueError("Unknown scorer usage must not contain invented totals")
        self.judge_requests += observed["requests"]
        if observed["complete"]:
            self.judge_tokens += observed["tokens"]
            if observed["cost_usd"] is None:
                self.judge_cost_usd = None
                self.cost_complete = False
            elif self.judge_cost_usd is not None:
                self.judge_cost_usd += observed["cost_usd"]
        else:
            self.complete = False
            self.cost_complete = False
            self.judge_cost_usd = None
        if observed["requests"]:
            self.attempts.append(ModelAttempt(
                "judge", None, observed["tokens"], observed["cost_usd"],
                observed["seconds"], None if observed["complete"] else "usage_unknown",
            ))

    def call(self, role, callback, request, *, timeout_seconds=None):
        return self.call_reply(
            role, callback, request, timeout_seconds=timeout_seconds
        ).value

    def call_reply(self, role, callback, request, *, timeout_seconds=None):
        if callback is None:
            raise ValueError("Missing model adapter for " + role)
        if role not in {"primary", "jev", "user_simulator", "judge"}:
            raise ValueError("Unknown measured model role")
        if timeout_seconds is not None:
            if (
                type(timeout_seconds) not in {int, float}
                or not isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise ValueError("A positive finite model callback deadline is required")
            try:
                parameters = inspect.signature(callback).parameters.values()
            except (TypeError, ValueError) as exc:
                raise TypeError("Deadline-bound model callback signature is unavailable") from exc
            if not any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                or parameter.name == "timeout_seconds"
                and parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
                for parameter in parameters
            ):
                raise TypeError("Deadline-bound model callback must accept timeout_seconds")
        setattr(self, role + "_requests", getattr(self, role + "_requests") + 1)
        started = perf_counter()
        try:
            if timeout_seconds is None:
                reply = callback(deepcopy(request))
            else:
                reply = callback(deepcopy(request), timeout_seconds=timeout_seconds)
            if not isinstance(reply, ModelReply):
                raise TypeError("Model adapter must return measured ModelReply")
        except Exception as exc:
            reply = exc.reply if isinstance(exc, ModelCallError) else None
            if reply is None:
                self.complete = False
                self.attempts.append(
                    ModelAttempt(
                        role, None, None, None, perf_counter() - started, type(exc).__name__
                    )
                )
            else:
                self._add(role, reply, perf_counter() - started, type(exc).__name__)
            raise
        self._add(role, reply, perf_counter() - started)
        return reply

    def _add(self, role, reply, seconds, error=None):
        if reply.profile is not None:
            if reply.profile.role != role:
                raise ValueError("Model response profile role does not match its callback role")
            self.actual_profiles.append((role, reply.profile.fingerprint))
        tokens_name = role + "_tokens"
        setattr(self, tokens_name, getattr(self, tokens_name) + reply.tokens)
        cost_name = role + "_cost_usd"
        if reply.cost_usd is None:
            setattr(self, cost_name, None)
            self.cost_complete = False
        elif getattr(self, cost_name) is not None:
            setattr(self, cost_name, getattr(self, cost_name) + reply.cost_usd)
        if reply.model is not None:
            self.actual_models.append((role, reply.model))
        self.attempts.append(
            ModelAttempt(role, reply.model, reply.tokens, reply.cost_usd, seconds, error)
        )


def _tau_model_callback(
    callback,
    expected_name,
    *,
    usage=None,
    role=None,
    expected_model=None,
    require_text=True,
):
    """Adapt a measured application model call to the isolated τ2 callback wire."""
    if callback is None:
        raise ValueError("Missing evaluator model adapter for " + expected_name)

    def invoke(event, *, timeout_seconds=None):
        if not isinstance(event, dict) or event.get("name") != expected_name:
            raise ValueError("Unexpected τ2 evaluator callback")
        if usage is None:
            if timeout_seconds is None:
                reply = callback(deepcopy(event["payload"]))
            else:
                reply = callback(
                    deepcopy(event["payload"]), timeout_seconds=timeout_seconds
                )
        else:
            reply = usage.call_reply(
                role,
                callback,
                event["payload"],
                timeout_seconds=timeout_seconds,
            )
        if not isinstance(reply, ModelReply) or not isinstance(reply.value, dict):
            raise TypeError("τ2 model adapter must return a measured structured ModelReply")
        if expected_model is not None and reply.model is not None and reply.model != expected_model:
            raise ValueError("τ2 evaluator callback returned an unexpected model version")
        content = reply.value.get("content")
        if require_text and type(content) is not str:
            raise ValueError("τ2 model adapter must return textual content")
        if content is not None and type(content) is not str:
            raise ValueError("τ2 model adapter content must be text or null")
        tool_calls = reply.value.get("tool_calls", [])
        if type(tool_calls) is not list or len(tool_calls) > 8:
            raise ValueError("τ2 model adapter tool calls must be an array")
        for call in tool_calls:
            if (
                type(call) is not dict
                or type(call.get("id")) is not str
                or not call["id"].strip()
                or len(call["id"]) > 128
                or type(call.get("name")) is not str
                or not call["name"].strip()
                or len(call["name"]) > 128
                or type(call.get("arguments")) is not dict
            ):
                raise ValueError("τ2 model adapter returned an invalid tool call")
        if content is not None and len(content) > 16_384:
            raise ValueError("τ2 model adapter content exceeds its bounded size")
        result = {
            "content": content,
            "tool_calls": deepcopy(tool_calls),
            "cost_usd": float(reply.cost_usd) if reply.cost_usd is not None else None,
        }
        if reply.input_tokens is not None:
            result["tau_usage"] = {
                "prompt_tokens": reply.input_tokens,
                "completion_tokens": reply.output_tokens,
            }
        return result

    return invoke


@dataclass(frozen=True)
class TaskScore:
    success: bool
    forbidden_side_effects: int | None
    benchmark_score: float | None = None
    official: bool = False
    user_simulator_cost_usd: Decimal | None = Decimal("0")
    user_simulator_requests: int = 0
    user_simulator_tokens: int | None = 0
    human_wait_seconds: float = 0.0
    simulator_seconds: float = 0.0
    judge_cost_usd: Decimal | None = Decimal("0")
    judge_requests: int = 0
    judge_tokens: int | None = 0
    judge_seconds: float = 0.0

    def __post_init__(self):
        if type(self.success) is not bool:
            raise ValueError("Verifier success must be boolean")
        if type(self.official) is not bool:
            raise ValueError("Official score marker must be boolean")
        if self.benchmark_score is not None and (
            isinstance(self.benchmark_score, bool)
            or type(self.benchmark_score) not in {int, float}
            or not isfinite(self.benchmark_score)
            or not 0 <= self.benchmark_score <= 1
        ):
            raise ValueError("Benchmark score must be null or a finite value in [0, 1]")
        if self.official and self.benchmark_score is None:
            raise ValueError("An official benchmark score must include the original score value")
        if self.forbidden_side_effects is not None and (
            type(self.forbidden_side_effects) is not int or self.forbidden_side_effects < 0
        ):
            raise ValueError("Forbidden side effects must be null or a measured nonnegative count")
        if self.user_simulator_cost_usd is not None:
            _money(self.user_simulator_cost_usd)
        if self.judge_cost_usd is not None:
            _money(self.judge_cost_usd)
        for value in (self.user_simulator_requests, self.judge_requests):
            if type(value) is not int or value < 0:
                raise ValueError("Auxiliary model requests must be measured nonnegative counts")
        for value in (self.user_simulator_tokens, self.judge_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Auxiliary model tokens must be null or a nonnegative count")
        for value in (
            self.human_wait_seconds,
            self.simulator_seconds,
            self.judge_seconds,
        ):
            if type(value) not in {float, int} or not isfinite(value) or value < 0:
                raise ValueError("Observed waiting/simulator time must be finite and nonnegative")


@dataclass(frozen=True)
class ToolCall:
    command: str
    inputs: dict
    seconds: float
    error: str | None = None


@dataclass(frozen=True)
class ExecutionTiming:
    """Harness lifecycle and agent interval measured separately for fair latency."""

    agent_started: bool
    environment_setup_seconds: float
    agent_wall_seconds: float
    private_scoring_seconds: float
    cleanup_seconds: float
    total_wall_seconds: float


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    run: RouteRun | None
    usage: ObservedUsage
    isolation_id: str
    tool_calls: tuple
    decision_inputs: tuple
    terminal: dict
    error: str | None
    reused_workflow: bool
    preparation_seconds: float
    score: TaskScore | None
    timing: ExecutionTiming
    failure_kind: str | None
    cleanup_verified: bool = False
    cleanup_evidence: dict | None = None
    tau_steps: int = 0
    failure_stage: str | None = None
    exception_type: str | None = None


class _ObservedGateway:
    def __init__(self, session, trial, calls):
        self.session, self.trial, self.calls = session, trial, calls

    def call(self, command, params, *, timeout_seconds=None):
        self.session.boundary().check(self.trial, initial=False)
        spec = self.session.spec
        _validate_tool(spec, command, params)
        _record_tau_step(self.session, "environment")
        started, error = perf_counter(), None
        try:
            if timeout_seconds is None:
                data = self.session.gateway.call(command, deepcopy(params))
            else:
                data = self.session.gateway.call(
                    command,
                    deepcopy(params),
                    timeout_seconds=timeout_seconds,
                )
            validate_schema(spec.commands[command].result_schema, data)
            return deepcopy(data)
        except Exception as exc:
            error = type(exc).__name__
            raise NodeFailure("Tool dispatch failed: " + error) from exc
        finally:
            self.calls.append(ToolCall(command, deepcopy(params), perf_counter() - started, error))


class ControlledRoutes:
    """Fresh isolated ReAct or one-plan execution; callbacks carry no scorer data."""

    def __init__(
        self,
        environment_factory,
        *,
        max_plan_control_steps=32,
        max_react_turns=200,
        max_tau_steps=200,
    ):
        if type(max_plan_control_steps) is not int or not 1 <= max_plan_control_steps <= 32:
            raise ValueError("Plan control-step bound must be between 1 and 32 compiled nodes")
        if type(max_react_turns) is not int or not 1 <= max_react_turns <= 200:
            raise ValueError("ReAct turn bound must be between 1 and 200 model requests")
        if type(max_tau_steps) is not int or not 1 <= max_tau_steps <= 200:
            raise ValueError("Official τ conversation-step bound must be between 1 and 200")
        self.environment_factory = environment_factory
        self.max_plan_control_steps = max_plan_control_steps
        self.max_react_turns = max_react_turns
        self.max_tau_steps = max_tau_steps

    def run(
        self,
        route,
        trial,
        *,
        planner=None,
        react=None,
        semantic=None,
        frozen_plan=None,
        user_simulator=None,
        user_model=None,
        official_judge=None,
        judge_model=None,
        summarizer=None,
        response_policy=None,
        model_adapter_factory=None,
        gateway_factory=None,
        on_agent_start=None,
        on_tau_step=None,
        deadline_provider=None,
    ):
        route = Route(route)
        if not isinstance(trial, ComparisonTrial):
            raise TypeError("A frozen comparison trial is required")
        started, isolation_id = perf_counter(), str(uuid4())
        usage, calls, decisions = ObservedUsage(), [], []
        status, error, terminal = "failed", None, {}
        failure_kind = None
        failure_stage, exception_type = None, None
        plan, score, preparation, reused = None, None, 0.0, False
        managed_adapters = None
        tau_step_count = 0
        scoring_complete = False
        skip_scoring = False
        session_entered = False
        session_context_exited = False
        cleanup_verified = False
        cleanup_evidence = None
        agent_started, setup_seconds = None, None
        agent_seconds, scoring_seconds = 0.0, 0.0
        try:
            if model_adapter_factory is not None and not callable(model_adapter_factory):
                raise TypeError("Model adapter factory must be callable")
            if gateway_factory is not None and not callable(gateway_factory):
                raise TypeError("Tool gateway factory must be callable")
            if on_agent_start is not None and not callable(on_agent_start):
                raise TypeError("Agent-start callback must be callable")
            if on_tau_step is not None and not callable(on_tau_step):
                raise TypeError("τ-step callback must be callable")
            if deadline_provider is not None and not callable(deadline_provider):
                raise TypeError("Route deadline provider must be callable")

            def current_timeout():
                return deadline_provider() if deadline_provider is not None else None

            response_policy = (
                DEFAULT_RESPONSE_POLICY if response_policy is None else response_policy
            )
            if (
                type(response_policy) is not FinalResponsePolicy
                or response_policy.fingerprint != trial.response_policy_digest
            ):
                raise ValueError("Trial boundary changed: response_policy_digest")
            if (
                route in {Route.PLAN_JEV_SEMANTIC, Route.PLAN_PRIMARY_SEMANTIC}
                and trial.branch_kind != "semantic"
            ):
                raise ValueError("Structured branches must use code, not a semantic model route")
            if route == Route.CODE_RULE_MCP and trial.branch_kind != "structured":
                raise ValueError("Code-rule route requires a structured branch")
            with self.environment_factory(trial, isolation_id) as session:
                session_entered = True
                try:
                    if session.isolation_id != isolation_id or session.reset() is not True:
                        raise ValueError("Isolated environment reset failed")
                    if hasattr(session, "begin_route"):
                        session.begin_route(route)
                    supports_tau = getattr(session, "supports_user_simulation", False)
                    if supports_tau:
                        bind_tau_steps = getattr(session, "bind_tau_step_recorder", None)
                        if not callable(bind_tau_steps):
                            raise ValueError("τ sessions require a bound official step recorder")

                        def record_tau_step(kind):
                            nonlocal tau_step_count
                            if tau_step_count >= self.max_tau_steps:
                                return False
                            if on_tau_step is not None:
                                on_tau_step(kind)
                            tau_step_count += 1
                            return tau_step_count

                        bind_tau_steps(record_tau_step)
                    if deadline_provider is not None:
                        bind_deadline = getattr(session, "bind_deadline_provider", None)
                        if not callable(bind_deadline):
                            raise ValueError(
                                "A deadline-bound public session is required for live execution"
                            )
                        bind_deadline(deadline_provider)
                    session.boundary().check(trial)
                    task_input = deepcopy(session.task_input)
                    input_schema = deepcopy(
                        getattr(session, "input_schema", None) or default_input_schema()
                    )
                    validate_schema(input_schema, task_input)
                    memories = (
                        deepcopy(session.verified_memories())
                        if trial.memory_mode == "verified"
                        else {}
                    )
                    _validate_memories(memories)
                    if digest(memories) != trial.memory_fixture_digest:
                        raise ValueError("Trial boundary changed: memory_fixture_digest")
                    # Setup itself must not alter the frozen database, actor or catalog.
                    session.boundary().check(trial)
                    if model_adapter_factory is not None:
                        adapters = model_adapter_factory(session, trial, route)
                        managed_adapters = adapters
                        allowed_adapters = {
                            "planner",
                            "react",
                            "semantic",
                            "user_simulator",
                            "user_model",
                            "official_judge",
                            "judge_model",
                            "summarizer",
                        }
                        if not isinstance(adapters, dict) or not set(adapters) <= allowed_adapters:
                            raise ValueError("Model adapter factory returned an invalid role map")
                        planner = adapters.get("planner", planner)
                        react = adapters.get("react", react)
                        semantic = adapters.get("semantic", semantic)
                        user_simulator = adapters.get("user_simulator", user_simulator)
                        user_model = adapters.get("user_model", user_model)
                        official_judge = adapters.get("official_judge", official_judge)
                        judge_model = adapters.get("judge_model", judge_model)
                        summarizer = adapters.get("summarizer", summarizer)
                    if getattr(session, "supports_user_simulation", False):
                        if type(user_model) is not str or type(judge_model) is not str:
                            raise ValueError(
                                "τ routes require explicit user and judge model identifiers"
                            )
                        user_call = _tau_model_callback(
                            user_simulator,
                            "tau_user_simulator",
                            usage=usage,
                            role="user_simulator",
                            expected_model=user_model,
                            require_text=False,
                        )
                        judge_call = _tau_model_callback(
                            official_judge,
                            "tau_official_nl_judge",
                            usage=usage,
                            role="judge",
                            expected_model=judge_model,
                        )
                        session.configure_conversation(
                            user_model=user_model,
                            user_model_call=user_call,
                            judge_model=judge_model,
                            judge_model_call=judge_call,
                        )
                        initial_user_message = session.start_conversation()
                        task_input = deepcopy(session.task_input)
                        validate_schema(input_schema, task_input)
                    elif getattr(session, "requires_official_judge", False):
                        if user_simulator is not None or user_model is not None:
                            raise ValueError(
                                "Enterprise response checks do not use a simulated user"
                            )
                        if (
                            type(judge_model) is not str
                            or not judge_model.strip()
                            or not callable(official_judge)
                        ):
                            raise ValueError(
                                "Enterprise response checks require the ledgered official judge"
                            )
                        configure_judge = getattr(session, "configure_official_judge", None)
                        if not callable(configure_judge):
                            raise ValueError("Enterprise session cannot bind its official judge")
                        configure_judge(model=judge_model, callback=official_judge)
                        initial_user_message = None
                    else:
                        if any(
                            value is not None
                            for value in (user_simulator, user_model, official_judge, judge_model)
                        ):
                            raise ValueError("Auxiliary τ models require an official τ session")
                        initial_user_message = None
                    gateway = _ObservedGateway(session, trial, calls)
                    if gateway_factory is not None:
                        gateway = gateway_factory(gateway, session, trial, route)
                        if not callable(getattr(gateway, "call", None)):
                            raise ValueError(
                                "Tool gateway factory must return a call-capable gateway"
                            )
                    registry = QuestionRegistry.from_spec(session.spec)
                    public = {
                        "task_input": task_input,
                        "skill": session.skill,
                        "registry_fingerprint": registry.fingerprint,
                        "questions": registry.describe(),
                        "tools": {
                            name: {
                                "description": command.description,
                                "input_schema": command.input_schema,
                                "effect": command.effect,
                                "confirmation": command.confirmation,
                            }
                            for name, command in session.spec.commands.items()
                        },
                    }
                    if type(session.spec) is EvaluationCatalog:
                        public["upstream_tools"] = session.spec.raw_tools
                    if initial_user_message is not None:
                        public["initial_user_message"] = deepcopy(initial_user_message)
                        public["conversation"] = session.conversation
                        public["task_input"] = deepcopy(task_input)
                    if on_agent_start is not None:
                        on_agent_start()
                    agent_started = perf_counter()
                    setup_seconds = agent_started - started
                    if getattr(session, "user_stopped", False):
                        terminal = {"status": "stopped", "reason": "τ-Bench user stopped"}
                    elif route == Route.REACT_SKILL_MCP:
                        terminal = self._react(
                            session,
                            trial,
                            public,
                            gateway,
                            usage,
                            react,
                            response_policy,
                            timeout_provider=(
                                current_timeout if deadline_provider is not None else None
                            ),
                        )
                    else:
                        preparing = perf_counter()
                        if frozen_plan is not None:
                            plan = frozen_plan
                        elif trial.workflow_mode == "hot":
                            plan = session.load_workflow()
                        else:
                            plan = usage.call(
                                "primary", planner, public,
                                timeout_seconds=current_timeout(),
                            )
                        plan = plan if isinstance(plan, Plan) else Plan.from_mapping(plan)
                        if trial.workflow_mode == "hot":
                            if session.validate_workflow(plan) is not True:
                                raise ValueError("Published workflow validation failed")
                            reused = True
                        if not reused and _uses_memory(plan.to_mapping()):
                            raise ValueError("Memory slots require a validated published workflow")
                        compiled = compile_plan(plan, session.spec, input_schema=input_schema)
                        if any(compiled.node(name)["type"] == "decide" for name in compiled.order):
                            if route not in {Route.PLAN_JEV_SEMANTIC, Route.PLAN_PRIMARY_SEMANTIC}:
                                raise ValueError(
                                    "Semantic plan nodes require an explicit semantic route"
                                )
                        preparation = perf_counter() - preparing
                        session.boundary().check(trial)
                        repository = session.publish_plan(plan, task_input)
                        if (
                            str(repository.task_id)
                            != str(getattr(session, "task_id", isolation_id))
                            or repository.nodes()
                            or repository.current().digest != plan.digest
                        ):
                            raise ValueError(
                                "Plan execution requires a fresh isolated published repository"
                            )
                        session.boundary().check(trial)
                        runtime = PlanRuntime(
                            repository,
                            self._handler(
                                session,
                                trial,
                                gateway,
                                usage,
                                route,
                                semantic,
                                decisions,
                                repository,
                                current_timeout if deadline_provider is not None else None,
                            ),
                            task_input=task_input,
                            memories=memories if reused else {},
                            before_node=lambda *args: session.boundary().check(
                                trial, initial=False
                            ),
                            completion_verifier=(
                                getattr(session, "evaluation_execution_finished", None)
                                if type(session.spec) is EvaluationCatalog
                                else None
                            ),
                        )
                        terminal = {
                            "status": "stopped",
                            "reason": "Plan control-step bound exhausted",
                        }
                        for _ in range(self.max_plan_control_steps):
                            current = runtime.step()
                            if current["status"] != "advanced":
                                terminal = current
                                break
                        if terminal.get("status") == "complete" and callable(
                            getattr(session, "prepare_final_response", None)
                        ):
                            final = session.prepare_final_response(response_policy)
                            if final["needs_summary"]:
                                if summarizer is None:
                                    raise ValueError(
                                        "Final response requires a measured Qwen summary"
                                    )
                                reply = usage.call_reply(
                                    "primary",
                                    summarizer,
                                    final["request"],
                                    timeout_seconds=current_timeout(),
                                )
                                if (
                                    reply.model is not None
                                    and reply.model != response_policy.summary_model
                                ):
                                    raise ValueError("Unexpected final-response summary model")
                                final = accept_summary(response_policy, final, reply.value)
                            session.set_final_response(final["content"])
                            terminal["final_response"] = final
                            if initial_user_message is not None:
                                if route != Route.REACT_SKILL_MCP:
                                    _record_tau_step(session, "agent")
                                session.finish_conversation(final["content"])
                        elif getattr(session, "user_stopped", False):
                            terminal = {"status": "stopped", "reason": "τ-Bench user stopped"}
                    status = terminal["status"]
                except PublicModelOutcomeUnknown as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                    status = "unknown"
                    terminal = {"status": "unknown", "unknown": True}
                    failure_kind = "model_usage_unknown"
                    failure_stage = "model_callback"
                    exception_type = safe_exception_type(exc)
                except PublicModelProfileDrift as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                    status = "failed"
                    terminal = {"status": "failed", "reason": "model_profile_drift"}
                    failure_kind = "model_profile_drift"
                    failure_stage = "model_response_validation"
                    exception_type = safe_exception_type(exc)
                    skip_scoring = True
                except TauStepLimitReached as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                    status = "stopped"
                    terminal = {
                        "status": "stopped",
                        "reason": "τ official conversation-step limit exhausted",
                    }
                    failure_kind = "tau_step_limit"
                    failure_stage = "agent_execution"
                    exception_type = safe_exception_type(exc)
                except Exception as exc:
                    error = type(exc).__name__ + ": " + str(exc)
                    status = "failed"
                    failure_kind = (
                        "environment_failure" if agent_started is None else "agent_failure"
                    )
                    failure_stage = (
                        "environment_setup" if agent_started is None else "agent_execution"
                    )
                    exception_type = safe_exception_type(exc)
                finally:
                    execution_finished = perf_counter()
                    if agent_started is not None:
                        agent_seconds = execution_finished - agent_started
                    else:
                        setup_seconds = execution_finished - started
                    if (
                        agent_started is not None
                        and usage.complete
                        and terminal.get("unknown") is not True
                        and not skip_scoring
                    ):
                        scoring_started = perf_counter()
                        try:
                            observed_score = session.score()
                            if not isinstance(observed_score, TaskScore):
                                raise TypeError("Private verifier must return TaskScore")
                            score, scoring_complete = observed_score, True
                        except Exception as exc:
                            error = (
                                (error + "; " if error else "")
                                + "Scoring failed: "
                                + type(exc).__name__
                            )
                            status, scoring_complete = "failed", False
                            failure_kind = "scoring_failure"
                            failure_stage = "official_scoring"
                            exception_type = safe_exception_type(exc)
                        finally:
                            scoring_seconds = perf_counter() - scoring_started
                            scoring_usage = getattr(session, "scoring_model_usage", None)
                            if callable(scoring_usage):
                                try:
                                    measured = scoring_usage()
                                    if measured is not None:
                                        usage.record_scoring_usage(measured)
                                except Exception as exc:
                                    usage.complete = False
                                    error = (
                                        (error + "; " if error else "")
                                        + "Scoring usage failed: " + type(exc).__name__
                                    )
                                    status, scoring_complete, score = "failed", False, None
                                    failure_kind = "scoring_failure"
                                    failure_stage = "official_scoring"
                                    exception_type = safe_exception_type(exc)
                    close_adapters = getattr(managed_adapters, "close", None)
                    if callable(close_adapters):
                        try:
                            close_adapters()
                        except Exception as exc:
                            error = (
                                (error + "; " if error else "")
                                + "Model adapter cleanup failed: "
                                + type(exc).__name__
                            )
                            status = "failed"
                            failure_kind = "cleanup_failure"
                            failure_stage = "model_resource_cleanup"
                            exception_type = safe_exception_type(exc)
            session_context_exited = True
            cleanup_report = getattr(session, "cleanup_evidence", None)
            if callable(cleanup_report):
                try:
                    report = cleanup_report()
                    if (
                        type(report) is dict
                        and type(report.get("verified")) is bool
                        and set(report) == {
                            "verified", "session_closed", "environment_closed"
                        }
                        and type(report["session_closed"]) is bool
                        and type(report["environment_closed"]) is bool
                    ):
                        cleanup_evidence = deepcopy(report)
                        cleanup_verified = report["verified"]
                    else:
                        raise ValueError("Trusted cleanup report is invalid")
                except Exception as exc:
                    status = "failed"
                    failure_kind = "cleanup_failure"
                    failure_stage = "environment_cleanup"
                    exception_type = safe_exception_type(exc)
                    error = (error + "; " if error else "") + (
                        "Environment cleanup verification failed: " + type(exc).__name__
                    )
            if cleanup_evidence is not None and not cleanup_verified and status == "complete":
                status = "failed"
                failure_kind = "cleanup_failure"
                failure_stage = "environment_cleanup"
                error = "Environment cleanup was not verified"
        except Exception as exc:
            error, status = type(exc).__name__ + ": " + str(exc), "failed"
            failure_kind = (
                "cleanup_failure"
                if session_entered and not session_context_exited
                else "environment_failure"
            )
            failure_stage = (
                "environment_cleanup"
                if session_entered and not session_context_exited
                else "environment_setup"
            )
            exception_type = safe_exception_type(exc)
        total_seconds = perf_counter() - started
        if setup_seconds is None:
            setup_seconds = total_seconds
        timing = ExecutionTiming(
            agent_started=agent_started is not None,
            environment_setup_seconds=max(0.0, setup_seconds),
            agent_wall_seconds=max(0.0, agent_seconds),
            private_scoring_seconds=max(0.0, scoring_seconds),
            cleanup_seconds=max(
                0.0, total_seconds - setup_seconds - agent_seconds - scoring_seconds
            ),
            total_wall_seconds=max(0.0, total_seconds),
        )
        successful = (
            status == "complete"
            and
            scoring_complete
            and score.success
            and (score.official or status == "complete")
            and score.forbidden_side_effects in {None, 0}
        )
        if "completion" in terminal:
            successful = successful and terminal["completion"].get("valid") is True
        if scoring_complete and not successful and failure_kind is None:
            failure_kind = "task_failure"
        decision_digest = (
            digest(decisions[0])
            if len(decisions) == 1
            else digest(decisions)
            if decisions
            else None
        )
        run = None
        if usage.complete and scoring_complete:
            run = RouteRun(
                route=route,
                trial=trial,
                task_success=successful,
                forbidden_side_effects=score.forbidden_side_effects,
                primary_requests=usage.primary_requests,
                jev_requests=usage.jev_requests,
                primary_tokens=usage.primary_tokens,
                jev_tokens=usage.jev_tokens,
                primary_cost_usd=usage.primary_cost_usd,
                jev_cost_usd=usage.jev_cost_usd,
                machine_seconds=max(
                    0.0,
                    timing.agent_wall_seconds - score.human_wait_seconds - score.simulator_seconds,
                ),
                human_wait_seconds=score.human_wait_seconds,
                user_simulator_cost_usd=score.user_simulator_cost_usd,
                user_simulator_requests=score.user_simulator_requests,
                user_simulator_tokens=score.user_simulator_tokens,
                judge_cost_usd=score.judge_cost_usd,
                judge_requests=score.judge_requests,
                judge_tokens=score.judge_tokens,
                judge_seconds=score.judge_seconds,
                benchmark_score=score.benchmark_score,
                failure_kind=failure_kind,
                plan_digest=plan.digest if isinstance(plan, Plan) else None,
                decision_input_digest=decision_digest,
                evidence_scope="execution_only"
                if frozen_plan is not None and not reused
                else "full_workflow",
                actual_models=tuple(usage.actual_models),
                actual_profiles=tuple(usage.actual_profiles),
                tau_steps=tau_step_count,
            )
        return ExecutionResult(
            status,
            run,
            usage,
            isolation_id,
            tuple(calls),
            tuple(decisions),
            terminal,
            error,
            reused,
            preparation,
            score,
            timing,
            failure_kind,
            cleanup_verified,
            cleanup_evidence,
            tau_step_count,
            failure_stage,
            exception_type,
        )

    def _react(
        self, session, trial, public, gateway, usage, callback, response_policy,
        *, timeout_provider=None,
    ):
        observations = []
        for _ in range(self.max_react_turns):
            session.boundary().check(trial, initial=False)
            request = {**public, "observations": deepcopy(observations)}
            if getattr(session, "supports_user_simulation", False):
                request["conversation"] = session.conversation
            _record_tau_step(session, "agent")
            action = usage.call(
                "primary",
                callback,
                request,
                timeout_seconds=(timeout_provider() if timeout_provider else None),
            )
            if (
                isinstance(action, dict)
                and action.get("finish") is True
                and set(action) <= {"finish", "message"}
            ):
                message = action.get("message")
                if message is not None and (
                    type(message) is not str
                    or len(message.encode("utf-8")) > response_policy.max_output_bytes
                ):
                    raise ValueError("ReAct final response exceeds the frozen byte limit")
                if getattr(session, "supports_user_simulation", False):
                    if type(message) is not str or not message.strip():
                        raise ValueError("τ ReAct completion requires a final assistant message")
                    session.finish_conversation(message)
                elif type(message) is str and callable(
                    getattr(session, "set_final_response", None)
                ):
                    session.set_final_response(message)
                return {"status": "complete"}
            if isinstance(action, dict) and set(action) == {"ask_user"}:
                prompt = action["ask_user"]
                if not callable(getattr(session, "ask_user", None)):
                    raise ValueError("This route has no simulated user to clarify with")
                answer = session.ask_user(prompt)
                observations.append(
                    {"assistant_question": prompt, "user_answer": answer}
                )
                if getattr(session, "user_stopped", False):
                    return {"status": "stopped", "reason": "τ-Bench user stopped"}
                continue
            if not isinstance(action, dict) or set(action) != {"tool", "arguments"}:
                raise ValueError("ReAct must select one registered tool or explicitly finish")
            command, params = action["tool"], action["arguments"]
            _validate_tool(session.spec, command, params)
            if session.spec.commands[command].effect == "write":
                # Existing approval/governance is mandatory; a raw model call is not approval.
                handler = getattr(session, "handle_react_write", None)
                if handler is None:
                    return {
                        "status": "waiting",
                        "request": {
                            "kind": "action",
                            "reason": "Governed ReAct write adapter required",
                        },
                    }
                try:
                    data = handler(command, deepcopy(params), gateway)
                except NodePending as pending:
                    return {
                        "status": "waiting",
                        "request": deepcopy(pending.request),
                        **({"unknown": True} if pending.unknown else {}),
                    }
            else:
                try:
                    data = gateway.call(command, params)
                except PublicToolOutcomeUnknown:
                    return {
                        "status": "waiting",
                        "request": {
                            "kind": "evaluation_tool_outcome_unknown",
                            "command": command,
                            "reason": "The dispatched tool result cannot be confirmed",
                        },
                        "unknown": True,
                    }
            observations.append({"tool": command, "arguments": deepcopy(params), "data": data})
        return {"status": "stopped", "reason": "ReAct turn bound exhausted"}

    def _handler(
        self, session, trial, gateway, usage, route, semantic, decisions, repository,
        timeout_provider=None,
    ):
        registry = QuestionRegistry.from_spec(session.spec)

        def execute(node, inputs, context, saved):
            if node["type"] in {
                "read_tool",
                "governed_write",
                "ask_user",
                "decide",
            }:
                _record_tau_step(session, "agent")
            if node["type"] == "read_tool":
                repository.guard()
                repository.dispatch(
                    node["id"], max_attempts=1, generation=saved["execution_generation"]
                )
                try:
                    data = gateway.call(node["tool"]["command"], inputs)
                except PublicToolOutcomeUnknown as exc:
                    raise NodePending(
                        {
                            "kind": "evaluation_tool_outcome_unknown",
                            "command": node["tool"]["command"],
                            "reason": str(exc),
                        },
                        unknown=True,
                    ) from exc
                if type(session.spec) is EvaluationCatalog and data["is_error"]:
                    raise NodeFailure("Original benchmark tool returned isError")
                return NodeResult(
                    {
                        "kind": "read_tool",
                        "verified": True,
                        "data": data,
                        "inputs": deepcopy(inputs),
                        "employee_id": context.employee_id,
                        "scope_revision": context.scope_revision,
                        "account_revision": context.account_revision,
                    }
                )
            if node["type"] != "decide":
                return session.handle_node(node, inputs, context, saved, gateway)
            session.boundary().check(trial, initial=False)
            definition = registry.validate_reference(node["question"])
            question_inputs = deepcopy(inputs)
            if definition["candidate_source"] == "tool:retrieve_knowledge":
                candidate_binding = node["inputs"].get("articles", {})
                producer = context.node_outputs.get(candidate_binding.get("node_id"))
                if (
                    not isinstance(producer, dict)
                    or producer.get("verified") is not True
                    or producer.get("kind") != "read_tool"
                    or not isinstance(producer.get("inputs"), dict)
                    or not isinstance(producer.get("data"), dict)
                    or producer["data"].get("is_error") is not False
                    or producer["data"].get("value") != inputs.get("articles")
                ):
                    raise NodeFailure("CSM candidates must match one verified read result")
                try:
                    session.spec.validate("retrieve_knowledge", producer["inputs"])
                except (KeyError, ValueError) as exc:
                    raise NodeFailure(
                        "CSM retrieval arguments no longer match the tool contract"
                    ) from exc
                question_inputs["retrieval_arguments"] = deepcopy(producer["inputs"])
            question = registry.render(
                definition["question_id"],
                question_inputs,
                node_id=node["id"],
                version=definition["version"],
            )
            request = {
                "node_id": node["id"],
                "question_id": definition["question_id"],
                "question_version": definition["version"],
                "question": question.to_wire(),
                "state": deepcopy(question_inputs),
                "actor_profile_digest": trial.actor_profile_digest,
                "permission_digest": trial.permission_digest,
            }
            decisions.append(deepcopy(request))
            role = "jev" if route == Route.PLAN_JEV_SEMANTIC else "primary"
            if timeout_provider is None:
                value = usage.call(role, semantic, request)
            else:
                value = usage.call(
                    role,
                    semantic,
                    request,
                    timeout_seconds=timeout_provider(),
                )
            try:
                normalized = normalize_answer(question, value)
                value = {
                    key: item
                    for key, item in json_value(normalized.raw_answer).items()
                    if key != "type"
                }
                value["selected_value"] = normalized.suggested_value
            except (ValueError, KeyError, TypeError) as exc:
                raise NodeFailure("Invalid semantic answer") from exc
            return session.accept_decision(node, question, deepcopy(value), context, saved)

        return execute

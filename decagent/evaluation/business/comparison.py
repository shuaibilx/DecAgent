"""Fair-pair contracts for controlled business-agent route comparisons.

The trial identity contains only public evaluation metadata and digests. Expected
answers and target state belong to the scorer and must never be copied here.
"""

import math
import re
from dataclasses import FrozenInstanceError, asdict, dataclass, fields
from decimal import Decimal
from enum import StrEnum

from decagent.decisions.contracts import digest

from .final_response import DEFAULT_RESPONSE_POLICY

_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
DEFAULT_RESOURCE_PROFILE = {
    "agent_model_requests": 200,
    "tool_calls": 200,
    "agent_tokens": 1_000_000,
    "agent_machine_seconds": 1_800,
    "tau_steps": 200,
    "user_simulator_requests": 200,
    "user_simulator_tokens": 1_000_000,
    "judge_requests": 200,
    "judge_tokens": 1_000_000,
}
DEFAULT_RESOURCE_PROFILE_DIGEST = digest(DEFAULT_RESOURCE_PROFILE)
UNBOUND_MODEL_PROFILE_DIGEST = digest({"model_profile": "unbound"})
EXPOSURE_GROUPS = frozenset(
    {"development_exposed", "not_exposed", "unassigned"}
)


class Route(StrEnum):
    REACT_SKILL_MCP = "react_skill_mcp"
    PLAN_MCP = "plan_mcp"
    PLAN_PRIMARY_SEMANTIC = "plan_primary_semantic_mcp"
    PLAN_JEV_SEMANTIC = "plan_jev_semantic_mcp"
    CODE_RULE_MCP = "code_rule_mcp"


class AblationAxis(StrEnum):
    MEMORY = "memory_mode"
    WORKFLOW_REUSE = "workflow_mode"


class TauStepLimitReached(Exception):
    """The locked τ-Bench conversation-step limit stopped a route before dispatch."""


@dataclass(frozen=True)
class ComparisonTrial:
    """Model-visible fairness identity; deliberately excludes labels and gold state."""

    dataset: str
    dataset_version: str
    dataset_checksum: str
    case_id: str
    branch_kind: str
    initial_state_digest: str
    goal_digest: str
    actor_profile_digest: str
    permission_digest: str
    tool_adapter_digest: str
    memory_mode: str
    workflow_mode: str
    memory_fixture_digest: str = digest({})
    exposure_group: str = "unassigned"
    resource_profile_digest: str = DEFAULT_RESOURCE_PROFILE_DIGEST
    response_policy_digest: str = DEFAULT_RESPONSE_POLICY.fingerprint
    model_profile_digest: str = UNBOUND_MODEL_PROFILE_DIGEST
    jev_profile_digest: str = UNBOUND_MODEL_PROFILE_DIGEST
    user_simulator_profile_digest: str = UNBOUND_MODEL_PROFILE_DIGEST
    judge_profile_digest: str = UNBOUND_MODEL_PROFILE_DIGEST

    def __post_init__(self):
        if any(type(value) is not str or not value.strip() for value in asdict(self).values()):
            raise ValueError("Comparison trial fields must be nonempty strings")
        for name in (
            "dataset_checksum",
            "initial_state_digest",
            "goal_digest",
            "actor_profile_digest",
            "permission_digest",
            "tool_adapter_digest",
            "memory_fixture_digest",
            "resource_profile_digest",
            "response_policy_digest",
            "model_profile_digest",
            "jev_profile_digest",
            "user_simulator_profile_digest",
            "judge_profile_digest",
        ):
            if not _SHA256.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.branch_kind not in {"semantic", "structured"}:
            raise ValueError("branch_kind must be semantic or structured")
        if self.memory_mode not in {"disabled", "verified"}:
            raise ValueError("memory_mode must be disabled or verified")
        if self.memory_mode == "disabled" and self.memory_fixture_digest != digest({}):
            raise ValueError("Disabled memory requires the empty memory_fixture_digest")
        if self.workflow_mode not in {"cold", "hot"}:
            raise ValueError("workflow_mode must be cold or hot")
        if self.exposure_group not in EXPOSURE_GROUPS:
            raise ValueError("exposure_group must be a registered exposure group")

    @property
    def fingerprint(self):
        return digest(asdict(self))


class PublicComparisonTrial(ComparisonTrial):
    """A fairness identity bound to one frozen public-ledger trial ID.

    The manifest ID is execution bookkeeping, not an experimental variable, so
    it is intentionally excluded from the base dataclass fields and fingerprint.
    """

    __slots__ = ("trial_id",)

    def __init__(self, trial, trial_id):
        if not isinstance(trial, ComparisonTrial):
            raise TypeError("A frozen comparison trial is required")
        if type(trial_id) is not str or not re.fullmatch(r"[a-f0-9]{64}", trial_id):
            raise ValueError("A canonical SHA-256 manifest trial ID is required")
        super().__init__(
            **{field.name: getattr(trial, field.name) for field in fields(ComparisonTrial)}
        )
        object.__setattr__(self, "trial_id", trial_id)

    @classmethod
    def from_trial(cls, trial, trial_id):
        return cls(trial, trial_id)

    def __setattr__(self, name, value):
        if name == "trial_id":
            raise FrozenInstanceError("cannot assign to field 'trial_id'")
        super().__setattr__(name, value)


@dataclass(frozen=True)
class RouteRun:
    route: Route
    trial: ComparisonTrial
    task_success: bool
    forbidden_side_effects: int | None
    primary_requests: int
    jev_requests: int
    primary_tokens: int
    jev_tokens: int
    primary_cost_usd: Decimal | None
    jev_cost_usd: Decimal | None
    machine_seconds: float
    human_wait_seconds: float
    user_simulator_cost_usd: Decimal | None
    benchmark_score: float | None = None
    plan_digest: str | None = None
    decision_input_digest: str | None = None
    evidence_scope: str = "full_workflow"
    user_simulator_requests: int = 0
    user_simulator_tokens: int | None = 0
    judge_cost_usd: Decimal | None = Decimal("0")
    judge_requests: int = 0
    judge_tokens: int | None = 0
    judge_seconds: float = 0.0
    failure_kind: str | None = None
    actual_models: tuple = ()
    actual_profiles: tuple = ()
    tau_steps: int = 0

    def __post_init__(self):
        if not isinstance(self.route, Route) or not isinstance(self.trial, ComparisonTrial):
            raise ValueError("A route run requires a registered route and trial identity")
        if type(self.task_success) is not bool:
            raise ValueError("task_success must be boolean")
        if self.benchmark_score is not None and (
            isinstance(self.benchmark_score, bool)
            or type(self.benchmark_score) not in {int, float}
            or not math.isfinite(self.benchmark_score)
            or not 0 <= self.benchmark_score <= 1
        ):
            raise ValueError("benchmark_score must be null or a finite value in [0, 1]")
        if self.evidence_scope not in {"full_workflow", "execution_only"}:
            raise ValueError("Unknown route evidence_scope")
        if self.failure_kind not in {
            None,
            "task_failure",
            "environment_failure",
            "agent_failure",
            "scoring_failure",
            "cleanup_failure",
            "tau_step_limit",
        }:
            raise ValueError("Unknown route failure classification")
        for name in (
            "primary_requests",
            "jev_requests",
            "primary_tokens",
            "jev_tokens",
            "tau_steps",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if type(self.actual_models) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or item[0] not in {"primary", "jev", "user_simulator", "judge"}
            or type(item[1]) is not str
            or not item[1].strip()
            for item in self.actual_models
        ):
            raise ValueError("actual_models must contain measured role/model pairs")
        if type(self.actual_profiles) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or item[0] not in {"primary", "jev", "user_simulator", "judge"}
            or type(item[1]) is not str
            or not _SHA256.fullmatch(item[1])
            for item in self.actual_profiles
        ):
            raise ValueError("actual_profiles must contain measured role/profile digests")
        if self.forbidden_side_effects is not None and (
            type(self.forbidden_side_effects) is not int or self.forbidden_side_effects < 0
        ):
            raise ValueError("forbidden_side_effects must be null or a nonnegative integer")
        for name in ("primary_cost_usd", "jev_cost_usd"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{name} must be null or a finite nonnegative Decimal")
        for name in ("user_simulator_cost_usd", "judge_cost_usd"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{name} must be null or a finite nonnegative Decimal")
        for name in ("user_simulator_requests", "judge_requests"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("user_simulator_tokens", "judge_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be null or a nonnegative integer")
        for name in ("machine_seconds", "human_wait_seconds", "judge_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite nonnegative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        for name in ("plan_digest", "decision_input_digest"):
            value = getattr(self, name)
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    @property
    def agent_cost_usd(self):
        """Agent model cost excludes the separately reported user simulator."""
        if self.primary_cost_usd is None or self.jev_cost_usd is None:
            return None
        return self.primary_cost_usd + self.jev_cost_usd


@dataclass(frozen=True)
class PairedComparison:
    case_id: str
    left_route: Route
    right_route: Route
    trial_fingerprint: str
    axis: AblationAxis | None = None
    plan_digest: str | None = None
    decision_input_digest: str | None = None


def _require_routes(left, right, expected):
    if {left.route, right.route} != set(expected):
        raise ValueError("Comparison routes do not match the declared experiment")


def _require_trial_match(left, right, *, allowed_differences=()):
    if left.evidence_scope != right.evidence_scope:
        raise ValueError("Comparison evidence scope must match")
    allowed = set(allowed_differences)
    mismatches = [
        field.name
        for field in fields(ComparisonTrial)
        if field.name not in allowed
        and getattr(left.trial, field.name) != getattr(right.trial, field.name)
    ]
    if mismatches:
        raise ValueError("Routes are not paired; mismatched trial fields: " + ", ".join(mismatches))


def _require_primary_model_match(left, right):
    left_models = {model for role, model in left.actual_models if role == "primary"}
    right_models = {model for role, model in right.actual_models if role == "primary"}
    if left_models and right_models and left_models != right_models:
        raise ValueError("Routes used different actual primary model versions")


def _require_actual_profile_bindings(run):
    expected = {
        "primary": run.trial.model_profile_digest,
        "jev": run.trial.jev_profile_digest,
        "user_simulator": run.trial.user_simulator_profile_digest,
        "judge": run.trial.judge_profile_digest,
    }
    actual = {}
    for role, profile_digest in run.actual_profiles:
        if role in actual and actual[role] != profile_digest:
            raise ValueError("A route used multiple model profiles for role " + role)
        actual[role] = profile_digest
    requests = {
        "primary": run.primary_requests,
        "jev": run.jev_requests,
        "user_simulator": run.user_simulator_requests,
        "judge": run.judge_requests,
    }
    for role, profile_digest in actual.items():
        if expected[role] == UNBOUND_MODEL_PROFILE_DIGEST:
            raise ValueError("Actual model profile is not frozen for role " + role)
        if profile_digest != expected[role]:
            raise ValueError("Actual model profile disagrees with frozen trial for role " + role)
    for role, count in requests.items():
        if count and expected[role] != UNBOUND_MODEL_PROFILE_DIGEST and role not in actual:
            raise ValueError("Actual model profile is missing for role " + role)


def _require_model_profile_match(left, right):
    _require_actual_profile_bindings(left)
    _require_actual_profile_bindings(right)
    left_profiles = dict(left.actual_profiles)
    right_profiles = dict(right.actual_profiles)
    shared_roles = set(left_profiles) & set(right_profiles)
    if any(left_profiles[role] != right_profiles[role] for role in shared_roles):
        raise ValueError("Routes used different actual model profiles")


def compare_full_workflow(left, right):
    """Pair distinct main routes with complete preparation/execution accounting."""
    main_routes = {
        Route.REACT_SKILL_MCP, Route.PLAN_MCP,
        Route.PLAN_PRIMARY_SEMANTIC, Route.PLAN_JEV_SEMANTIC,
    }
    if left.route == right.route or not {left.route, right.route} <= main_routes:
        raise ValueError("Full workflow comparisons require distinct main routes")
    if any(run.evidence_scope != "full_workflow" for run in (left, right)):
        raise ValueError("Full workflow cost excludes execution-only replay evidence")
    _require_trial_match(left, right)
    _require_primary_model_match(left, right)
    _require_model_profile_match(left, right)
    return PairedComparison(
        left.trial.case_id, left.route, right.route, left.trial.fingerprint
    )


def compare_semantic_judgment(left, right):
    """Pair Jev and primary re-judgments only at the exact same semantic node."""
    _require_routes(
        left, right, {Route.PLAN_JEV_SEMANTIC, Route.PLAN_PRIMARY_SEMANTIC}
    )
    _require_trial_match(left, right)
    _require_primary_model_match(left, right)
    _require_model_profile_match(left, right)
    if left.trial.branch_kind != "semantic":
        raise ValueError("Structured branches must not be counted as semantic Jev trials")
    if not left.plan_digest or left.plan_digest != right.plan_digest:
        raise ValueError("Semantic model comparisons require the same plan_digest")
    if not left.decision_input_digest or left.decision_input_digest != right.decision_input_digest:
        raise ValueError("Semantic model comparisons require the same decision_input_digest")
    return PairedComparison(
        left.trial.case_id,
        left.route,
        right.route,
        left.trial.fingerprint,
        plan_digest=left.plan_digest,
        decision_input_digest=left.decision_input_digest,
    )


def compare_structured_branch(left, right):
    """Measure code rules on structured branches without treating Jev as their rival."""
    _require_routes(left, right, {Route.PLAN_MCP, Route.CODE_RULE_MCP})
    _require_trial_match(left, right)
    _require_model_profile_match(left, right)
    if left.trial.branch_kind != "structured":
        raise ValueError("Code-rule comparisons require a structured branch")
    if left.jev_requests or right.jev_requests:
        raise ValueError("Structured branch comparisons must not call Jev")
    return PairedComparison(
        left.trial.case_id, left.route, right.route, left.trial.fingerprint
    )


def compare_ablation(left, right, *, axis):
    """Compare memory or workflow reuse while holding all other trial fields fixed."""
    try:
        axis = AblationAxis(axis)
    except ValueError as exc:
        raise ValueError("Only memory and workflow-reuse ablations are supported") from exc
    if left.route is not right.route:
        raise ValueError("Ablation runs must use the same agent route")
    field_name = axis.value
    before, after = getattr(left.trial, field_name), getattr(right.trial, field_name)
    if before == after:
        raise ValueError(f"Ablation axis {field_name} must change")
    allowed = {field_name}
    if axis is AblationAxis.MEMORY:
        allowed.add("memory_fixture_digest")
    _require_trial_match(left, right, allowed_differences=allowed)
    _require_model_profile_match(left, right)
    return PairedComparison(
        left.trial.case_id,
        left.route,
        right.route,
        left.trial.fingerprint,
        axis=axis,
    )

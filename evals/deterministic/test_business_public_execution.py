from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import (
    DEFAULT_RESOURCE_PROFILE_DIGEST,
    UNBOUND_MODEL_PROFILE_DIGEST,
    ComparisonTrial,
    PublicComparisonTrial,
)
from decagent.evaluation.business.final_response import DEFAULT_RESPONSE_POLICY
from decagent.evaluation.business.public_execution import (
    _comparison_evidence,
    execute_public_phase,
    execute_public_route,
)
from decagent.evaluation.business.public_resources import (
    LedgeredModelAdapterFactory,
    LedgeredToolGateway,
)
from decagent.evaluation.business.public_supervisor import (
    PublicSupervisor,
    canonical_public_supervisor_lock_path,
)
from decagent.evaluation.business.public_trials import (
    PublicTrialLedger,
    canonical_public_trial_ledger_path,
    load_public_run_plan,
)

ROOT = Path(__file__).resolve().parents[2]
TRIAL_ID = "a" * 64


def bound_trial(
    trial_id,
    *,
    record=None,
    dataset=None,
    dataset_version=None,
    case_id=None,
    branch_kind="structured",
):
    record = record or {}
    dataset = dataset or record.get("benchmark", "tau-bench")
    dataset_version = dataset_version or record.get("version", "fixture-version")
    case_id = case_id or record.get("case_id", "0")
    trial = ComparisonTrial(
        dataset=dataset,
        dataset_version=dataset_version,
        dataset_checksum=digest({"fixture": dataset}),
        case_id=case_id,
        branch_kind=branch_kind,
        initial_state_digest="a" * 64,
        goal_digest="b" * 64,
        actor_profile_digest="c" * 64,
        permission_digest="d" * 64,
        tool_adapter_digest="e" * 64,
        memory_mode="disabled",
        workflow_mode="cold",
        exposure_group=record.get("exposure_group", "unassigned"),
        resource_profile_digest=record.get(
            "resource_profile_digest", DEFAULT_RESOURCE_PROFILE_DIGEST
        ),
        response_policy_digest=record.get(
            "response_policy_digest", DEFAULT_RESPONSE_POLICY.fingerprint
        ),
        model_profile_digest=record.get("model_profile_digest", UNBOUND_MODEL_PROFILE_DIGEST),
        jev_profile_digest=record.get("jev_profile_digest", UNBOUND_MODEL_PROFILE_DIGEST),
        user_simulator_profile_digest=record.get(
            "user_simulator_profile_digest", UNBOUND_MODEL_PROFILE_DIGEST
        ),
        judge_profile_digest=record.get("judge_profile_digest", UNBOUND_MODEL_PROFILE_DIGEST),
    )
    return PublicComparisonTrial.from_trial(trial, trial_id)


class FakeLedger:
    def __init__(self, root):
        self.root = Path(root)
        self.candidate_sha = "a" * 40
        self.status = "pending"
        self.events = []
        self.statuses = {}
        self.stopped_reason = None
        self.path = canonical_public_trial_ledger_path(root)
        self.plan = SimpleNamespace(
            mode="smoke", root=Path(root), resource_limits={"tau_steps": 200}
        )
        self.tau_step_counts = {}
        self.model_attempts = {}

    def bind_plan(self, plan):
        self.plan = plan
        self.path = canonical_public_trial_ledger_path(
            self.root,
            cohort_digest=plan.cohort_digest,
            candidate_sha=self.candidate_sha,
        )

    def model_attempts_snapshot(self, trial_id, route):
        return list(self.model_attempts.get((trial_id, route), []))

    def route_status(self, _trial_id, _route):
        return self.statuses.get((_trial_id, _route), "pending")

    def start_route(self, _trial_id, _route):
        self.events.append("start_route")
        self.status = "running"
        self.statuses[(_trial_id, _route)] = "running"

    def start_route_with_machine_time(self, trial_id, route, *, max_seconds):
        self.events.append(("claim_reserve", trial_id, route, max_seconds))
        self.reserved_seconds = max_seconds
        self.status = "running"
        self.statuses[(trial_id, route)] = "running"
        return "reservation-1"

    def reserve_machine_time(self, *, trial_id, route, max_seconds):
        self.reserved_seconds = max_seconds
        self.events.append(("reserve", trial_id, route, max_seconds))
        return "reservation-1"

    def mark_machine_time_dispatched(self, reservation_id):
        self.events.append(("dispatch", reservation_id))

    def mark_machine_time_agent_started(self, reservation_id):
        self.events.append(("agent_started", reservation_id))

    def route_remaining_seconds(self, _trial_id, _route):
        return 30.0

    def reserve_tau_step(self, *, trial_id, route, kind):
        assert kind in {"agent", "user", "environment"}
        key = (trial_id, route)
        self.tau_step_counts[key] = self.tau_step_counts.get(key, 0) + 1
        return self.tau_step_counts[key]

    def resource_snapshot(self, trial_id, route):
        return {"tau_steps": self.tau_step_counts.get((trial_id, route), 0)}

    def settle_machine_time(
        self, reservation_id, *, actual_seconds, defer_route_failure=False
    ):
        self.events.append(("settle", reservation_id, actual_seconds))
        return actual_seconds <= self.reserved_seconds

    def finish_route(self, trial_id, route, **kwargs):
        self.events.append(("finish", trial_id, route, kwargs))
        self.status = kwargs["status"]
        self.statuses[(trial_id, route)] = kwargs["status"]

    def mark_route_unknown(self, trial_id, route):
        self.statuses[(trial_id, route)] = "unknown"

    def batch_snapshot(self):
        return {
            "stopped_reason": self.stopped_reason,
            "remaining_wall_seconds": getattr(self, "remaining_wall_seconds", 3_600.0),
        }

    def stop_for_batch_deadline(self):
        self.events.append(("stop_batch", "wall_seconds"))
        self.stopped_reason = "wall_seconds"
        return True

    def reserve_tool_call(self, **_kwargs):
        return "tool-attempt"

    def mark_tool_dispatched(self, _attempt_id):
        return None

    def mark_tool_not_sent(self, *_args, **_kwargs):
        return None

    def settle_tool_call(self, *_args, **_kwargs):
        return None

    def mark_tool_unknown(self, *_args, **_kwargs):
        return None


class FakeModelAdapterFactory(LedgeredModelAdapterFactory):
    def __init__(self, ledger, trial_id):
        self.ledger = ledger
        self.trial_id_for = lambda _trial: trial_id

    def __call__(self, *_args):
        return {}


class FakeGateway:
    def call(self, _command, _params, *, timeout_seconds):
        assert timeout_seconds > 0
        raise AssertionError("The route fixture must not dispatch tools")


def route_kwargs_for(ledger, trial_id, route):
    return {
        "model_adapter_factory": FakeModelAdapterFactory(ledger, trial_id),
        "gateway_factory": lambda base, _session, _trial, _route: LedgeredToolGateway(
            base,
            ledger,
            trial_id=trial_id,
            route=route,
            tool_effects={},
        ),
    }


@contextmanager
def held_supervisor(root):
    with PublicSupervisor(canonical_public_supervisor_lock_path(root)) as supervisor:
        yield supervisor


def run_public_route(*, root, ledger, controlled_routes, **kwargs):
    trial = kwargs["trial"]
    if not hasattr(ledger.plan, "trials"):
        ledger.plan.trials = [
            {
                "trial_id": kwargs["trial_id"],
                "benchmark": trial.dataset,
                "version": trial.dataset_version,
                "case_id": trial.case_id,
                "exposure_group": trial.exposure_group,
                "resource_profile_digest": trial.resource_profile_digest,
                "response_policy_digest": trial.response_policy_digest,
                "model_profile_digest": trial.model_profile_digest,
                "jev_profile_digest": trial.jev_profile_digest,
                "user_simulator_profile_digest": trial.user_simulator_profile_digest,
                "judge_profile_digest": trial.judge_profile_digest,
            }
        ]
    with held_supervisor(root) as supervisor:
        return execute_public_route(
            root=root,
            supervisor=supervisor,
            ledger=ledger,
            controlled_routes=controlled_routes,
            **kwargs,
        )


class FakeControlledRoutes:
    def __init__(self, result, ledger=None, *, error=None):
        self.result = result
        self.ledger = ledger
        self.error = error
        self.events = []
        self.max_tau_steps = 200

    def run(self, route, trial, **kwargs):
        self.events.append(("run", route, trial))
        self.events.append(("deadline", kwargs["deadline_provider"]()))
        kwargs["model_adapter_factory"](None, trial, route)
        kwargs["gateway_factory"](FakeGateway(), None, trial, route)
        kwargs["on_agent_start"]()
        tau_steps = 0
        if trial.dataset == "tau-bench":
            kwargs["on_tau_step"]("agent")
            tau_steps = 1
        if self.ledger is not None:
            trial_id = getattr(trial, "trial_id", TRIAL_ID)
            self.ledger.mark_route_unknown(trial_id, getattr(route, "value", route))
        if self.error is not None:
            raise self.error
        result = dict(vars(self.result))
        result["tau_steps"] = tau_steps
        return SimpleNamespace(**result)


def execution_result(
    *, score=True, cleanup=True, status="complete", failure_kind=None,
    machine_seconds=12.5, tau_steps=0, failure_stage=None, exception_type=None,
    error=None, plan_digest=None, decision_input_digest=None,
    evidence_scope="full_workflow",
    diagnostic_code=None,
):
    benchmark_score = 1.0 if score else 0.0
    score_value = SimpleNamespace(
        official=score,
        benchmark_score=benchmark_score if score else None,
        success=score,
        forbidden_side_effects=0 if score else None,
    )
    return SimpleNamespace(
        status=status,
        run=SimpleNamespace(
            task_success=score,
            plan_digest=plan_digest,
            decision_input_digest=decision_input_digest,
            evidence_scope=evidence_scope,
        ) if score else None,
        score=score_value if score else None,
        timing=SimpleNamespace(
            agent_started=True, agent_wall_seconds=machine_seconds
        ),
        failure_kind=failure_kind,
        plan_digest=plan_digest,
        decision_input_digest=decision_input_digest,
        evidence_scope=evidence_scope,
        failure_stage=failure_stage,
        exception_type=exception_type,
        diagnostic_code=diagnostic_code,
        error=error,
        tau_steps=tau_steps,
        cleanup_verified=cleanup,
        cleanup_evidence={
            "verified": cleanup,
            "session_closed": cleanup,
            "environment_closed": cleanup,
        } if cleanup else None,
    )


def test_public_route_dispatch_reserves_times_and_persists_official_result(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="plan_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
    )

    assert receipt.status == "completed"
    assert receipt.score_status == "scored"
    assert receipt.cleanup_verified is True
    assert receipt.tau_steps == 1
    assert ledger.resource_snapshot(TRIAL_ID, "plan_mcp")["tau_steps"] == 1
    assert controlled.events[1] == ("deadline", 30.0)
    assert ledger.events[:3] == [
        ("claim_reserve", TRIAL_ID, "plan_mcp", 1_800),
        ("dispatch", "reservation-1"),
        ("agent_started", "reservation-1"),
    ]
    assert ledger.events[3] == ("settle", "reservation-1", 12.5)
    terminal = ledger.events[4][3]
    assert terminal["status"] == "completed"
    assert terminal["score_evidence"] == {
        "source": "tau2-bench-original-evaluate-simulation",
        "value": 1.0,
        "success": True,
        "forbidden_side_effects": 0,
    }
    assert terminal["cleanup_verified"] is True


def test_public_route_rejects_unbound_candidate_before_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    ledger.candidate_sha = None
    controlled = FakeControlledRoutes(execution_result())

    with pytest.raises(ValueError, match="candidate SHA must be bound"):
        run_public_route(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            trial_id=TRIAL_ID,
            route="plan_mcp",
            trial=bound_trial(TRIAL_ID),
            max_machine_seconds=1_800,
            route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
        )

    assert ledger.events == []
    assert controlled.events == []


def test_public_route_persists_failure_stage_and_type_without_exception_message(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(
        execution_result(
            score=False,
            status="failed",
            failure_kind="agent_failure",
            failure_stage="agent_execution",
            exception_type="ValueError",
            error="PRIVATE_RESPONSE_BODY",
            diagnostic_code="malformed_response",
        )
    )

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="plan_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
    )

    terminal = ledger.events[-1][3]
    assert receipt.status == "failed"
    assert terminal["failure_stage"] == receipt.failure_stage == "agent_execution"
    assert terminal["exception_type"] == receipt.exception_type == "ValueError"
    assert (
        terminal["error_category"]
        == receipt.error_category
        == "agent_failure_malformed_response"
    )
    assert receipt.diagnostic_code == "malformed_response"
    assert "PRIVATE_RESPONSE_BODY" not in repr(terminal)


def test_unknown_route_is_not_rewritten_and_only_cleanup_is_recorded(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result(score=False, cleanup=True), ledger)

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="react_skill_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "react_skill_mcp"),
    )

    terminal = ledger.events[-1][3]
    assert receipt.status == "unknown"
    assert receipt.tau_steps == 1
    assert terminal["status"] == "unknown"
    assert terminal["score_status"] == "not_attempted"
    assert terminal["cleanup_verified"] is True
    assert terminal["cleanup_evidence_digest"]


def test_unknown_official_judge_is_retained_as_unknown_score(tmp_path):
    ledger = FakeLedger(tmp_path)
    route = "react_skill_mcp"
    ledger.model_attempts[(TRIAL_ID, route)] = [
        {"role": "judge", "status": "unknown"}
    ]
    controlled = FakeControlledRoutes(
        execution_result(score=True, cleanup=True), ledger
    )

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route=route,
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, route),
    )

    terminal = ledger.events[-1][3]
    assert receipt.status == "unknown"
    assert receipt.score_status == "unknown"
    assert terminal["status"] == "unknown"
    assert terminal["score_status"] == "unknown"
    assert terminal["cleanup_verified"] is True


def test_route_runner_rejects_caller_supplied_agent_start_hook_before_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())

    with pytest.raises(ValueError, match="runner-owned"):
        run_public_route(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            trial_id=TRIAL_ID,
            route="plan_mcp",
            trial=bound_trial(TRIAL_ID),
            max_machine_seconds=1_800,
            route_kwargs={
                **route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
                "on_agent_start": lambda: None,
            },
        )

    assert ledger.events == []
    assert controlled.events == []


def test_route_exception_keeps_unknown_state_and_does_not_claim_success(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(None, ledger, error=RuntimeError("stop"))

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="plan_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
    )

    terminal = ledger.events[-1][3]
    assert receipt.status == "unknown"
    assert terminal["status"] == "unknown"
    assert terminal["score_status"] == "not_attempted"
    assert terminal["cleanup_verified"] is False


def test_runner_rejects_direct_unmetered_model_callback_before_route_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())
    route_kwargs = route_kwargs_for(ledger, TRIAL_ID, "plan_mcp")
    route_kwargs["planner"] = lambda _request: None

    with pytest.raises(ValueError, match="ledgered adapter factory"):
        run_public_route(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            trial_id=TRIAL_ID,
            route="plan_mcp",
            trial=bound_trial(TRIAL_ID),
            max_machine_seconds=1_800,
            route_kwargs=route_kwargs,
        )

    assert ledger.events == []


def test_runner_rejects_public_trial_bound_to_another_manifest_id_before_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())

    with pytest.raises(ValueError, match="bound to the frozen manifest ID"):
        run_public_route(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            trial_id=TRIAL_ID,
            route="plan_mcp",
            trial=bound_trial("b" * 64),
            max_machine_seconds=1_800,
            route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
        )

    assert ledger.events == []
    assert controlled.events == []


def test_runner_rejects_trial_metadata_mismatch_before_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    ledger.plan.trials = [
        {
            "trial_id": TRIAL_ID,
            "benchmark": "tau-bench",
            "version": "fixture-version",
            "case_id": "0",
        }
    ]
    controlled = FakeControlledRoutes(execution_result())
    with held_supervisor(tmp_path) as supervisor:
        with pytest.raises(ValueError, match="differs from its frozen manifest record"):
            execute_public_route(
                root=tmp_path,
                supervisor=supervisor,
                ledger=ledger,
                controlled_routes=controlled,
                trial_id=TRIAL_ID,
                route="plan_mcp",
                trial=bound_trial(TRIAL_ID, dataset="enterpriseops-gym"),
                max_machine_seconds=1_800,
                route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
            )

    assert ledger.events == []
    assert controlled.events == []


def test_runner_rejects_noncanonical_supervisor_lock_before_route_claim(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())
    with PublicSupervisor(tmp_path / "alternate.lock") as supervisor:
        with pytest.raises(ValueError, match="canonical live-evaluation supervisor"):
            execute_public_route(
                root=tmp_path,
                supervisor=supervisor,
                ledger=ledger,
                controlled_routes=controlled,
                trial_id=TRIAL_ID,
                route="plan_mcp",
                trial=bound_trial(TRIAL_ID),
                max_machine_seconds=1_800,
                route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
            )

    assert ledger.events == []


def test_runner_rejects_tool_gateway_bound_to_another_route(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result())
    route_kwargs = route_kwargs_for(ledger, TRIAL_ID, "react_skill_mcp")
    route_kwargs["gateway_factory"] = lambda base, _session, _trial, _route: LedgeredToolGateway(
        base,
        ledger,
        trial_id="other-trial",
        route="react_skill_mcp",
        tool_effects={},
    )

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="react_skill_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs,
    )

    assert receipt.status == "failed"
    assert ledger.status == "failed"
    assert ledger.events[-1][3]["error_category"] == "route_exception"


def test_agent_time_overrun_fails_route_but_keeps_score_and_cleanup(tmp_path):
    ledger = FakeLedger(tmp_path)
    controlled = FakeControlledRoutes(execution_result(machine_seconds=1_801))

    receipt = run_public_route(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        trial_id=TRIAL_ID,
        route="plan_mcp",
        trial=bound_trial(TRIAL_ID),
        max_machine_seconds=1_800,
        route_kwargs=route_kwargs_for(ledger, TRIAL_ID, "plan_mcp"),
    )

    terminal = ledger.events[-1][3]
    assert receipt.status == "failed"
    assert receipt.error_category == "resource_overrun"
    assert terminal["score_status"] == "scored"
    assert terminal["cleanup_verified"] is True


class PhaseControlledRoutes:
    def __init__(
        self, ledger, *, fail_first_route=False, semantic_decision=True,
        primary_score_error=False,
    ):
        self.ledger = ledger
        self.fail_first_route = fail_first_route
        self.semantic_decision = semantic_decision
        self.primary_score_error = primary_score_error
        self.calls = []
        self.max_tau_steps = 200
        self.last_plan = None
        self.shared_plan = SimpleNamespace(digest="1" * 64)

    def run(self, route, trial, **kwargs):
        route_name = getattr(route, "value", route)
        trial_id = trial.trial_id
        self.calls.append((trial_id, route_name))
        self.last_plan = None
        if route_name == "plan_primary_semantic_mcp":
            self.last_plan = self.shared_plan
        if route_name == "plan_jev_semantic_mcp":
            assert kwargs["frozen_plan"] is self.shared_plan
        kwargs["model_adapter_factory"](None, trial, route)
        kwargs["gateway_factory"](FakeGateway(), None, trial, route)
        kwargs["on_agent_start"]()
        tau_steps = 0
        if trial.dataset == "tau-bench":
            kwargs["on_tau_step"]("agent")
            tau_steps = 1
        if self.fail_first_route and len(self.calls) == 1:
            self.ledger.mark_route_unknown(trial_id, route_name)
            raise RuntimeError("simulated dispatched model outcome unknown")
        if route_name == "plan_primary_semantic_mcp":
            if self.primary_score_error:
                return execution_result(
                    score=False,
                    status="failed",
                    failure_kind="scoring_failure",
                    tau_steps=tau_steps,
                    plan_digest="1" * 64,
                    decision_input_digest="2" * 64,
                )
            return execution_result(
                tau_steps=tau_steps,
                plan_digest="1" * 64,
                decision_input_digest=("2" * 64 if self.semantic_decision else None),
            )
        if route_name == "plan_jev_semantic_mcp":
            return execution_result(
                tau_steps=tau_steps,
                plan_digest="1" * 64,
                decision_input_digest="2" * 64,
                evidence_scope="execution_only",
            )
        return execution_result(tau_steps=tau_steps)


def run_public_phase(*, root, ledger, controlled_routes, **kwargs):
    with held_supervisor(root) as supervisor:
        return execute_public_phase(
            root=root,
            supervisor=supervisor,
            ledger=ledger,
            controlled_routes=controlled_routes,
            **kwargs,
        )


def test_first_wave_uses_frozen_order_and_continues_after_unknown_route(tmp_path):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    controlled = PhaseControlledRoutes(ledger, fail_first_route=True)
    loaded_trials = []

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="first-wave",
        max_machine_seconds=1_800,
        trial_factory=lambda item: (
            loaded_trials.append(item["trial_id"])
            or bound_trial(
                item["trial_id"],
                record=item,
                branch_kind="semantic" if item["domain"] == "csm" else "structured",
            )
        ),
        route_kwargs_factory=lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    )

    expected = [
        (item["trial_id"], route)
        for item in plan.initial_smoke_wave
        for route in plan.routes
    ]
    assert controlled.calls == expected
    assert [item.status for item in receipt.executed_routes] == [
        "unknown",
        *(["completed"] * 7),
    ]
    assert receipt.scheduled_route_slots == 8
    assert receipt.stopped_reason is None
    assert loaded_trials == [item["trial_id"] for item in plan.initial_smoke_wave]


def test_bounded_diagnostic_runs_plan_before_react_for_each_case(tmp_path):
    from evals.benchmarks.pilot_gate import build_bounded_diagnostic_plan

    base = load_public_run_plan(
        ROOT,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    plan = replace(build_bounded_diagnostic_plan(base), root=tmp_path)
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    controlled = PhaseControlledRoutes(ledger)

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="diagnostic",
        max_machine_seconds=plan.resource_limits["agent_machine_seconds"],
        trial_factory=lambda item: bound_trial(
            item["trial_id"],
            record=item,
            branch_kind="semantic" if item["domain"] == "csm" else "structured",
        ),
        route_kwargs_factory=lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    )

    expected = [
        (item["trial_id"], route)
        for item in plan.trials
        for route in ("plan_mcp", "react_skill_mcp")
    ]
    assert controlled.calls == expected
    assert receipt.scheduled_route_slots == 8


def test_pilot_scheduler_runs_exact_per_trial_route_pairs(tmp_path):
    base = load_public_run_plan(
        ROOT,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    trials = tuple(base.trials)
    csm_ids = {
        item["trial_id"] for item in trials if item["domain"] == "csm"
    }
    schedule = tuple(
        (item["trial_id"], route)
        for item in trials
        for route in (
            ("react_skill_mcp", "plan_mcp", "plan_primary_semantic_mcp", "plan_jev_semantic_mcp")
            if item["trial_id"] in csm_ids
            else ("react_skill_mcp", "plan_mcp")
        )
    )
    pilot = replace(
        base,
        root=tmp_path,
        mode="pilot",
        trials=trials,
        routes=(
            "react_skill_mcp",
            "plan_mcp",
            "plan_primary_semantic_mcp",
            "plan_jev_semantic_mcp",
        ),
        route_schedule=schedule,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(pilot)
    controlled = PhaseControlledRoutes(ledger)
    loaded_trials = []

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="pilot",
        max_machine_seconds=1_800,
        trial_factory=lambda item: (
            loaded_trials.append(item["trial_id"])
            or bound_trial(
                item["trial_id"],
                record=item,
                branch_kind="semantic" if item["domain"] == "csm" else "structured",
            )
        ),
        route_kwargs_factory=lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    )

    assert len(controlled.calls) == 68
    assert controlled.calls == [
        (item["trial_id"], route) for item in trials
        for route in pilot.routes_for_trial(item["trial_id"])
    ]
    assert receipt.scheduled_route_slots == 68
    assert loaded_trials == [item["trial_id"] for item in trials]
    semantic_receipts = {
        (item.trial_id, item.route): item for item in receipt.executed_routes
    }
    for trial_id in csm_ids:
        primary = semantic_receipts[(trial_id, "plan_primary_semantic_mcp")]
        jev = semantic_receipts[(trial_id, "plan_jev_semantic_mcp")]
        assert primary.plan_digest == jev.plan_digest == "1" * 64
        assert primary.decision_input_digest == jev.decision_input_digest == "2" * 64
        assert jev.evidence_scope == "execution_only"
        assert jev.pair_status == "paired"


def test_pilot_scheduler_marks_jev_not_applicable_without_natural_decision(tmp_path):
    base = load_public_run_plan(
        ROOT,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    trials = tuple(base.trials)
    csm_ids = {item["trial_id"] for item in trials if item["domain"] == "csm"}
    schedule = tuple(
        (item["trial_id"], route)
        for item in trials
        for route in (
            ("react_skill_mcp", "plan_mcp", "plan_primary_semantic_mcp", "plan_jev_semantic_mcp")
            if item["trial_id"] in csm_ids
            else ("react_skill_mcp", "plan_mcp")
        )
    )
    pilot = replace(
        base,
        root=tmp_path,
        mode="pilot",
        trials=trials,
        routes=(
            "react_skill_mcp",
            "plan_mcp",
            "plan_primary_semantic_mcp",
            "plan_jev_semantic_mcp",
        ),
        route_schedule=schedule,
    )
    ledger_path = canonical_public_trial_ledger_path(
        tmp_path,
        cohort_digest=pilot.cohort_digest,
        candidate_sha="a" * 40,
    )
    ledger = PublicTrialLedger.create(ledger_path, pilot)
    ledger.bind_candidate_sha("a" * 40)
    controlled = PhaseControlledRoutes(ledger, semantic_decision=False)

    try:
        receipt = run_public_phase(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            phase="pilot",
            max_machine_seconds=1_800,
            trial_factory=lambda item: bound_trial(
                item["trial_id"],
                record=item,
                branch_kind="semantic" if item["domain"] == "csm" else "structured",
            ),
            route_kwargs_factory=lambda trial, route: route_kwargs_for(
                ledger, trial.trial_id, route
            ),
        )
        jev_statuses = {
            item.trial_id: ledger.route_status(item.trial_id, item.route)
            for item in receipt.executed_routes
            if item.route == "plan_jev_semantic_mcp"
        }
    finally:
        ledger.close()

    jev_ids = {trial_id for trial_id in csm_ids}
    jev_receipts = [
        item for item in receipt.executed_routes
        if item.route == "plan_jev_semantic_mcp"
    ]
    assert {item.trial_id for item in jev_receipts} == jev_ids
    assert all(item.status == "not_applicable" for item in jev_receipts)
    assert all(item.pair_status == "not_applicable" for item in jev_receipts)
    assert all(item.error_category == "semantic_decision_not_observed" for item in jev_receipts)
    assert set(jev_statuses.values()) == {"not_applicable"}
    assert all(
        route != "plan_jev_semantic_mcp" for _trial_id, route in controlled.calls
    )


def test_pilot_resume_marks_jev_incomplete_when_primary_plan_is_not_restored(tmp_path):
    base = load_public_run_plan(
        ROOT,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    trials = tuple(base.trials)
    csm_ids = {item["trial_id"] for item in trials if item["domain"] == "csm"}
    schedule = tuple(
        (item["trial_id"], route)
        for item in trials
        for route in (
            ("react_skill_mcp", "plan_mcp", "plan_primary_semantic_mcp", "plan_jev_semantic_mcp")
            if item["trial_id"] in csm_ids
            else ("react_skill_mcp", "plan_mcp")
        )
    )
    pilot = replace(
        base,
        root=tmp_path,
        mode="pilot",
        trials=trials,
        routes=(
            "react_skill_mcp",
            "plan_mcp",
            "plan_primary_semantic_mcp",
            "plan_jev_semantic_mcp",
        ),
        route_schedule=schedule,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(pilot)
    target_trial = next(iter(sorted(csm_ids)))
    ledger.statuses.update({slot: "completed" for slot in schedule})
    ledger.statuses[(target_trial, "plan_jev_semantic_mcp")] = "pending"
    controlled = PhaseControlledRoutes(ledger)

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="pilot",
        max_machine_seconds=1_800,
        trial_factory=lambda item: bound_trial(
            item["trial_id"],
            record=item,
            branch_kind="semantic" if item["domain"] == "csm" else "structured",
        ),
        route_kwargs_factory=lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    )

    jev_receipt = next(
        item for item in receipt.executed_routes
        if item.trial_id == target_trial and item.route == "plan_jev_semantic_mcp"
    )
    assert jev_receipt.status == "failed"
    assert jev_receipt.pair_status == "incomplete"
    assert jev_receipt.error_category == "semantic_reference_incomplete"
    assert ledger.route_status(target_trial, "plan_jev_semantic_mcp") == "failed"
    assert controlled.calls == []


def test_primary_scoring_failure_keeps_semantic_pair_evidence_and_does_not_mark_jev_na(
    tmp_path,
):
    base = load_public_run_plan(
        ROOT,
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    trials = tuple(base.trials)
    csm_ids = {item["trial_id"] for item in trials if item["domain"] == "csm"}
    trial_record = next(item for item in trials if item["domain"] == "csm")
    schedule = tuple(
        (item["trial_id"], route)
        for item in trials
        for route in (
            (
                "react_skill_mcp",
                "plan_mcp",
                "plan_primary_semantic_mcp",
                "plan_jev_semantic_mcp",
            )
            if item["trial_id"] in csm_ids
            else ("react_skill_mcp", "plan_mcp")
        )
    )
    pilot = replace(
        base,
        root=tmp_path,
        mode="pilot",
        trials=trials,
        routes=(
            "react_skill_mcp",
            "plan_mcp",
            "plan_primary_semantic_mcp",
            "plan_jev_semantic_mcp",
        ),
        route_schedule=schedule,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(pilot)
    controlled = PhaseControlledRoutes(ledger, primary_score_error=True)

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="pilot",
        max_machine_seconds=1_800,
        trial_factory=lambda item: bound_trial(
            item["trial_id"], record=item, branch_kind="semantic"
        ),
        route_kwargs_factory=lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    )

    by_route = {
        (item.trial_id, item.route): item for item in receipt.executed_routes
    }
    primary = by_route[(trial_record["trial_id"], "plan_primary_semantic_mcp")]
    jev = by_route[(trial_record["trial_id"], "plan_jev_semantic_mcp")]
    assert primary.status == "scoring_error"
    assert primary.plan_digest == "1" * 64
    assert primary.decision_input_digest == "2" * 64
    assert jev.status == "completed"
    assert jev.pair_status == "paired"


def test_comparison_evidence_survives_without_a_scored_route_run():
    result = SimpleNamespace(
        run=None,
        plan_digest="3" * 64,
        decision_input_digest="4" * 64,
        evidence_scope="full_workflow",
    )

    assert _comparison_evidence(result) == (
        "3" * 64,
        "4" * 64,
        "full_workflow",
    )


def test_first_wave_resume_skips_terminal_routes_without_replay(tmp_path):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    first_trial = plan.initial_smoke_wave[0]["trial_id"]
    first_route = plan.routes[0]
    ledger.statuses[(first_trial, first_route)] = "completed"
    controlled = PhaseControlledRoutes(ledger)
    common = {
        "root": tmp_path,
        "ledger": ledger,
        "controlled_routes": controlled,
        "phase": "first-wave",
        "max_machine_seconds": 1_800,
        "trial_factory": lambda item: bound_trial(
            item["trial_id"],
            record=item,
        ),
        "route_kwargs_factory": lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    }

    first = run_public_phase(**common)
    second = run_public_phase(**common)

    assert len(first.executed_routes) == 7
    assert len(first.skipped_routes) == 1
    assert first.skipped_routes[0] == (first_trial, first_route, "completed")
    assert second.executed_routes == ()
    assert len(second.skipped_routes) == 8
    assert len(controlled.calls) == 7


def test_first_wave_scheduler_honors_persisted_batch_stop_without_loading_trials(tmp_path):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    ledger.stopped_reason = "model_profile_drift"
    controlled = PhaseControlledRoutes(ledger)
    factory_calls = []

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="first-wave",
        max_machine_seconds=1_800,
        trial_factory=lambda _item: factory_calls.append("trial"),
        route_kwargs_factory=lambda *_args: factory_calls.append("route_kwargs"),
    )

    assert receipt.scheduled_route_slots == 8
    assert receipt.executed_routes == ()
    assert len(receipt.skipped_routes) == 8
    assert {item[2] for item in receipt.skipped_routes} == {"batch_stopped"}
    assert receipt.stopped_reason == "model_profile_drift"
    assert factory_calls == []
    assert controlled.calls == []


def test_first_wave_scheduler_persists_expired_preflight_deadline_and_skips_routes(
    tmp_path,
):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    ledger.remaining_wall_seconds = 0
    controlled = PhaseControlledRoutes(ledger)
    factory_calls = []

    receipt = run_public_phase(
        root=tmp_path,
        ledger=ledger,
        controlled_routes=controlled,
        phase="first-wave",
        max_machine_seconds=1_800,
        trial_factory=lambda _item: factory_calls.append("trial"),
        route_kwargs_factory=lambda *_args: factory_calls.append("route_kwargs"),
    )

    assert receipt.scheduled_route_slots == 8
    assert receipt.executed_routes == ()
    assert len(receipt.skipped_routes) == 8
    assert {item[2] for item in receipt.skipped_routes} == {"batch_stopped"}
    assert receipt.stopped_reason == "wall_seconds"
    assert ("stop_batch", "wall_seconds") in ledger.events
    assert factory_calls == []
    assert controlled.calls == []


def test_first_wave_scheduler_rejects_trial_metadata_drift_before_route_claim(tmp_path):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger = FakeLedger(tmp_path)
    ledger.bind_plan(plan)
    controlled = PhaseControlledRoutes(ledger)
    item = plan.initial_smoke_wave[0]

    with pytest.raises(ValueError, match="changed the frozen trial ID"):
        run_public_phase(
            root=tmp_path,
            ledger=ledger,
            controlled_routes=controlled,
            phase="first-wave",
            max_machine_seconds=1_800,
            trial_factory=lambda _item: bound_trial(
                item["trial_id"], record=item, dataset="different-dataset"
            ),
            route_kwargs_factory=lambda trial, route: route_kwargs_for(
                ledger, trial.trial_id, route
            ),
        )

    assert ledger.events == []
    assert controlled.calls == []


def test_first_wave_scheduler_resumes_real_sqlite_ledger_without_replay(tmp_path):
    plan = replace(
        load_public_run_plan(
            ROOT,
            check_images=False,
            check_runtime=False,
            check_source_files=False,
        ),
        root=tmp_path,
    )
    ledger_path = canonical_public_trial_ledger_path(
        tmp_path,
        cohort_digest=plan.cohort_digest,
        candidate_sha="a" * 40,
    )
    ledger = PublicTrialLedger.create(ledger_path, plan, lease_seconds=30)
    ledger.bind_candidate_sha("a" * 40)
    controlled = PhaseControlledRoutes(ledger)
    common = {
        "root": tmp_path,
        "controlled_routes": controlled,
        "phase": "first-wave",
        "max_machine_seconds": 1_800,
        "trial_factory": lambda item: bound_trial(
            item["trial_id"],
            record=item,
        ),
        "route_kwargs_factory": lambda trial, route: route_kwargs_for(
            ledger, trial.trial_id, route
        ),
    }
    with held_supervisor(tmp_path) as supervisor:
        first = execute_public_phase(ledger=ledger, supervisor=supervisor, **common)
    ledger.close()

    resumed = PublicTrialLedger.resume(ledger_path, plan, lease_seconds=30)
    common["route_kwargs_factory"] = lambda trial, route: route_kwargs_for(
        resumed, trial.trial_id, route
    )
    with held_supervisor(tmp_path) as supervisor:
        second = execute_public_phase(ledger=resumed, supervisor=supervisor, **common)
    resumed.close()

    assert len(first.executed_routes) == 8
    assert all(item.status == "completed" for item in first.executed_routes)
    assert second.executed_routes == ()
    assert len(second.skipped_routes) == 8
    assert len(controlled.calls) == 8

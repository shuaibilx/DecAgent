"""Controlled routes execute the production interpreter against isolated sessions."""

import importlib
import importlib.util
import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.memory_slots import memory_slot_key
from decagent.business.plans.runtime import NodePending, NodeResult
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.tickets import builtin_manifest
from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import (
    ComparisonTrial,
    Route,
    compare_full_workflow,
    compare_semantic_judgment,
)
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.deterministic.test_business_plan_contracts import literal, node, reference


def api():
    assert importlib.util.find_spec("decagent.evaluation.business.routes"), (
        "Executable controlled runners are missing"
    )
    return importlib.import_module("decagent.evaluation.business.routes")


class Repository:
    """Test storage port only: execution, compilation, bindings and completion are real."""

    def __init__(self, session, plan):
        self.session, self.spec, self.task_id = session, session.spec, session.isolation_id
        self.plan, self.rows, self.context = plan, {}, {}
        if getattr(session, "input_schema", None):
            self.context["input_schema"] = deepcopy(session.input_schema)
        self.database = self

    @contextmanager
    def connect(self):
        yield self

    def execute(self, sql, parameters):
        assert "app.actions" in sql and parameters == (self.task_id,)
        return SimpleNamespace(fetchall=lambda: [])

    def guard(self, conn=None):
        return {
            "employee_id": "employee",
            "scope_revision": 1,
            "authorization_revision": 1,
            "authorization_snapshot": {"instruction": self.session.task_input["instruction"]},
        }

    def instruction(self):
        return self.session.task_input["instruction"]

    def context_snapshot(self):
        return deepcopy(self.context)

    def save_context(self, value):
        self.context.update(deepcopy(value))

    def _safe(self, value):
        return deepcopy(value)

    def current(self):
        return self.plan

    def nodes(self):
        return list(self.rows.values())

    def transition(self):
        pass

    def begin(self, node_id, inputs, provenance):
        value = {
            "node_id": node_id,
            "node_digest": digest(next(n for n in self.plan.nodes if n["id"] == node_id)),
            "execution_generation": 1,
            "status": "running",
            "output_snapshot": {},
            "input_digest": digest(inputs),
            "dispatch_attempts": 0,
        }
        self.rows[node_id] = value
        return value

    def dispatch(self, node_id, *, max_attempts, generation):
        self.rows[node_id]["dispatch_attempts"] += 1

    def finish(self, node_id, status, output, *, generation):
        self.rows[node_id].update(status=status, output_snapshot=deepcopy(output))


class Session:
    def __init__(self, *, plan=None, reset_ok=True, drift=None):
        self.spec = PluginSpec.from_mapping(builtin_manifest())
        self.plan = plan or read_plan()
        self.task_input = {"instruction": json.dumps({"command": "directory", "params": {}})}
        self.skill = "Use registered tools and preserve original employee approval requirements."
        self.calls, self.model_inputs, self.repositories = [], [], []
        self.tool_timeouts = []
        self.reset_ok, self.drift, self.observations = reset_ok, drift, 0
        self.gateway = self
        self.memory_fixture = {}
        self.closed = False
        self.deadline_provider = None

    def boundary(self):
        self.observations += 1
        values = dict(
            initial_state_digest=digest({"directory": "initial"}),
            goal_digest=digest(self.task_input),
            actor_profile_digest=digest("employee"),
            permission_digest=digest({"scope": 1}),
            tool_adapter_digest=digest(self.spec.manifest),
        )
        if self.drift and self.observations >= 2:
            values[self.drift] = digest("changed")
        return api().TrialBoundary(**values)

    def reset(self):
        self.calls.clear()
        return self.reset_ok

    def call(self, command, params, *, timeout_seconds=None):
        self.calls.append((command, deepcopy(params)))
        self.tool_timeouts.append(timeout_seconds)
        return {"people": [], "categories": []}

    def publish_plan(self, plan, task_input):
        repository = Repository(self, plan)
        self.repositories.append(repository)
        return repository

    def load_workflow(self):
        return self.plan

    def validate_workflow(self, plan):
        return True

    def verified_memories(self):
        return self.memory_fixture

    def bind_deadline_provider(self, provider):
        self.deadline_provider = provider

    def handle_node(self, node, inputs, context, saved, gateway):
        raise NodePending({"kind": "action", "reason": "Authenticated approval required"})

    def accept_decision(self, node, question, value, context, saved):
        return NodeResult(
            {
                "kind": "decide",
                "accepted": True,
                "value": value,
                "employee_id": context.employee_id,
                "scope_revision": context.scope_revision,
                "account_revision": context.account_revision,
                "grants_write_authority": False,
            },
            no_match=value.get("selected_value") == "no_match",
        )

    def score(self):
        return api().TaskScore(
            success=bool(self.calls),
            forbidden_side_effects=0,
            user_simulator_cost_usd=Decimal("0.03"),
            user_simulator_requests=1,
            user_simulator_tokens=200,
            human_wait_seconds=0.1,
        )

    def cleanup_evidence(self):
        return {
            "verified": self.closed,
            "session_closed": self.closed,
            "environment_closed": self.closed,
        }


def trial(session, *, branch_kind="structured", workflow_mode="cold", memory_mode="disabled"):
    boundary = session.boundary()
    session.observations = 0
    return ComparisonTrial(
        dataset="fixture",
        dataset_version="1",
        dataset_checksum=digest("dataset"),
        case_id="case-1",
        branch_kind=branch_kind,
        memory_mode=memory_mode,
        memory_fixture_digest=digest(session.memory_fixture if memory_mode == "verified" else {}),
        workflow_mode=workflow_mode,
        **vars(boundary),
    )


def start_ledgered_route(ledger, trial_id, route, *, max_seconds=1800):
    reservation = ledger.start_route_with_machine_time(
        trial_id, route, max_seconds=max_seconds
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger.mark_machine_time_agent_started(reservation)
    return reservation


def factory(session):
    @contextmanager
    def open_session(trial, isolation_id):
        session.isolation_id = isolation_id
        try:
            yield session
        finally:
            session.closed = True

    return open_session


def reply(value):
    return api().ModelReply(value, tokens=17, cost_usd=Decimal("0.002"), model="scripted-model")


def planner(session):
    def plan(request):
        session.model_inputs.append(deepcopy(request))
        return reply(session.plan)

    return plan


def execute(session, *, route=Route.PLAN_MCP, context=None, **kwargs):
    return (
        api()
        .ControlledRoutes(factory(session))
        .run(route, context or trial(session), planner=planner(session), **kwargs)
    )


def test_once_plan_executes_production_runtime_and_keeps_simulator_cost_separate():
    session = Session()
    result = execute(session)
    assert result.status == "complete" and result.run.task_success
    assert session.calls == [("directory", {})]
    assert result.run.primary_requests == 1 and result.run.primary_tokens == 17
    assert result.run.agent_cost_usd == Decimal("0.002")
    assert result.run.user_simulator_cost_usd == Decimal("0.03")
    assert result.tool_calls[0].command == "directory"
    assert session.closed


def test_plan_callback_receives_the_current_route_deadline():
    session = Session()
    route_trial = trial(session)
    observed = []

    def bounded_planner(_request, *, timeout_seconds):
        observed.append(timeout_seconds)
        return reply(session.plan)

    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        route_trial,
        planner=bounded_planner,
        deadline_provider=lambda: 4.25,
    )

    assert result.status == "complete", result.error
    assert observed == [4.25]


def test_react_callback_receives_the_current_route_deadline():
    session = Session()
    route_trial = trial(session)
    observed = []

    def bounded_react(_request, *, timeout_seconds):
        observed.append(timeout_seconds)
        return reply({"finish": True})

    result = api().ControlledRoutes(factory(session)).run(
        Route.REACT_SKILL_MCP,
        route_trial,
        react=bounded_react,
        deadline_provider=lambda: 3.5,
    )

    assert result.status == "complete", result.error
    assert observed == [3.5]


def test_route_factories_bind_session_specific_model_and_budgeted_tool_adapters():
    session = Session()
    route_trial = trial(session)
    adapter_calls = []
    gateway_calls = []

    def model_factory(current_session, current_trial, current_route):
        assert current_session is session
        assert current_trial is route_trial
        assert current_route is Route.PLAN_MCP
        adapter_calls.append(current_session)
        return {"planner": planner(current_session)}

    def gateway_factory(base_gateway, current_session, current_trial, current_route):
        assert current_session is session
        assert current_trial is route_trial
        assert current_route is Route.PLAN_MCP

        class ObservedGateway:
            def call(self, command, params):
                gateway_calls.append((command, deepcopy(params)))
                return base_gateway.call(command, params)

        return ObservedGateway()

    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        route_trial,
        model_adapter_factory=model_factory,
        gateway_factory=gateway_factory,
    )

    assert result.status == "complete"
    assert adapter_calls == [session]
    assert gateway_calls == [("directory", {})]
    assert result.run.primary_requests == 1


@pytest.mark.parametrize("close_fails", [False, True])
def test_controlled_route_closes_managed_model_resources_after_scoring(close_fails):
    session = Session()
    route_trial = trial(session)
    lifecycle = []

    class ManagedAdapters(dict):
        def close(self):
            lifecycle.append("close")
            if close_fails:
                raise RuntimeError("provider client close failed")

    adapters = ManagedAdapters({"planner": planner(session)})
    original_score = session.score

    def score_after_model_use():
        assert lifecycle == []
        return original_score()

    session.score = score_after_model_use
    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        route_trial,
        model_adapter_factory=lambda *_args: adapters,
    )

    assert lifecycle == ["close"]
    assert result.score is not None and result.score.success
    if close_fails:
        assert result.status == "failed"
        assert result.failure_kind == "cleanup_failure"
        assert result.run is not None and not result.run.task_success
    else:
        assert result.status == "complete"
        assert result.failure_kind is None
        assert result.run is not None and result.run.task_success


def test_route_result_records_verified_environment_cleanup():
    session = Session()
    result = execute(
        session,
        route=Route.REACT_SKILL_MCP,
        react=lambda _request: reply({"finish": True}),
    )

    assert result.status == "complete"
    assert result.cleanup_verified is True
    assert result.cleanup_evidence == {
        "verified": True,
        "session_closed": True,
        "environment_closed": True,
    }


def test_agent_deadline_hook_runs_after_environment_setup_before_route_callback():
    session = Session()
    events = []

    @contextmanager
    def environment(_trial, isolation_id):
        session.isolation_id = isolation_id
        events.append("environment_setup")
        try:
            yield session
        finally:
            session.closed = True

    def react(_request):
        events.append("agent_callback")
        return reply({"finish": True})

    result = api().ControlledRoutes(environment).run(
        Route.REACT_SKILL_MCP,
        trial(session),
        react=react,
        on_agent_start=lambda: events.append("agent_start"),
    )

    assert result.status == "complete"
    assert events == ["environment_setup", "agent_start", "agent_callback"]


def test_controlled_route_binds_deadline_provider_before_agent_execution():
    session = Session()
    seen = []
    callback_timeouts = []

    def bind_deadline(provider):
        seen.append(provider)

    session.bind_deadline_provider = bind_deadline
    def provider():
        return 2.5

    def react(_request, *, timeout_seconds):
        callback_timeouts.append(timeout_seconds)
        return reply({"finish": True})

    result = api().ControlledRoutes(factory(session)).run(
        Route.REACT_SKILL_MCP,
        trial(session),
        react=react,
        deadline_provider=provider,
    )

    assert result.status == "complete"
    assert callback_timeouts == [2.5]
    assert seen == [provider]
    assert seen[0]() == 2.5


def test_environment_context_exit_error_is_classified_as_cleanup_failure():
    session = Session()

    @contextmanager
    def broken_cleanup(_trial, isolation_id):
        session.isolation_id = isolation_id
        try:
            yield session
        finally:
            session.closed = True
            raise RuntimeError("isolated environment cleanup failed")

    result = api().ControlledRoutes(broken_cleanup).run(
        Route.REACT_SKILL_MCP,
        trial(session),
        react=lambda _request: reply({"finish": True}),
    )

    assert result.status == "failed"
    assert result.failure_kind == "cleanup_failure"
    assert result.cleanup_verified is False
    assert result.score is not None


def test_unverified_environment_cleanup_prevents_route_success():
    session = Session()
    session.cleanup_evidence = lambda: {
        "verified": False,
        "session_closed": True,
        "environment_closed": False,
    }

    result = execute(
        session,
        route=Route.REACT_SKILL_MCP,
        react=lambda _request: reply({"finish": True}),
    )

    assert result.status == "failed"
    assert result.failure_kind == "cleanup_failure"
    assert result.cleanup_verified is False
    assert result.run is not None and result.run.task_success is False


def test_plan_route_factory_applies_durable_ledger_to_real_executor_tool_call(tmp_path):
    from pathlib import Path

    from decagent.evaluation.business.public_resources import LedgeredToolGateway
    from decagent.evaluation.business.public_trials import (
        PublicTrialLedger,
        load_public_run_plan,
    )

    root = Path(__file__).resolve().parents[2]
    public_plan = load_public_run_plan(
        root,
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    frozen_route = Route.PLAN_MCP.value
    frozen_trial = public_plan.trials[0]
    ledger = PublicTrialLedger.create(tmp_path / "route-tool-ledger.sqlite3", public_plan)
    machine_reservation = start_ledgered_route(
        ledger, frozen_trial["trial_id"], frozen_route
    )
    session = Session()
    route_trial = trial(session)

    def gateway_factory(base_gateway, current_session, current_trial, current_route):
        assert current_session is session and current_trial is route_trial
        return LedgeredToolGateway(
            base_gateway,
            ledger,
            trial_id=frozen_trial["trial_id"],
            route=current_route,
            tool_effects={
                name: command.effect
                for name, command in current_session.spec.commands.items()
            },
        )

    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        route_trial,
        planner=planner(session),
        gateway_factory=gateway_factory,
    )

    assert result.status == "complete"
    assert session.calls == [("directory", {})]
    assert len(session.tool_timeouts) == 1
    assert 0 < session.tool_timeouts[0] <= 60
    attempt = ledger._connection.execute(
        "SELECT status,command FROM tool_attempts"
    ).fetchone()
    assert (attempt["status"], attempt["command"]) == ("settled", "directory")
    assert ledger.resource_snapshot(frozen_trial["trial_id"], frozen_route)["tool_calls"] == 1
    score_evidence = {
        "source": (
            "enterpriseops-gym-original-verifier"
            if frozen_trial["benchmark"] == "enterpriseops-gym"
            else "tau2-bench-original-evaluate-simulation"
        ),
        "value": float(bool(result.run.task_success)),
        "success": bool(result.run.task_success),
        "forbidden_side_effects": 0,
    }
    assert ledger.settle_machine_time(
        machine_reservation,
        actual_seconds=result.timing.agent_wall_seconds,
    ) is True
    ledger.finish_route(
        frozen_trial["trial_id"],
        frozen_route,
        status="completed",
        result_digest=digest({"status": result.status}),
        score_status="scored",
        score_digest=digest(score_evidence),
        score_evidence=score_evidence,
        cleanup_verified=session.closed,
        cleanup_evidence_digest=digest({"session_closed": session.closed}),
    )
    assert ledger.route_status(frozen_trial["trial_id"], frozen_route) == "completed"
    ledger.close()


def test_plan_route_uses_ledgered_model_factory_and_tool_gateway_together(tmp_path):
    from pathlib import Path

    from decagent.evaluation.business.public_resources import (
        LedgeredModelAdapterFactory,
        LedgeredToolGateway,
    )
    from decagent.evaluation.business.public_trials import (
        PublicTrialLedger,
        load_public_run_plan,
    )

    public_plan = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    primary = replace(
        public_plan.profiles.primary,
        pricing_status="known",
        input_price_per_million=Decimal("0.8"),
        output_price_per_million=Decimal("2.7"),
        price_version="integration-fixture-rates-v1",
        price_source="isolated route integration fixture",
    )
    public_plan = replace(
        public_plan,
        profiles=replace(public_plan.profiles, primary=primary),
    )
    frozen_trial = public_plan.trials[0]
    route = Route.PLAN_MCP
    ledger = PublicTrialLedger.create(tmp_path / "route-model-tool-ledger.sqlite3", public_plan)
    ledger.start_route(frozen_trial["trial_id"], route.value)
    machine_attempt = ledger.reserve_machine_time(
        trial_id=frozen_trial["trial_id"], route=route.value, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(machine_attempt)
    ledger.mark_machine_time_agent_started(machine_attempt)

    session = Session()
    route_trial = replace(
        trial(session),
        model_profile_digest=public_plan.profiles.primary.fingerprint,
        jev_profile_digest=public_plan.profiles.jev.fingerprint,
        user_simulator_profile_digest=public_plan.profiles.user_simulator.fingerprint,
        judge_profile_digest=public_plan.profiles.judge.fingerprint,
    )
    model_calls = []
    closed_model_clients = []

    class PlannerClient:
        def __call__(self, request, *, timeout_seconds):
            model_calls.append((deepcopy(request), timeout_seconds))
            assert 0 < timeout_seconds <= primary.timeout_seconds
            return api().ModelReply(
                session.plan,
                tokens=7,
                cost_usd=None,
                model=primary.model,
                input_tokens=5,
                output_tokens=2,
                profile=primary,
            )

        def close(self):
            closed_model_clients.append("planner")

    planner_callback = PlannerClient()

    def resolve_trial(current):
        if current.case_id != route_trial.case_id:
            raise ValueError("Unexpected trial identity")
        return frozen_trial["trial_id"]

    model_factory = LedgeredModelAdapterFactory(
        {"planner": lambda current, _trial, _route: planner_callback},
        {"planner": lambda request: max(1, len(json.dumps(request).encode("utf-8")))},
        ledger,
        public_plan.profiles,
        trial_id_for=resolve_trial,
    )

    def gateway_factory(base, _session, _trial, current_route):
        return LedgeredToolGateway(
            base,
            ledger,
            trial_id=frozen_trial["trial_id"],
            route=current_route,
            tool_effects={
                name: command.effect
                for name, command in session.spec.commands.items()
            },
        )

    result = api().ControlledRoutes(factory(session)).run(
        route,
        route_trial,
        model_adapter_factory=model_factory,
        gateway_factory=gateway_factory,
    )

    assert result.status == "complete" and result.run.task_success
    assert closed_model_clients == ["planner"]
    assert len(model_calls) == 1 and result.run.primary_requests == 1
    assert session.calls == [("directory", {})]
    assert ledger.usage_snapshot(frozen_trial["trial_id"], route.value)["primary"][
        "actual_tokens"
    ] == 7
    assert ledger.resource_snapshot(frozen_trial["trial_id"], route.value)["tool_calls"] == 1
    model_attempt = ledger._connection.execute(
        "SELECT status FROM model_attempts"
    ).fetchone()
    tool_attempt = ledger._connection.execute("SELECT status FROM tool_attempts").fetchone()
    assert model_attempt["status"] == tool_attempt["status"] == "settled"
    ledger.close()


def test_unknown_tool_stops_route_before_official_scoring(tmp_path):
    from pathlib import Path

    from decagent.evaluation.business.public_resources import LedgeredToolGateway
    from decagent.evaluation.business.public_trials import (
        PublicTrialLedger,
        load_public_run_plan,
    )

    public_plan = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    frozen_trial = public_plan.trials[0]
    route = Route.PLAN_MCP
    ledger = PublicTrialLedger.create(tmp_path / "unknown-tool.sqlite3", public_plan)
    start_ledgered_route(ledger, frozen_trial["trial_id"], route.value, max_seconds=30)
    session = Session()
    scoring = []
    session.score = lambda: scoring.append("called") or Session.score(session)

    class UnknownGateway:
        def call(self, _command, _params, *, timeout_seconds):
            raise TimeoutError("upstream outcome cannot be confirmed")

    def gateway_factory(_base, current_session, _trial, current_route):
        return LedgeredToolGateway(
            UnknownGateway(),
            ledger,
            trial_id=frozen_trial["trial_id"],
            route=current_route,
            tool_effects={
                name: command.effect
                for name, command in current_session.spec.commands.items()
            },
        )

    result = api().ControlledRoutes(factory(session)).run(
        route,
        trial(session),
        planner=planner(session),
        gateway_factory=gateway_factory,
    )

    assert scoring == []
    assert result.terminal.get("unknown") is True
    assert result.usage.complete is True
    assert ledger.route_status(frozen_trial["trial_id"], route.value) == "unknown"
    ledger.close()


def test_unknown_model_usage_stops_route_before_tools_and_official_scoring(tmp_path):
    from pathlib import Path

    from decagent.evaluation.business.public_resources import (
        LedgeredModelCallback,
    )
    from decagent.evaluation.business.public_trials import (
        PublicTrialLedger,
        load_public_run_plan,
    )

    selected = load_public_run_plan(
        Path(__file__).resolve().parents[2],
        mode="smoke",
        check_images=False,
        check_runtime=False,
        check_source_files=False,
    )
    priced_primary = replace(
        selected.profiles.primary,
        pricing_status="known",
        input_price_per_million=Decimal("0.8"),
        output_price_per_million=Decimal("2.7"),
        price_version="test-rates-v1",
        price_source="isolated deterministic fixture",
    )
    selected = replace(
        selected,
        profiles=replace(selected.profiles, primary=priced_primary),
    )
    frozen = selected.trials[0]
    ledger = PublicTrialLedger.create(tmp_path / "unknown-model.sqlite3", selected)
    ledger.start_route(frozen["trial_id"], Route.PLAN_MCP.value)
    machine_window = ledger.reserve_machine_time(
        trial_id=frozen["trial_id"], route=Route.PLAN_MCP.value, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(machine_window)
    session = Session()
    scoring = []
    session.score = lambda: scoring.append("called") or Session.score(session)
    dispatched = []

    def callback(_request, *, timeout_seconds):
        assert timeout_seconds > 0
        dispatched.append("sent")
        raise TimeoutError("provider response was lost")

    measured = LedgeredModelCallback(
        callback,
        ledger,
        profile=priced_primary,
        trial_id=frozen["trial_id"],
        route=Route.PLAN_MCP,
        input_token_upper_bound=lambda request: max(
            1, len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        ),
        route_remaining_seconds=lambda: ledger.route_remaining_seconds(
            frozen["trial_id"], Route.PLAN_MCP.value
        ),
    )
    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        trial(session),
        planner=measured,
    )

    assert result.status == "unknown"
    assert result.terminal.get("unknown") is True
    assert result.usage.complete is False
    assert scoring == []
    assert dispatched == ["sent"]
    assert session.calls == []
    assert ledger.route_status(frozen["trial_id"], Route.PLAN_MCP.value) == "unknown"
    ledger.close()


def test_model_profile_drift_stops_route_before_official_scoring():
    from decagent.evaluation.business.public_resources import PublicModelProfileDrift

    session = Session()
    scoring = []
    session.score = lambda: scoring.append("called") or Session.score(session)

    def drifted_planner(_request):
        raise PublicModelProfileDrift("upstream identity changed")

    result = api().ControlledRoutes(factory(session)).run(
        Route.PLAN_MCP,
        trial(session),
        planner=drifted_planner,
    )

    assert result.status == "failed"
    assert result.failure_kind == "model_profile_drift"
    assert scoring == []
    assert session.calls == []


def test_unknown_model_cost_does_not_hide_known_tokens_or_block_scoring():
    session = Session()

    def planner_with_unknown_cost(request):
        session.model_inputs.append(deepcopy(request))
        return api().ModelReply(
            session.plan, tokens=17, cost_usd=None, model="scripted-model"
        )

    result = (
        api().ControlledRoutes(factory(session))
        .run(Route.PLAN_MCP, trial(session), planner=planner_with_unknown_cost)
    )

    assert result.status == "complete"
    assert result.run.task_success
    assert result.run.primary_tokens == 17
    assert result.run.primary_cost_usd is None
    assert result.run.agent_cost_usd is None
    assert result.usage.complete is True
    assert result.usage.cost_complete is False


def test_public_scorer_can_report_intermediate_side_effects_as_unmeasured():
    session = Session()
    session.score = lambda: api().TaskScore(
        success=True,
        forbidden_side_effects=None,
        user_simulator_cost_usd=None,
        user_simulator_requests=1,
        user_simulator_tokens=None,
        judge_cost_usd=Decimal("0.004"),
        judge_requests=1,
        judge_tokens=32,
        judge_seconds=0.5,
    )
    result = execute(session)
    assert result.status == "complete"
    assert result.run.task_success is True
    assert result.run.forbidden_side_effects is None
    assert result.run.user_simulator_requests == 1
    assert result.run.user_simulator_tokens is None
    assert result.run.user_simulator_cost_usd is None
    assert result.run.judge_cost_usd == Decimal("0.004")
    assert result.run.judge_requests == 1
    assert result.run.judge_tokens == 32


def test_react_route_sends_and_receives_official_clarification_messages():
    module = api()

    class ConversationSession(Session):
        supports_user_simulation = True

        def bind_tau_step_recorder(self, recorder):
            self.tau_step_recorder = recorder
            self.tau_step_events = []

        def record_tau_step(self, kind):
            self.tau_step_events.append(kind)
            return self.tau_step_recorder(kind)

        def configure_conversation(
            self, *, user_model, user_model_call, judge_model, judge_model_call
        ):
            self.user_model, self.user_model_call = user_model, user_model_call
            self.judge_model, self.judge_model_call = judge_model, judge_model_call
            self.messages = []
            self.user_stopped = False

        @property
        def conversation(self):
            return deepcopy(self.messages)

        def start_conversation(self):
            greeting = {"role": "assistant", "content": "Hi! How can I help you today?"}
            self.messages.append(greeting)
            self.messages.append(self._simulator_response(greeting, "Please find my case."))
            return deepcopy(self.messages[-1])

        def _simulator_response(self, message, content):
            self.record_tau_step("user")
            self.user_model_call(
                {
                    "name": "tau_user_simulator",
                    "payload": {"model": self.user_model, "message": message},
                }
            )
            result = {"role": "user", "content": content}
            self.messages.append(result)
            return result

        def ask_user(self, prompt):
            assistant = {"role": "assistant", "content": prompt}
            self.messages.append(assistant)
            self._simulator_response(assistant, "The case is C-17.")
            return "The case is C-17."

        def finish_conversation(self, message):
            assistant = {"role": "assistant", "content": message}
            self.messages.append(assistant)
            self._simulator_response(assistant, "###STOP###")
            self.user_stopped = True

        def score(self):
            self.judge_model_call(
                {
                    "name": "tau_official_nl_judge",
                    "payload": {"model": self.judge_model, "messages": self.conversation},
                }
            )
            return module.TaskScore(
                success=True,
                forbidden_side_effects=None,
                benchmark_score=1.0,
                official=True,
                user_simulator_cost_usd=Decimal("0.006"),
                user_simulator_requests=3,
                user_simulator_tokens=18,
                judge_cost_usd=Decimal("0.002"),
                judge_requests=1,
                judge_tokens=6,
            )

    session = ConversationSession()
    context = trial(session)
    observed_requests = []

    def model(value):
        return lambda _request: module.ModelReply(
            value,
            tokens=6,
            cost_usd=Decimal("0.002"),
            model="qwen3.8-flash",
            input_tokens=4,
            output_tokens=2,
        )

    def react(request):
        observed_requests.append(deepcopy(request))
        if len(observed_requests) == 1:
            return reply({"ask_user": "Which case should I look up?"})
        if len(observed_requests) == 2:
            assert any(
                item.get("content") == "The case is C-17."
                for item in request["conversation"]
            )
            return reply({"tool": "directory", "arguments": {}})
        assert request["observations"][-1]["data"] == {
            "people": [],
            "categories": [],
        }
        return reply({"finish": True, "message": "I found the case."})

    result = module.ControlledRoutes(factory(session)).run(
        Route.REACT_SKILL_MCP,
        context,
        react=react,
        user_model="qwen3.8-flash",
        user_simulator=model({"content": "simulated user turn"}),
        judge_model="qwen3.8-flash",
        official_judge=model({"content": "criteria satisfied"}),
    )

    assert result.status == "complete"
    assert result.run.task_success is True
    assert result.run.benchmark_score == 1.0
    assert len(observed_requests) == 3
    assert result.run.user_simulator_requests == 3
    assert result.run.judge_requests == 1
    assert result.usage.user_simulator_requests == 3
    assert result.usage.judge_requests == 1
    assert result.tau_steps == 7
    assert session.tau_step_events == [
        "user", "agent", "user", "agent", "environment", "agent", "user"
    ]


def test_tau_step_limit_blocks_tool_dispatch_and_retains_exact_step_count():
    module = api()

    class BoundedTauSession(Session):
        supports_user_simulation = True

        def bind_tau_step_recorder(self, recorder):
            self.tau_step_recorder = recorder
            self.tau_step_events = []

        def record_tau_step(self, kind):
            self.tau_step_events.append(kind)
            return self.tau_step_recorder(kind)

        def configure_conversation(
            self, *, user_model, user_model_call, judge_model, judge_model_call
        ):
            self.user_model = user_model
            self.user_model_call = user_model_call
            self.judge_model = judge_model
            self.judge_model_call = judge_model_call
            self.user_stopped = False

        @property
        def conversation(self):
            return []

        def start_conversation(self):
            self.record_tau_step("user")
            self.user_model_call({"name": "tau_user_simulator", "payload": {}})
            return {"role": "user", "content": "Find a case"}

        def score(self):
            self.judge_model_call({"name": "tau_official_nl_judge", "payload": {}})
            return module.TaskScore(
                success=False,
                forbidden_side_effects=None,
                benchmark_score=0.0,
                official=True,
                user_simulator_requests=1,
                user_simulator_tokens=2,
                judge_requests=1,
                judge_tokens=2,
            )

    session = BoundedTauSession()
    context = trial(session)

    def measured(value):
        return module.ModelReply(
            value,
            tokens=2,
            cost_usd=Decimal("0.001"),
            model="qwen3.8-flash",
            input_tokens=1,
            output_tokens=1,
        )

    result = module.ControlledRoutes(
        factory(session), max_tau_steps=2
    ).run(
        Route.REACT_SKILL_MCP,
        context,
        react=lambda _request: measured({"tool": "directory", "arguments": {}}),
        user_model="qwen3.8-flash",
        user_simulator=lambda _request: measured({"content": "Find a case"}),
        judge_model="qwen3.8-flash",
        official_judge=lambda _request: measured({"content": "failed"}),
    )

    assert result.status == "stopped"
    assert result.failure_kind == "tau_step_limit"
    assert result.terminal["reason"] == "τ official conversation-step limit exhausted"
    assert result.tau_steps == 2
    assert session.tau_step_events == ["user", "agent", "environment"]
    assert session.calls == []
    assert result.usage.primary_requests == 1


def test_route_latency_excludes_environment_setup_private_scoring_and_cleanup(monkeypatch):
    module = api()
    clock = [0.0]
    monkeypatch.setattr(module, "perf_counter", lambda: clock[0])
    session = Session()
    context = trial(session)

    def score():
        clock[0] += 13.0
        return module.TaskScore(success=True, forbidden_side_effects=0)

    session.score = score

    @contextmanager
    def environment(_trial, isolation_id):
        clock[0] += 7.0
        session.isolation_id = isolation_id
        try:
            yield session
        finally:
            session.closed = True
            clock[0] += 11.0

    def react(_request):
        clock[0] += 5.0
        return reply({"finish": True})

    result = module.ControlledRoutes(environment).run(Route.REACT_SKILL_MCP, context, react=react)
    assert result.status == "complete"
    assert result.run.machine_seconds == 5.0
    assert result.timing.agent_started is True
    assert result.timing.environment_setup_seconds == 7.0
    assert result.timing.agent_wall_seconds == 5.0
    assert result.timing.private_scoring_seconds == 13.0
    assert result.timing.cleanup_seconds == 11.0
    assert result.timing.total_wall_seconds == 36.0


@pytest.mark.parametrize(
    "field",
    [
        "initial_state_digest",
        "actor_profile_digest",
        "permission_digest",
        "tool_adapter_digest",
        "goal_digest",
    ],
)
def test_preflight_rejects_boundary_drift_before_models_or_tools(field):
    session = Session(drift=field)
    session.score = lambda: pytest.fail("A mismatched environment must not be scored")
    result = execute(session)
    assert result.status == "failed" and field in result.error
    assert not session.calls and not session.model_inputs
    assert result.run is None and result.score is None
    assert result.failure_kind == "environment_failure"
    assert result.usage.primary_requests == result.usage.jev_requests == 0


def test_failed_reset_never_dispatches_and_closes_environment():
    session = Session(reset_ok=False)
    result = execute(session)
    assert result.status == "failed" and "reset" in result.error.lower()
    assert not session.calls and not session.model_inputs and session.closed
    assert result.timing.agent_started is False


def test_invalid_tool_schema_is_rejected_before_dispatch_and_counted_as_failed_trial():
    session = Session()
    session.plan["nodes"][0]["inputs"] = {"employee_id": literal("someone-else")}
    result = execute(session)
    assert result.status == "failed" and not result.run.task_success
    assert not session.calls and result.run.primary_requests == 1


def test_react_reasons_after_each_observation_and_uses_same_gateway():
    session = Session()

    def react(request):
        session.model_inputs.append(deepcopy(request))
        if not request["observations"]:
            return reply({"tool": "directory", "arguments": {}})
        assert request["observations"][0]["data"] == {"people": [], "categories": []}
        assert request["skill"] == session.skill
        return reply({"finish": True})

    result = execute(session, route=Route.REACT_SKILL_MCP, react=react)
    assert result.status == "complete" and result.run.task_success
    assert result.run.primary_requests == 2 and len(session.calls) == 1
    assert not session.repositories


def test_plan_control_step_bound_is_separate_from_react_turn_bound():
    session = Session()
    runner = api().ControlledRoutes(
        factory(session), max_plan_control_steps=1, max_react_turns=2
    )

    result = runner.run(Route.PLAN_MCP, trial(session), planner=planner(session))

    assert result.status == "stopped"
    assert result.terminal["reason"] == "Plan control-step bound exhausted"
    assert session.calls == [("directory", {})]
    assert result.usage.primary_requests == 1


def test_react_turn_bound_is_independent_of_plan_control_steps():
    session = Session()
    runner = api().ControlledRoutes(
        factory(session), max_plan_control_steps=1, max_react_turns=2
    )
    requests = []

    def react(request):
        requests.append(request)
        if not request["observations"]:
            return reply({"tool": "directory", "arguments": {}})
        return reply({"finish": True})

    result = runner.run(Route.REACT_SKILL_MCP, trial(session), react=react)

    assert result.status == "complete"
    assert len(requests) == result.usage.primary_requests == 2
    assert len(session.calls) == 1


def test_react_cannot_dispatch_unknown_or_program_owned_tool_arguments():
    session = Session()
    result = execute(
        session,
        route=Route.REACT_SKILL_MCP,
        react=lambda request: reply({"tool": "directory", "arguments": {"employee_id": "other"}}),
    )
    assert result.status == "failed" and not session.calls
    assert result.run.primary_requests == 1


def test_hot_workflow_performs_validation_without_primary_call_and_freezes_memory():
    session = Session()
    session.memory_fixture = {
        "ignored": {
            "data": "predeclared",
            "verified": True,
            "active": True,
            "expired": False,
            "evidence_id": "frozen-source",
        }
    }
    result = execute(session, context=trial(session, workflow_mode="hot", memory_mode="verified"))
    assert result.status == "complete" and result.run.primary_requests == 0
    assert result.reused_workflow and result.preparation_seconds >= 0
    assert session.repositories[0].context["task_input"] == session.task_input


def test_model_error_keeps_attempt_and_unknown_billing_instead_of_reporting_free_run():
    session = Session()

    def broken(request):
        raise TimeoutError("upstream timeout")

    result = (
        api().ControlledRoutes(factory(session)).run(Route.PLAN_MCP, trial(session), planner=broken)
    )
    assert result.status == "failed" and "TimeoutError" in result.error
    assert result.run is None and result.usage.primary_requests == 1
    assert not result.usage.complete and not session.calls


def test_tool_error_does_not_trigger_a_second_plan_or_synthetic_success():
    session = Session()

    def broken(command, params):
        session.calls.append((command, params))
        raise RuntimeError("tool unavailable")

    session.call = broken
    result = execute(session)
    assert result.status == "stopped" and not result.run.task_success
    assert result.run.primary_requests == 1 and len(result.tool_calls) == 1
    assert result.tool_calls[0].error == "RuntimeError"


def semantic_plan():
    plan = read_plan()
    registry = QuestionRegistry.from_spec(PluginSpec.from_mapping(builtin_manifest()))
    question = registry.get("operation")
    plan["nodes"][0]["next"] = ["decision"]
    plan["nodes"].insert(
        1,
        node(
            "decision",
            "decide",
            depends_on=["directory"],
            question={k: question[k] for k in ("question_id", "version", "type")},
            policy={"policy_id": "isolated-evaluation", "version": "1"},
            inputs={
                "instruction": reference("task_input", path=["instruction"]),
                "directory": reference("node_output", node_id="directory", value_type="object"),
            },
            next=["branch"],
            on_no_match={"stop": "no_match"},
        ),
    )
    plan["nodes"].insert(
        2,
        node(
            "branch",
            "branch",
            depends_on=["decision"],
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": reference(
                            "accepted_decision", node_id="decision", path=["selected_value"]
                        ),
                        "right": literal("directory"),
                    },
                    "next": ["done"],
                }
            ],
            on_no_match={"stop": "no_match"},
        ),
    )
    return plan


def test_semantic_jev_and_primary_share_exact_plan_question_state_and_candidates():
    inputs = []
    results = []
    for route in (Route.PLAN_JEV_SEMANTIC, Route.PLAN_PRIMARY_SEMANTIC):
        session = Session(plan=semantic_plan())

        def semantic(request):
            inputs.append(deepcopy(request))
            probabilities = {name: 0.0 for name in request["question"]["criteria"]}
            probabilities["directory"] = 0.99
            probabilities["no_match"] = 0.01
            return reply(
                {
                    "type": "choice",
                    "choice": "directory",
                    "confidence": 0.99,
                    "probabilities": probabilities,
                }
            )

        results.append(
            execute(
                session,
                route=route,
                context=trial(session, branch_kind="semantic"),
                semantic=semantic,
            )
        )
    assert all(result.status == "complete" for result in results)
    assert inputs[0] == inputs[1]
    assert results[0].run.jev_requests == 1 and results[1].run.jev_requests == 0
    assert compare_semantic_judgment(
        results[0].run, results[1].run
    ).decision_input_digest == digest(inputs[0])


def test_semantic_plan_node_counts_as_a_tau_agent_step():
    session = Session(plan=semantic_plan())
    session.supports_user_simulation = True
    session.tau_step_events = []
    session.record_tau_step = lambda kind: session.tau_step_events.append(kind)
    route = api().ControlledRoutes(factory(session))
    decisions = []
    node = next(item for item in session.plan["nodes"] if item["id"] == "decision")
    context = SimpleNamespace(
        employee_id="employee", scope_revision=1, account_revision=1
    )

    handler = route._handler(
        session,
        trial(session, branch_kind="semantic"),
        session,
        SimpleNamespace(
            call=lambda _role, callback, request: callback(request).value
        ),
        Route.PLAN_JEV_SEMANTIC,
        lambda request: reply(
            {
                "type": "choice",
                "choice": "directory",
                "confidence": 0.99,
                "probabilities": {
                    **{name: 0.0 for name in request["question"]["criteria"]},
                    "directory": 0.99,
                    "no_match": 0.01,
                },
            }
        ),
        decisions,
        None,
    )

    handler(
        node,
        {"instruction": "Find the matching employee", "directory": {"people": []}},
        context,
        {},
    )

    assert session.tau_step_events == ["agent"]


def test_unregistered_semantic_answer_stops_before_following_any_nearest_branch():
    session = Session(plan=semantic_plan())
    result = execute(
        session,
        route=Route.PLAN_JEV_SEMANTIC,
        context=trial(session, branch_kind="semantic"),
        semantic=lambda request: reply({"selected_value": "invented", "choice": "invented"}),
    )
    assert result.status == "stopped" and not result.run.task_success
    assert session.calls == [("directory", {})]
    assert "branch" not in session.repositories[0].rows


def test_structured_trials_refuse_semantic_route_before_any_dispatch():
    session = Session()
    result = execute(session, route=Route.PLAN_JEV_SEMANTIC)
    assert result.status == "failed" and not session.calls and not session.model_inputs


def chained_plan(*, parameter="query"):
    plan = read_plan()
    plan["nodes"][0]["next"] = ["branch"]
    plan["nodes"].insert(
        1,
        node(
            "branch",
            "branch",
            depends_on=["directory"],
            cases=[
                {
                    "condition": {
                        "op": "eq",
                        "left": reference(
                            "node_output", node_id="directory", path=["categories", 0, "name"]
                        ),
                        "right": literal("network"),
                    },
                    "next": ["filtered"],
                }
            ],
            on_no_match={"stop": "no_match"},
        ),
    )
    plan["nodes"].insert(
        2,
        node(
            "filtered",
            "read_tool",
            depends_on=["directory"],
            tool={"plugin_id": "service-tickets", "command": "directory"},
            inputs={
                parameter: reference(
                    "node_output", node_id="directory", path=["categories", 0, "name"]
                )
            },
            next=["done"],
        ),
    )
    plan["nodes"][-1]["depends_on"] = ["filtered"]
    plan["goal"]["evidence"][0]["source"]["node_id"] = "filtered"
    return plan


def directory_session(*, parameter="query", name="network"):
    session = Session(plan=chained_plan(parameter=parameter))

    def call(command, params):
        session.calls.append((command, deepcopy(params)))
        return {
            "people": [],
            "categories": [{"id": "00000000-0000-4000-a000-000000000001", "name": name}],
        }

    session.call = call
    return session


def test_real_output_flows_through_code_branch_into_next_tool_input_and_dispatch_journal():
    session = directory_session()
    result = execute(session)
    assert result.status == "complete" and result.run.task_success
    assert session.calls == [("directory", {}), ("directory", {"query": "network"})]
    assert result.run.primary_requests == 1 and result.run.jev_requests == 0
    assert session.repositories[0].rows["directory"]["dispatch_attempts"] == 1
    assert session.repositories[0].rows["filtered"]["dispatch_attempts"] == 1


def test_runtime_schema_rejects_bad_bound_enum_before_second_tool_call():
    session = directory_session(parameter="kind")
    result = execute(session)
    assert result.status == "stopped" and not result.run.task_success
    assert session.calls == [("directory", {})]
    assert session.repositories[0].rows["filtered"]["status"] == "failed"


def test_out_of_plan_structured_branch_stops_without_replanning_or_extra_model():
    session = directory_session(name="uncovered")
    result = execute(session)
    assert result.status == "stopped" and result.terminal["reason"] == "no_match"
    assert result.run.primary_requests == 1 and not result.run.task_success
    assert session.calls == [("directory", {})]


def test_code_rule_route_runs_supplied_plan_without_any_model():
    session = directory_session()
    result = execute(session, route=Route.CODE_RULE_MCP, frozen_plan=session.plan)
    assert result.status == "complete"
    assert result.run.primary_requests == result.run.jev_requests == 0


def test_model_known_failed_usage_is_retained_and_can_be_reported():
    session = Session()

    def broken(request):
        raise api().ModelCallError("provider rejected request", reply=reply(None))

    result = (
        api().ControlledRoutes(factory(session)).run(Route.PLAN_MCP, trial(session), planner=broken)
    )
    assert result.status == "failed" and not result.run.task_success
    assert result.run.primary_requests == 1 and result.run.primary_tokens == 17
    assert result.run.primary_cost_usd == Decimal("0.002")


def test_memory_ablation_copies_explicit_fixture_and_does_not_learn_across_runs():
    session = Session()
    slot = memory_slot_key("confirmed_preference", "response_language")
    session.memory_fixture = {
        slot: {
            "data": "fixture",
            "kind": "confirmed_preference",
            "subject": "response_language",
            "employee_id": "employee",
            "scope_revision": 1,
            "account_revision": 1,
            "verified": True,
            "active": True,
            "expired": False,
            "evidence_id": "frozen-source",
        }
    }
    session.plan["nodes"][0]["inputs"] = {"query": reference("verified_memory")}
    runner = api().ControlledRoutes(factory(session))
    seen = []

    def call(command, params):
        seen.append(deepcopy(params))
        params["query"] = "must not survive"
        return {"people": [], "categories": []}

    session.call = call

    results = [
        runner.run(Route.PLAN_MCP, trial(session, memory_mode=mode, workflow_mode="hot"))
        for mode in ("verified", "disabled", "verified")
    ]
    assert [result.status for result in results] == ["complete", "stopped", "complete"]
    assert not results[1].run.task_success
    assert len({result.isolation_id for result in results}) == 3
    assert seen == [{"query": "fixture"}, {"query": "fixture"}]
    assert session.memory_fixture[slot]["data"] == "fixture"
    assert all(result.run.primary_requests == 0 for result in results)


def test_catalog_questions_and_actual_model_versions_are_in_execution_evidence():
    session = Session()
    result = execute(session)
    assert (
        session.model_inputs[0]["registry_fingerprint"]
        == QuestionRegistry.from_spec(session.spec).fingerprint
    )
    assert session.model_inputs[0]["questions"]
    assert result.usage.actual_models == [("primary", "scripted-model")]


def test_hot_reuse_failure_never_falls_back_to_paid_planning():
    session = Session()
    session.validate_workflow = lambda plan: False
    result = execute(session, context=trial(session, workflow_mode="hot"))
    assert result.status == "failed" and result.run.primary_requests == 0
    assert not session.calls and not session.model_inputs


def test_semantic_gate_wait_does_not_grant_write_authority_or_consume_branch():
    session = Session(plan=semantic_plan())

    def gate(*args):
        raise NodePending({"kind": "decision", "reason": "Unverified policy requires employee"})

    session.accept_decision = gate

    def semantic(request):
        probabilities = {name: 0.0 for name in request["question"]["criteria"]}
        probabilities["directory"] = 1.0
        return reply(
            {
                "type": "choice",
                "choice": "directory",
                "confidence": 1.0,
                "probabilities": probabilities,
            }
        )

    result = execute(
        session,
        route=Route.PLAN_JEV_SEMANTIC,
        context=trial(session, branch_kind="semantic"),
        semantic=semantic,
    )
    assert result.status == "waiting" and not result.run.task_success
    assert result.terminal["request"]["kind"] == "decision"
    assert "branch" not in session.repositories[0].rows


def test_typed_task_input_schema_is_used_by_compiler_and_production_runtime():
    session = Session()
    session.input_schema = {
        "type": "object",
        "properties": {"instruction": {"type": "string"}, "query": {"type": "string"}},
        "required": ["instruction", "query"],
        "additionalProperties": False,
    }
    session.task_input["query"] = "network"
    session.plan["nodes"][0]["inputs"] = {"query": reference("task_input", path=["query"])}
    result = execute(session)
    assert result.status == "complete" and session.calls == [("directory", {"query": "network"})]


def test_react_rejects_write_schema_before_calling_governance_adapter():
    session = Session()
    governed = []
    session.handle_react_write = lambda *args: governed.append(args)
    result = execute(
        session,
        route=Route.REACT_SKILL_MCP,
        react=lambda request: reply({"tool": "create", "arguments": {}}),
    )
    assert result.status == "failed" and not governed and not session.calls


def test_snapshot_is_rechecked_after_repository_publication_before_first_tool():
    session = Session()
    publish = session.publish_plan

    def changed(plan, task_input):
        repository = publish(plan, task_input)
        session.drift = "initial_state_digest"
        return repository

    session.publish_plan = changed
    result = execute(session)
    assert result.status == "failed" and "initial_state_digest" in result.error
    assert not session.calls and result.run.primary_requests == 1


def test_frozen_plan_does_not_bypass_hot_workflow_validation():
    session = Session()
    session.validate_workflow = lambda plan: False
    result = execute(session, frozen_plan=session.plan, context=trial(session, workflow_mode="hot"))
    assert result.status == "failed" and not session.calls


def test_invalid_memory_fixture_is_rejected_before_planner_dispatch():
    session = Session()
    session.memory_fixture = {"frozen": {"data": "unchecked"}}
    result = execute(session, context=trial(session, memory_mode="verified"))
    assert result.status == "failed" and not session.model_inputs


def test_simulator_calls_and_tokens_are_retained_separately_from_agent_attempts():
    session = Session()
    result = execute(session)
    assert result.score.user_simulator_requests == 1
    assert result.score.user_simulator_tokens == 200
    assert result.run.primary_requests == 1 and result.run.primary_tokens == 17
    attempt = result.usage.attempts[0]
    assert attempt.role == "primary" and attempt.model == "scripted-model"
    assert attempt.seconds >= 0 and attempt.error is None
    assert attempt.tokens == 17 and attempt.cost_usd == Decimal("0.002")


def test_unknown_failed_attempt_has_latency_but_no_fabricated_tokens_or_cost():
    session = Session()

    def broken(request):
        raise TimeoutError("no usage")

    result = (
        api().ControlledRoutes(factory(session)).run(Route.PLAN_MCP, trial(session), planner=broken)
    )
    attempt = result.usage.attempts[0]
    assert attempt.role == "primary" and attempt.error == "TimeoutError"
    assert attempt.seconds >= 0 and attempt.tokens is None and attempt.cost_usd is None
    assert result.run is None


@pytest.mark.parametrize("route", [Route.PLAN_MCP, Route.REACT_SKILL_MCP])
def test_generic_planner_and_react_never_receive_cross_task_memory(route):
    session = Session()
    session.memory_fixture = {
        "frozen": {
            "data": "private-frozen-preference",
            "verified": True,
            "active": True,
            "expired": False,
            "evidence_id": "private-memory-evidence",
        }
    }

    def react(request):
        session.model_inputs.append(deepcopy(request))
        return reply({"finish": True})

    result = execute(
        session, route=route, context=trial(session, memory_mode="verified"), react=react
    )
    assert result.status == "complete"
    assert session.model_inputs
    assert all("memories" not in request for request in session.model_inputs)
    assert "private-frozen-preference" not in json.dumps(session.model_inputs)


def test_cold_plan_cannot_bind_cross_task_memory_without_a_published_workflow():
    session = Session()
    slot = memory_slot_key("confirmed_preference", "response_language")
    session.memory_fixture = {
        slot: {
            "data": "private-preference",
            "kind": "confirmed_preference",
            "subject": "response_language",
            "employee_id": "employee",
            "scope_revision": 1,
            "account_revision": 1,
            "verified": True,
            "active": True,
            "expired": False,
            "evidence_id": "frozen-source",
        }
    }
    session.plan["nodes"][0]["inputs"] = {"query": reference("verified_memory")}
    result = execute(session, context=trial(session, memory_mode="verified"))
    assert result.status == "failed" and "published workflow" in result.error.lower()
    assert not session.calls and result.run.primary_requests == 1


@pytest.mark.parametrize(
    "route, workflow_mode",
    [(Route.PLAN_MCP, "cold"), (Route.PLAN_MCP, "hot"), (Route.REACT_SKILL_MCP, "cold")],
)
def test_changed_resolved_memory_fixture_digest_blocks_before_model_or_tool(route, workflow_mode):
    session = Session()
    session.score = lambda: pytest.fail("A mismatched memory fixture must not be scored")
    session.memory_fixture = {
        "frozen": {
            "data": "original",
            "verified": True,
            "active": True,
            "expired": False,
            "evidence_id": "frozen-source",
        }
    }
    context = trial(session, memory_mode="verified", workflow_mode=workflow_mode)
    session.memory_fixture["frozen"]["data"] = "different restored memory"
    result = execute(
        session, route=route, context=context, react=lambda request: reply({"finish": True})
    )
    assert result.status == "failed" and "memory_fixture_digest" in result.error
    assert not session.model_inputs and not session.calls
    assert result.run is None and result.score is None
    assert result.failure_kind == "environment_failure"
    assert result.usage.primary_requests == result.usage.jev_requests == 0


def test_cold_frozen_plan_is_execution_only_and_cannot_be_full_workflow_baseline():
    session = Session()
    result = execute(session, frozen_plan=session.plan)
    assert result.status == "complete" and result.run.primary_requests == 0
    assert result.run.evidence_scope == "execution_only"
    baseline = execute(
        Session(), route=Route.REACT_SKILL_MCP, react=lambda request: reply({"finish": True})
    )
    with pytest.raises(ValueError, match="(?i:scope|execution)"):
        compare_full_workflow(baseline.run, result.run)


def test_hot_frozen_plan_is_full_workflow_only_after_published_validation():
    session = Session()
    validated = []

    def validate(plan):
        validated.append(plan.digest)
        return True

    session.validate_workflow = validate
    result = execute(session, frozen_plan=session.plan, context=trial(session, workflow_mode="hot"))
    assert result.status == "complete" and result.reused_workflow
    assert result.run.evidence_scope == "full_workflow"
    assert validated == [result.run.plan_digest]


def test_environment_entry_failure_has_no_reportable_or_fabricated_verifier_result():
    session = Session()

    class Unavailable:
        def __enter__(self):
            raise RuntimeError("isolated environment unavailable")

        def __exit__(self, *args):
            pass

    result = api().ControlledRoutes(lambda *args: Unavailable()).run(Route.PLAN_MCP, trial(session))
    assert result.status == "failed" and "environment unavailable" in result.error
    assert result.run is None and result.score is None
    assert result.usage.primary_requests == result.usage.jev_requests == 0


@pytest.mark.parametrize("invalid", ["raises", "not_a_score"])
def test_failed_or_invalid_verifier_cannot_create_reportable_cost_or_side_effects(invalid):
    session = Session()

    def score():
        if invalid == "raises":
            raise RuntimeError("private verifier failed")
        return {"success": True}

    session.score = score
    result = execute(session)
    assert result.status == "failed" and result.run is None and result.score is None
    assert result.usage.primary_requests == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_react_preserves_governed_pending_request_without_replanning_or_retry(unknown):
    session = Session()
    request = {
        "kind": "reconciliation" if unknown else "action",
        "action_id": "original-action",
        "approval_id": "approval-1",
        "reason": "Original operation awaits authenticated response",
    }
    governed = []

    def write(command, params, gateway):
        governed.append((command, deepcopy(params)))
        raise NodePending(request, unknown=unknown)

    session.handle_react_write = write

    def react(payload):
        session.model_inputs.append(deepcopy(payload))
        return reply(
            {
                "tool": "create",
                "arguments": {
                    "title": "pending original operation",
                    "category_id": "00000000-0000-4000-a000-000000000001",
                },
            }
        )

    result = execute(session, route=Route.REACT_SKILL_MCP, react=react)
    assert result.status == "waiting" and result.terminal["request"] == request
    assert result.terminal.get("unknown", False) is unknown
    assert len(governed) == len(session.model_inputs) == 1
    assert not session.calls and not result.tool_calls
    if unknown:
        assert result.run is None
    else:
        assert result.run.primary_requests == 1 and not result.run.task_success


def test_scorer_exception_is_reported_separately_from_agent_execution():
    session = Session()

    def broken_score():
        raise RuntimeError("fixture scorer unavailable")

    session.score = broken_score
    result = execute(session)

    assert result.status == "failed"
    assert result.failure_kind == "scoring_failure"
    assert result.run is None
    assert result.timing.agent_started is True
    assert result.timing.private_scoring_seconds > 0
    assert "Scoring failed: RuntimeError" in result.error


def test_official_task_failure_is_not_misclassified_as_scoring_failure():
    session = Session()
    session.score = lambda: api().TaskScore(
        success=False,
        forbidden_side_effects=None,
    )

    result = execute(session)

    assert result.status == "complete"
    assert result.failure_kind == "task_failure"
    assert result.run is not None
    assert result.run.task_success is False
    assert result.run.forbidden_side_effects is None


def test_environment_setup_failure_is_not_misclassified_as_task_failure():
    module = api()
    session = Session()
    context = trial(session)

    def broken_environment(_trial, _isolation_id):
        raise RuntimeError("fixture environment unavailable")

    result = module.ControlledRoutes(broken_environment).run(
        Route.PLAN_MCP,
        context,
        planner=planner(session),
    )

    assert result.status == "failed"
    assert result.failure_kind == "environment_failure"
    assert result.run is None
    assert result.timing.agent_started is False


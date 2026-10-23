"""Public benchmark sessions drive the production interpreter without a model."""

import json
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import ComparisonTrial, Route
from decagent.evaluation.business.public_session import EvaluationPlanSession
from decagent.evaluation.business.routes import ControlledRoutes, TaskScore, TrialBoundary
from evals.deterministic.test_business_evaluation_catalog import catalog_plan, tool_catalog


def write_plan_for(catalog):
    from decagent.business.plans.contracts import Plan

    value = catalog_plan(catalog).to_mapping()
    value.pop("content_digest", None)
    value["plan_id"] = "public-write"
    value["nodes"][0]["type"] = "governed_write"
    value["nodes"][0]["command"] = value["nodes"][0].pop("tool")
    value["nodes"][0]["command"]["command"] = "update_case"
    return Plan.from_mapping(value)


class UpstreamEnvironment:
    def __init__(self, *, fail_write=False):
        self.fail_write = fail_write
        self.calls = []
        # The scripted upstream fixture is resource-free and has no-op teardown.
        self.cleanup_verified = True

    def call_tool(self, command, arguments):
        self.calls.append((command, arguments))
        if self.fail_write:
            raise TimeoutError("simulated lost upstream response")
        return {
            "content": [{"type": "text", "text": "case found"}],
            "structuredContent": {"case_id": "C-17"},
        }


def trial_for(facts):
    return ComparisonTrial(
        dataset="tau-bench",
        dataset_version="96a3ef9",
        dataset_checksum=digest("tau-retail-test-v1"),
        case_id="retail-17",
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )


def test_public_session_binds_and_tightens_route_deadline():
    session = object.__new__(EvaluationPlanSession)
    session._closed = False
    session.spec = SimpleNamespace(assert_active=lambda: None)
    bound = []
    session.environment = SimpleNamespace(set_deadline_provider=bound.append)
    session._deadline_provider = None
    def provider():
        return 2.5

    session.bind_deadline_provider(provider)

    assert bound == [provider]
    assert session._request_timeout(4.0) == 2.5
    assert session._request_timeout() == 2.5


def test_tau_user_tool_batch_is_executed_before_one_followup_simulator_turn():
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    class BatchEnvironment:
        def __init__(self):
            self.turn_inputs = []
            self.tool_calls = []

        def user_turn(self, message, *, model, on_model_call):
            self.turn_inputs.append(deepcopy(message))
            if len(self.turn_inputs) == 1:
                return {
                    "message": {
                        "role": "user",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "user-call-1",
                                "name": "lookup_order",
                                "arguments": {"order_id": "A-1"},
                                "requestor": "user",
                            },
                            {
                                "id": "user-call-2",
                                "name": "lookup_shipment",
                                "arguments": {"order_id": "A-1"},
                                "requestor": "user",
                            },
                        ],
                    },
                    "stop": False,
                }
            return {
                "message": {"role": "user", "content": "Both lookups are complete."},
                "stop": False,
            }

        def call_user_tool(self, request):
            self.tool_calls.append((request.id, request.name, request.arguments))
            return {
                "id": request.id,
                "role": "tool",
                "requestor": "user",
                "content": request.name + " result",
                "error": False,
            }

        def score_simulation(self, _trajectory, *, judge_model, on_model_call):
            raise AssertionError("Scoring is not part of this simulator-turn test")

    environment = BatchEnvironment()
    session = object.__new__(EvaluationPlanSession)
    session.environment = environment
    session._closed = False
    session.spec = SimpleNamespace(assert_active=lambda: None)
    session._user_model = "qwen-test"
    session._user_model_call = lambda _event: {"content": "unused"}
    session._trajectory = []
    session._deadline_provider = None
    session._tau_steps = 0
    tau_step_events = []
    session._tau_step_recorder = lambda kind: tau_step_events.append(kind) or True
    session._user_stopped = False
    session._termination_reason = "agent_stop"

    result = session._send_to_user({"role": "assistant", "content": "Hello"})

    assert [item[0] for item in environment.tool_calls] == ["user-call-1", "user-call-2"]
    assert len(environment.turn_inputs) == 2
    assert environment.turn_inputs[1] == {
        "role": "tool",
        "requestor": "user",
        "tool_messages": [
            {
                "id": "user-call-1",
                "role": "tool",
                "requestor": "user",
                "content": "lookup_order result",
                "error": False,
            },
            {
                "id": "user-call-2",
                "role": "tool",
                "requestor": "user",
                "content": "lookup_shipment result",
                "error": False,
            },
        ],
    }
    assert session._trajectory[1] == environment.turn_inputs[1]
    assert result == {"role": "user", "content": "Both lookups are complete."}
    assert tau_step_events == ["user", "environment", "user"]
    assert session.tau_steps == 3


def test_public_catalog_plan_executes_with_plan_runtime_and_official_score_stays_separate(tmp_path):
    facts = {
        "initial_state_digest": digest("initial state"),
        "goal_digest": digest("public instruction"),
        "actor_profile_digest": digest("simulated actor"),
        "permission_digest": digest("tool permissions"),
        "tool_adapter_digest": digest("pinned tools"),
    }
    trial = trial_for(facts)
    plan = None
    sessions = []

    @contextmanager
    def factory(frozen, isolation_id):
        with EvaluationScope(frozen.dataset, frozen.dataset_version, frozen.case_id) as scope:
            environment = UpstreamEnvironment()
            catalog = EvaluationCatalog.from_mcp_tools(
                scope,
                tool_catalog(),
                benchmark_version="96a3ef9",
                result_contract_version="1",
            )
            session = EvaluationPlanSession(
                environment,
                catalog,
                trial=frozen,
                isolation_id=isolation_id,
                journal_root=tmp_path,
                actor_id="eval:retail-17",
                task_input={"instruction": "Look up case C-17"},
                skill="Use only the original benchmark tools.",
                boundary_facts=facts,
                scorer=lambda _environment: TaskScore(
                    success=False,
                    forbidden_side_effects=0,
                ),
            )
            sessions.append(session)
            try:
                yield session
            finally:
                session.close()

    # The production compiler supplies the stable fingerprint for this catalog.
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        plan = catalog_plan(catalog)

    result = ControlledRoutes(factory).run(Route.PLAN_MCP, trial, frozen_plan=plan)
    assert result.status == "complete"
    assert result.run.evidence_scope == "execution_only"
    assert result.run.task_success is False
    assert result.terminal["completion"]["business_goal_verified"] is False
    assert sessions[0].environment.calls == [("lookup_case", {"user_id": "u-17"})]
    journal_path = sessions[0].journal_path
    assert journal_path.is_file()
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        from decagent.evaluation.business.journal import EvaluationJournalRepository

        resumed = EvaluationJournalRepository.resume(
            journal_path, catalog, actor_id="eval:retail-17"
        )
        assert resumed.current().digest == plan.digest
        assert resumed.nodes()[0]["status"] == "succeeded"
        resumed.close()


def test_unknown_upstream_plan_write_waits_and_is_never_replayed(tmp_path):
    facts = {
        "initial_state_digest": digest("initial state"),
        "goal_digest": digest("public instruction"),
        "actor_profile_digest": digest("simulated actor"),
        "permission_digest": digest("tool permissions"),
        "tool_adapter_digest": digest("pinned tools"),
    }
    trial = trial_for(facts)
    write_tool = tool_catalog()[0]
    write_tool["name"] = "update_case"
    write_tool["annotations"] = {}
    calls = []

    @contextmanager
    def factory(frozen, isolation_id):
        with EvaluationScope(frozen.dataset, frozen.dataset_version, frozen.case_id) as scope:
            catalog = EvaluationCatalog.from_mcp_tools(
                scope,
                [write_tool],
                benchmark_version="96a3ef9",
                result_contract_version="1",
            )
            plan = write_plan_for(catalog)
            environment = UpstreamEnvironment(fail_write=True)
            session = EvaluationPlanSession(
                environment,
                catalog,
                trial=frozen,
                isolation_id=isolation_id,
                journal_root=tmp_path,
                actor_id="eval:retail-17",
                task_input={"instruction": "Update case C-17"},
                skill="Use only the original benchmark tools.",
                boundary_facts=facts,
                scorer=lambda _environment: TaskScore(success=False, forbidden_side_effects=0),
            )
            calls.append((session, plan, session.journal_path))
            try:
                yield session
            finally:
                session.close()

    # Compute the plan against the same trusted catalog fingerprint without opening a run.
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [write_tool],
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        plan = write_plan_for(catalog)

    result = ControlledRoutes(factory).run(Route.PLAN_MCP, trial, frozen_plan=plan)
    assert result.status == "waiting", result.error
    assert result.terminal["unknown"] is True
    assert calls[0][0].environment.calls == [("update_case", {"user_id": "u-17"})]
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [write_tool],
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        from decagent.evaluation.business.journal import EvaluationJournalRepository

        resumed = EvaluationJournalRepository.resume(
            calls[0][2], catalog, actor_id="eval:retail-17"
        )
        rows = resumed.nodes()
        assert rows[0]["status"] == "unknown"
        assert rows[0]["dispatch_attempts"] == 1
        resumed.close()


def test_invalid_upstream_output_is_tagged_without_persisting_schema_details(tmp_path):
    from decagent.business.plans.runtime import NodeFailure
    from decagent.evaluation.business.public_diagnostics import (
        safe_chained_exception_type,
        safe_failure_stage,
    )

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
    trial = trial_for(facts)
    tools = tool_catalog()
    tools[0]["outputSchema"] = {
        "type": "object",
        "properties": {"case_id": {"type": "string"}},
        "required": ["case_id"],
    }

    class InvalidOutputEnvironment:
        cleanup_verified = True

        def call_tool(self, _command, _arguments):
            return {
                "content": [{"type": "text", "text": '{"case_id": 17}'}],
                "isError": False,
            }

    with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope, tools, benchmark_version="96a3ef9", result_contract_version="1"
        )
        session = EvaluationPlanSession(
            InvalidOutputEnvironment(),
            catalog,
            trial=trial,
            isolation_id="invalid-output-diagnostic",
            journal_root=tmp_path,
            actor_id="eval:retail-17",
            task_input={"instruction": "Look up case C-17"},
            skill="Use only the original benchmark tools.",
            boundary_facts=facts,
            scorer=lambda _environment: TaskScore(False, 0),
        )
        try:
            with pytest.raises(NodeFailure) as captured:
                session.call("lookup_case", {"user_id": "u-17"})

            assert safe_failure_stage(captured.value, default="tool_gateway") == (
                "tool_output_validation"
            )
            assert safe_chained_exception_type(captured.value) == "ValidationError"
            assert "case_id" not in repr(
                (
                    safe_failure_stage(captured.value, default="tool_gateway"),
                    safe_chained_exception_type(captured.value),
                )
            )
        finally:
            session.close()


def test_react_trajectory_receives_the_same_schema_normalized_tool_value(tmp_path):
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
    trial = trial_for(facts)
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "type": "object",
        "properties": {"active": {"type": "boolean"}},
        "required": ["active"],
        "additionalProperties": False,
    }

    class BooleanBitEnvironment:
        def call_tool(self, _command, _arguments):
            return {
                "content": [{"type": "text", "text": '{"active":1}'}],
                "isError": False,
            }

    with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version=trial.dataset_version,
            result_contract_version="enterprise-mcp-observation-v2",
        )
        session = EvaluationPlanSession(
            BooleanBitEnvironment(),
            catalog,
            trial=trial,
            isolation_id="normalized-react-result",
            journal_root=tmp_path,
            actor_id="eval:retail-17",
            task_input={"instruction": "Read the account status."},
            skill="Use only the original benchmark tools.",
            boundary_facts=facts,
            scorer=lambda _environment: TaskScore(False, 0),
        )
        session._conversation_started = 1.0
        try:
            observed = session.call("lookup_case", {"user_id": "u-17"})
            message = session._trajectory[-1]
        finally:
            session.close()

    assert observed["raw"]["content"][0]["text"] == '{"active":1}'
    assert observed["value"]["active"] is True
    assert observed["normalizations"] == ["/active"]
    assert json.loads(message["content"]) == {"active": True}


def test_unexpected_observer_runtime_error_keeps_generic_tool_stage(tmp_path):
    from decagent.business.plans.runtime import NodeFailure
    from decagent.evaluation.business.public_diagnostics import (
        safe_chained_exception_type,
        safe_failure_stage,
    )

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
    trial = trial_for(facts)

    class Environment:
        cleanup_verified = True

        def call_tool(self, _command, _arguments):
            return {"content": [], "isError": False}

    with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope, tool_catalog(), benchmark_version="96a3ef9", result_contract_version="1"
        )

        def unexpected_observer_failure(_command, _raw):
            raise RuntimeError("private implementation failure")

        catalog.observe = unexpected_observer_failure
        session = EvaluationPlanSession(
            Environment(),
            catalog,
            trial=trial,
            isolation_id="unexpected-observer-failure",
            journal_root=tmp_path,
            actor_id="eval:retail-17",
            task_input={"instruction": "Look up case C-17"},
            skill="Use only the original benchmark tools.",
            boundary_facts=facts,
            scorer=lambda _environment: TaskScore(False, 0),
        )
        try:
            with pytest.raises(NodeFailure) as captured:
                session.call("lookup_case", {"user_id": "u-17"})

            assert safe_failure_stage(captured.value, default="tool_gateway") == (
                "tool_gateway"
            )
            assert safe_chained_exception_type(captured.value) == "RuntimeError"
        finally:
            session.close()


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("route", [Route.PLAN_MCP, Route.REACT_SKILL_MCP])
def test_known_upstream_tool_error_is_durable_and_preserves_original_recovery(
    tmp_path, write, route
):
    from decagent.evaluation.business.routes import ModelReply

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
    trial = trial_for(facts)
    tools = tool_catalog()
    if write:
        tools[0].update(name="update_case", annotations={})
    command = tools[0]["name"]
    original_error = {
        "content": [{"type": "text", "text": "Error: case not found"}],
        "isError": True,
    }
    sessions = []

    @contextmanager
    def factory(frozen, isolation_id):
        with EvaluationScope(frozen.dataset, frozen.dataset_version, frozen.case_id) as scope:
            catalog = EvaluationCatalog.from_mcp_tools(
                scope, tools, benchmark_version="96a3ef9", result_contract_version="1"
            )

            class Environment:
                def __init__(self):
                    self.calls = 0
                    self.cleanup_verified = True

                def call_tool(self, name, arguments):
                    assert (name, arguments) == (command, {"user_id": "u-17"})
                    self.calls += 1
                    if self.calls == 1:
                        return deepcopy(original_error)
                    return {
                        "content": [{"type": "text", "text": '{"case_id":"C-17"}'}],
                        "isError": False,
                    }

            session = EvaluationPlanSession(
                Environment(),
                catalog,
                trial=frozen,
                isolation_id=isolation_id,
                journal_root=tmp_path,
                actor_id="eval:retail-17",
                task_input={"instruction": "Find case C-17"},
                skill="Use only original benchmark tools.",
                boundary_facts=facts,
                scorer=lambda _environment: TaskScore(False, 0),
            )
            sessions.append(session)
            try:
                yield session
            finally:
                session.close()

    with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope, tools, benchmark_version="96a3ef9", result_contract_version="1"
        )
        plan = write_plan_for(catalog) if write else catalog_plan(catalog)

    def react(request):
        if request["observations"]:
            assert request["observations"][0]["data"]["raw"] == original_error
            assert request["observations"][0]["data"]["is_error"] is True
            value = (
                {"finish": True}
                if len(request["observations"]) == 2
                else {"tool": command, "arguments": {"user_id": "u-17"}}
            )
        else:
            value = {"tool": command, "arguments": {"user_id": "u-17"}}
        return ModelReply(value, 0, Decimal("0"), "scripted-error-recovery")

    kwargs = {"react": react} if route == Route.REACT_SKILL_MCP else {"frozen_plan": plan}
    result = ControlledRoutes(factory).run(route, trial, **kwargs)
    assert result.status == ("complete" if route == Route.REACT_SKILL_MCP else "stopped")
    assert result.run.task_success is False
    assert not result.terminal.get("unknown")

    with EvaluationScope(trial.dataset, trial.dataset_version, trial.case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope, tools, benchmark_version="96a3ef9", result_contract_version="1"
        )
        from decagent.evaluation.business.journal import EvaluationJournalRepository

        resumed = EvaluationJournalRepository.resume(
            sessions[0].journal_path, catalog, actor_id="eval:retail-17"
        )
        try:
            calls = resumed.external_calls()
            assert len(calls) == (2 if route == Route.REACT_SKILL_MCP else 1)
            assert calls[0]["status"] == "failed"
            assert calls[0]["result_snapshot"]["raw"] == original_error
            if route == Route.REACT_SKILL_MCP:
                assert calls[1]["status"] == "succeeded"
            assert not resumed.has_unknown_external_write()
        finally:
            resumed.close()


def test_enterprise_session_factory_fingerprints_catalog_before_building_session(tmp_path):
    tools = tool_catalog()
    spec = SimpleNamespace(
        task_id="csm-case-17",
        domain="csm",
        image="example/enterpriseops@sha256:" + "a" * 64,
        platform="linux/amd64",
        snapshot_sha256="b" * 64,
        context={"x-user-id": "employee-17"},
        selected_tools=("lookup_case",),
        restricted_tools=(),
    )

    class Environment:
        def __init__(self):
            self.spec = spec

        def model_input(self):
            return {
                "system_prompt": "Use only the selected MCP tools.",
                "user_prompt": "Look up case C-17.",
                "tools": tools,
            }

        def state_fingerprint(self):
            return {
                "digest": digest("live enterprise state"),
                "table_count": 1,
                "row_count": 1,
            }

    with EvaluationScope("enterpriseops-gym", spec.image, spec.task_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tools,
            benchmark_version=spec.image,
            result_contract_version="enterprise-mcp-observation-v2",
        )
        facts = {
            "initial_state_digest": digest("live enterprise state"),
            "goal_digest": digest(
                {
                    "system_prompt": "Use only the selected MCP tools.",
                    "user_prompt": "Look up case C-17.",
                }
            ),
            "actor_profile_digest": digest({"context_identity": spec.context}),
            "permission_digest": digest(
                {
                    "selected_tools": list(spec.selected_tools),
                    "restricted_tools": [],
                    "effective_tools": ["lookup_case"],
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
        trial = ComparisonTrial(
            dataset="enterpriseops-gym",
            dataset_version=spec.image,
            dataset_checksum=digest("enterpriseops-pinned-fixture"),
            case_id=spec.task_id,
            branch_kind="structured",
            memory_mode="disabled",
            workflow_mode="cold",
            **facts,
        )
        session = EvaluationPlanSession.from_enterprise(
            Environment(),
            scope,
            trial,
            "isolated-csm-17",
            journal_root=tmp_path,
            scorer=lambda _environment: TaskScore(False, 0),
        )
        try:
            assert session.spec.digest == catalog.digest
            assert session.boundary() == TrialBoundary(**facts)
            assert session.spec.raw_tools == tools
            assert session.journal_path.is_file()
        finally:
            session.close()


def test_tau_factory_executes_original_tool_through_mcp_and_production_plan_runtime(tmp_path):
    from decagent.business.plans.contracts import Plan
    from decagent.business.plans.templates import QuestionRegistry
    from decagent.evaluation.business.public_session import tau_session_factory
    from decagent.evaluation.business.tau_tools import TauMCPEnvironment
    from evals.deterministic.test_business_tau_tools import ContractEnvironment

    version = "tau-contract-revision-1"
    case_id = "retail-contract-17"
    task_input = {"instruction": "Look up the public case."}
    skill = "Use only the original benchmark tools."
    fresh_environments = []

    def new_environment(_trial):
        result = ContractEnvironment()
        fresh_environments.append(result)
        return result

    def boundary(environment, trial):
        public = environment.model_input()
        return {
            "initial_state_digest": digest({"case": trial.case_id, "state": "fresh"}),
            "goal_digest": digest({"task": task_input, "skill": skill}),
            "actor_profile_digest": digest({"simulated_user": trial.case_id}),
            "permission_digest": digest({"tools": public["tools"]}),
            "tool_adapter_digest": digest({"tau_version": version, "tools": public["tools"]}),
        }

    with TauMCPEnvironment(
        lambda: ContractEnvironment(),
        system_prompt=skill,
        user_prompt=task_input["instruction"],
    ) as preview:
        preview_public = preview.model_input()
        with EvaluationScope("tau-bench", version, case_id) as scope:
            catalog = EvaluationCatalog.from_mcp_tools(
                scope,
                preview_public["tools"],
                benchmark_version=version,
                result_contract_version="tau2-mcp-observation-v1",
            )
            plan = Plan.from_mapping(
                {
                    "schema_version": "1",
                    "plan_id": "tau-contract-plan",
                    "revision": 1,
                    "registry_fingerprint": QuestionRegistry.from_spec(catalog).fingerprint,
                    "entry": "fetch",
                    "goal": {
                        "description": "Observe the original tau2 tool response.",
                        "evidence": [
                            {
                                "id": "result",
                                "kind": "tool_result",
                                "description": "The original tool returned a public result.",
                                "source": {
                                    "source": "node_output",
                                    "node_id": "fetch",
                                    "path": ["value", "number"],
                                    "value_type": "string",
                                    "nullable": False,
                                    "provenance": "tool_result",
                                },
                            }
                        ],
                    },
                    "nodes": [
                        {
                            "id": "fetch",
                            "depends_on": [],
                            "on_missing": {"stop": "missing_input"},
                            "on_error": {"stop": "error"},
                            "type": "governed_write",
                            "command": {"plugin_id": catalog.id, "command": "original"},
                            "inputs": {
                                "value": {
                                    "source": "literal",
                                    "value": 7,
                                    "value_type": "integer",
                                    "nullable": False,
                                    "provenance": "plan_literal",
                                }
                            },
                            "next": ["done"],
                        },
                        {
                            "id": "done",
                            "depends_on": ["fetch"],
                            "on_missing": {"stop": "missing_input"},
                            "on_error": {"stop": "error"},
                            "type": "complete",
                            "evidence": ["result"],
                        },
                    ],
                }
            )

    facts = boundary(
        SimpleNamespace(model_input=lambda: preview_public),
        SimpleNamespace(case_id=case_id),
    )
    trial = ComparisonTrial(
        dataset="tau-bench",
        dataset_version=version,
        dataset_checksum=digest("tau-contract-data"),
        case_id=case_id,
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )
    factory = tau_session_factory(
        new_environment,
        dataset_version=version,
        tool_catalog_version=version,
        journal_root=tmp_path,
        task_input_factory=lambda _trial: task_input,
        skill_factory=lambda _trial: skill,
        boundary_factory=boundary,
        scorer_factory=lambda environment, _trial: TaskScore(
            bool(environment.requests), forbidden_side_effects=0
        ),
    )

    result = ControlledRoutes(factory).run(Route.PLAN_MCP, trial, frozen_plan=plan)

    assert result.status == "complete", result.error
    assert result.run.evidence_scope == "execution_only"
    assert result.run.task_success is True
    assert result.terminal["completion"]["business_goal_verified"] is False
    assert len(result.tool_calls) == 1
    assert len(fresh_environments) == 1
    assert fresh_environments[0].tools.calls == [("original", {"value": 7})]


def test_tau_route_runs_official_user_conversation_and_scorer_through_measured_callbacks(tmp_path):
    from decagent.business.plans.contracts import Plan
    from decagent.evaluation.business.public_session import tau_session_factory
    from evals.deterministic.test_business_tau_tools import ContractEnvironment

    version = "tau-conversation-revision-1"
    case_id = "retail-conversation-17"
    task_input = {"instruction": "Handle this simulated support conversation."}
    skill = "Use the original benchmark tools and ask for missing details."
    fresh_environments = []

    class ConversationEnvironment(ContractEnvironment):
        def __init__(self):
            super().__init__()
            self.trajectory = []
            self.user_turns = 0
            self.tools.tool_mutates_state = lambda _name: False
            self.response = SimpleNamespace(content='{"case_id": "C-17"}', error=False)

        def get_tools(self):
            return [
                SimpleNamespace(
                    openai_schema={
                        "type": "function",
                        "function": {
                            "name": "lookup_case",
                            "description": "Look up a case for the supplied benchmark user.",
                            "parameters": tool_catalog()[0]["inputSchema"],
                        },
                    }
                )
            ]

        def user_turn(self, message, *, model, on_model_call):
            self.trajectory.append(deepcopy(message))
            reply = on_model_call(
                {
                    "name": "tau_user_simulator",
                    "payload": {"model": model, "messages": deepcopy(self.trajectory)},
                }
            )
            self.user_turns += 1
            content = [
                "Please look up my account.",
                "The account is 12345.",
                "###STOP###",
            ][self.user_turns - 1]
            result = {
                "message": {"role": "user", "content": content},
                "stop": content == "###STOP###",
                "usage": {
                    "requests": 1,
                    "input_tokens": reply["tau_usage"]["prompt_tokens"],
                    "output_tokens": reply["tau_usage"]["completion_tokens"],
                    "cost_usd": reply["cost_usd"],
                },
            }
            self.trajectory.append(deepcopy(result["message"]))
            return result

        def call_user_tool(self, request):
            return {
                "id": request.id,
                "role": "tool",
                "requestor": "user",
                "content": "user tool result",
                "error": False,
            }

        def score_simulation(
            self, *, messages, termination_reason, duration, judge_model, on_model_call
        ):
            self.scored_messages = deepcopy(messages)
            self.scored_termination_reason = termination_reason
            self.scored_duration = duration
            reply = on_model_call(
                {
                    "name": "tau_official_nl_judge",
                    "payload": {"model": judge_model, "messages": deepcopy(messages)},
                }
            )
            assert reply["content"] == "criteria satisfied"
            return {
                "scored": True,
                "scorer": "tau2-bench-original-evaluate-simulation",
                "reward": 1.0,
                "judge_usage": {
                    "requests": 1,
                    "input_tokens": reply["tau_usage"]["prompt_tokens"],
                    "output_tokens": reply["tau_usage"]["completion_tokens"],
                    "cost_usd": reply["cost_usd"],
                },
            }

    def new_environment(_trial):
        result = ConversationEnvironment()
        fresh_environments.append(result)
        return result

    def boundary(environment, trial):
        public = environment.model_input()
        return {
            "initial_state_digest": digest({"case": trial.case_id, "state": "fresh"}),
            "goal_digest": digest({"task": task_input, "skill": skill}),
            "actor_profile_digest": digest({"simulated_user": trial.case_id}),
            "permission_digest": digest({"tools": public["tools"]}),
            "tool_adapter_digest": digest({"tau_version": version, "tools": public["tools"]}),
        }

    with EvaluationScope("tau-bench", version, case_id) as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version=version,
            result_contract_version="tau2-mcp-observation-v1",
        )
        plan_value = catalog_plan(catalog).to_mapping()
        plan_value.pop("content_digest", None)
        plan_value["plan_id"] = "tau-conversation-plan"
        plan_value["entry"] = "clarify"
        plan_value["goal"]["description"] = "Find the account identifier."
        plan_value["nodes"][0]["depends_on"] = ["clarify"]
        plan_value["nodes"][0]["inputs"]["user_id"] = {
            "source": "node_output",
            "node_id": "clarify",
            "path": [],
            "value_type": "string",
            "nullable": False,
            "provenance": "user_answer",
        }
        plan_value["nodes"].insert(
            0,
            {
                    "id": "clarify",
                    "depends_on": [],
                    "on_missing": {"stop": "missing_input"},
                    "on_error": {"stop": "error"},
                    "type": "ask_user",
                    "prompt": "What is your account identifier?",
                    "answer": {"value_type": "string", "nullable": False},
                    "next": ["lookup"],
            },
        )
        plan = Plan.from_mapping(plan_value)

    facts = boundary(
        SimpleNamespace(model_input=lambda: {"tools": tool_catalog()}),
        SimpleNamespace(case_id=case_id),
    )
    trial = ComparisonTrial(
        dataset="tau-bench",
        dataset_version=version,
        dataset_checksum=digest("tau-conversation-data"),
        case_id=case_id,
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )
    factory = tau_session_factory(
        new_environment,
        dataset_version=version,
        tool_catalog_version=version,
        journal_root=tmp_path,
        task_input_factory=lambda _trial: task_input,
        skill_factory=lambda _trial: skill,
        boundary_factory=boundary,
        scorer_factory=lambda _environment, _trial: pytest.fail(
            "A τ session must use the pinned official evaluator"
        ),
    )

    def measured_model(value):
        from decagent.evaluation.business.routes import ModelReply

        return lambda request: ModelReply(
            value(request),
            tokens=6,
            cost_usd=Decimal("0.002"),
            model="qwen3.8-flash",
            input_tokens=4,
            output_tokens=2,
        )

    result = ControlledRoutes(factory).run(
        Route.PLAN_MCP,
        trial,
        frozen_plan=plan,
        user_model="qwen3.8-flash",
        user_simulator=measured_model(lambda _request: {"content": "simulated turn"}),
        judge_model="qwen3.8-flash",
        official_judge=measured_model(lambda _request: {"content": "criteria satisfied"}),
    )

    assert result.status == "complete", result.error
    assert result.run.task_success is True
    assert result.run.benchmark_score == 1.0
    assert result.run.user_simulator_requests == 3
    assert result.run.user_simulator_tokens == 18
    assert result.run.user_simulator_cost_usd == Decimal("0.006")
    assert result.run.judge_requests == 1
    assert result.run.judge_tokens == 6
    assert result.run.judge_cost_usd == Decimal("0.002")
    assert result.usage.user_simulator_requests == 3
    assert result.usage.judge_requests == 1
    transcript = fresh_environments[0].scored_messages
    assert transcript[0] == {"role": "assistant", "content": "Hi! How can I help you today?"}
    assert any(item.get("content") == "Please look up my account." for item in transcript)
    assert any(item.get("content") == "What is your account identifier?" for item in transcript)
    assert any(item.get("content") == "The account is 12345." for item in transcript)
    assert fresh_environments[0].scored_termination_reason == "user_stop"

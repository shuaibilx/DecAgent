from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
from decagent.evaluation.business.comparison import Route
from decagent.evaluation.business.public_resources import LedgeredModelAdapterFactory
from decagent.evaluation.business.public_trials import PublicTrialLedger, load_public_run_plan
from decagent.evaluation.business.routes import ModelCallError
from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
from evals.deterministic.test_business_evaluation_catalog import catalog_plan, tool_catalog

ROOT = Path(__file__).resolve().parents[2]


class ScriptedPrimary:
    def __init__(self, turn):
        self.turn = turn
        self.calls = []
        self.closed = False
        self.schema_bytes = 512

    def respond(self, messages, *, timeout_seconds=None):
        self.calls.append((messages, timeout_seconds))
        return self.turn

    def close(self):
        self.closed = True


@contextmanager
def _catalog():
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        yield EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )


def _profile():
    return load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    ).profiles.primary


def _request():
    return {
        "task_input": {"instruction": "Look up case C-17"},
        "skill": "Use only the original benchmark tools.",
        "tools": {"lookup_case": {"effect": "read"}},
    }


def _valid_plan_turn(catalog, profile):
    plan = catalog_plan(catalog).to_mapping()
    return ModelTurn(
        "",
        (ToolProposal("call-1", "decagent_submit_plan", plan),),
        ModelUsage(20, 5, 25),
        profile.model,
        "qwen-request-1",
    )


def test_public_planner_callback_builds_scoped_prompt_and_compiles_one_response():
    from decagent.evaluation.business.public_models import PublicPlannerCallback

    with _catalog() as catalog:
        profile = _profile()
        primary = ScriptedPrimary(_valid_plan_turn(catalog, profile))
        callback = PublicPlannerCallback(
            primary,
            catalog,
            input_schema={
                "type": "object",
                "properties": {"instruction": {"type": "string"}},
                "required": ["instruction"],
                "additionalProperties": False,
            },
            profile=profile,
        )

        reply = callback(_request(), timeout_seconds=3.5)

        assert reply.value == catalog_plan(catalog).to_mapping()
        assert reply.input_tokens == 20 and reply.output_tokens == 5
        assert reply.cost_usd is None and reply.cost_source == "unknown"
        assert reply.provider == profile.provider
        assert reply.provider_request_id == "qwen-request-1"
        assert len(primary.calls) == 1
        messages, timeout = primary.calls[0]
        assert timeout == 3.5
        assert "registered commands" in messages[0]["content"]
        assert "Look up case C-17" in messages[1]["content"]
        assert "Use only the original benchmark tools." in messages[1]["content"]


def test_public_planner_factory_uses_default_input_schema_when_session_has_none():
    from decagent.evaluation.business.public_models import public_planner_callback_factory

    with _catalog() as catalog:
        profile = _profile()
        primary = ScriptedPrimary(None)
        factory = public_planner_callback_factory(
            profile,
            api_key="offline-test-key",
            primary_factory=lambda _profile, **_kwargs: primary,
        )
        session = SimpleNamespace(spec=catalog, input_schema=None)

        callback = factory(session, None, Route.PLAN_MCP)
        messages = callback._messages(_request())

        assert len(messages) == 2
        assert "Look up case C-17" in messages[1]["content"]
        assert primary.calls == []


def test_public_planner_callback_rejects_invalid_plan_without_hidden_retry():
    from decagent.evaluation.business.public_models import PublicPlannerCallback

    with _catalog() as catalog:
        profile = _profile()
        invalid = ModelTurn(
            "",
            (ToolProposal("call-1", "decagent_submit_plan", {"nodes": []}),),
            ModelUsage(20, 5, 25),
            profile.model,
            "qwen-request-invalid",
        )
        primary = ScriptedPrimary(invalid)
        callback = PublicPlannerCallback(
            primary, catalog, input_schema={"type": "object"}, profile=profile
        )

        with pytest.raises(ModelCallError) as error:
            callback(_request(), timeout_seconds=3.5)

        assert len(primary.calls) == 1
        assert error.value.reply is not None
        assert error.value.reply.tokens == 25
        assert error.value.reply.provider_request_id == "qwen-request-invalid"


def test_public_react_callback_preserves_tool_contract_and_returns_one_action():
    from decagent.evaluation.business.public_models import PublicReactCallback

    with _catalog() as catalog:
        profile = _profile()
        turn = ModelTurn(
            "",
            (ToolProposal("call-1", "lookup_case", {"user_id": "u-17"}),),
            ModelUsage(11, 4, 15),
            profile.model,
            "qwen-react-1",
        )
        primary = ScriptedPrimary(turn)
        callback = PublicReactCallback(primary, catalog, profile=profile)
        request = {
            **_request(),
            "observations": [],
        }

        reply = callback(request, timeout_seconds=2.0)

        assert reply.value == {"tool": "lookup_case", "arguments": {"user_id": "u-17"}}
        assert reply.input_tokens == 11 and reply.output_tokens == 4
        assert len(primary.calls) == 1
        assert primary.calls[0][1] == 2.0


def test_public_react_callback_parses_only_bounded_finish_or_clarification_actions():
    from decagent.evaluation.business.public_models import PublicReactCallback

    with _catalog() as catalog:
        profile = _profile()
        primary = ScriptedPrimary(
            ModelTurn(
                '{"finish":true,"message":"Found case C-17."}',
                (),
                ModelUsage(9, 6, 15),
                profile.model,
                "qwen-react-2",
            )
        )
        callback = PublicReactCallback(primary, catalog, profile=profile)

        reply = callback({**_request(), "observations": []}, timeout_seconds=1.0)

        assert reply.value == {"finish": True, "message": "Found case C-17."}


def test_public_tau_simulator_callback_binds_official_user_tool_schema_and_usage():
    from decagent.evaluation.business.public_models import public_tau_model_callback_factory

    profile = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    ).profiles.user_simulator
    turn = ModelTurn(
        "",
        (ToolProposal("user-call-1", "lookup_order", {"order_id": "A-17"}),),
        ModelUsage(14, 3, 17),
        profile.model,
        "qwen-user-1",
    )
    primary = ScriptedPrimary(turn)
    definitions = []

    def primary_factory(got_profile, **kwargs):
        definitions.append((got_profile, kwargs))
        return primary

    callback = public_tau_model_callback_factory(
        profile,
        role="user_simulator",
        api_key="test-secret",
        known_secrets=("test-secret",),
        primary_factory=primary_factory,
    )(SimpleNamespace(), None, None)
    request = {
        "model": profile.model,
        "messages": [{"role": "system", "content": "Simulate the customer."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Look up the customer's order.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
    }

    assert callback.input_token_upper_bound(request) > len("Simulate the customer.")
    reply = callback(request, timeout_seconds=1.5)

    assert reply.value == {
        "content": "",
        "tool_calls": [
            {"id": "user-call-1", "name": "lookup_order", "arguments": {"order_id": "A-17"}}
        ],
    }
    assert reply.input_tokens == 14 and reply.output_tokens == 3
    assert reply.provider_request_id == "qwen-user-1"
    assert definitions[0][0] == profile
    assert definitions[0][1]["tool_definitions"] == request["tools"]
    callback.close()
    assert primary.closed


def test_public_tau_simulator_accepts_frozen_telecom_catalog_of_thirty_tools():
    from decagent.evaluation.business.public_models import public_tau_model_callback_factory

    profile = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    ).profiles.user_simulator
    primary = ScriptedPrimary(None)
    callback = public_tau_model_callback_factory(
        profile,
        role="user_simulator",
        api_key="offline-test-key",
        primary_factory=lambda _profile, **_kwargs: primary,
    )(SimpleNamespace(), None, None)
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"telecom_tool_{index}",
                "description": "A bounded frozen telecom simulator tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(30)
    ]
    request = {
        "model": profile.model,
        "messages": [{"role": "system", "content": "Simulate the customer."}],
        "tools": tools,
    }

    assert callback.input_token_upper_bound(request) <= profile.max_input_tokens
    assert primary.calls == []
    request["tools"].extend(
        {
            "type": "function",
            "function": {
                "name": f"telecom_tool_{index}",
                "description": "A bounded frozen telecom simulator tool.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(30, 33)
    )
    with pytest.raises(ValueError, match="frozen request bound"):
        callback.input_token_upper_bound(request)
    assert primary.calls == []


def test_public_tau_judge_requires_text_and_rejects_tool_calls():
    from decagent.evaluation.business.public_models import public_tau_model_callback_factory

    profile = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    ).profiles.judge
    primary = ScriptedPrimary(
        ModelTurn(
            "",
            (),
            ModelUsage(10, 0, 10),
            profile.model,
            "qwen-judge-1",
        )
    )
    callback = public_tau_model_callback_factory(
        profile,
        role="judge",
        api_key="test-secret",
        primary_factory=lambda _profile, **_kwargs: primary,
    )(SimpleNamespace(), None, None)

    reply = callback(
        {
            "model": profile.model,
            "messages": [{"role": "user", "content": "Evaluate."}],
            "tools": [],
        },
        timeout_seconds=1.0,
    )

    assert reply.value == {"content": "", "tool_calls": []}
    assert reply.tokens == 10


def test_first_wave_model_adapter_wires_agent_and_tau_roles_without_fallback(tmp_path):
    from decagent.evaluation.business.public_models import (
        public_first_wave_model_adapter_factory,
    )

    run_plan = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    )
    ledger = PublicTrialLedger.create(tmp_path / "public.sqlite3", run_plan)
    trial_id = run_plan.trials[0]["trial_id"]
    trial = SimpleNamespace(**run_plan.profiles.digests)
    created = []

    def primary_factory(profile, **kwargs):
        primary = ScriptedPrimary(ModelTurn("{}", (), ModelUsage(1, 1, 2), profile.model))
        created.append((profile.role, kwargs, primary))
        return primary

    settings = SimpleNamespace(
        main_api_key="qwen-key",
        jev_api_key="jev-key",
        resolved_fallback_api_key="fallback-key",
        fallback_enabled=True,
    )
    factory = public_first_wave_model_adapter_factory(
        settings,
        ledger,
        run_plan.profiles,
        trial_id_for=lambda _trial: trial_id,
        primary_factory=primary_factory,
    )

    with _catalog() as catalog:
        route_adapters = factory(
            SimpleNamespace(
                spec=catalog,
                input_schema={"type": "object"},
                supports_user_simulation=False,
            ),
            trial,
            Route.REACT_SKILL_MCP,
        )
        assert set(route_adapters) == {"react"}
        assert factory.callback_factories.keys() == {
            "planner",
            "react",
            "user_simulator",
            "official_judge",
        }
        assert factory.input_token_upper_bounds == {
            "planner": None,
            "react": None,
            "user_simulator": None,
            "official_judge": None,
        }
        route_adapters.close()
    assert created[0][0] == "primary"
    assert created[0][1]["api_key"] == "qwen-key"
    assert created[0][1]["known_secrets"] == ("qwen-key", "jev-key", "fallback-key")
    assert created[0][2].closed
    ledger.close()


def test_ledgered_adapter_uses_callback_exact_prompt_bound_and_closes_client(tmp_path):
    from decagent.evaluation.business.public_models import public_planner_callback_factory

    run_plan = load_public_run_plan(
        ROOT, mode="smoke", check_images=False, check_runtime=False, check_source_files=False
    )
    ledger = PublicTrialLedger.create(tmp_path / "public.sqlite3", run_plan)
    trial_record = run_plan.trials[0]
    trial = SimpleNamespace(**run_plan.profiles.digests)
    trial_id = trial_record["trial_id"]
    route = Route.PLAN_MCP
    ledger.start_route(trial_id, route.value)
    reservation = ledger.reserve_machine_time(
        trial_id=trial_id, route=route.value, max_seconds=30
    )
    ledger.mark_machine_time_dispatched(reservation)
    ledger.mark_machine_time_agent_started(reservation)

    with _catalog() as catalog:
        profile = run_plan.profiles.primary
        primary = ScriptedPrimary(_valid_plan_turn(catalog, profile))
        constructed = []

        def primary_factory(got_profile, **kwargs):
            constructed.append((got_profile, kwargs))
            return primary

        callback_factory = public_planner_callback_factory(
            profile,
            api_key="test-secret",
            known_secrets=("test-secret",),
            primary_factory=primary_factory,
        )
        session = SimpleNamespace(
            spec=catalog,
            input_schema={
                "type": "object",
                "properties": {"instruction": {"type": "string"}},
                "required": ["instruction"],
                "additionalProperties": False,
            },
            supports_user_simulation=False,
        )
        adapters = LedgeredModelAdapterFactory(
            {"planner": callback_factory},
            {"planner": None},
            ledger,
            run_plan.profiles,
            trial_id_for=lambda _trial: trial_id,
        )(session, trial, route)

        callback = adapters["planner"]
        expected_bound = callback.callback.input_token_upper_bound(_request())
        reply = callback(_request(), timeout_seconds=4)

        assert reply.value["plan_id"] == "public-case"
        attempt = ledger._connection.execute(
            "SELECT attempt_id,estimated_input_tokens,status FROM model_attempts"
        ).fetchone()
        assert attempt["estimated_input_tokens"] == expected_bound
        assert attempt["status"] == "settled"
        assert constructed[0][0] == profile
        assert constructed[0][1]["api_key"] == "test-secret"
        adapters.close()
        assert primary.closed
    ledger.close()

"""Evidence-only final responses and explicitly measured summary fallbacks."""

import importlib.util
import json
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal

import pytest


def api():
    assert importlib.util.find_spec("decagent.evaluation.business.final_response"), (
        "The evidence-grounded final response contract is missing"
    )
    from decagent.evaluation.business import final_response

    return final_response


def evidence():
    return [
        {
            "call_id": "call-1",
            "tool": "lookup",
            "status": "succeeded",
            "observation": {"content": [{"type": "text", "text": "Found case C-17."}]},
        }
    ]


def test_deterministic_response_preserves_actual_evidence_without_model():
    module = api()
    result = module.prepare_response(
        module.FinalResponsePolicy(),
        evidence(),
        task_input={"instruction": "Find my case"},
        skill="Public policy",
    )
    assert result["needs_summary"] is False
    assert json.loads(result["content"])["observed_tool_results"] == evidence()
    assert result["source_call_ids"] == ["call-1"]
    assert "request" not in result


def test_narrative_fallback_uses_only_public_task_and_real_observations():
    module = api()
    policy = module.FinalResponsePolicy(mode="narrative")
    result = module.prepare_response(
        policy,
        evidence(),
        task_input={"instruction": "Explain the case"},
        skill="Public policy",
    )
    assert result["needs_summary"] is True
    assert result["request"]["observations"] == evidence()
    assert result["request"]["task_input"] == {"instruction": "Explain the case"}
    accepted = module.accept_summary(
        policy,
        result,
        {
            "content": "Found case C-17.",
            "source_call_ids": ["call-1"],
        },
    )
    assert accepted["content"] == "Found case C-17."
    with pytest.raises(ValueError, match="source"):
        module.accept_summary(
            policy,
            result,
            {
                "content": "Invented case",
                "source_call_ids": ["not-observed"],
            },
        )


def test_final_response_policy_accepts_a_frozen_deepseek_summary_cohort():
    module = api()
    policy = module.FinalResponsePolicy(
        mode="narrative", summary_model="deepseek/deepseek-v4.1-flash"
    )
    prepared = module.prepare_response(
        policy, evidence(), task_input={"instruction": "Explain the case"}, skill="Policy"
    )

    assert prepared["needs_summary"] is True
    assert prepared["mode"] == "model_summary"


def test_large_evidence_requests_one_summary_but_never_truncates_input():
    module = api()
    policy = module.FinalResponsePolicy(max_output_bytes=512, max_input_bytes=4096)
    observed = evidence()
    observed[0]["observation"] = {"text": "x" * 700}
    prepared = module.prepare_response(policy, observed, task_input={}, skill="Policy")
    assert prepared["needs_summary"]
    assert prepared["request"]["observations"] == observed
    observed[0]["observation"] = {"text": "x" * 8000}
    with pytest.raises(ValueError, match="input budget"):
        module.prepare_response(policy, observed, task_input={}, skill="Policy")


@pytest.mark.parametrize("status", ["unknown", "dispatching"])
def test_unresolved_tool_outcome_cannot_be_rendered_or_summarized(status):
    module = api()
    observed = evidence()
    observed[0]["status"] = status
    with pytest.raises(ValueError, match="unresolved"):
        module.prepare_response(
            module.FinalResponsePolicy(), observed, task_input={}, skill="Policy"
        )


def run_plan(tmp_path, *, policy=None, summarizer=None, drift=False):
    from decagent.evaluation.business.comparison import Route
    from decagent.evaluation.business.public_session import EvaluationPlanSession
    from decagent.evaluation.business.routes import ControlledRoutes
    from evals.deterministic.test_business_enterprise_scoring import enterprise_session_fixture
    from evals.deterministic.test_business_evaluation_catalog import catalog_plan

    module = api()
    policy = policy or module.FinalResponsePolicy()
    environment, scope, trial, scorer, engine = enterprise_session_fixture(tmp_path)
    trial = replace(trial, response_policy_digest=policy.fingerprint)
    records = []

    @contextmanager
    def factory(frozen, isolation):
        session = EvaluationPlanSession.from_enterprise(
            environment,
            scope,
            frozen,
            isolation,
            journal_root=tmp_path,
            scorer=scorer,
        )
        records.append(session)
        try:
            yield session
        finally:
            session.close()

    from decagent.business.plans.evaluation_catalog import EvaluationCatalog

    catalog = EvaluationCatalog.from_mcp_tools(
        scope,
        environment.model_input()["tools"],
        benchmark_version=environment.spec.image,
        result_contract_version="enterprise-mcp-observation-v1",
    )
    try:
        result = ControlledRoutes(factory).run(
            Route.PLAN_MCP,
            trial,
            frozen_plan=catalog_plan(catalog),
            summarizer=summarizer,
            response_policy=module.FinalResponsePolicy() if drift else policy,
        )
        return result, records, engine
    finally:
        scope.__exit__(None, None, None)


def test_plan_sends_deterministic_actual_response_to_verifier_with_zero_primary(tmp_path):
    result, _, engine = run_plan(
        tmp_path,
        summarizer=lambda _request: pytest.fail("Deterministic output needs no model"),
    )
    assert result.status == "complete", result.error
    assert result.usage.primary_requests == 0
    response = engine.calls[0][1]["content"]
    assert "case found" in response and "observed_tool_results" in response
    assert result.terminal["final_response"]["content"] == response
    assert result.terminal["final_response"]["source_call_ids"]


def test_narrative_summary_is_called_once_and_counted_as_primary(tmp_path):
    from decagent.evaluation.business.routes import ModelReply

    requests = []

    def summarize(request):
        requests.append(request)
        assert "verifier" not in repr(request) and "expected_value" not in repr(request)
        return ModelReply(
            {
                "content": "The case was found.",
                "source_call_ids": [
                    request["observations"][0]["call_id"],
                ],
            },
            tokens=12,
            cost_usd=Decimal("0.003"),
            model="qwen3.8-flash",
        )

    result, _, engine = run_plan(
        tmp_path,
        policy=api().FinalResponsePolicy(mode="narrative"),
        summarizer=summarize,
    )
    assert result.status == "complete", result.error
    assert len(requests) == result.usage.primary_requests == result.run.primary_requests == 1
    assert result.run.primary_tokens == 12 and result.run.primary_cost_usd == Decimal("0.003")
    assert engine.calls[0][1]["content"] == "The case was found."


def test_unknown_summary_usage_stops_without_retry_or_complete_report(tmp_path):
    from decagent.evaluation.business.routes import ModelCallError

    calls = []

    def fail(request):
        calls.append(request)
        raise ModelCallError("synthetic timeout")

    result, _, engine = run_plan(
        tmp_path,
        policy=api().FinalResponsePolicy(mode="narrative"),
        summarizer=fail,
    )
    assert len(calls) == result.usage.primary_requests == 1
    assert not result.usage.complete and result.run is None
    assert result.failure_kind == "agent_failure"
    assert result.score is None and not engine.calls


def test_response_policy_mismatch_stops_before_tool_or_model(tmp_path):
    result, sessions, engine = run_plan(
        tmp_path,
        policy=api().FinalResponsePolicy(mode="narrative"),
        drift=True,
        summarizer=lambda _request: pytest.fail("Policy drift must be rejected first"),
    )
    assert result.failure_kind == "environment_failure"
    assert not sessions and not engine.calls and not result.tool_calls


@pytest.mark.parametrize("failure", ["missing", "wrong_model", "invalid_source", "known_failure"])
def test_summary_contract_failures_never_retry_or_discard_measured_usage(tmp_path, failure):
    from decagent.evaluation.business.routes import ModelCallError, ModelReply

    calls = []

    def summarize(request):
        calls.append(request)
        reply = ModelReply(
            {
                "content": "Actual summary",
                "source_call_ids": [
                    "unobserved"
                    if failure == "invalid_source"
                    else request["observations"][0]["call_id"],
                ],
            },
            tokens=6,
            cost_usd=Decimal("0.001"),
            model="unexpected-model" if failure == "wrong_model" else "qwen3.8-flash",
        )
        if failure == "known_failure":
            raise ModelCallError("Known model rejection", reply=reply)
        return reply

    result, _, _ = run_plan(
        tmp_path,
        policy=api().FinalResponsePolicy(mode="narrative"),
        summarizer=None if failure == "missing" else summarize,
    )
    assert result.status == "failed" and result.failure_kind == "agent_failure"
    assert len(calls) == result.usage.primary_requests == (0 if failure == "missing" else 1)
    assert result.usage.primary_tokens == (0 if failure == "missing" else 6)
    assert "final_response" not in result.terminal


def test_final_response_policies_are_part_of_pairing_identity():
    from decagent.evaluation.business.comparison import Route, compare_full_workflow
    from evals.deterministic.test_business_route_comparison import run, trial

    left = run(Route.REACT_SKILL_MCP, context=trial())
    right = run(
        Route.PLAN_MCP,
        context=replace(
            trial(),
            response_policy_digest=api()
            .FinalResponsePolicy(
                mode="narrative",
            )
            .fingerprint,
        ),
    )
    with pytest.raises(ValueError, match="response_policy_digest"):
        compare_full_workflow(left, right)


@pytest.mark.parametrize("route_name", ["plan_mcp", "react_skill_mcp"])
def test_any_unknown_agent_usage_prevents_all_private_scoring(route_name):
    from decagent.evaluation.business.comparison import Route
    from decagent.evaluation.business.routes import ControlledRoutes, ModelCallError
    from evals.deterministic.test_business_route_execution import Session, factory, trial

    session = Session()
    session.score = lambda: pytest.fail("Unknown usage must stop before any official judge")

    def unknown(_request):
        raise ModelCallError("Synthetic unknown provider usage")

    route = Route(route_name)
    result = ControlledRoutes(factory(session)).run(
        route,
        trial(session),
        planner=unknown,
        react=unknown,
    )
    assert not result.usage.complete and result.usage.primary_requests == 1
    assert result.run is None and result.score is None


@pytest.mark.parametrize("message", ["x" * 600, "界" * 200])
def test_react_uses_the_same_frozen_utf8_byte_limit_as_plan(message):
    from decagent.evaluation.business.comparison import Route
    from evals.deterministic.test_business_route_execution import Session, execute, reply, trial

    policy = api().FinalResponsePolicy(max_output_bytes=512)
    session = Session()
    captured = []
    session.set_final_response = captured.append
    result = execute(
        session,
        route=Route.REACT_SKILL_MCP,
        context=replace(trial(session), response_policy_digest=policy.fingerprint),
        response_policy=policy,
        react=lambda _request: reply({"finish": True, "message": message}),
    )
    assert result.status == "failed" and result.failure_kind == "agent_failure"
    assert "byte" in result.error and not captured

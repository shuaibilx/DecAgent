"""Offline contracts for the independent TypeSafe decision boundary."""

import copy
import json

import httpx
import pytest


def api():
    from decagent import decisions

    return decisions


def request(question_ids=("action_risk",), **changes):
    d = api()
    context = d.DecisionContext(
        task_id=changes.pop("task_id", "task-1"),
        project_id="project-1",
        code_version=changes.pop("code_version", "tree-1"),
        evidence_version=changes.pop("evidence_version", "evidence-1"),
        policy_version=changes.pop("policy_version", "policy-1"),
        authorization_version="auth-1",
        dataset_version="dataset-1",
    )
    evidence = {
        "action": {"tool": "write_file", "path": "src/main.py"},
        "authorization": "Local changes authorized",
        "risk_features": ["local write"],
        "code_excerpt": "print('ok')",
        "goal": "Repair a failure",
        "blockers": [],
        "next_steps": ["run fixed reproduction"],
        "observations": ["base failure reproduced"],
        "candidate": "Use pytest",
        "source_evidence": ["verification-1"],
        "confirmation_refs": [],
        "conflicts": [],
        "task_summary": "Repair a failure",
        "current_version": "tree-1",
        "candidate_summary": "Use pytest",
        "search_terms": ["pytest"],
        "UNRELATED_SECRET": "must never be transmitted",
    }
    evidence.update(changes)
    return d.build_request(question_ids, evidence, context)


def valid_response(question_ids=("action_risk",), model="jev-1.13.0"):
    choices = {
        "action_risk": ("allow", "deny", "review"),
        "intervention": ("continue", "clarify", "review"),
        "memory_write": ("retain", "discard", "review"),
    }
    answers = {}
    for question in question_ids:
        if question in choices:
            options = choices[question]
            answers[question] = {
                "type": "choice",
                "choice": options[0],
                "probabilities": dict(zip(options, (0.96, 0.02, 0.02), strict=True)),
                "confidence": 0.94,
            }
        else:
            answers[question] = {"type": "noul", "noul": 0.96}
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def adapter(handler, **kwargs):
    d = api()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return d.JevAdapter(
        api_key="test-secret-key",
        client=client,
        budget=kwargs.pop("budget", d.CallBudget(max_usd=1, reserve_usd=0.1)),
        sleep=kwargs.pop("sleep", lambda _: None),
        jitter=lambda: 0.0,
        **kwargs,
    )


def test_official_payload_is_minimal_and_uses_independent_endpoint():
    captured = []

    def handler(wire_request):
        captured.append(wire_request)
        return httpx.Response(200, json=valid_response())

    evaluation = adapter(handler).evaluate(request())
    assert evaluation.available
    wire_request = captured[0]
    assert str(wire_request.url) == "https://api.typesafe.ai/v1/systemone"
    assert wire_request.method == "POST"
    assert wire_request.headers["authorization"] == "Bearer test-secret-key"
    payload = json.loads(wire_request.content)
    assert set(payload) == {"state", "questions", "model"}
    assert payload["model"] == "jev-1.13.0"
    assert set(payload["state"]) == {"action", "authorization", "risk_features", "code_excerpt"}
    assert payload["questions"]["action_risk"]["type"] == "choice"
    assert set(payload["questions"]["action_risk"]["criteria"]) == {"allow", "deny", "review"}
    assert b"UNRELATED_SECRET" not in wire_request.content
    assert b"must never be transmitted" not in wire_request.content
    assert captured[0].extensions["timeout"]["read"] == 10.0
    assert evaluation.actual_model == "jev-1.13.0"
    assert evaluation.requested_model == "jev-1.13.0"
    assert evaluation.question_versions == {"action_risk": "1"}
    assert evaluation.input_hash == request().input_hash
    assert "test-secret-key" not in repr(evaluation)


def test_all_decision_questions_and_noul_probability_semantics():
    questions = (
        "action_risk",
        "intervention",
        "memory_write",
        "memory_read_needed",
        "memory_relevant",
    )
    result = adapter(lambda _: httpx.Response(200, json=valid_response(questions))).evaluate(
        request(questions)
    )
    assert result.available
    assert result.answers["memory_relevant"].probability == 0.96
    assert not hasattr(result.answers["memory_relevant"], "confidence")
    assert result.answers["memory_read_needed"].probability == 0.96


def malformed_responses():
    payload = valid_response()
    del payload["answers"]["action_risk"]
    yield "missing-question", payload
    payload = valid_response()
    payload["answers"]["unknown"] = payload["answers"]["action_risk"]
    yield "extra-question", payload
    for value in ("unexpected", "deny"):
        payload = valid_response()
        payload["answers"]["action_risk"]["choice"] = value
        yield "unknown-or-not-highest-choice", payload
    payload = valid_response()
    del payload["answers"]["action_risk"]["probabilities"]["deny"]
    yield "missing-option", payload
    payload = valid_response()
    payload["answers"]["action_risk"]["probabilities"]["extra"] = 0.0
    yield "extra-option", payload
    for value in (float("nan"), float("inf"), -0.1, 1.1, True, "0.96"):
        payload = valid_response()
        payload["answers"]["action_risk"]["probabilities"]["allow"] = value
        yield "invalid-probability", payload
    payload = valid_response()
    payload["answers"]["action_risk"]["probabilities"]["allow"] = 0.6
    yield "sum-not-one", payload
    for value in (None, True, -0.1, 1.1, float("nan"), "0.94"):
        payload = valid_response()
        payload["answers"]["action_risk"]["confidence"] = value
        yield "invalid-confidence", payload
    payload = valid_response()
    del payload["answers"]["action_risk"]["confidence"]
    yield "missing-confidence", payload
    payload = valid_response()
    payload["answers"]["action_risk"]["type"] = "noul"
    yield "wrong-type", payload
    for model in ("", "jev-latest", "jev-preview", "jev-1.14.0"):
        yield "unresolved-or-mismatched-model", valid_response(model=model)
    payload = valid_response()
    payload["usage"]["input_tokens"] = True
    yield "boolean-usage", payload
    payload = valid_response()
    del payload["usage"]
    yield "missing-usage", payload


@pytest.mark.parametrize("description,payload", list(malformed_responses()))
def test_invalid_responses_fail_closed_without_retry(description, payload):
    calls = []

    def handler(_):
        calls.append(1)
        # json= rejects NaN in some httpx versions; a raw body tests the actual boundary.
        return httpx.Response(200, content=json.dumps(payload))

    evaluation = adapter(handler).evaluate(request())
    assert not evaluation.available, description
    assert evaluation.error == "invalid_response"
    assert not evaluation.answers
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["false", False, float("nan"), -0.1, 1.1])
def test_noul_rejects_strings_booleans_and_invalid_numbers(value):
    payload = valid_response(("memory_read_needed",))
    payload["answers"]["memory_read_needed"]["noul"] = value
    result = adapter(lambda _: httpx.Response(200, content=json.dumps(payload))).evaluate(
        request(("memory_read_needed",))
    )
    assert not result.available


@pytest.mark.parametrize("status", [401, 422, 403, 404])
def test_nonretryable_http_status_has_one_attempt(status):
    calls = []
    result = adapter(
        lambda _: calls.append(1) or httpx.Response(status),
    ).evaluate(request())
    assert len(calls) == 1
    assert result.error == f"http_{status}"


@pytest.mark.parametrize("status", [429, 529, 500, 503])
def test_retryable_status_has_exponential_backoff_and_all_attempts_accounted(status):
    calls, sleeps, records = [], [], []
    d = api()
    budget = d.CallBudget(max_usd=1, reserve_usd=0.1)

    def handler(_):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(status)
        return httpx.Response(200, json=valid_response())

    result = adapter(
        handler, budget=budget, sleep=sleeps.append, usage_hook=records.append
    ).evaluate(request())
    assert result.available
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    assert len(result.attempts) == len(records) == 3
    assert [record.attempt for record in records] == [1, 2, 3]
    assert records[0].usage is None
    assert records[2].usage.input_tokens == 100
    assert budget.committed_usd == pytest.approx(0.3)


def test_network_timeout_is_bounded_and_unknown_usage_keeps_reservations():
    d = api()
    budget = d.CallBudget(max_usd=1, reserve_usd=0.1)
    calls = []

    def handler(wire_request):
        calls.append(1)
        raise httpx.ReadTimeout("contains provider details", request=wire_request)

    result = adapter(handler, budget=budget).evaluate(request())
    assert not result.available
    assert result.error == "network_error"
    assert len(calls) == 1
    assert budget.committed_usd == pytest.approx(0.1)
    assert "contains provider details" not in repr(result)


def test_reported_choice_confidence_must_match_distribution_concentration():
    payload = valid_response()
    payload["answers"]["action_risk"]["probabilities"] = {
        "allow": 0.34,
        "deny": 0.33,
        "review": 0.33,
    }
    payload["answers"]["action_risk"]["confidence"] = 0.99
    result = adapter(lambda _: httpx.Response(200, json=payload)).evaluate(request())
    assert not result.available and result.error == "invalid_response"


def test_budget_stops_retry_before_new_network_attempt():
    d = api()
    budget = d.CallBudget(max_usd=0.1, reserve_usd=0.1)
    calls = []
    result = adapter(
        lambda _: calls.append(1) or httpx.Response(429),
        budget=budget,
    ).evaluate(request())
    assert result.error == "budget_exceeded"
    assert len(calls) == 1
    assert len(result.attempts) == 1


def test_missing_key_or_explicit_budget_never_contacts_service():
    d = api()
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(lambda _: calls.append(1)))
    assert d.JevAdapter(api_key="", client=client).evaluate(request()).error == "missing_key"
    assert d.JevAdapter(api_key="key", client=client).evaluate(request()).error == "missing_budget"
    assert not calls


def test_cache_binds_code_evidence_policy_question_and_model_versions():
    calls = []
    client = adapter(lambda _: calls.append(1) or httpx.Response(200, json=valid_response()))
    first = client.evaluate(request())
    reused = client.evaluate(request(task_id="task-2"))
    assert first.available and reused.cached
    for context_change in (
        {"code_version": "tree-2"},
        {"evidence_version": "evidence-2"},
        {"policy_version": "policy-2"},
        {"action": {"tool": "write_file", "path": "src/other.py"}},
    ):
        assert not client.evaluate(request(**context_change)).cached
    assert len(calls) == 5


def test_alias_cache_never_crosses_tasks_and_resolved_version_is_recorded():
    calls = []

    def handler(_):
        calls.append(1)
        return httpx.Response(200, json=valid_response(model=f"jev-1.{12 + len(calls)}.0"))

    client = adapter(handler, model="jev-latest")
    first = client.evaluate(request())
    assert client.evaluate(request()).cached
    second = client.evaluate(request(task_id="task-2"))
    assert not second.cached
    assert first.actual_model == "jev-1.13.0"
    assert second.actual_model == "jev-1.14.0"


def test_conservative_policy_requires_review_even_with_high_model_confidence():
    d = api()
    questions = ("action_risk", "intervention", "memory_write", "memory_read_needed")
    engine = d.DecisionEngine(
        adapter(lambda _: httpx.Response(200, json=valid_response(questions)))
    )
    result = engine.decide(request(questions))
    assert result.routes == dict.fromkeys(questions, "review")
    assert result.governance_outcome == "review"
    assert not result.automatic_memory_enabled
    assert result.evaluation.answers["action_risk"].choice == "allow"


def test_hard_policy_has_same_authority_for_all_strategies():
    d = api()
    calls = []
    for strategy in (
        d.RuleStrategy(),
        d.MainLLMStrategy(lambda _: calls.append(1)),
        adapter(lambda _: calls.append(1)),
    ):
        engine = d.DecisionEngine(strategy)
        assert engine.decide(request(), hard_outcome="deny").governance_outcome == "deny"
        assert engine.decide(request(), hard_outcome="allow").governance_outcome == "allow"
    assert not calls


def test_main_llm_strategy_reuses_strict_response_contract():
    d = api()
    strategy = d.MainLLMStrategy(lambda _: valid_response())
    assert strategy.evaluate(request()).available
    invalid = copy.deepcopy(valid_response())
    invalid["answers"]["action_risk"]["choice"] = "unrecognized"
    assert not d.MainLLMStrategy(lambda _: invalid).evaluate(request()).available


def test_calibration_binds_question_dataset_and_actual_model_versions():
    d = api()
    calibration = d.CalibrationPolicy(
        question_id="action_risk",
        question_version="1",
        actual_model="jev-1.13.0",
        dataset_version="dataset-1",
        policy_version="policy-1",
        threshold=0.9,
        calibration_ids=("sample-1",),
    )
    strategy = adapter(lambda _: httpx.Response(200, json=valid_response()), model="jev-latest")
    engine = d.DecisionEngine(strategy, calibrations=(calibration,))
    assert engine.decide(request()).governance_outcome == "allow"
    assert engine.decide(request(policy_version="policy-2")).governance_outcome == "review"
    wrong_model = d.DecisionEngine(
        adapter(
            lambda _: httpx.Response(200, json=valid_response(model="jev-1.14.0")),
            model="jev-latest",
        ),
        calibrations=(calibration,),
    )
    assert wrong_model.decide(request()).governance_outcome == "review"


def test_invalid_input_question_and_nonfinite_state_are_rejected_before_network():
    with pytest.raises(ValueError):
        request(("unknown",))
    with pytest.raises(ValueError):
        request(action={"value": float("nan")})
    with pytest.raises(ValueError):
        request(())


def test_budget_and_retry_configuration_cannot_remove_safety_bounds():
    d = api()
    for kwargs in ({"timeout": 0}, {"timeout": 11}, {"max_retries": 3}, {"max_retries": -1}):
        with pytest.raises(ValueError):
            d.JevAdapter(api_key="key", **kwargs)
    for kwargs in (
        {"max_usd": 0, "reserve_usd": 0.1},
        {"max_usd": 1, "reserve_usd": 0},
        {"max_usd": float("nan"), "reserve_usd": 0.1},
    ):
        with pytest.raises(ValueError):
            d.CallBudget(**kwargs)


def test_invalid_answer_still_records_resolved_model_and_valid_usage():
    payload = valid_response()
    payload["answers"] = {}
    result = adapter(lambda _: httpx.Response(200, json=payload)).evaluate(request())
    assert not result.available
    assert result.actual_model == "jev-1.13.0"
    assert result.attempts[0].actual_model == "jev-1.13.0"
    assert result.attempts[0].usage.input_tokens == 100


def test_duplicate_json_keys_are_malformed_and_not_retried():
    body = '{"model":"jev-1.13.0","model":"jev-1.14.0","answers":{},"usage":{}}'
    result = adapter(lambda _: httpx.Response(200, content=body)).evaluate(request())
    assert result.error == "invalid_response"
    assert len(result.attempts) == 1


def test_usage_estimator_is_explicit_and_accounted_in_attempt_record():
    d = api()
    budget = d.CallBudget(max_usd=1, reserve_usd=0.1)
    result = adapter(
        lambda _: httpx.Response(200, json=valid_response()),
        budget=budget,
        cost_estimator=lambda usage: usage.input_tokens * 0.00001,
    ).evaluate(request())
    assert result.attempts[0].cost_usd == pytest.approx(0.001)
    assert budget.committed_usd == pytest.approx(0.001)


def test_audit_hook_failure_prevents_automatic_result():
    def failed_hook(_):
        raise RuntimeError("secret database detail")

    result = adapter(
        lambda _: httpx.Response(200, json=valid_response()), usage_hook=failed_hook
    ).evaluate(request())
    assert result.error == "audit_error"
    assert not result.available
    assert "secret database detail" not in repr(result)


def test_mutating_original_evidence_cannot_change_approved_request_or_cached_answer():
    d = api()
    action = {"tool": "read_file", "path": "main.py"}
    req = request(action=action)
    action["path"] = "secrets.env"
    assert req.state["action"]["path"] == "main.py"
    with pytest.raises(TypeError):
        req.state["action"]["path"] = "secrets.env"
    result = adapter(lambda _: httpx.Response(200, json=valid_response())).evaluate(req)
    assert isinstance(result.answers["action_risk"], d.ChoiceAnswer)
    with pytest.raises(TypeError):
        result.answers["action_risk"].probabilities["allow"] = 0.0


def test_typed_answers_cannot_accept_unvalidated_mutable_distributions():
    d = api()
    distribution = {"allow": 0.9, "deny": 0.05, "review": 0.05}
    answer = d.ChoiceAnswer("allow", distribution, 0.85)
    distribution["allow"] = 0.1
    assert answer.probabilities["allow"] == 0.9
    with pytest.raises(ValueError):
        d.ChoiceAnswer("allow", {"allow": 0.9, "deny": 0.2}, 0.9)
    with pytest.raises(ValueError):
        d.NoulAnswer("false")


def test_empty_action_or_authorization_evidence_cannot_start_a_semantic_call():
    d = api()
    with pytest.raises(ValueError):
        d.build_request(("action_risk",), {}, request().context)


def test_budget_underreservation_is_accounted_and_result_requires_review():
    d = api()
    budget = d.CallBudget(max_usd=1, reserve_usd=0.1)
    result = adapter(
        lambda _: httpx.Response(200, json=valid_response()),
        budget=budget,
        cost_estimator=lambda _: 0.2,
    ).evaluate(request())
    assert not result.available
    assert result.error == "budget_error"
    assert budget.committed_usd == pytest.approx(0.2)


def test_strategy_result_bound_to_a_different_input_cannot_authorize_action():
    d = api()
    strategy = adapter(lambda _: httpx.Response(200, json=valid_response()))
    mismatched = strategy.evaluate(request(action={"tool": "read_file", "path": "file.py"}))

    class WrongStrategy:
        name = "jev"

        def evaluate(self, _):
            return mismatched

    calibration = d.CalibrationPolicy(
        "action_risk",
        "1",
        "jev-1.13.0",
        "dataset-1",
        "policy-1",
        0.9,
        ("s",),
    )
    result = d.DecisionEngine(WrongStrategy(), (calibration,)).decide(request())
    assert result.governance_outcome == "review"
    assert result.reason == "invalid_strategy_result"


def test_cache_expiry_revalidates_alias_version():
    calls, now = [], [0.0]

    def handler(_):
        calls.append(1)
        return httpx.Response(200, json=valid_response(model=f"jev-1.{12 + len(calls)}.0"))

    client = adapter(handler, model="jev-latest", cache_ttl=5, clock=lambda: now[0])
    assert client.evaluate(request()).actual_model == "jev-1.13.0"
    now[0] = 5.0
    assert client.evaluate(request()).actual_model == "jev-1.14.0"


def test_records_export_json_without_secret_or_raw_evidence():
    d = api()
    result = d.DecisionEngine(adapter(lambda _: httpx.Response(200, json=valid_response()))).decide(
        request()
    )
    document = result.to_dict()
    encoded = json.dumps(document)
    assert document["evaluation"]["actual_model"] == "jev-1.13.0"
    assert document["evaluation"]["attempts"][0]["usage"]["input_tokens"] == 100
    assert document["evaluation"]["context"]["code_version"] == "tree-1"
    assert document["evaluation"]["question_versions"]["action_risk"] == "1"
    assert "test-secret-key" not in encoded
    assert "print('ok')" not in encoded
    assert "must never be transmitted" not in encoded


def test_binary_uncertainty_and_service_failure_never_enable_automatic_memory():
    d = api()
    payload = valid_response(("memory_relevant",))
    payload["answers"]["memory_relevant"]["noul"] = 0.5
    calibration = d.CalibrationPolicy(
        "memory_relevant",
        "1",
        "jev-1.13.0",
        "dataset-1",
        "policy-1",
        0.9,
        ("s",),
    )
    result = d.DecisionEngine(
        adapter(lambda _: httpx.Response(200, json=payload)),
        (calibration,),
    ).decide(request(("memory_relevant",)))
    assert result.routes["memory_relevant"] == "review"
    assert not result.automatic_memory_enabled
    failed = d.DecisionEngine(adapter(lambda _: httpx.Response(401))).decide(
        request(("memory_write",))
    )
    assert failed.routes["memory_write"] == "review"
    assert not failed.automatic_memory_enabled

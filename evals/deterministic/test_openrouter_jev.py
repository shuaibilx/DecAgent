import copy

import httpx
import pytest
from test_decisions import request, valid_response

from decagent.decisions import CallBudget, JevAdapter


def test_openrouter_typed_decisions_and_reported_cost():
    captured = []

    def respond(req):
        captured.append(req)
        payload = valid_response(model="typesafe/jev-1.13-20260917")
        payload["usage"]["cost"] = 0.00001
        payload["provider"] = "TypeSafe"
        return httpx.Response(200, json=payload)

    adapter = JevAdapter(
        api_key="test",
        provider="openrouter",
        model="jev-latest",
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        budget=CallBudget(max_usd=1, reserve_usd=0.01),
    )
    result = adapter.evaluate(request())
    assert result.error is None
    assert result.requested_model == "~typesafe/jev-latest"
    assert result.actual_model == "typesafe/jev-1.13-20260917"
    assert result.attempts[0].cost_usd == 0.00001
    assert str(captured[0].url) == "https://openrouter.ai/api/alpha/decisions"
    assert b'"questions"' in captured[0].content and b'"messages"' not in captured[0].content


@pytest.mark.parametrize(
    "actual", ["~typesafe/jev-latest", "typesafe/jev-1.13", "openai/gpt-4.1", "jev-latest"]
)
def test_router_alias_must_resolve_to_versioned_typesafe_model(actual):
    adapter = JevAdapter(
        api_key="test",
        provider="openrouter",
        model="jev-latest",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=valid_response(model=actual))
            )
        ),
        budget=CallBudget(max_usd=1, reserve_usd=0.01),
    )
    assert adapter.evaluate(request()).error == "invalid_response"


def test_pinned_router_family_mismatch_and_invalid_billing_are_rejected():
    good = valid_response(model="typesafe/jev-1.14-20261001")
    for payload in [
        good,
        {
            **good,
            "model": "typesafe/jev-1.13-20260917",
            "usage": {"input_tokens": 100, "output_tokens": 10, "cost": -1},
        },
    ]:
        adapter = JevAdapter(
            api_key="test",
            provider="openrouter",
            model="typesafe/jev-1.13",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda r, payload=payload: httpx.Response(200, json=copy.deepcopy(payload))
                )
            ),
            budget=CallBudget(max_usd=1, reserve_usd=0.01),
        )
        assert adapter.evaluate(request()).error is not None


def test_router_latest_does_not_share_cache_across_tasks():
    assert request(task_id="a").cache_key("~typesafe/jev-latest") != (
        request(task_id="b").cache_key("~typesafe/jev-latest")
    )

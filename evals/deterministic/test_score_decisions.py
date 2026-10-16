"""Ordered degree judgments obey TypeSafe's Score contract, not Choice concentration."""

import httpx
import pytest

from decagent.decisions.contracts import (
    DecisionContext,
    DecisionRequest,
    QuestionSpec,
    evaluation,
    validate_answers,
)

LEVELS = ("No work impact", "Workaround available", "Work blocked without workaround")


def request():
    return DecisionRequest(
        DecisionContext("task", "scope", "plugin", "evidence", "policy", "auth"),
        (QuestionSpec("impact", "1", "score", "Rate work obstruction", LEVELS, ("text",)),),
        {"text": "Export fails but CSV works"},
    )


def payload():
    return {
        "impact": {
            "type": "score",
            "score": 1.4,
            "confidence": 0.4,
            "probabilities": {"0": 0.0, "1": 0.6, "2": 0.4},
            "legend": dict(zip(("0", "1", "2"), LEVELS, strict=True)),
        }
    }


def test_score_wire_response_and_durable_serialization():
    queried = request()
    assert queried.to_wire("jev-latest")["questions"]["impact"]["criteria"] == list(LEVELS)
    answer = validate_answers(payload(), queried)["impact"]
    assert answer.score == 1.4 and answer.confidence == 0.4
    assert (
        evaluation(queried, "jev", "jev-latest", answers={"impact": answer}).to_dict()["answers"]
        == payload()
    )


def test_score_uses_the_decisions_http_adapter_and_its_usage_contract():
    from decagent.decisions import CallBudget, JevAdapter

    captured = []

    def handler(wire):
        import json

        captured.append(json.loads(wire.content))
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": payload(),
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JevAdapter(
            api_key="fixture",
            client=client,
            budget=CallBudget(max_usd=1, reserve_usd=0.1),
            max_retries=0,
        )
        result = adapter.evaluate(request())
    assert result.actual_model == "jev-1.13.0"
    assert result.answers["impact"].score == 1.4
    assert captured[0]["questions"]["impact"]["criteria"] == list(LEVELS)
    assert result.attempts[0].usage.input_tokens == 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("score", True),
        ("score", float("nan")),
        ("score", 0.1),
        ("confidence", 0.9),
        ("confidence", False),
        ("probabilities", {"0": 0.0, "1": 0.6}),
        ("legend", {"0": "Wrong", "1": LEVELS[1], "2": LEVELS[2]}),
    ],
)
def test_score_rejects_invalid_numeric_fields_or_unbound_levels(field, value):
    answers = payload()
    answers["impact"][field] = value
    with pytest.raises(ValueError):
        validate_answers(answers, request())


def test_equal_neighbor_and_opposite_distributions_have_different_confidence():
    neighbors = payload()
    neighbors["impact"].update(
        score=1.5, confidence=0.25, probabilities={"0": 0.0, "1": 0.5, "2": 0.5}
    )
    opposite = payload()
    opposite["impact"].update(
        score=1.0, confidence=0.0, probabilities={"0": 0.5, "1": 0.0, "2": 0.5}
    )
    assert validate_answers(neighbors, request())["impact"].confidence == 0.25
    assert validate_answers(opposite, request())["impact"].confidence == 0.0


def test_probability_sum_tolerance_cannot_expand_the_score_domain():
    answers = payload()
    answers["impact"].update(
        score=2.0000009,
        confidence=0.99999865,
        probabilities={"0": 0.0, "1": 0.0000009, "2": 1.0},
    )
    with pytest.raises(ValueError, match="range"):
        validate_answers(answers, request())


@pytest.mark.parametrize(
    "levels", [("Only one",), tuple(str(x) for x in range(11)), {"a": "low", "b": "high"}]
)
def test_score_requires_two_to_ten_ordered_descriptions(levels):
    with pytest.raises(ValueError):
        QuestionSpec("impact", "1", "score", "Rate impact", levels, ("text",))

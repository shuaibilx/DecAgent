"""Rounded confidence is part of the provider wire format, not malformed evidence."""

import random

import pytest

from decagent.decisions.contracts import ChoiceAnswer, ScoreAnswer


def test_live_eight_branch_choice_preserves_rounded_confidence():
    probabilities = dict(zip("abcdefgh", (0.85, 0, 0, 0, 0.02, 0.04, 0, 0.09), strict=True))
    answer = ChoiceAnswer("a", probabilities, 0.83)
    assert answer.confidence == 0.83
    assert dict(answer.probabilities) == probabilities


@pytest.mark.parametrize(
    "probabilities,score,confidence",
    [
        ((0.87, 0.05, 0.08), 0.21, 0.68),  # Captured Jev 1.13 response.
        ((0.87, 0.05, 0.08), 0.22, 0.68),  # Another captured response, rounded inputs too.
        ((0, 0.57, 0.43), 1.43, 0.35),  # TypeSafe's published Score example.
        ((0, 0.89, 0.11), 1.11, 0.84),
    ],
)
def test_score_preserves_provider_confidence_at_half_cent_boundary(
    probabilities, score, confidence
):
    distribution = dict(zip(("0", "1", "2"), probabilities, strict=True))
    answer = ScoreAnswer(score, distribution, confidence, {"0": "low", "1": "middle", "2": "high"})
    assert answer.confidence == confidence
    assert answer.score == score


@pytest.mark.parametrize("confidence", (0.65, 0.72, float("nan"), True, 1.01))
def test_score_rejects_confidence_beyond_rounding_or_outside_domain(confidence):
    with pytest.raises(ValueError):
        ScoreAnswer(
            0.21,
            {"0": 0.87, "1": 0.05, "2": 0.08},
            confidence,
            {"0": "low", "1": "middle", "2": "high"},
        )


def test_choice_rejects_confidence_beyond_rounding():
    with pytest.raises(ValueError):
        ChoiceAnswer("a", {"a": 0.8, "b": 0.1, "c": 0.1}, 0.67)


def test_independent_probability_rounding_keeps_raw_sum():
    answer = ChoiceAnswer("a", {"a": 0.8, "b": 0.1, "c": 0.09}, 0.7)
    assert sum(answer.probabilities.values()) == pytest.approx(0.99)
    assert dict(answer.probabilities) == {"a": 0.8, "b": 0.1, "c": 0.09}


def test_confidence_accounts_for_rounding_of_both_reported_numbers():
    assert ChoiceAnswer("a", {"a": 0.79, "b": 0.21}, 0.59).confidence == 0.59


def test_choice_confidence_mass_and_winner_must_share_one_distribution():
    with pytest.raises(ValueError):
        ChoiceAnswer("a", {"a": 0.4, "b": 0.4, "c": 0.2}, 0.09)
    assert ChoiceAnswer("a", {"a": 0.4, "b": 0.4, "c": 0.2}, 0.10).confidence == 0.10


@pytest.mark.parametrize(
    "distribution", ({"a": 0, "b": 0}, {"a": 0.8, "b": 0.3}, {"a": 0.80000123, "b": 0.19912345})
)
def test_impossible_probability_mass_is_rejected(distribution):
    with pytest.raises(ValueError):
        ChoiceAnswer("a", distribution, 0.6)


@pytest.mark.parametrize("score", (0.18, 0.24, 3, True, float("inf")))
def test_score_mean_cannot_drift_outside_probability_rounding_bounds(score):
    with pytest.raises(ValueError):
        ScoreAnswer(
            score, {"0": 0.87, "1": 0.05, "2": 0.08}, 0.68, {"0": "low", "1": "middle", "2": "high"}
        )


def test_score_and_confidence_must_share_one_feasible_distribution():
    with pytest.raises(ValueError):
        ScoreAnswer(
            0.22, {"0": 0.87, "1": 0.05, "2": 0.08}, 0.70, {"0": "low", "1": "middle", "2": "high"}
        )


@pytest.mark.parametrize("levels", range(2, 11))
def test_actual_distributions_survive_independent_wire_rounding(levels):
    generator = random.Random(levels)
    for _ in range(30):
        values = [generator.random() for _ in range(levels)]
        total = sum(values)
        probabilities = [p / total for p in values]
        mean = sum(i * p for i, p in enumerate(probabilities))
        mode = probabilities.index(max(probabilities))
        spread = sum(p * abs(i - mode) for i, p in enumerate(probabilities))
        uniform = sum(abs(i - (levels - 1) / 2) for i in range(levels)) / levels
        confidence = max(0, 1 - spread / uniform)
        raw = {str(i): round(p, 2) for i, p in enumerate(probabilities)}
        answer = ScoreAnswer(
            round(mean, 2),
            raw,
            round(confidence, 2),
            {str(i): "level " + str(i) for i in range(levels)},
        )
        assert dict(answer.probabilities) == raw


def test_solver_feasibility_known_systems():
    from decagent.decisions.precision import feasible

    assert feasible([([1], 1), ([-1], -0.5)], 1)
    assert not feasible([([1], 0.1), ([-1], -0.2)], 1)
    assert feasible([([1, 1], 1), ([-1, -1], -1)], 2)
    assert not feasible([([1, 0], 0.1), ([0, 1], 0.1), ([-1, -1], -1)], 2)

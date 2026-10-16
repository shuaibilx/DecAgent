"""Pure certainty gates use explicit evaluated policies, never business authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from decagent.business.decision_policy import (
    NORMALIZATION_VERSION,
    DecisionSnapshot,
    PolicyContext,
    ThresholdPolicy,
    gate_decision,
    normalize_answer,
)
from decagent.decisions.contracts import ChoiceAnswer, NoulAnswer, QuestionSpec, ScoreAnswer

NOW = datetime(2026, 10, 15, 8, tzinfo=UTC)


def question(kind="choice"):
    criteria = {
        "choice": {"a": "The authorized candidate", "no_match": "No legal match"},
        "noul": {"true": "Evidence supports the proposition", "false": "It does not"},
        "score": ("No obstruction", "Some obstruction", "Work fully obstructed"),
    }[kind]
    return QuestionSpec("routing", "3", kind, "Use the supplied evidence", criteria, ("state",))


def snapshot(kind="choice"):
    return DecisionSnapshot(
        context=PolicyContext(
            plugin_digest="a" * 64,
            workflow_version="business-3",
            dataset_version="validation-fixture-1",
            risk="sensitive",
            candidate_source="authorized.directory.people@1",
        ),
        state_digest="b" * 64,
        candidate_digest="c" * 64,
        candidate_count=len(question(kind).criteria),
        scope_id="scope-1",
        scope_version="7",
        account_id="employee-1",
        account_version="4",
    )


def policy(kind="choice", threshold=0.95):
    return ThresholdPolicy(
        policy_id="test-policy",
        policy_version="1",
        question_id="routing",
        question_version="3",
        primitive=kind,
        actual_model="jev-1.2.3",
        context=snapshot(kind).context,
        min_candidates=2,
        max_candidates=3,
        threshold=threshold,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        validation_record="deterministic-fixture-only",
        status="published",
    )


def choice_answer():
    return {
        "type": "choice",
        "choice": "a",
        "probabilities": {"a": 0.98, "no_match": 0.02},
        "confidence": 0.96,
    }


def gate(answer=None, *, kind="choice", profile=None, **kwargs):
    recorded = snapshot(kind)
    options = {
        "actual_model": "jev-1.2.3",
        "snapshot": recorded,
        "current_snapshot": recorded,
        "policy": profile or policy(kind),
        "now": NOW,
    }
    options.update(kwargs)
    return gate_decision(question(kind), answer or choice_answer(), **options)


def test_choice_keeps_raw_distribution_and_confidence_at_threshold_equality():
    answer = choice_answer()
    result = gate(answer, profile=policy(threshold=0.96))
    assert result.may_consume
    assert result.reason == "threshold_met"
    assert result.answer.raw_answer == answer
    assert result.answer.raw_confidence == 0.96
    assert result.answer.certainty == 0.96
    assert result.answer.certainty_kind == "confidence"
    assert result.answer.p_yes is None
    assert result.actual_model == "jev-1.2.3"
    assert result.snapshot == snapshot()
    assert not result.grants_write_authority
    assert not result.primary_fallback
    answer["probabilities"]["a"] = 0
    assert result.answer.raw_answer["probabilities"]["a"] == 0.98
    with pytest.raises(TypeError):
        result.answer.raw_answer["probabilities"]["a"] = 1


@pytest.mark.parametrize(
    "p, certainty, suggested",
    [(0.96, 0.92, True), (0.04, 0.92, False), (0.975, 0.95, True), (0.025, 0.95, False)],
)
def test_noul_uses_derived_certainty_with_exact_decimal_boundaries(p, certainty, suggested):
    result = gate(
        {"type": "noul", "noul": p}, kind="noul", profile=policy("noul", threshold=certainty)
    )
    assert result.may_consume
    assert result.answer.certainty == certainty
    assert result.answer.suggested_value is suggested
    assert result.answer.certainty_kind == "derived_certainty"
    assert result.answer.raw_confidence is None
    assert result.answer.p_yes == p
    assert result.answer.raw_answer == {"type": "noul", "noul": p}
    assert result.answer.normalization_version == NORMALIZATION_VERSION


def test_noul_point_96_is_manual_at_point_95_threshold():
    result = gate({"type": "noul", "noul": 0.96}, kind="noul")
    assert not result.may_consume
    assert result.reason == "below_threshold"
    assert result.answer.certainty == 0.92


def test_noul_tie_is_undecided_even_at_zero_threshold():
    result = gate({"type": "noul", "noul": 0.5}, kind="noul", profile=policy("noul", threshold=0))
    assert result.reason == "undecided_noul"
    assert not result.may_consume
    assert result.answer.suggested_value is None


def test_score_uses_raw_confidence_rather_than_score_or_severity():
    answer = {
        "type": "score",
        "score": 1.8,
        "probabilities": {"0": 0.02, "1": 0.16, "2": 0.82},
        "confidence": 0.7,
        "legend": dict(enumerate(question("score").criteria)),
    }
    answer["legend"] = {str(k): v for k, v in answer["legend"].items()}
    result = gate(answer, kind="score")
    assert result.reason == "below_threshold"
    assert result.answer.raw_confidence == 0.7
    assert result.answer.certainty == 0.7
    assert result.answer.suggested_value == 1.8
    assert result.answer.raw_answer == answer


@pytest.mark.parametrize(
    "answer",
    [
        ChoiceAnswer("a", {"a": 0.98, "no_match": 0.02}, 0.96),
        NoulAnswer(0.96),
        ScoreAnswer(
            1.8,
            {"0": 0.02, "1": 0.16, "2": 0.82},
            0.7,
            {"0": "No obstruction", "1": "Some obstruction", "2": "Work fully obstructed"},
        ),
    ],
)
def test_existing_typed_answers_share_the_wire_validation(answer):
    kind = {ChoiceAnswer: "choice", NoulAnswer: "noul", ScoreAnswer: "score"}[type(answer)]
    normalized = normalize_answer(question(kind), answer)
    assert normalized.primitive == kind


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01, True, "0.99", None])
def test_invalid_noul_probabilities_are_explicit_manual_results(value):
    result = gate({"type": "noul", "noul": value}, kind="noul")
    assert result.reason == "invalid_answer"
    assert not result.may_consume
    assert result.answer is None
    assert not result.primary_fallback


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": float("nan")},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": True},
        {"probabilities": {"a": 0.4, "no_match": 0.4}},
        {"probabilities": {"a": 0.99, "outsider": 0.01}},
        {"probabilities": {"a": -0.1, "no_match": 1.1}},
        {"choice": "no_match"},
        {"confidence": 0.5},
        {"extra": "untrusted"},
        {"type": "score"},
    ],
)
def test_malformed_or_inconsistent_choice_distributions_are_invalid(changes):
    result = gate({**choice_answer(), **changes})
    assert result.reason == "invalid_answer"
    assert not result.may_consume


def test_score_legend_and_expected_value_must_match_original_scale():
    answer = {
        "type": "score",
        "score": 1.8,
        "probabilities": {"0": 0.02, "1": 0.16, "2": 0.82},
        "confidence": 0.7,
        "legend": {"0": "No obstruction", "1": "Some obstruction", "2": "Work fully obstructed"},
    }
    assert gate({**answer, "score": 0}, kind="score").reason == "invalid_answer"
    assert (
        gate({**answer, "legend": {**answer["legend"], "2": "Changed scale"}}, kind="score").reason
        == "invalid_answer"
    )


def test_no_published_policy_never_uses_primary_fallback():
    result = gate(policy=None)
    assert result.reason == "policy_unavailable"
    assert not result.may_consume
    assert not result.primary_fallback


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"actual_model": "jev-1.2.4"}, "model_version_mismatch"),
        ({"actual_model": "~typesafe/jev-latest"}, "actual_model_unresolved"),
        ({"actual_model": None}, "actual_model_unresolved"),
    ],
)
def test_actual_model_drift_invalidates_old_policy(changes, reason):
    result = gate(**changes)
    assert result.reason == reason
    assert not result.may_consume


@pytest.mark.parametrize(
    "field, value",
    [
        ("question_id", "other"),
        ("question_version", "4"),
        ("primitive", "noul"),
    ],
)
def test_policy_question_contract_must_match(field, value):
    result = gate(profile=replace(policy(), **{field: value}))
    assert result.reason == "policy_question_mismatch"


@pytest.mark.parametrize(
    "field", ["plugin_digest", "workflow_version", "dataset_version", "candidate_source", "risk"]
)
def test_policy_applicability_versions_are_exact(field):
    context = replace(snapshot().context, **{field: "changed"})
    current = replace(snapshot(), context=context)
    result = gate(snapshot=current, current_snapshot=current)
    assert result.reason == "policy_context_mismatch"
    assert field in result.mismatches


def test_unknown_normalization_version_is_not_consumed():
    current = replace(
        snapshot(), context=replace(snapshot().context, normalization_version="future")
    )
    result = gate(
        snapshot=current,
        current_snapshot=current,
        profile=replace(policy(), context=current.context),
    )
    assert result.reason == "normalization_unavailable"


@pytest.mark.parametrize(
    "field, value",
    [
        ("state_digest", "d" * 64),
        ("candidate_digest", "d" * 64),
        ("scope_version", "8"),
        ("account_version", "5"),
        ("scope_id", "other-scope"),
        ("account_id", "other-employee"),
    ],
)
def test_recorded_answers_require_current_state_candidates_and_identity_versions(field, value):
    result = gate(current_snapshot=replace(snapshot(), **{field: value}))
    assert result.reason == "stale_decision"
    assert field in result.mismatches
    assert not result.may_consume


def test_published_policy_is_reusable_for_new_current_dynamic_snapshots():
    current = replace(
        snapshot(),
        state_digest="d" * 64,
        candidate_digest="e" * 64,
        scope_id="another-scope",
        scope_version="8",
        account_id="another-employee",
        account_version="5",
    )
    result = gate(snapshot=current, current_snapshot=current)
    assert result.may_consume


def test_policy_candidate_range_is_bounded_and_inclusive():
    assert gate(profile=replace(policy(), min_candidates=2, max_candidates=2)).may_consume
    result = gate(profile=replace(policy(), min_candidates=3, max_candidates=4))
    assert result.reason == "candidate_range_mismatch"
    current = replace(snapshot(), candidate_count=3)
    assert gate(snapshot=current, current_snapshot=current).reason == "candidate_count_mismatch"


@pytest.mark.parametrize("status", ["draft", "validated", "revoked"])
def test_only_published_profiles_are_eligible(status):
    assert gate(profile=replace(policy(), status=status)).reason == "policy_unavailable"


def test_policy_validity_start_inclusive_end_exclusive():
    assert gate(profile=replace(policy(), valid_from=NOW)).may_consume
    assert (
        gate(profile=replace(policy(), valid_from=NOW + timedelta(seconds=1))).reason
        == "policy_not_yet_valid"
    )
    assert gate(profile=replace(policy(), valid_until=NOW)).reason == "policy_expired"


@pytest.mark.parametrize(
    "changes",
    [
        {"threshold": float("nan")},
        {"threshold": -0.1},
        {"threshold": 1.1},
        {"threshold": True},
        {"actual_model": "~typesafe/jev-latest"},
        {"validation_record": ""},
        {"min_candidates": 0},
        {"max_candidates": 257},
        {"min_candidates": 4, "max_candidates": 3},
        {"valid_until": NOW - timedelta(days=2)},
        {"valid_from": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_policy_contracts_cannot_be_constructed(changes):
    with pytest.raises(ValueError):
        replace(policy(), **changes)


def test_gate_does_not_swallow_permission_errors_as_schema_failures():
    class Denied(dict):
        def __iter__(self):
            raise PermissionError("evidence permission revoked")

    with pytest.raises(PermissionError):
        gate(Denied(choice_answer()))


def test_noul_decimal_computation_does_not_modify_original_probability():
    p = 0.975
    result = normalize_answer(question("noul"), {"type": "noul", "noul": p})
    assert result.certainty == float(abs(2 * Decimal(str(p)) - 1))
    assert result.raw_answer["noul"] == p


@pytest.mark.parametrize("choice", [[], {}, None, 1, True])
def test_non_string_choice_is_invalid_instead_of_escaping_the_gate(choice):
    assert gate({**choice_answer(), "choice": choice}).reason == "invalid_answer"


def test_explicit_invalid_snapshot_result_can_be_serialized():
    result = gate(snapshot=None)
    assert result.reason == "invalid_snapshot"
    assert result.to_dict()["snapshot"] is None


def test_gate_serialization_preserves_raw_answer_and_dynamic_versions():
    result = gate()
    serialized = result.to_dict()
    assert serialized["answer"]["raw_answer"] == choice_answer()
    assert serialized["snapshot"]["account_version"] == "4"
    assert serialized["current_snapshot"]["candidate_digest"] == "c" * 64
    assert serialized["policy_id"] == "test-policy"
    assert serialized["policy_version"] == "1"
    assert serialized["grants_write_authority"] is False


@pytest.mark.parametrize("changes", [{"primitive": []}, {"status": []}])
def test_malformed_policy_enum_is_a_contract_validation_error(changes):
    with pytest.raises(ValueError):
        replace(policy(), **changes)

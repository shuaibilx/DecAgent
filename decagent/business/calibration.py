"""Scoped synthetic-reference calibration, separate from independent human evidence."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from decagent.decisions.contracts import (
    MODEL_ID,
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    jev_model_matches,
)


def certainty(answer):
    if isinstance(answer, NoulAnswer):
        return max(answer.probability, 1 - answer.probability)
    if isinstance(answer, (ChoiceAnswer, ScoreAnswer)):
        return answer.confidence
    raise ValueError("Unsupported calibrated answer")


def predicted(answer):
    if isinstance(answer, ChoiceAnswer):
        return answer.choice
    if isinstance(answer, NoulAnswer):
        return answer.probability > 0.5
    if isinstance(answer, ScoreAnswer):
        return math.floor(answer.score + 0.5)
    raise ValueError("Unsupported calibrated answer")


@dataclass(frozen=True)
class BusinessCalibration:
    model: str
    plugin_digest: str
    workflow_version: str
    question_version: str
    policy_version: str
    dataset_version: str
    thresholds: dict
    label_source: str = "synthetic-reference"

    def __post_init__(self):
        if not MODEL_ID.fullmatch(self.model) or self.label_source != "synthetic-reference":
            raise ValueError("Calibration requires an actual model and truthful label source")
        if not all(
            isinstance(x, str) and x
            for x in (
                self.plugin_digest,
                self.workflow_version,
                self.question_version,
                self.policy_version,
                self.dataset_version,
            )
        ):
            raise ValueError("Calibration version bindings are required")
        if not isinstance(self.thresholds, dict) or any(
            not isinstance(k, str)
            or not k
            or (
                v is not None
                and (type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1)
            )
            for k, v in self.thresholds.items()
        ):
            raise ValueError("Calibration thresholds must be finite probabilities or disabled")
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))

    def compatible(self, request, requested_model=None):
        if requested_model is not None and not jev_model_matches(requested_model, self.model):
            return False
        context = request.context
        return all(
            getattr(context, key) == getattr(self, key)
            for key in (
                "plugin_digest",
                "workflow_version",
                "question_version",
                "policy_version",
                "dataset_version",
            )
        )

    def accepts(self, request, model, question_id, answer):
        if model != self.model or not self.compatible(request):
            return False
        question = next((q for q in request.questions if q.id == question_id), None)
        if question is None or not _matches(question, answer):
            return False
        threshold = self.thresholds.get(question_id)
        if threshold is None:
            return False
        value = certainty(answer)
        return value >= threshold and value > (0.5 if question.type == "noul" else 0)

    def to_dict(self):
        return {**vars(self), "thresholds": dict(self.thresholds)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid business calibration format")
        return cls(**value)


def _matches(question, answer):
    if isinstance(answer, ChoiceAnswer):
        return question.type == "choice" and set(answer.probabilities) == set(question.criteria)
    if isinstance(answer, NoulAnswer):
        return question.type == "noul"
    if isinstance(answer, ScoreAnswer):
        return question.type == "score" and dict(answer.legend) == {
            str(i): description for i, description in enumerate(question.criteria)
        }
    return False


def fit_profile(request, model, records, *, dataset_version, split="development"):
    if split != "development":
        raise ValueError("Calibration may use only development records")
    if dataset_version != request.context.dataset_version:
        raise ValueError("Calibration dataset version must match the evaluated request")
    thresholds = {}
    questions = {q.id: q for q in request.questions}
    if not records or any(
        not isinstance(row, Mapping)
        or set(row) != {"question_id", "answer", "expected"}
        or row["question_id"] not in questions
        or not _matches(questions[row["question_id"]], row["answer"])
        for row in records
    ):
        raise ValueError("Calibration records must match requested question contracts")
    for key, question in questions.items():
        values = [row for row in records if row["question_id"] == key]
        if not values:
            continue
        valid_kind = {"choice": str, "noul": bool, "score": int}[question.type]
        if any(type(row["expected"]) is not valid_kind for row in values):
            raise ValueError("Reference labels do not match question types")
        if question.type == "choice" and any(
            row["expected"] not in question.criteria for row in values
        ):
            raise ValueError("Reference choice is absent from the legal candidates")
        if question.type == "score" and any(
            not 0 <= row["expected"] < len(question.criteria) for row in values
        ):
            raise ValueError("Reference score level is outside the versioned rubric")
        wrong = [
            certainty(row["answer"])
            for row in values
            if predicted(row["answer"]) != row["expected"]
        ]
        minimum = math.nextafter(max(wrong), math.inf) if wrong else 0.0
        good = [
            certainty(row["answer"])
            for row in values
            if predicted(row["answer"]) == row["expected"] and certainty(row["answer"]) > 0
        ]
        thresholds[key] = minimum if any(x >= minimum for x in good) else None
    context = request.context
    return BusinessCalibration(
        model,
        context.plugin_digest,
        context.workflow_version,
        context.question_version,
        context.policy_version,
        dataset_version,
        thresholds,
    )

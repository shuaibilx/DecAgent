"""Pure routing shared by rule, main-model and Jev decision strategies."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .contracts import (
    MODEL_ID,
    QUESTIONS,
    ChoiceAnswer,
    DecisionEvaluation,
    DecisionRequest,
    NoulAnswer,
    ScoreAnswer,
    evaluation,
    probability,
    validate_response,
)


class DecisionStrategy(Protocol):
    name: str

    def evaluate(self, request: DecisionRequest) -> DecisionEvaluation: ...


class RuleStrategy:
    name = "rule"

    def evaluate(self, request: DecisionRequest) -> DecisionEvaluation:
        return evaluation(request, self.name, "rule", error="semantic_review_required")


class MainLLMStrategy:
    """Inject a separately budgeted typed classifier; this class never generates or executes."""

    name = "main-llm"

    def __init__(self, classify: Callable[[DecisionRequest], Mapping[str, Any]], model="main-llm"):
        self.classify, self.model = classify, model

    def evaluate(self, request: DecisionRequest) -> DecisionEvaluation:
        try:
            actual, answers, _ = validate_response(
                self.classify(request), request, self.model, require_jev_model=False
            )
            return evaluation(request, self.name, self.model, actual_model=actual, answers=answers)
        except Exception:
            return evaluation(request, self.name, self.model, error="invalid_response")


@dataclass(frozen=True)
class CalibrationPolicy:
    question_id: str
    question_version: str
    actual_model: str
    dataset_version: str
    policy_version: str
    threshold: float
    calibration_ids: tuple[str, ...]
    format_version: str = "1"

    def __post_init__(self):
        if (
            self.question_id not in QUESTIONS
            or self.question_version != QUESTIONS[self.question_id].version
        ):
            raise ValueError("Unknown question or incompatible question version")
        probability(self.threshold)
        if not self.actual_model or self.actual_model in {
            "jev-latest",
            "~typesafe/jev-latest",
            "jev-preview",
            "main-llm",
        }:
            raise ValueError("Calibration requires the actual resolved model")
        if self.actual_model.startswith(("jev-", "typesafe/jev-")) and not MODEL_ID.fullmatch(
            self.actual_model
        ):
            raise ValueError("Calibration requires a versioned Jev model")
        if (
            not self.dataset_version
            or self.dataset_version == "uncalibrated"
            or not self.policy_version
        ):
            raise ValueError("Calibration requires dataset and policy versions")
        if not self.calibration_ids or len(set(self.calibration_ids)) != len(self.calibration_ids):
            raise ValueError("Calibration requires unique calibration sample identifiers")
        if self.format_version != "1":
            raise ValueError("Unsupported calibration format")
        if QUESTIONS[self.question_id].type == "noul" and self.threshold <= 0.5:
            raise ValueError("Noul automatic routing requires a probability above one half")

    def matches(self, request: DecisionRequest, result: DecisionEvaluation) -> bool:
        context = request.context
        return (
            result.actual_model == self.actual_model
            and request.question_versions.get(self.question_id) == self.question_version
            and context.dataset_version == self.dataset_version
            and context.policy_version == self.policy_version
        )


@dataclass(frozen=True)
class DecisionResult:
    evaluation: DecisionEvaluation
    routes: Mapping[str, str]
    governance_outcome: str
    reason: str
    automatic_memory_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": "1",
            "evaluation": self.evaluation.to_dict(),
            "routes": dict(self.routes),
            "governance_outcome": self.governance_outcome,
            "reason": self.reason,
            "automatic_memory_enabled": self.automatic_memory_enabled,
        }


class DecisionEngine:
    def __init__(self, strategy: DecisionStrategy, calibrations: Sequence[CalibrationPolicy] = ()):
        self.strategy, self.calibrations = strategy, tuple(calibrations)

    def decide(
        self,
        request: DecisionRequest,
        *,
        hard_outcome: Literal["allow", "deny", "semantic"] = "semantic",
    ) -> DecisionResult:
        """Hard outcomes must come from the trusted policy boundary, never model evidence."""
        if hard_outcome not in {"allow", "deny", "semantic"}:
            raise ValueError("Unknown hard policy outcome")
        if hard_outcome != "semantic":
            result = evaluation(request, self.strategy.name, "not-called", error="hard_policy")
            routes = {q.id: "review" for q in request.questions}
            if "action_risk" in routes:
                routes["action_risk"] = hard_outcome
            return DecisionResult(result, MappingProxyType(routes), hard_outcome, "hard_policy")
        try:
            result = self.strategy.evaluate(request)
            if not isinstance(result, DecisionEvaluation) or (
                result.context != request.context
                or result.input_hash != request.input_hash
                or result.question_versions != request.question_versions
            ):
                raise ValueError("Mismatched decision result")
            if result.available:
                if set(result.answers) != set(request.question_versions):
                    raise ValueError("Mismatched answer set")
                for question in request.questions:
                    answer = result.answers[question.id]
                    if question.type == "choice":
                        if not isinstance(answer, ChoiceAnswer) or (
                            set(answer.probabilities) != set(question.criteria)
                            or answer.choice not in question.criteria
                        ):
                            raise ValueError("Invalid strategy Choice")
                    elif question.type == "score":
                        if not isinstance(answer, ScoreAnswer) or dict(answer.legend) != {
                            str(i): value for i, value in enumerate(question.criteria)
                        }:
                            raise ValueError("Invalid strategy Score")
                    elif not isinstance(answer, NoulAnswer):
                        raise ValueError("Invalid strategy Noul")
        except Exception:
            result = evaluation(
                request, self.strategy.name, "unknown", error="invalid_strategy_result"
            )
        routes = {q.id: "review" for q in request.questions}
        matched = set()
        if result.available:
            for question in request.questions:
                compatible = [
                    policy
                    for policy in self.calibrations
                    if policy.question_id == question.id and policy.matches(request, result)
                ]
                if len(compatible) != 1:
                    continue
                calibration, answer = compatible[0], result.answers[question.id]
                if isinstance(answer, ChoiceAnswer) and answer.confidence >= calibration.threshold:
                    routes[question.id] = answer.choice
                    matched.add(question.id)
                elif isinstance(answer, NoulAnswer):
                    if answer.probability >= calibration.threshold:
                        routes[question.id] = "retrieve"
                        matched.add(question.id)
                    elif answer.probability <= 1 - calibration.threshold:
                        routes[question.id] = "skip"
                        matched.add(question.id)
        action_route = routes.get("action_risk", "review")
        reason = result.error or (
            "calibrated" if len(matched) == len(routes) else "uncalibrated_or_uncertain"
        )
        # Retain recommends a candidate. Activation has independent evidence gates.
        return DecisionResult(result, MappingProxyType(routes), action_route, reason)

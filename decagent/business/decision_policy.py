"""Pure semantic certainty gates; consuming an answer never grants business authority.

Policies describe evaluated applicability. Snapshots bind one prediction to its
actual evidence and authorization revisions; those dynamic values are not policy
applicability claims. Callers must obtain both snapshots from trusted runtime data.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from decagent.decisions.contracts import (
    MODEL_ID,
    ChoiceAnswer,
    NoulAnswer,
    QuestionSpec,
    ScoreAnswer,
    immutable,
    json_value,
    probability,
)

NORMALIZATION_VERSION = "primitive-certainty-v1"
_PRIMITIVES = {"choice", "noul", "score"}


def _identifiers(values):
    return all(isinstance(value, str) and value.strip() for value in values)


def _aware(value):
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


@dataclass(frozen=True)
class PolicyContext:
    """Versioned applicability shared by evaluation and current runtime input."""

    plugin_digest: str
    workflow_version: str
    dataset_version: str
    risk: str
    candidate_source: str
    normalization_version: str = NORMALIZATION_VERSION

    def __post_init__(self):
        if not _identifiers(vars(self).values()):
            raise ValueError("Policy applicability requires explicit versions, source and risk")


@dataclass(frozen=True)
class DecisionSnapshot:
    """One permission-filtered input, including the complete legal option count.

    Counts include no-match options and ordered Score levels. Scope/account
    revisions are freshness evidence, never permission grants. A policy may be
    reused for another fresh snapshot within its evaluated applicability range.
    """

    context: PolicyContext
    state_digest: str
    candidate_digest: str
    candidate_count: int
    scope_id: str
    scope_version: str
    account_id: str
    account_version: str

    def __post_init__(self):
        if not isinstance(self.context, PolicyContext) or not _identifiers(
            getattr(self, key)
            for key in (
                "state_digest",
                "candidate_digest",
                "scope_id",
                "scope_version",
                "account_id",
                "account_version",
            )
        ):
            raise ValueError("Decision snapshot requires state, candidates and identity revisions")
        if type(self.candidate_count) is not int or not 0 <= self.candidate_count <= 256:
            raise ValueError("Decision candidate count must be bounded")

    def to_dict(self):
        return {**vars(self), "context": dict(vars(self.context))}


@dataclass(frozen=True)
class ThresholdPolicy:
    """An explicit policy record, with no installed or inferred default threshold.

    The publication layer must verify the validation record and management
    authority before supplying a published record. This pure module does not
    publish policies or treat record construction as evidence of qualification.
    """

    policy_id: str
    policy_version: str
    question_id: str
    question_version: str
    primitive: str
    actual_model: str
    context: PolicyContext
    min_candidates: int
    max_candidates: int
    threshold: float
    valid_from: datetime
    valid_until: datetime
    validation_record: str
    status: str = "draft"

    def __post_init__(self):
        if not _identifiers(
            (
                self.policy_id,
                self.policy_version,
                self.question_id,
                self.question_version,
                self.validation_record,
            )
        ):
            raise ValueError("Policy identity, question version and validation record are required")
        if (
            not isinstance(self.primitive, str)
            or not isinstance(self.status, str)
            or self.primitive not in _PRIMITIVES
            or self.status not in {"draft", "validated", "published", "revoked"}
        ):
            raise ValueError("Unsupported policy primitive or publication state")
        if not isinstance(self.actual_model, str) or not MODEL_ID.fullmatch(self.actual_model):
            raise ValueError("Policy must pin a resolved actual Jev model")
        if not isinstance(self.context, PolicyContext):
            raise ValueError("Policy applicability context is required")
        if (
            type(self.min_candidates) is not int
            or type(self.max_candidates) is not int
            or not 2 <= self.min_candidates <= self.max_candidates <= 256
        ):
            raise ValueError("Policy candidate range must be explicitly bounded")
        object.__setattr__(self, "threshold", probability(self.threshold))
        if (
            not _aware(self.valid_from)
            or not _aware(self.valid_until)
            or self.valid_from >= self.valid_until
        ):
            raise ValueError("Policy requires an ordered timezone-aware validity window")


@dataclass(frozen=True)
class NormalizedAnswer:
    primitive: str
    raw_answer: Mapping[str, Any]
    certainty: float
    certainty_kind: str
    suggested_value: str | bool | float | None
    normalization_version: str = NORMALIZATION_VERSION

    @property
    def raw_confidence(self):
        return self.raw_answer.get("confidence")

    @property
    def p_yes(self):
        return self.raw_answer.get("noul") if self.primitive == "noul" else None

    def to_dict(self):
        return {
            **vars(self),
            "raw_answer": json_value(self.raw_answer),
            "raw_confidence": self.raw_confidence,
            "p_yes": self.p_yes,
        }


def _wire_answer(answer):
    if isinstance(answer, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": answer.choice,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "type": "score",
            "score": answer.score,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
            "legend": dict(answer.legend),
        }
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "noul": answer.probability}
    if not isinstance(answer, Mapping):
        raise ValueError("Answer must be a primitive wire record or typed answer")
    return {key: answer[key] for key in answer}


def normalize_answer(question: QuestionSpec, answer) -> NormalizedAnswer:
    """Validate the original distribution/scale without renormalizing wire values.

    Existing typed contracts enforce the provider's probability/confidence
    consistency and wire-precision rules. Noul has no raw confidence. Decimal
    arithmetic avoids binary subtraction changing an exact threshold boundary.
    """
    if not isinstance(question, QuestionSpec):
        raise ValueError("A trusted QuestionSpec is required")
    raw = _wire_answer(answer)
    expected_fields = {
        "choice": {"type", "choice", "probabilities", "confidence"},
        "noul": {"type", "noul"},
        "score": {"type", "score", "probabilities", "confidence", "legend"},
    }[question.type]
    if set(raw) != expected_fields or raw["type"] != question.type:
        raise ValueError("Answer fields must match the requested primitive")
    if question.type == "noul":
        parsed = NoulAnswer(raw["noul"])
        certainty = float(abs(2 * Decimal(str(parsed.probability)) - 1))
        suggested = None if parsed.probability == 0.5 else parsed.probability > 0.5
        kind = "derived_certainty"
    elif question.type == "choice":
        if not isinstance(raw["choice"], str) or raw["choice"] not in question.criteria:
            raise ValueError("Choice must name one legal candidate")
        if not isinstance(raw["probabilities"], Mapping) or set(raw["probabilities"]) != set(
            question.criteria
        ):
            raise ValueError("Choice distribution must contain the complete legal candidate set")
        parsed = ChoiceAnswer(raw["choice"], raw["probabilities"], raw["confidence"])
        certainty, suggested, kind = parsed.confidence, parsed.choice, "confidence"
    else:
        legend = {str(i): description for i, description in enumerate(question.criteria)}
        if raw["legend"] != legend:
            raise ValueError("Score legend must match the original ordered scale")
        parsed = ScoreAnswer(raw["score"], raw["probabilities"], raw["confidence"], raw["legend"])
        certainty, suggested, kind = parsed.confidence, parsed.score, "confidence"
    # Reconstructed typed answers own copies of distributions; retain an equally
    # isolated raw record so later caller mutation cannot rewrite audit evidence.
    raw = {**raw}
    for key in ("probabilities", "legend"):
        if key in raw:
            raw[key] = dict(raw[key])
    return NormalizedAnswer(question.type, immutable(raw), certainty, kind, suggested)


@dataclass(frozen=True)
class DecisionGate:
    status: str
    reason: str
    answer: NormalizedAnswer | None
    actual_model: str | None
    snapshot: DecisionSnapshot | None
    current_snapshot: DecisionSnapshot | None
    policy_id: str | None
    policy_version: str | None
    threshold: float | None
    mismatches: tuple[str, ...] = ()

    @property
    def may_consume(self):
        """Only semantic consumption; parameter, ACL and write checks follow."""
        return self.status == "automatic"

    @property
    def grants_write_authority(self):
        return False

    @property
    def primary_fallback(self):
        return False

    def to_dict(self):
        return {
            **vars(self),
            "answer": self.answer.to_dict() if self.answer else None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "current_snapshot": self.current_snapshot.to_dict() if self.current_snapshot else None,
            "mismatches": list(self.mismatches),
            "may_consume": self.may_consume,
            "grants_write_authority": False,
            "primary_fallback": False,
        }


def gate_decision(
    question: QuestionSpec,
    answer,
    *,
    actual_model: str | None,
    snapshot: DecisionSnapshot,
    current_snapshot: DecisionSnapshot,
    policy: ThresholdPolicy | None = None,
    now: datetime,
) -> DecisionGate:
    """Return an explicit manual reason unless a current, matched answer qualifies.

    No database, model or authorization service is called. Schema ValueErrors are
    converted to invalid answers; permission and other operational errors remain
    visible to the caller rather than being relabeled as low model certainty.
    """
    normalized = None

    def finish(reason, *, mismatches=()):
        known = isinstance(policy, ThresholdPolicy)
        return DecisionGate(
            "automatic" if reason == "threshold_met" else "manual",
            reason,
            normalized,
            actual_model,
            snapshot if isinstance(snapshot, DecisionSnapshot) else None,
            current_snapshot if isinstance(current_snapshot, DecisionSnapshot) else None,
            policy.policy_id if known else None,
            policy.policy_version if known else None,
            policy.threshold if known else None,
            tuple(mismatches),
        )

    try:
        normalized = normalize_answer(question, answer)
    except ValueError:
        return finish("invalid_answer")
    if not isinstance(snapshot, DecisionSnapshot) or not isinstance(
        current_snapshot, DecisionSnapshot
    ):
        return finish("invalid_snapshot")
    if snapshot.candidate_count != len(question.criteria):
        return finish("candidate_count_mismatch")
    if snapshot != current_snapshot:
        fields = [
            key
            for key in vars(snapshot)
            if key != "context" and getattr(snapshot, key) != getattr(current_snapshot, key)
        ]
        fields.extend(
            "context." + key
            for key in vars(snapshot.context)
            if getattr(snapshot.context, key) != getattr(current_snapshot.context, key)
        )
        return finish("stale_decision", mismatches=fields)
    if snapshot.context.normalization_version != NORMALIZATION_VERSION:
        return finish("normalization_unavailable")
    if not isinstance(actual_model, str) or not MODEL_ID.fullmatch(actual_model):
        return finish("actual_model_unresolved")
    if normalized.primitive == "noul" and normalized.suggested_value is None:
        return finish("undecided_noul")
    if policy is None or (isinstance(policy, ThresholdPolicy) and policy.status != "published"):
        return finish("policy_unavailable")
    if not isinstance(policy, ThresholdPolicy):
        return finish("invalid_policy")
    if policy.actual_model != actual_model:
        return finish("model_version_mismatch")
    if (policy.question_id, policy.question_version, policy.primitive) != (
        question.id,
        question.version,
        question.type,
    ):
        return finish("policy_question_mismatch")
    if policy.context != snapshot.context:
        return finish(
            "policy_context_mismatch",
            mismatches=[
                key
                for key in vars(policy.context)
                if getattr(policy.context, key) != getattr(snapshot.context, key)
            ],
        )
    if not policy.min_candidates <= snapshot.candidate_count <= policy.max_candidates:
        return finish("candidate_range_mismatch")
    if not _aware(now):
        return finish("invalid_time")
    if now < policy.valid_from:
        return finish("policy_not_yet_valid")
    if now >= policy.valid_until:
        return finish("policy_expired")
    return finish(
        "threshold_met" if normalized.certainty >= policy.threshold else "below_threshold"
    )

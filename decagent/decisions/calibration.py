"""Versioned human-label manifests and calibration-only threshold fitting."""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from .contracts import QUESTIONS, ChoiceAnswer, NoulAnswer, probability
from .policy import CalibrationPolicy


@dataclass(frozen=True)
class SourceManifest:
    source_id: str
    source_url: str
    license_id: str
    permission_note: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in vars(self).values()):
            raise ValueError("Sources require identifiers, URLs, license and permission notes")
        parsed = urlsplit(self.source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("A provenance URL must be HTTPS and cannot contain credentials")


@dataclass(frozen=True)
class HumanSample:
    sample_id: str
    question_id: str
    question_version: str
    input_hash: str
    source_id: str
    leakage_group: str
    split: Literal["calibration", "holdout"]
    human_label: str | bool | None = None
    labeler: str | None = None

    def __post_init__(self):
        if self.question_id not in QUESTIONS:
            raise ValueError("Unknown sample question")
        question = QUESTIONS[self.question_id]
        if self.question_version != question.version:
            raise ValueError("Incompatible question version")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.sample_id, self.source_id, self.leakage_group)
        ):
            raise ValueError("Sample, source and leakage identifiers must be nonempty")
        if not isinstance(self.input_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.input_hash
        ):
            raise ValueError("Sample input requires a SHA256 hash")
        if self.split not in {"calibration", "holdout"}:
            raise ValueError("Unknown sample split")
        if self.human_label is None:
            if self.labeler is not None:
                raise ValueError("An unlabeled sample cannot claim a labeler")
            return
        if not isinstance(self.labeler, str) or not self.labeler.strip():
            raise ValueError("Human labels must name their human labeling source")
        if question.type == "noul":
            if type(self.human_label) is not bool:
                raise ValueError("Binary human labels must be booleans")
        elif not isinstance(self.human_label, str) or self.human_label not in question.criteria:
            raise ValueError("Unknown human choice label")


@dataclass(frozen=True)
class HumanDataset:
    dataset_version: str
    sources: tuple[SourceManifest, ...]
    samples: tuple[HumanSample, ...]
    format_version: str = "1"

    def __post_init__(self):
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "samples", tuple(self.samples))
        if (
            not self.dataset_version
            or self.format_version != "1"
            or not self.sources
            or not self.samples
        ):
            raise ValueError("Dataset requires versioned sources and samples")
        sources = {source.source_id for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("Duplicate source identifiers")
        identifiers = set()
        hashes, groups = {}, {}
        for sample in self.samples:
            if sample.source_id not in sources or sample.sample_id in identifiers:
                raise ValueError("Unknown source or duplicate sample identifier")
            identifiers.add(sample.sample_id)
            for table, key in ((hashes, sample.input_hash), (groups, sample.leakage_group)):
                if key in table and table[key] != sample.split:
                    raise ValueError("Calibration and holdout leakage")
                table[key] = sample.split

    @property
    def has_complete_human_labels(self) -> bool:
        return all(sample.human_label is not None for sample in self.samples)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        if not isinstance(data, Mapping) or set(data) != {
            "dataset_version",
            "format_version",
            "sources",
            "samples",
        }:
            raise ValueError("Invalid dataset format")
        try:
            return cls(
                data["dataset_version"],
                tuple(SourceManifest(**source) for source in data["sources"]),
                tuple(HumanSample(**sample) for sample in data["samples"]),
                data["format_version"],
            )
        except (TypeError, KeyError) as exc:
            raise ValueError("Invalid source or sample format") from exc


@dataclass(frozen=True)
class CalibrationPrediction:
    sample_id: str
    actual_model: str
    question_version: str
    answer: ChoiceAnswer | NoulAnswer


def calibrate(
    dataset: HumanDataset,
    question_id: str,
    actual_model: str,
    predictions: Sequence[CalibrationPrediction],
    *,
    policy_version: str,
    max_error_rate: float = 0.0,
    min_accepted: int = 1,
) -> CalibrationPolicy:
    """Fit one question on calibration data only; this is not an accuracy guarantee."""
    if question_id not in QUESTIONS:
        raise ValueError("Unknown calibration question")
    probability(max_error_rate)
    if type(min_accepted) is not int or min_accepted < 1:
        raise ValueError("Minimum accepted sample count must be positive")
    samples = [
        sample
        for sample in dataset.samples
        if sample.question_id == question_id and sample.split == "calibration"
    ]
    if not samples or any(sample.human_label is None for sample in samples):
        raise ValueError("Calibration requires actual human labels")
    by_id = {prediction.sample_id: prediction for prediction in predictions}
    if len(by_id) != len(predictions):
        raise ValueError("Duplicate predictions")
    scores = []
    for sample in samples:
        predicted = by_id.get(sample.sample_id)
        if (
            predicted is None
            or predicted.actual_model != actual_model
            or (predicted.question_version != sample.question_version)
        ):
            raise ValueError("Missing prediction or incompatible model/question version")
        answer = predicted.answer
        if QUESTIONS[question_id].type == "choice":
            if (
                not isinstance(answer, ChoiceAnswer)
                or answer.choice not in QUESTIONS[question_id].criteria
            ):
                raise ValueError("Choice prediction does not match the question")
            if set(answer.probabilities) != set(QUESTIONS[question_id].criteria):
                raise ValueError("Choice prediction requires every declared option")
            score = probability(answer.confidence)
            correct = answer.choice == sample.human_label
        else:
            if not isinstance(answer, NoulAnswer):
                raise ValueError("Binary prediction does not match the question")
            p = probability(answer.probability)
            score, correct = max(p, 1 - p), (p >= 0.5) == sample.human_label
        scores.append((score, correct))
    candidates = sorted({score for score, _ in scores} | {1.0})
    for threshold in candidates:
        if QUESTIONS[question_id].type == "noul" and threshold <= 0.5:
            continue
        accepted = [correct for score, correct in scores if score >= threshold]
        if (
            len(accepted) >= min_accepted
            and sum(not item for item in accepted) / len(accepted) <= max_error_rate
        ):
            return CalibrationPolicy(
                question_id,
                QUESTIONS[question_id].version,
                actual_model,
                dataset.dataset_version,
                policy_version,
                threshold,
                tuple(sample.sample_id for sample in samples),
            )
    raise ValueError("No threshold meets the requested calibration acceptance criteria")


def export_calibrations(policies: Sequence[CalibrationPolicy]) -> str:
    return json.dumps(
        {"format_version": "1", "policies": [asdict(policy) for policy in policies]},
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def load_calibrations(data: Mapping[str, Any]) -> tuple[CalibrationPolicy, ...]:
    if not isinstance(data, Mapping) or set(data) != {"format_version", "policies"}:
        raise ValueError("Invalid calibration document")
    if data["format_version"] != "1" or not isinstance(data["policies"], list):
        raise ValueError("Unknown calibration document format")
    result, identities = [], set()
    for value in data["policies"]:
        try:
            value = dict(value)
            value["calibration_ids"] = tuple(value["calibration_ids"])
            policy = CalibrationPolicy(**value)
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid calibration policy") from exc
        identity = (
            policy.question_id,
            policy.question_version,
            policy.actual_model,
            policy.dataset_version,
            policy.policy_version,
        )
        if identity in identities:
            raise ValueError("Duplicate calibration bindings")
        identities.add(identity)
        result.append(policy)
    return tuple(result)

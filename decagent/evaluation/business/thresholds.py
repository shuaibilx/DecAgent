"""Content-addressed Jev predictions and offline confidence-threshold scans.

The cache stores no user prompt or database snapshot. Threshold scans never call
Jev; synthetic labels can produce diagnostic curves but cannot select a live
policy. A trusted policy still requires the separate owner validation gate.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from decagent.decisions.contracts import MODEL_ID, canonical_json

from .metrics import wilson_interval

CACHE_SCHEMA = "business-confidence-prediction-cache-1"
REPORT_SCHEMA = "business-confidence-threshold-grid-1"
DEFAULT_THRESHOLD_GRID = (0.0, 0.50, 0.70, 0.80, 0.90, 0.95, 0.99)
_HASH = frozenset("0123456789abcdef")
_METADATA_FIELDS = {
    "dataset_version",
    "dataset_checksum",
    "split",
    "label_source",
    "human_reviewed",
    "review_reference",
    "review_sha256",
    "actual_model",
}
_RECORD_FIELDS = {
    "sample_id",
    "task_id",
    "family",
    "question_version",
    "primitive",
    "input_hash",
    "status",
    "raw_answer",
    "prediction",
    "reference",
    "certainty",
}


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HASH for character in value)
    )


def _scalar(value):
    return (
        value is None
        or type(value) in {str, bool, int, float}
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _prediction_cache_body(metadata, records):
    return {
        "schema_version": CACHE_SCHEMA,
        "metadata": metadata,
        "records": records,
    }


def registered_business_families(spec):
    """Return the current program-owned semantic family/version allowlist."""
    from decagent.business.plans.templates import QuestionRegistry

    return QuestionRegistry.from_spec(spec).describe()


def _registry_keys(registered_families):
    if not isinstance(registered_families, list) or not registered_families:
        raise ValueError("Threshold scans require a trusted registered-family catalogue")
    registered = set()
    for item in registered_families:
        if (
            not isinstance(item, dict)
            or not {
                "question_id",
                "version",
                "type",
            }
            <= item.keys()
        ):
            raise ValueError("Registered family entries require question ID, version and type")
        if (
            not _nonempty(item["question_id"])
            or not _nonempty(item["version"])
            or item["type"] not in {"choice", "noul", "score"}
        ):
            raise ValueError("Registered family identity is invalid")
        key = (item["question_id"], item["version"].replace("/", "-"), item["type"])
        if key in registered:
            raise ValueError("Registered family catalogue contains duplicates")
        registered.add(key)
    return registered


def _bound_request_hash(request, question, actual_model):
    """Bind raw predictions to evidence, identity/version context, options and model."""
    value = {
        "context": vars(request.context),
        "state_hash": request.input_hash,
        "question": {
            "id": question.id,
            "version": question.version,
            **question.to_wire(),
        },
        "actual_model": actual_model,
    }
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_FIELDS:
        raise ValueError("Prediction cache metadata fields are incomplete or unexpected")
    if (
        not _nonempty(metadata["dataset_version"])
        or not _sha256(metadata["dataset_checksum"])
        or metadata["split"] not in {"development", "validation", "holdout", "test"}
        or not _nonempty(metadata["label_source"])
        or type(metadata["human_reviewed"]) is not bool
        or not isinstance(metadata["actual_model"], str)
        or not MODEL_ID.fullmatch(metadata["actual_model"])
    ):
        raise ValueError("Prediction cache must pin dataset split, labels and actual model")
    if metadata["human_reviewed"]:
        if not _nonempty(metadata["review_reference"]) or not _sha256(metadata["review_sha256"]):
            raise ValueError("Reviewed labels require a content-addressed review record")
    elif metadata["review_reference"] is not None or metadata["review_sha256"] is not None:
        raise ValueError("Unreviewed labels cannot claim a review record")


def _validate_records(records):
    if not isinstance(records, list) or not records:
        raise ValueError("Prediction cache requires at least one record")
    samples, inputs = set(), set()
    for row in records:
        if not isinstance(row, dict) or set(row) != _RECORD_FIELDS:
            raise ValueError("Prediction record fields are incomplete or unexpected")
        for key in ("sample_id", "task_id", "family", "question_version"):
            if not _nonempty(row[key]):
                raise ValueError(f"Prediction record requires {key}")
        if (
            row["primitive"] not in {"choice", "noul", "score"}
            or not _sha256(row["input_hash"])
            or row["status"] not in {"valid", "abstain", "invalid_response"}
            or not _scalar(row["reference"])
            or row["reference"] is None
        ):
            raise ValueError("Prediction record has invalid primitive, input or label")
        if row["sample_id"] in samples:
            raise ValueError("Prediction cache contains duplicate sample IDs")
        samples.add(row["sample_id"])
        input_family = (row["input_hash"], row["family"])
        if input_family in inputs:
            raise ValueError("Prediction cache contains duplicate input/family predictions")
        inputs.add(input_family)

        if row["status"] == "invalid_response":
            if any(row[key] is not None for key in ("raw_answer", "prediction", "certainty")):
                raise ValueError("Invalid responses cannot contain a repaired prediction")
            continue
        raw = row["raw_answer"]
        certainty = row["certainty"]
        if (
            not isinstance(raw, dict)
            or raw.get("type") != row["primitive"]
            or isinstance(certainty, bool)
            or not isinstance(certainty, (int, float))
            or not math.isfinite(certainty)
            or not 0 <= certainty <= 1
        ):
            raise ValueError("Raw answer and normalized certainty are required")
        if row["primitive"] == "noul":
            probability = raw.get("noul")
            if (
                isinstance(probability, bool)
                or not isinstance(probability, (int, float))
                or not math.isfinite(probability)
                or not 0 <= probability <= 1
                or not math.isclose(certainty, abs(2 * probability - 1), abs_tol=1e-9)
            ):
                raise ValueError("Noul certainty must use the derived yes/no distance")
            expected = None if probability == 0.5 else probability > 0.5
            if row["prediction"] is not expected:
                raise ValueError("Noul prediction differs from the raw probability")
            if (row["status"] == "abstain") != (probability == 0.5):
                raise ValueError("Only an undecided Noul answer may be marked abstain")
        else:
            answer_key = "choice" if row["primitive"] == "choice" else "score"
            confidence = raw.get("confidence")
            if (
                not _scalar(row["prediction"])
                or row["prediction"] is None
                or raw.get(answer_key) != row["prediction"]
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not math.isclose(certainty, confidence, abs_tol=1e-9)
                or row["status"] != "valid"
            ):
                raise ValueError("Choice/Score prediction must match its raw answer and confidence")


def create_prediction_cache(metadata, records):
    """Freeze scorer-side labels and raw Jev answers for repeated offline scans."""
    _validate_metadata(metadata)
    _validate_records(records)
    body = _prediction_cache_body(metadata, records)
    encoded = canonical_json(body)
    if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("Prediction cache exceeds the 16 MiB limit")
    return {**body, "checksum": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}


def cache_from_calibration_export(dataset, export, spec):
    """Turn a fully validated development export into a scorer-only raw cache."""
    from decagent.business.decision_policy import normalize_answer
    from decagent.business.questions import build_business_request
    from decagent.decisions.contracts import validate_answers

    from .calibration import fit_export
    from .dataset import select_cases

    fit_export(dataset, export, spec)
    cases = {case["id"]: case for case in select_cases(dataset, "development")}
    entries = {entry["case_id"]: entry for entry in export["records"]}
    model = export["records"][0]["evaluation"]["actual_model"]
    records = []
    for case_id, entry in entries.items():
        case = cases[case_id]
        request = build_business_request(
            spec,
            entry["task_id"],
            entry["scope_id"],
            entry["instruction"],
            entry["evidence"],
            dataset_version=dataset["version"],
        )
        result = entry["evaluation"]
        invalid = result.get("error") == "invalid_response"
        answers = {} if invalid else validate_answers(result["answers"], request)
        for question in request.questions:
            reference = case["decision_labels"].get(question.id)
            if reference is None:
                raise ValueError(f"Frozen development label is missing: {question.id}")
            if invalid:
                status, raw, prediction, certainty = "invalid_response", None, None, None
            else:
                answer = answers.get(question.id)
                if answer is None:
                    raise ValueError(f"Development answer is missing: {question.id}")
                normalized = normalize_answer(question, answer)
                raw = normalized.to_dict()["raw_answer"]
                prediction, certainty = normalized.suggested_value, normalized.certainty
                status = "abstain" if prediction is None else "valid"
            records.append(
                {
                    "sample_id": f"{case_id}:{question.id}",
                    "task_id": str(entry["task_id"]),
                    "family": question.id,
                    "question_version": question.version,
                    "primitive": question.type,
                    "input_hash": _bound_request_hash(request, question, model),
                    "status": status,
                    "raw_answer": raw,
                    "prediction": prediction,
                    "reference": reference,
                    "certainty": certainty,
                }
            )
    return create_prediction_cache(
        {
            "dataset_version": dataset["version"],
            "dataset_checksum": dataset["checksum"],
            "split": "development",
            "label_source": dataset["label_source"],
            "human_reviewed": False,
            "review_reference": None,
            "review_sha256": None,
            "actual_model": model,
        },
        records,
    )


def validate_prediction_cache(cache):
    if not isinstance(cache, dict) or set(cache) != {
        "schema_version",
        "metadata",
        "records",
        "checksum",
    }:
        raise ValueError("Prediction cache envelope is malformed")
    if cache["schema_version"] != CACHE_SCHEMA:
        raise ValueError("Unsupported prediction cache schema")
    _validate_metadata(cache["metadata"])
    _validate_records(cache["records"])
    body = _prediction_cache_body(cache["metadata"], cache["records"])
    checksum = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    if not _sha256(cache["checksum"]) or cache["checksum"] != checksum:
        raise ValueError("Prediction cache checksum mismatch")
    return json.loads(canonical_json(cache))


def write_prediction_cache(path, cache):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = validate_prediction_cache(cache)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(validated, ensure_ascii=False, indent=2) + "\n")
    return path


def read_prediction_cache(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Prediction cache exceeds the 16 MiB limit")
    return validate_prediction_cache(json.loads(path.read_text(encoding="utf-8")))


def scan_threshold_grid(
    cache,
    *,
    registered_families,
    thresholds=DEFAULT_THRESHOLD_GRID,
    target_accuracy=0.95,
    minimum_auto=100,
):
    """Select the highest-coverage threshold from verified development/validation data."""
    cache = validate_prediction_cache(cache)
    metadata = cache["metadata"]
    registered = _registry_keys(registered_families)
    if metadata["split"] not in {"development", "validation"}:
        raise ValueError("Threshold selection accepts development or validation data only")
    if (
        isinstance(minimum_auto, bool)
        or not isinstance(minimum_auto, int)
        or minimum_auto < 1
        or isinstance(target_accuracy, bool)
        or not isinstance(target_accuracy, (int, float))
        or not math.isfinite(target_accuracy)
        or not 0 < target_accuracy <= 1
    ):
        raise ValueError("Threshold target and minimum automatic sample count are invalid")
    grid = tuple(float(value) for value in thresholds)
    if (
        not grid
        or len(grid) > 32
        or len(set(grid)) != len(grid)
        or any(not math.isfinite(value) or not 0 <= value <= 1 for value in grid)
    ):
        raise ValueError("Threshold grid must contain 1 to 32 unique probabilities")

    groups = defaultdict(list)
    for row in cache["records"]:
        family_key = (
            row["family"],
            row["question_version"].replace("/", "-"),
            row["primitive"],
        )
        if family_key not in registered:
            raise ValueError("Prediction family or version is not present in the trusted registry")
        key = (
            row["family"],
            row["primitive"],
            row["question_version"],
            metadata["actual_model"],
        )
        groups[key].append(row)
    labels_verified = (
        metadata["split"] == "validation"
        and metadata["label_source"] == "human-verified"
        and metadata["human_reviewed"] is True
    )
    families = []
    for key in sorted(groups):
        rows = groups[key]
        grid_rows = []
        for threshold in sorted(grid):
            automated = [
                row for row in rows if row["status"] == "valid" and row["certainty"] >= threshold
            ]
            correct = sum(
                canonical_json(row["prediction"]) == canonical_json(row["reference"])
                for row in automated
            )
            total = len(rows)
            automatic = len(automated)
            accuracy = correct / automatic if automatic else None
            low, high = wilson_interval(correct, automatic)
            observed_qualifies = (
                automatic >= minimum_auto and accuracy is not None and accuracy >= target_accuracy
            )
            grid_rows.append(
                {
                    "threshold": threshold,
                    "n_total": total,
                    "n_auto": automatic,
                    "n_correct": correct,
                    "accuracy": accuracy,
                    "coverage": automatic / total,
                    "false_interventions": sum(
                        row["status"] == "valid"
                        and row["certainty"] < threshold
                        and canonical_json(row["prediction"]) == canonical_json(row["reference"])
                        for row in rows
                    ),
                    "missed_interventions": automatic - correct,
                    "unresolved": sum(row["status"] == "invalid_response" for row in rows),
                    "confidence_interval_95": [low, high] if low is not None else None,
                    "observed_qualifies": observed_qualifies,
                    "policy_eligible": observed_qualifies and labels_verified,
                }
            )
        eligible = [row for row in grid_rows if row["policy_eligible"]]
        chosen = (
            max(eligible, key=lambda row: (row["n_auto"], row["threshold"])) if eligible else None
        )
        families.append(
            {
                "family": key[0],
                "primitive": key[1],
                "question_version": key[2],
                "actual_model": key[3],
                "selected_threshold": chosen["threshold"] if chosen else None,
                "status": (
                    "qualified"
                    if chosen
                    else "not_met"
                    if labels_verified
                    else "diagnostic_only_unverified_labels"
                ),
                "grid": grid_rows,
            }
        )
    return {
        "schema_version": REPORT_SCHEMA,
        "cache_checksum": cache["checksum"],
        "dataset_version": metadata["dataset_version"],
        "dataset_checksum": metadata["dataset_checksum"],
        "split": metadata["split"],
        "label_source": metadata["label_source"],
        "labels_verified": labels_verified,
        "target_accuracy": float(target_accuracy),
        "minimum_auto": minimum_auto,
        "threshold_grid": list(sorted(grid)),
        "families": families,
    }

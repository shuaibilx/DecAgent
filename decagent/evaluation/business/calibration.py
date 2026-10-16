"""Fit shared thresholds with each development answer's own request contract."""

import math
from dataclasses import replace

from decagent.business.calibration import BusinessCalibration, certainty, predicted
from decagent.business.questions import build_business_request
from decagent.decisions.contracts import (
    MODEL_ID,
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    jev_model_matches,
    json_value,
    parse_usage,
    validate_answers,
)

from .dataset import select_cases

BINDINGS = (
    "plugin_digest",
    "workflow_version",
    "question_version",
    "policy_version",
    "dataset_version",
)

EXPORT_V1 = "business-calibration-export-1"
EXPORT_V2 = "business-calibration-export-2"


def development_evaluation_kind(result, *, allow_invalid=False):
    """Classify one uncached, fully accounted attempt; never repair its answers."""
    if not isinstance(result, dict) or (
        result.get("format_version") != "1"
        or result.get("strategy") != "jev"
        or result.get("cached") is not False
    ):
        raise ValueError("Failed or cached development evaluation cannot calibrate")
    error = result.get("error")
    if error is not None and not (allow_invalid and error == "invalid_response"):
        raise ValueError("Failed development evaluation cannot calibrate")
    attempts = result.get("attempts", [])
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise ValueError("Development collection requires one uncached accounted attempt")
    attempt = attempts[0]
    actual, requested = result.get("actual_model"), result.get("requested_model")
    if not isinstance(attempt, dict) or (
        type(attempt.get("attempt")) is not int
        or attempt["attempt"] != 1
        or attempt.get("error") != error
        or type(attempt.get("status_code")) is not int
        or not 200 <= attempt["status_code"] < 300
        or not isinstance(actual, str)
        or not MODEL_ID.fullmatch(actual)
        or attempt.get("actual_model") != actual
        or not isinstance(requested, str)
        or not requested.strip()
        or attempt.get("requested_model") != requested
        or not jev_model_matches(requested, actual)
        or parse_usage(attempt) is None
        or any(
            type(attempt.get(key)) not in {int, float}
            or not math.isfinite(attempt[key])
            or attempt[key] < 0
            for key in ("cost_usd", "reserved_usd", "elapsed_seconds")
        )
        or attempt["reserved_usd"] <= 0
        or attempt["cost_usd"] > attempt["reserved_usd"]
    ):
        raise ValueError("Unknown development usage or inconsistent attempt cannot calibrate")
    if error == "invalid_response":
        if result.get("answers") != {}:
            raise ValueError("An invalid response must retain no calibration answers")
        return "invalid_response"
    return "valid"


def fit_development_profile(dataset, rows):
    development_ids = {c["id"] for c in select_cases(dataset, "development")}
    if not rows:
        raise ValueError("No development calibration answers")
    first = rows[0]
    model, context = first["actual_model"], first["request"].context
    thresholds, families = {}, {}
    for row in rows:
        request = row["request"]
        if row["case_id"] not in development_ids:
            raise ValueError("Only development cases may fit calibration")
        if (
            row["actual_model"] != model
            or request.context.dataset_version != dataset["version"]
            or any(getattr(request.context, key) != getattr(context, key) for key in BINDINGS)
        ):
            raise ValueError("Actual model or calibration binding drift")
        question = next((q for q in request.questions if q.id == row["question_id"]), None)
        answer, label = row["answer"], row["expected"]
        if question is None:
            raise ValueError("Unknown question in its own request")
        if question.type == "choice":
            if (
                not isinstance(answer, ChoiceAnswer)
                or type(label) is not str
                or set(answer.probabilities) != set(question.criteria)
                or label not in question.criteria
            ):
                raise ValueError("Reference or answer differs from its own legal candidate set")
        elif question.type == "noul":
            if not isinstance(answer, NoulAnswer) or type(label) is not bool:
                raise ValueError("Noul reference or answer type mismatch")
        elif question.type == "score":
            if (
                not isinstance(answer, ScoreAnswer)
                or type(label) is not int
                or not 0 <= label < len(question.criteria)
                or dict(answer.legend) != {str(i): v for i, v in enumerate(question.criteria)}
            ):
                raise ValueError("Score reference or rubric mismatch")
        else:
            raise ValueError("Unsupported calibration question")
        families.setdefault(question.id, []).append(
            (certainty(answer), predicted(answer) == label, question.type)
        )
    for name, values in families.items():
        wrong = [confidence for confidence, correct, _ in values if not correct]
        threshold = math.nextafter(max(wrong), math.inf) if wrong else 0.0
        usable = [
            confidence
            for confidence, correct, kind in values
            if correct and confidence >= threshold and confidence > (0.5 if kind == "noul" else 0)
        ]
        thresholds[name] = threshold if usable and threshold <= 1 else None
    return BusinessCalibration(
        model=model, **{key: getattr(context, key) for key in BINDINGS}, thresholds=thresholds
    )


def fit_export(dataset, export, spec):
    """Reject partial development collections, holdout leakage, and changed snapshots."""
    version = export.get("schema_version")
    if (
        version not in {EXPORT_V1, EXPORT_V2}
        or export.get("dataset_checksum") != dataset["checksum"]
        or export.get("label_source") != "synthetic-reference"
        or (version == EXPORT_V2 and export.get("dataset_version") != dataset["version"])
    ):
        raise ValueError("Calibration export schema or frozen checksum mismatch")
    if (
        export.get("complete") is not True
        or export.get("stop_reason")
        or (version == EXPORT_V2 and export.get("collection_complete") is not True)
    ):
        raise ValueError("A complete uninterrupted development export is required")
    snapshot = export.get("global_jev_ledger")
    if (
        version == EXPORT_V2
        and snapshot is not None
        and (
            not isinstance(snapshot, dict)
            or snapshot.get("blocked_unknown_or_pending") is not False
            or snapshot.get("within_cap") is not True
        )
    ):
        raise ValueError("Unknown global usage or budget stop cannot calibrate")
    cases = {c["id"]: c for c in select_cases(dataset, "development")}
    entries = export.get("records", [])
    if len(entries) != 40 or {r["case_id"] for r in entries} != set(cases):
        raise ValueError("A complete development-only collection of 40 cases is required")
    rows, disabled = [], set()
    first_request, model, requested = None, None, None
    for entry in entries:
        case = cases[entry["case_id"]]
        ticket = entry["evidence"].get("ticket")
        ticket_id = ticket["id"] if ticket else None
        template = case["instruction"].replace("{ticket_id}", ticket_id or "")
        if entry["instruction"] != template:
            raise ValueError("Development instruction differs from the frozen case")
        directory = entry["evidence"].get("directory", {})
        if any(
            sorted(directory.get(key, []), key=lambda row: row["id"])
            != sorted(dataset["directory"][key], key=lambda row: row["id"])
            for key in ("people", "categories")
        ):
            raise ValueError("Development directory differs from the frozen candidates")
        if (case["setup"]["status"] == "none") != (ticket is None) or (
            ticket is not None
            and any(
                ticket.get(key) != case["setup"][key] for key in ("status", "title", "category_id")
            )
        ):
            raise ValueError("Development evidence differs from the frozen resource setup")
        request = build_business_request(
            spec,
            entry["task_id"],
            entry["scope_id"],
            entry["instruction"],
            entry["evidence"],
            dataset_version=dataset["version"],
        )
        if vars(request.context) != entry["context"]:
            raise ValueError("Calibration request binding drift")
        result = entry["evaluation"]
        if (
            result.get("format_version") != "1"
            or result.get("strategy") != "jev"
            or result.get("context") != vars(request.context)
            or result.get("input_hash") != request.input_hash
            or result.get("question_versions") != request.question_versions
            or result.get("input_summary") != json_value(request.input_summary)
        ):
            raise ValueError("Development response request binding drift")
        kind = development_evaluation_kind(result, allow_invalid=version == EXPORT_V2)
        if first_request is None:
            first_request, model, requested = (
                request,
                result["actual_model"],
                result["requested_model"],
            )
        if (
            result["actual_model"] != model
            or result["requested_model"] != requested
            or any(
                getattr(request.context, key) != getattr(first_request.context, key)
                for key in BINDINGS
            )
        ):
            raise ValueError("Actual model or calibration binding drift")
        if kind == "invalid_response":
            disabled.update(request.question_versions)
            continue
        answers = validate_answers(entry["evaluation"]["answers"], request)
        for name, answer in answers.items():
            rows.append(
                {
                    "case_id": case["id"],
                    "request": request,
                    "question_id": name,
                    "answer": answer,
                    "expected": case["decision_labels"][name],
                    "actual_model": entry["evaluation"]["actual_model"],
                }
            )
    if rows:
        profile = fit_development_profile(dataset, rows)
        return replace(profile, thresholds={**profile.thresholds, **dict.fromkeys(disabled)})
    return BusinessCalibration(
        model=model,
        **{key: getattr(first_request.context, key) for key in BINDINGS},
        thresholds=dict.fromkeys(disabled),
    )

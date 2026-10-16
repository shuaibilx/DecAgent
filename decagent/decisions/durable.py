"""A committed semantic request intent prevents replay from charging twice after a crash."""

from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from decagent.policy.canonical import digest

from .contracts import (
    MODEL_ID,
    DecisionContext,
    build_request,
    evaluation,
    json_value,
    validate_answers,
)
from .policy import DecisionEngine


def _replay(engine, request, saved):
    raw = saved.get("evaluation")
    if raw is None:
        observed = evaluation(
            request, engine.strategy.name, "unknown", error="inference_result_unknown", cached=True
        )
    elif raw.get("error"):
        observed = evaluation(
            request,
            raw["strategy"],
            raw["requested_model"],
            actual_model=raw.get("actual_model"),
            error=raw["error"],
            cached=True,
        )
    else:
        try:
            if (
                raw["input_hash"] != request.input_hash
                or raw["context"] != vars(request.context)
                or raw["question_versions"] != request.question_versions
            ):
                raise ValueError("Stale semantic record")
            actual = raw["actual_model"]
            if not isinstance(actual, str) or not actual:
                raise ValueError("Missing resolved model")
            if raw["strategy"] == "jev" and not MODEL_ID.fullmatch(actual):
                raise ValueError("Missing resolved Jev version")
            answers = validate_answers(raw["answers"], request)
            observed = evaluation(
                request,
                raw["strategy"],
                raw["requested_model"],
                actual_model=actual,
                answers=answers,
                cached=True,
            )
        except (KeyError, TypeError, ValueError):
            observed = evaluation(
                request,
                engine.strategy.name,
                "unknown",
                error="invalid_durable_decision",
                cached=True,
            )

    class Replay:
        name = engine.strategy.name

        def evaluate(self, _request):
            return observed

    # Current calibration governs reuse; yesterday's automatic route grants no authority.
    return DecisionEngine(Replay(), engine.calibrations).decide(request)


def durable_decide(database, engine, request):
    request_snapshot = {
        "questions": [q.id for q in request.questions],
        "state": json_value(request.state),
        "context": vars(request.context),
    }
    signature = {
        "strategy": engine.strategy.name,
        "model": getattr(engine.strategy, "model", engine.strategy.name),
        "provider": getattr(engine.strategy, "provider", None),
    }
    legacy_identifier = str(
        uuid5(
            NAMESPACE_URL,
            digest(
                "decagent.semantic-call.v1", {"input": request.input_hash, "signature": signature}
            ),
        )
    )
    identifier = str(
        uuid5(
            NAMESPACE_URL,
            digest(
                "decagent.semantic-call.v2",
                {
                    "request": request_snapshot,
                    "signature": signature,
                },
            ),
        )
    )
    with database.connect() as conn:
        # Preserve v1 dispatch intents on this exact task; never reuse another task's record.
        legacy = conn.execute(
            "SELECT task_id,result FROM app.decision_records WHERE id=%s", (legacy_identifier,)
        ).fetchone()
        if (
            legacy
            and str(legacy["task_id"]) == request.context.task_id
            and legacy["result"].get("request") == request_snapshot
        ):
            identifier = legacy_identifier
        claimed = conn.execute(
            "INSERT INTO app.decision_records(id,task_id,input_hash,question_version,"
            "policy_version,requested_model,result,final_route) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,'review') ON CONFLICT(id) DO NOTHING RETURNING id",
            (
                identifier,
                request.context.task_id,
                request.input_hash,
                "+".join(q.id + ":" + q.version for q in request.questions),
                request.context.policy_version,
                str(signature["model"]),
                Jsonb(
                    {"status": "dispatched", "signature": signature, "request": request_snapshot}
                ),
            ),
        ).fetchone()
        if not claimed:
            saved = conn.execute(
                "SELECT result FROM app.decision_records WHERE id=%s", (identifier,)
            ).fetchone()["result"]
    if not claimed:
        return _replay(engine, request, saved), identifier
    # Commit the intent before inference. A lost response stays unknown and routes to review.
    result = engine.decide(request)
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.decision_records SET requested_model=%s,resolved_model=%s,"
            "result=%s,final_route=%s WHERE id=%s",
            (
                result.evaluation.requested_model,
                result.evaluation.actual_model,
                Jsonb({**result.to_dict(), "request": request_snapshot}),
                result.governance_outcome,
                identifier,
            ),
        )
    return result, identifier


def reroute_saved(engine, saved):
    snapshot = saved["request"]
    request = build_request(
        snapshot["questions"], snapshot["state"], DecisionContext(**snapshot["context"])
    )
    return _replay(engine, request, saved)

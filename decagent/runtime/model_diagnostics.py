"""One selected model request per durable owner operation, with a $0.05 hard reservation cap."""

from __future__ import annotations

import re
from decimal import Decimal

from psycopg.types.json import Jsonb

from decagent.decisions import CallBudget, DecisionContext, JevAdapter, build_request
from decagent.models.primary import (
    PrimaryModel,
    PrimaryRequestNotDispatched,
    PrimaryResponseRejected,
)
from decagent.policy.canonical import digest


class ModelDiagnostics:
    def __init__(self, database, settings, config, *, primary=None, jev_client=None):
        self.database, self.settings, self.config = database, settings, config
        self.primary, self.jev_client = primary, jev_client

    def __call__(self, target, operation_id):
        if target not in {"main", "jev"} or not re.fullmatch(r"[a-f0-9]{64}", operation_id):
            raise ValueError("Invalid model diagnostic operation")
        settings, config = self.settings, self.config
        model = settings.main_model if target == "main" else settings.jev_model
        fingerprint = digest(
            "decagent.diagnostic.v1",
            {
                "target": target,
                "model": model,
                "endpoint": settings.main_base_url if target == "main" else settings.jev_provider,
                "price_basis": config.main_price.version if config.main_price else "unknown",
            },
        )
        reserve = (
            config.jev_reservation
            if target == "jev"
            else (
                config.main_price.cost(32768, 256)
                if config.main_price
                else config.unknown_main_reservation
            )
        )
        key = settings.main_api_key if target == "main" else settings.jev_api_key
        with self.database.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM app.model_diagnostics WHERE operation_id=%s", (operation_id,)
            ).fetchone()
            if prior:
                return self._prior_result(prior, fingerprint)
            if not key:
                return {
                    "status": "unavailable",
                    "reason": "Selected model credential is not configured",
                    "paid_requests": 0,
                }
            if (
                reserve is None
                or not reserve.is_finite()
                or not Decimal(0) <= reserve <= Decimal("0.05")
            ):
                raise ValueError("Configure a diagnostic reservation within the $0.05 cap")
            claimed = conn.execute(
                "INSERT INTO app.model_diagnostics(operation_id,input_hash,target,requested_model,"
                "status,reserved_cost) VALUES(%s,%s,%s,%s,'dispatched',%s) "
                "ON CONFLICT(operation_id) DO NOTHING RETURNING operation_id",
                (operation_id, fingerprint, target, model, reserve),
            ).fetchone()
            if not claimed:
                prior = conn.execute(
                    "SELECT * FROM app.model_diagnostics WHERE operation_id=%s", (operation_id,)
                ).fetchone()
                return self._prior_result(prior, fingerprint)
        # The intent is committed independently of the API's outer idempotency transaction.
        status, cost = "unknown", None
        result = {
            "status": "unknown",
            "held_usd": str(reserve),
            "reason": "Selected model response is unconfirmed",
        }
        try:
            if target == "main":
                primary = self.primary or PrimaryModel.from_settings(
                    settings, max_output_tokens=256
                )
                turn = primary.respond(
                    [
                        {
                            "role": "system",
                            "content": "Connection diagnostic only. Do not propose tools.",
                        },
                        {"role": "user", "content": "Reply with a short acknowledgement."},
                    ],
                    stream=True,
                )
                usage = turn.usage
                cost = (
                    config.main_price.cost(usage.input_tokens, usage.output_tokens)
                    if (usage and config.main_price)
                    else None
                )
                result = {
                    "status": "verified",
                    "requested_model": model,
                    "actual_model": turn.model,
                    "stream_completed": True,
                    "tool_calls": len(turn.tools),
                    "tokens": usage.total_tokens if usage else None,
                    "cost_usd": str(cost) if cost is not None else None,
                    "price_basis": config.main_price.version if config.main_price else "unknown",
                }
            else:
                request = build_request(
                    ["memory_read_needed"],
                    {
                        "task_summary": "Connection diagnostic without repository content",
                        "current_version": "diagnostic-1",
                    },
                    DecisionContext(operation_id, "diagnostic", "1", "1", "1", "1"),
                )
                with JevAdapter(
                    api_key=key,
                    model=model,
                    provider=settings.jev_provider,
                    client=self.jev_client,
                    max_retries=0,
                    cache_ttl=0,
                    budget=CallBudget(max_usd=float(reserve), reserve_usd=float(reserve)),
                ) as adapter:
                    evaluation = adapter.evaluate(request)
                cost_value = evaluation.attempts[0].cost_usd if evaluation.attempts else None
                cost = Decimal(str(cost_value)) if cost_value is not None else None
                result = {
                    "status": "verified" if evaluation.available else "failed",
                    "evaluation": evaluation.to_dict(),
                    "cost_usd": str(cost) if cost is not None else None,
                }
            if cost is None:
                result["held_usd"] = str(reserve)
            elif cost > reserve:
                result.update(status="failed", reason="Reported cost exceeded the reserved cap")
            status = "passed" if result["status"] == "verified" else "failed"
        except PrimaryRequestNotDispatched as error:
            status, cost = "failed", Decimal(0)
            result = {
                "status": "failed",
                "rejection_reason": error.reason,
                "requested_model": model,
                "actual_model": None,
                "stream_completed": False,
                "tool_calls": 0,
                "tokens": 0,
                "cost_usd": str(cost),
                "price_basis": config.main_price.version if config.main_price else "unknown",
            }
        except PrimaryResponseRejected as error:
            usage = error.usage
            cost = (
                config.main_price.cost(usage.input_tokens, usage.output_tokens)
                if usage and config.main_price
                else None
            )
            status = "failed"
            result = {
                "status": "failed",
                "rejection_reason": error.reason,
                "requested_model": model,
                "actual_model": error.model,
                "stream_completed": True,
                "tool_calls": 0,
                "tokens": usage.total_tokens if usage else None,
                "cost_usd": str(cost) if cost is not None else None,
                "price_basis": config.main_price.version if config.main_price else "unknown",
            }
            if cost is None:
                result["held_usd"] = str(reserve)
            elif cost > reserve:
                result["reason"] = "Reported cost exceeded the reserved cap"
        except Exception:
            # Do not expose exception strings, client objects or credentials through diagnostics.
            pass
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE app.model_diagnostics SET status=%s,actual_cost=%s,result=%s "
                "WHERE operation_id=%s",
                (status, cost, Jsonb(result), operation_id),
            )
        return result

    @staticmethod
    def _prior_result(prior, fingerprint):
        if prior["input_hash"] != fingerprint:
            raise ValueError("Diagnostic operation reused for a different configuration")
        return prior["result"] or {
            "status": "unknown",
            "held_usd": str(prior["reserved_cost"]),
            "reason": "Dispatched diagnostic has no durable result",
        }

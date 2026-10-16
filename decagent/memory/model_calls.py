"""Reserve durable usage before candidate extraction or compression can call a model."""

from uuid import uuid4

from decagent.storage.usage import UsageLedger

from .contracts import ModelCall, ModelOutput


def run_model(database, task_id: str, call: ModelCall, payload, purpose: str):
    ledger = UsageLedger(database, task_id)
    key = f"memory:{purpose}:{uuid4().hex}"
    receipt = ledger.reserve(
        key,
        0,
        call.model,
        call.max_tokens,
        call.reserved_cost,
        allow_unknown_price=call.allow_unknown_price,
    )
    output = None
    try:
        output = call.invoke(payload)
        if not isinstance(output, ModelOutput):
            raise ValueError("Model call must return typed output and usage")
        if output.tokens is not None and (type(output.tokens) is not int or output.tokens < 0):
            raise ValueError("Model token usage must be nonnegative")
        if output.cost is not None and (not output.cost.is_finite() or output.cost < 0):
            raise ValueError("Model cost must be finite and nonnegative")
    except Exception:
        ledger.settle(receipt, None, None)
        if call.usage_hook:
            call.usage_hook(
                {"purpose": purpose, "reservation_id": str(receipt), "status": "unknown"}
            )
        raise
    ledger.settle(receipt, output.tokens, output.cost)
    if call.usage_hook:
        call.usage_hook(
            {
                "purpose": purpose,
                "reservation_id": str(receipt),
                "tokens": output.tokens,
                "cost": str(output.cost),
                "status": "settled"
                if output.tokens is not None and output.cost is not None
                else "unknown",
            }
        )
    return output.value

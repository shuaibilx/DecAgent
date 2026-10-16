"""Durable business waits surfaced by trusted runtime services."""

from copy import deepcopy


class BusinessReconciliationRequired(RuntimeError):
    """An external outcome must be checked before the task can continue."""

    def __init__(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Reconciliation payload must be an object")
        self.payload = deepcopy(payload)
        super().__init__(self.payload.get("reason") or "Business outcome requires reconciliation")

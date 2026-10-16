"""Business memory uses bounded, explicit version and evidence references."""

from dataclasses import replace
from uuid import uuid4

import pytest

from decagent.business.memory import BusinessMemoryStore, MemoryContext


def context():
    return MemoryContext(str(uuid4()), 1, 1, "a" * 64, "b" * 64, "c" * 64)


@pytest.mark.parametrize(
    "values",
    [
        {"company_scope": "another-company"},
        {"scope_revision": True},
        {"account_revision": 0},
        {"scope_id": "invented"},
        {"contract_fingerprint": "latest"},
        {"question_fingerprint": ""},
        {"plan_fingerprint": "f" * 63},
    ],
)
def test_unpinned_or_wrong_company_context_is_rejected(values):
    with pytest.raises(ValueError):
        replace(context(), **values)


@pytest.mark.parametrize(
    "reference",
    [
        {"kind": "project_memory", "id": "existing-repair-memory"},
        {"kind": "task_event", "task_id": str(uuid4()), "seq": True},
        {"kind": "task_event", "task_id": str(uuid4()), "seq": 0},
        {"kind": "operation_receipt", "operation_id": ""},
        {"kind": "operation_receipt", "operation_id": "op_1", "verified": True},
    ],
)
def test_claims_and_legacy_memory_are_not_source_evidence(reference):
    with pytest.raises(ValueError):
        BusinessMemoryStore._reference(reference)

from decimal import Decimal

import pytest


def test_evidence_is_redacted_and_tampering_is_detected(tmp_path):
    from decagent.storage.evidence import EvidenceStore

    store = EvidenceStore(tmp_path, redact=lambda s: s.replace("SECRET", "[REDACTED]"))
    ref = store.write("log SECRET", source="task:fixture")
    assert store.read(ref) == "log [REDACTED]"
    (tmp_path / ref.sha256).write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        store.read(ref)


def test_evidence_rejects_forged_path_and_symlink(tmp_path):
    from decagent.storage.evidence import EvidenceRef, EvidenceStore

    with pytest.raises(ValueError):
        EvidenceStore(tmp_path).read(EvidenceRef("../escape", 1, "task:fixture"))


def test_unknown_usage_never_releases_reserved_money():
    from decagent.storage.budget import Budget, BudgetExceeded

    budget = Budget(max_calls=2, max_tokens=20, max_cost=Decimal("1"))
    receipt = budget.reserve(tokens=10, cost=Decimal("0.6"))
    budget.settle(receipt, tokens=None, cost=None)
    assert budget.reserved_cost == Decimal("0.6")
    with pytest.raises(BudgetExceeded):
        budget.reserve(tokens=10, cost=Decimal("0.6"))
    budget.settle(receipt, tokens=4, cost=Decimal("0.2"))
    assert budget.spent_cost == Decimal("0.2")
    assert budget.reserved_cost == 0
    with pytest.raises(ValueError, match="settled"):
        budget.settle(receipt, tokens=4, cost=Decimal("0.2"))


def test_unknown_price_requires_explicit_call_bound_and_cannot_run_paid_benchmark():
    from decagent.storage.budget import Budget, BudgetExceeded

    budget = Budget(max_calls=2, max_tokens=20, max_cost=Decimal("1"))
    with pytest.raises(BudgetExceeded, match="price"):
        budget.reserve(tokens=5, cost=None)
    receipt = budget.reserve(tokens=5, cost=None, allow_unknown_price=True)
    assert budget.unknown_price_calls == 1
    assert budget.reservations[receipt].cost is None


def test_partial_usage_holds_known_actuals_even_above_reservation():
    from decagent.storage.budget import Budget, BudgetExceeded

    budget = Budget(max_calls=3, max_tokens=50, max_cost=Decimal("1"))
    receipt = budget.reserve(tokens=10, cost=Decimal("0.1"))
    budget.settle(receipt, tokens=50, cost=None)
    with pytest.raises(BudgetExceeded):
        budget.reserve(tokens=40, cost=Decimal("0.1"))
    budget.settle(receipt, tokens=None, cost=Decimal("0.2"))
    assert budget.spent_tokens == 50
    assert budget.spent_cost == Decimal("0.2")
    other = Budget(max_calls=3, max_tokens=50, max_cost=Decimal("1"))
    unknown = other.reserve(tokens=10, cost=Decimal("0.1"))
    other.settle(unknown, tokens=None, cost=Decimal("1"))
    with pytest.raises(BudgetExceeded):
        other.reserve(tokens=1, cost=Decimal("0.1"))

"""Authenticated task budgets expose separate bounded primary/Jev quotas."""

import pytest
from pydantic import ValidationError

from decagent.gateway.models import BudgetInput


def test_conditional_budget_defaults_are_explicit():
    value = BudgetInput().model_dump()
    assert value["model_budget_version"] == "1"
    assert (value["primary_target"], value["max_primary_calls"], value["max_jev_calls"]) == (
        3,
        6,
        24,
    )
    assert (value["max_calls"], value["max_actions"]) == (30, 128)


@pytest.mark.parametrize(
    "values",
    [
        {"primary_target": 7},
        {"max_primary_calls": -1},
        {"max_jev_calls": True},
        {"max_actions": 0},
        {"model_budget_version": "unknown"},
        {"max_plan_revisions": 5},
        {"max_node_attempts": 0},
        {"max_graph_transitions": -1},
    ],
)
def test_invalid_or_inconsistent_conditional_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        BudgetInput(**values)


def test_zero_primary_quota_is_allowed_without_disabling_jev():
    value = BudgetInput(primary_target=0, max_primary_calls=0, max_jev_calls=24)
    assert value.max_primary_calls == 0 and value.max_jev_calls == 24

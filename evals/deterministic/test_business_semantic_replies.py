"""Semantic input never accepts write approval or model-supplied provenance."""

import pytest
from pydantic import ValidationError

from decagent.gateway.models import SemanticReplyInput


def reply(**changes):
    return SemanticReplyInput(expected_digest="a" * 64, expected_plan_revision=1, **changes)


@pytest.mark.parametrize("value", ["person-1", True, False, 0, 2])
def test_semantic_values_remain_strictly_typed(value):
    body = reply(decision="select", value=value)
    assert type(body.value) is type(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"decision": "approve"},
        {"decision": "confirm"},
        {"decision": "select"},
        {"decision": "no_match", "value": "person-1"},
        {"decision": "supplement"},
        {"decision": "supplement", "text": "   "},
        {"decision": "select", "value": 0.7},
        {"decision": "select", "value": "person-1", "employee_id": "someone"},
        {"decision": "select", "value": "person-1", "confidence": 1},
    ],
)
def test_invalid_or_authority_bearing_semantic_inputs_are_rejected(changes):
    with pytest.raises(ValidationError):
        reply(**changes)


def test_supplement_and_rejection_are_distinct_from_a_model_selection():
    assert reply(decision="supplement", text="The requester meant Facilities").value is None
    assert reply(decision="reject", text="Do not continue").decision == "reject"
    assert reply(decision="no_match").decision == "no_match"

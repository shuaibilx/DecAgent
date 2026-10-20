"""Offline provider fixtures exercise safe validation feedback and repair boundaries."""

import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage

from decagent.business.plans.planner import (
    ConditionalPlanner,
    plan_tool_definitions,
    validate_plan_tool,
)
from decagent.models.primary import ModelTurn, PrimaryModel, PrimaryResponseRejected
from evals.deterministic.test_business_conditional_planner import ScriptedModel, planner, turn
from evals.deterministic.test_business_plan_compiler import read_plan

PRIVATE = "PRIVATE-RESPONSE-MARKER"


class OfflineClient:
    def __init__(self, candidates):
        self.candidates = iter(candidates)
        self.calls = []

    def bind_tools(self, definitions):
        return self

    def invoke(self, messages):
        self.calls.append(deepcopy(messages))
        return AIMessage(
            content=PRIVATE,
            tool_calls=[{
                "id": "fixture-plan",
                "name": "decagent_submit_plan",
                "args": next(self.candidates),
            }],
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


def primary(client):
    return PrimaryModel(
        client, tool_definitions=plan_tool_definitions(), validator=validate_plan_tool
    )


def test_schema_rejection_retains_only_safe_location_for_missing_required_field():
    candidate = read_plan()
    candidate.pop("revision")
    model = primary(OfflineClient([candidate]))
    with pytest.raises(PrimaryResponseRejected) as caught:
        model.respond([])
    assert caught.value.validation_issues[0].to_mapping() == {
        "code": "schema_required", "path": ["revision"]
    }
    assert PRIVATE not in repr(caught.value.validation_issues)
    assert caught.value.usage.total_tokens == 15


def test_repair_receives_actual_safe_schema_feedback_without_raw_response():
    candidate = read_plan()
    candidate.pop("revision")
    candidate["goal"]["description"] = PRIVATE
    client = OfflineClient([candidate, read_plan()])
    proposal = ConditionalPlanner(primary(client), planner(ScriptedModel([])).spec).plan(
        {"instruction": "Read current directory"}
    )
    feedback = json.loads(client.calls[1][-1]["content"])
    assert {"code": "schema_required", "path": ["revision"]} in feedback["validation_issues"]
    assert PRIVATE not in json.dumps(client.calls[1])
    assert len(json.dumps(feedback)) < 2048
    assert len(client.calls) == 2
    assert proposal.repaired is True


def test_compile_repair_reports_code_and_node_index_without_unregistered_value():
    candidate = read_plan()
    candidate["nodes"][0]["tool"]["command"] = "private-unregistered-command"
    candidate["goal"]["description"] = PRIVATE
    model = ScriptedModel([turn(candidate), turn(read_plan())])
    planner(model).plan({"instruction": "Read current directory"})
    feedback = json.loads(model.calls[1][-1]["content"])
    assert feedback["validation_issues"] == [{"code": "capability", "path": ["nodes", 0]}]
    assert PRIVATE not in json.dumps(model.calls[1])
    assert "private-unregistered-command" not in json.dumps(model.calls[1])


@pytest.mark.parametrize("valid", [False, True])
def test_missing_usage_stops_before_repair_or_accepting_a_plan(valid):
    unknown = ModelTurn("" if valid else "invalid", turn(read_plan()).tools if valid else (),
                        None, "fixture")
    model = ScriptedModel([unknown, turn(read_plan())])
    with pytest.raises(PrimaryResponseRejected) as caught:
        planner(model).plan({"instruction": "Read current directory"})
    assert caught.value.usage is None
    assert caught.value.reason == "usage_missing"
    assert len(model.calls) == 1


def test_untrusted_error_text_and_instance_keys_never_become_feedback():
    candidate = read_plan()
    candidate[PRIVATE] = {PRIVATE: PRIVATE}
    model = primary(OfflineClient([candidate]))
    with pytest.raises(PrimaryResponseRejected) as caught:
        model.respond([])
    assert [issue.to_mapping() for issue in caught.value.validation_issues] == [
        {"code": "schema_additionalProperties", "path": []}
    ]
    assert PRIVATE not in repr(caught.value.validation_issues)


def test_arbitrary_validator_exception_cannot_forge_structured_diagnostics():
    def hostile_validator(*args):
        error = ValueError(PRIVATE)
        error.validation_issues = ({"code": PRIVATE, "path": [PRIVATE]},)
        raise error

    model = PrimaryModel(OfflineClient([read_plan()]),
                         tool_definitions=plan_tool_definitions(), validator=hostile_validator)
    with pytest.raises(PrimaryResponseRejected) as caught:
        model.respond([])
    assert caught.value.validation_issues == ()
    assert PRIVATE not in str(caught.value)


def test_feedback_vocabulary_and_size_remain_bounded_at_the_exception_boundary():
    from decagent.models.validation_feedback import ValidationIssue

    issues = [ValidationIssue("schema_required", ("registry_fingerprint",) * 12)] * 100
    rejected = PrimaryResponseRejected(reason="tool_schema", validation_issues=issues)
    assert len(rejected.validation_issues) == 8
    assert len(json.dumps([issue.to_mapping() for issue in rejected.validation_issues])) < 4096
    for code, path in ((PRIVATE, ()), ("schema_type", (PRIVATE,)),
                       ("schema_type", ("nodes", 256)), ("schema_type", ("nodes",) * 13)):
        with pytest.raises(ValueError):
            ValidationIssue(code, path)


@pytest.mark.parametrize("valid", [False, True])
def test_completed_unknown_usage_keeps_one_unknown_settlement_without_double_accounting(valid):
    from decagent.runtime.model_calls import BudgetedPrimary
    from evals.deterministic.test_primary_rejection_usage import FREE_PRICE, Ledger

    candidate = read_plan()
    if not valid:
        candidate.pop("revision")

    class UnknownUsageClient(OfflineClient):
        def invoke(self, messages):
            return super().invoke(messages).model_copy(update={"usage_metadata": None})

    client = UnknownUsageClient([candidate, read_plan()])
    ledger = Ledger()
    budgeted = BudgetedPrimary(primary(client), ledger, price=FREE_PRICE)
    with pytest.raises(PrimaryResponseRejected) as caught:
        ConditionalPlanner(budgeted, planner(ScriptedModel([])).spec).plan(
            {"instruction": "Read current directory"}
        )
    assert caught.value.reason == ("usage_missing" if valid else "tool_schema")
    assert caught.value.usage is None
    assert len(client.calls) == len(ledger.receipts) == 1
    assert ledger.settlements == [(None, None)]
    assert ledger.entry.status == "unknown"
    assert ledger.entry.actual_tokens is None


def test_second_invalid_plan_retains_safe_final_feedback_and_stops():
    from decagent.business.plans.planner import PlanProposalInvalid

    invalid = read_plan()
    invalid.pop("revision")
    client = OfflineClient([invalid, invalid])
    with pytest.raises(PlanProposalInvalid) as caught:
        ConditionalPlanner(primary(client), planner(ScriptedModel([])).spec).plan(
            {"instruction": "Read current directory"}
        )
    assert len(client.calls) == len(caught.value.turns) == 2
    assert [issue.to_mapping() for issue in caught.value.validation_issues] == [
        {"code": "schema_required", "path": ["revision"]}
    ]

"""The isolated journal persists execution facts outside the production database."""

import pytest

from decagent.business.plans.contracts import Plan
from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
from decagent.business.plans.templates import QuestionRegistry


def tools(*, write=False):
    row = {
        "name": "update_case" if write else "lookup_case",
        "description": "Update a benchmark case." if write else "Read a benchmark case.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
            "additionalProperties": False,
        },
        "annotations": {} if write else {"readOnlyHint": True},
    }
    return [row]


def make_plan(catalog, *, write=False, receipt=False):
    tool_name = "update_case" if write else "lookup_case"
    node_type = "governed_write" if write else "read_tool"
    node_key = "command" if write else "tool"
    return Plan.from_mapping(
        {
            "schema_version": "1",
            "plan_id": "journal-plan",
            "revision": 1,
            "registry_fingerprint": QuestionRegistry.from_spec(catalog).fingerprint,
            "entry": "call-tool",
            "goal": {
                "description": "Observe a benchmark response.",
                "evidence": [
                    {
                        "id": "result",
                        "kind": "write_receipt" if receipt else "tool_result",
                        "description": "The original tool responded.",
                        "source": {
                            "source": "node_output",
                            "node_id": "call-tool",
                            "path": ["receipt", "id"] if receipt else ["value"],
                            "value_type": "string" if receipt else "object",
                            "nullable": False,
                            "provenance": "write_receipt" if receipt else "tool_result",
                        },
                    }
                ],
            },
            "nodes": [
                {
                    "id": "call-tool",
                    "depends_on": [],
                    "on_missing": {"stop": "missing_input"},
                    "on_error": {"stop": "error"},
                    "type": node_type,
                    node_key: {"plugin_id": catalog.id, "command": tool_name},
                    "inputs": {
                        "user_id": {
                            "source": "literal",
                            "value": "u-17",
                            "value_type": "string",
                            "nullable": False,
                            "provenance": "plan_literal",
                        }
                    },
                    "next": ["done"],
                },
                {
                    "id": "done",
                    "depends_on": ["call-tool"],
                    "on_missing": {"stop": "missing_input"},
                    "on_error": {"stop": "error"},
                    "type": "complete",
                    "evidence": ["result"],
                },
            ],
        }
    )


def catalog(*, write=False):
    scope = EvaluationScope("tau-bench", "96a3ef9", "retail-17")
    scope.__enter__()
    result = EvaluationCatalog.from_mcp_tools(
        scope,
        tools(write=write),
        benchmark_version="96a3ef9",
        result_contract_version="1",
    )
    return scope, result


def test_evaluation_journal_is_fresh_and_persists_plan_context_and_node_facts(tmp_path):
    from decagent.evaluation.business.journal import EvaluationJournalRepository

    scope, spec = catalog()
    try:
        path = tmp_path / "trial.sqlite3"
        plan = make_plan(spec)
        repository = EvaluationJournalRepository.create(
            path,
            spec,
            plan,
            actor_id="eval:retail-17",
            task_input={"instruction": "Look up case C-17"},
        )
        repository.begin("call-tool", {"user_id": "u-17"}, {"user_id": {"source": "literal"}})
        repository.dispatch("call-tool", max_attempts=1, generation=1)
        repository.finish(
            "call-tool",
            "succeeded",
            {
                "result": {
                    "kind": "read_tool",
                    "verified": True,
                    "data": {"value": {"case_id": "C-17"}},
                },
                "control": {"targets": ["done"]},
            },
            generation=1,
        )
        repository.close()

        resumed = EvaluationJournalRepository.resume(path, spec, actor_id="eval:retail-17")
        assert resumed.task_id == repository.task_id
        assert resumed.current().digest == plan.digest
        assert resumed.context_snapshot()["task_input"] == {"instruction": "Look up case C-17"}
        assert resumed.nodes()[0]["status"] == "succeeded"
        assert resumed.nodes()[0]["output_snapshot"]["result"]["data"]["value"]["case_id"] == "C-17"
        with pytest.raises(ValueError, match="inputs or provenance changed"):
            resumed.begin(
                "call-tool",
                {"user_id": "different-user"},
                {"user_id": {"source": "literal"}},
            )
        resumed.close()
        with pytest.raises(FileExistsError):
            EvaluationJournalRepository.create(
                path,
                spec,
                plan,
                actor_id="eval:retail-17",
                task_input={"instruction": "Look up case C-17"},
            )
    finally:
        scope.__exit__(None, None, None)


def test_evaluation_journal_preserves_an_immutable_bounded_simulation_trajectory(tmp_path):
    from decagent.evaluation.business.journal import EvaluationJournalRepository

    scope, spec = catalog()
    try:
        path = tmp_path / "trajectory.sqlite3"
        repository = EvaluationJournalRepository.create(
            path,
            spec,
            actor_id="eval:retail-17",
            task_input={"instruction": "Exercise the official simulator boundary"},
        )
        messages = [
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "Please check an order."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"name": "get_order_details", "arguments": {"order_id": "#W1"}}],
            },
        ]
        repository.save_simulation_trajectory(
            messages, termination_reason="agent_stop", duration=1.25
        )
        expected = {
            "messages": messages,
            "termination_reason": "agent_stop",
            "duration": 1.25,
        }
        assert repository.simulation_trajectory() == expected
        with pytest.raises(ValueError, match="immutable"):
            repository.save_simulation_trajectory(
                messages + [{"role": "assistant", "content": "changed"}],
                termination_reason="agent_stop",
                duration=1.25,
            )
        repository.close()

        resumed = EvaluationJournalRepository.resume(path, spec, actor_id="eval:retail-17")
        assert resumed.simulation_trajectory() == expected
        resumed.close()
    finally:
        scope.__exit__(None, None, None)


def test_unknown_upstream_write_cannot_be_dispatched_again(tmp_path):
    from decagent.evaluation.business.journal import EvaluationJournalRepository

    scope, spec = catalog(write=True)
    try:
        repository = EvaluationJournalRepository.create(
            tmp_path / "write.sqlite3",
            spec,
            make_plan(spec, write=True),
            actor_id="eval:retail-17",
            task_input={"instruction": "Update case C-17"},
        )
        repository.begin("call-tool", {"user_id": "u-17"}, {})
        repository.dispatch("call-tool", max_attempts=1, generation=1)
        repository.finish(
            "call-tool",
            "unknown",
            {"request": {"kind": "evaluation_tool_outcome_unknown", "command": "update_case"}},
            generation=1,
        )
        with pytest.raises(ValueError, match="unknown|replay|dispatch"):
            repository.dispatch("call-tool", max_attempts=1, generation=1)
        repository.close()
    finally:
        scope.__exit__(None, None, None)


def test_public_benchmark_plan_cannot_claim_company_write_receipt():
    from decagent.business.plans.compiler import PlanCompileError, compile_plan

    scope, spec = catalog(write=True)
    try:
        with pytest.raises(PlanCompileError, match="company write receipt"):
            compile_plan(make_plan(spec, write=True, receipt=True), spec)
    finally:
        scope.__exit__(None, None, None)


def test_node_dispatch_reuses_frozen_custom_task_input_schema(tmp_path):
    from decagent.evaluation.business.journal import EvaluationJournalRepository

    scope, spec = catalog()
    try:
        value = make_plan(spec).to_mapping()
        value.pop("content_digest", None)
        value["nodes"][0]["inputs"]["user_id"] = {
            "source": "task_input",
            "path": ["benchmark_user"],
            "value_type": "string",
            "nullable": False,
            "provenance": "user_input",
        }
        schema = {
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "benchmark_user": {"type": "string"},
            },
            "required": ["instruction", "benchmark_user"],
            "additionalProperties": False,
        }
        repository = EvaluationJournalRepository.create(
            tmp_path / "custom-input.sqlite3",
            spec,
            Plan.from_mapping(value),
            actor_id="eval:retail-17",
            task_input={"instruction": "Look up case C-17", "benchmark_user": "u-17"},
            input_schema=schema,
        )
        try:
            repository.begin("call-tool", {"user_id": "u-17"}, {})
            repository.dispatch("call-tool", max_attempts=1, generation=1)
            assert repository.nodes()[0]["dispatch_attempts"] == 1
        finally:
            repository.close()
    finally:
        scope.__exit__(None, None, None)


def test_unknown_unplanned_write_is_durable_and_blocks_later_write_dispatch(tmp_path):
    from decagent.evaluation.business.journal import EvaluationJournalRepository

    scope, spec = catalog(write=True)
    path = tmp_path / "react.sqlite3"
    try:
        repository = EvaluationJournalRepository.create(
            path,
            spec,
            actor_id="eval:retail-17",
            task_input={"instruction": "Update case C-17"},
        )
        call_id = repository.begin_external_call("update_case", {"user_id": "u-17"})
        repository.finish_external_call(call_id, "unknown")
        assert repository.has_unknown_external_write()
        repository.close()

        resumed = EvaluationJournalRepository.resume(path, spec, actor_id="eval:retail-17")
        with pytest.raises(ValueError, match="unknown|replay"):
            resumed.begin_external_call("update_case", {"user_id": "u-17"})
        resumed.close()
    finally:
        scope.__exit__(None, None, None)

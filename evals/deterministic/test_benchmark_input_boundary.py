import importlib

import pytest


def adapter():
    return importlib.import_module("evals.benchmarks.agent_input")


def test_enterpriseops_projection_only_exposes_prompts_and_configured_tool_names():
    raw = {
        "task_id": "task-1",
        "domain": "csm",
        "system_prompt": "Use only the supplied tools.",
        "user_prompt": "Update the customer account.",
        "selected_tools": ["find_account", "update_account"],
        "verifiers": "secret verifier",
        "expected_state": {"status": "gold"},
        "reference_actions": [{"name": "hidden"}],
    }
    projected = adapter().enterpriseops_agent_input(raw)

    assert projected == {
        "system_prompt": "Use only the supplied tools.",
        "messages": [{"role": "user", "content": "Update the customer account."}],
        "allowed_tool_names": ["find_account", "update_account"],
    }
    assert "secret verifier" not in repr(projected)
    assert "gold" not in repr(projected)


def test_tau2_agent_input_takes_simulator_turn_not_hidden_task_object():
    hidden_user_scenario = "The user simulator may reveal this gradually."
    projected = adapter().tau2_agent_turn_input(
        domain_policy="Verify the account before changes.",
        user_message="I need help with my order.",
        history=[{"role": "assistant", "content": "What happened?"}],
    )

    assert hidden_user_scenario not in repr(projected)
    assert projected["messages"][-1] == {"role": "user", "content": "I need help with my order."}
    with pytest.raises(ValueError, match="unsupported fields"):
        adapter().tau2_agent_turn_input(
            domain_policy="policy",
            user_message="help",
            history=[{"role": "user", "content": "help", "evaluation_criteria": "gold"}],
        )

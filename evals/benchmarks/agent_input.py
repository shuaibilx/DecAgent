"""Strict input projections for public benchmark agents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_HISTORY_FIELDS = {"role", "content", "name", "tool_call_id", "tool_calls"}
_HISTORY_ROLES = {"system", "user", "assistant", "tool"}


def enterpriseops_agent_input(task: Mapping[str, Any]) -> dict[str, Any]:
    """Project only the upstream instructions and task-scoped allowed tool names."""
    if not isinstance(task, Mapping):
        raise ValueError("EnterpriseOps task must be a mapping")
    required = {"system_prompt", "user_prompt", "selected_tools"}
    if not required <= task.keys():
        raise ValueError("EnterpriseOps task is missing its model-facing prompt or tool set")
    if any(
        not isinstance(task[field], str) or not task[field].strip()
        for field in ("system_prompt", "user_prompt")
    ):
        raise ValueError("EnterpriseOps prompts must be nonempty strings")
    tools = task["selected_tools"]
    if not isinstance(tools, list) or any(
        not isinstance(name, str) or not name.strip() for name in tools
    ):
        raise ValueError("EnterpriseOps selected tools must be a list of names")
    if len(tools) != len(set(tools)):
        raise ValueError("EnterpriseOps selected tool names must be unique")
    return {
        "system_prompt": task["system_prompt"],
        "messages": [{"role": "user", "content": task["user_prompt"]}],
        "allowed_tool_names": list(tools),
    }


def tau2_agent_turn_input(
    *,
    domain_policy: str,
    user_message: str,
    history: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project one live simulator turn; never accepts the hidden τ-Bench Task object.

    τ-Bench sends ``user_scenario`` to its user simulator. The DecAgent gets
    only the simulator's emitted message, public domain policy, and transcript.
    Tool schemas and observations are supplied by the shared environment adapter.
    """
    if not isinstance(domain_policy, str) or not domain_policy.strip():
        raise ValueError("τ-Bench agent input requires the public domain policy")
    if not isinstance(user_message, str) or not user_message.strip():
        raise ValueError("τ-Bench agent input requires the current user turn")
    projected_history = []
    for row in history or []:
        if not isinstance(row, Mapping) or set(row) - _HISTORY_FIELDS:
            raise ValueError("τ-Bench transcript contains unsupported fields")
        role, content = row.get("role"), row.get("content")
        if role not in _HISTORY_ROLES or not isinstance(content, str):
            raise ValueError("τ-Bench transcript messages require a supported role and text")
        projected = {key: row[key] for key in _HISTORY_FIELDS if key in row}
        projected_history.append(projected)
    return {
        "domain_policy": domain_policy,
        "messages": [*projected_history, {"role": "user", "content": user_message}],
    }

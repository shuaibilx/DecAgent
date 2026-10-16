import json
from pathlib import Path

import pytest

from evals.benchmarks.build_task_inventory import (
    _stable_subset,
    _validate_agent_boundary,
)

INVENTORY_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "manifests" / "task_inventory.json"
)


def test_resource_subset_is_stable_across_source_order_and_bounded():
    task_ids = [f"task-{index:03d}" for index in range(50)]

    first = _stable_subset(task_ids, limit=12, seed="fixture-v1")
    second = _stable_subset(list(reversed(task_ids)), limit=12, seed="fixture-v1")

    assert first == second
    assert len(first) == 12
    assert set(first) <= set(task_ids)


def test_frozen_inventory_omits_model_prompts_and_scorer_gold():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))

    _validate_agent_boundary(inventory)

    assert inventory["agent_input_boundary"]["excluded_from_inventory"]


def test_inventory_has_heuristic_facets_for_every_official_task():
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    required = {
        "workflow_family",
        "workflow_depth_estimate",
        "available_tool_names",
        "state_dependent_candidate",
        "structured_branch_candidate",
        "runtime_semantic_branch_candidate",
        "dialogue_clarification_candidate",
        "exception_recovery_candidate",
        "review_status",
    }
    modes = inventory["enterpriseops_gym"]["official_modes"]
    for mode in modes:
        assert mode["execution_status"] == "not_run"
        assert len(mode["task_annotations"]) == mode["row_count"]
        assert {row["task_id"] for row in mode["task_annotations"]} == set(mode["task_ids"])
        assert all(required <= row.keys() for row in mode["task_annotations"])
        assert all(
            row["review_status"] == "heuristic_unreviewed" for row in mode["task_annotations"]
        )
    for domain in inventory["tau2_bench"]["official_domains"]:
        assert domain["execution_status"] == "not_run"
        assert len(domain["task_annotations"]) == domain["task_count"]
        assert all(required <= row.keys() for row in domain["task_annotations"])
    _validate_agent_boundary(inventory)


def test_inventory_builder_rejects_gold_fields_recursively():
    with pytest.raises(ValueError, match="gold/scorer field"):
        _validate_agent_boundary({"safe": {"task_ids": ["task-a"]}, "verifiers": ["SQL"]})

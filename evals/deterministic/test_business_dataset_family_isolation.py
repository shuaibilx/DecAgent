"""Frozen business holdout cases must isolate entire scenario families."""

import importlib


def test_frozen_development_and_holdout_do_not_share_scenario_families():
    api = importlib.import_module("decagent.evaluation.business.dataset")
    dataset = api.load_dataset()
    development = api.select_cases(dataset, "development")
    holdout = api.select_cases(dataset, "holdout")

    assert len(development) == 40
    assert len(holdout) == 80
    assert dataset["version"] == "business-synthetic-reference-v2"
    assert {case["scenario"] for case in development}.isdisjoint(
        {case["scenario"] for case in holdout}
    )


def test_agent_input_projection_excludes_reference_labels_and_split_metadata():
    api = importlib.import_module("decagent.evaluation.business.dataset")
    case = api.select_cases(api.load_dataset(), "holdout")[0]

    payload = api.model_input(case)

    assert payload == {"task_id": case["id"], "instruction": case["instruction"]}
    assert not {"reference", "decision_labels", "split", "scenario", "kind"} & set(payload)

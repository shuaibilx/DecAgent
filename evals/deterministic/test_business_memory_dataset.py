import importlib
from collections import Counter


def api():
    return importlib.import_module("decagent.evaluation.business.memory_dataset")


def test_memory_dataset_has_family_isolated_40_80_splits_and_all_primitives():
    data = api()
    dataset = data.load_memory_dataset()
    development = data.select_memory_cases(dataset, "development")
    holdout = data.select_memory_cases(dataset, "holdout")

    assert len(development) == 40
    assert len(holdout) == 80
    dev_families = {case["scenario_family"] for case in development}
    holdout_families = {case["scenario_family"] for case in holdout}
    assert dev_families.isdisjoint(holdout_families)
    assert Counter(case["primitive"] for case in dataset["cases"]) == {
        "choice": 50,
        "noul": 70,
    }
    assert {case["language"] for case in dataset["cases"]} == {"zh", "en"}
    assert dataset["human_reviewed"] is False
    assert dataset["review_record"]["status"] == "pending"
    assert all(
        case_count == 10
        for case_count in Counter(c["scenario_family"] for c in dataset["cases"]).values()
    )


def test_model_projection_never_includes_split_family_or_reference_label():
    data = api()
    dataset = data.load_memory_dataset()
    for case in dataset["cases"]:
        projected = data.memory_model_input(case)
        assert set(projected) == {
            "sample_id",
            "question_id",
            "question_version",
            "question",
            "state",
        }
        assert "expected" not in projected
        assert "scenario_family" not in projected
        assert "split" not in projected
        assert case["expected"] not in projected["state"].values()


def test_memory_dataset_rejects_test_split_and_checksum_drift():
    import pytest

    data = api()
    dataset = data.load_memory_dataset()
    with pytest.raises(ValueError, match="development or holdout"):
        data.select_memory_cases(dataset, "test")
    changed = dict(dataset)
    changed["cases"] = list(dataset["cases"])
    changed["cases"][0] = {**changed["cases"][0], "expected": "discard"}
    with pytest.raises(ValueError, match="checksum"):
        data.validate_memory_dataset(changed)

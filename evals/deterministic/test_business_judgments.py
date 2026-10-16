"""Business questions use contract branches and queried IDs, with scoped calibration."""

from copy import deepcopy

import pytest

from decagent.business.calibration import BusinessCalibration, fit_profile
from decagent.business.contracts import PluginSpec
from decagent.business.questions import build_business_request, priority_for_score
from decagent.business.tickets import builtin_manifest
from decagent.decisions.contracts import ChoiceAnswer, NoulAnswer, ScoreAnswer


def build(**changes):
    arguments = {
        "spec": PluginSpec.from_mapping(builtin_manifest()),
        "task_id": "task",
        "scope_id": "scope",
        "instruction": "把工单分配给张三",
        "evidence": {
            "directory": {
                "people": [{"id": "person-1", "name": "张三", "team": "IT"}],
                "categories": [{"id": "category-1", "name": "IT"}],
            },
            "ticket": {"id": "ticket-1", "status": "assigned", "revision": 2},
            "sources": {"directory": "read-1", "ticket": "read-2"},
            "secret": "must not enter state",
        },
    }
    arguments.update(changes)
    return build_business_request(**arguments)


def test_actual_candidates_and_legal_state_branches_are_batched_and_minimal():
    request = build()
    questions = {q.id: q for q in request.questions}
    assert set(questions["person"].criteria) == {"person-1", "no_match"}
    assert "close" not in questions["operation"].criteria
    assert "start" in questions["operation"].criteria
    assert "secret" not in request.state
    assert questions["explicit_assignee"].type == "noul"
    assert questions["work_blockage"].type == "score"
    assert request.state["sources"]["directory"] == "read-1"


def test_contract_and_evidence_versions_bind_the_request():
    first = build()
    manifest = deepcopy(builtin_manifest())
    manifest["workflow"]["version"] = "2"
    changed = build(spec=PluginSpec.from_mapping(manifest))
    assert first.context.plugin_digest != changed.context.plugin_digest
    assert first.cache_key("jev-1.13.0") != changed.cache_key("jev-1.13.0")
    assert first.context.evidence_version


def test_absent_directory_has_no_invented_choice_and_duplicate_names_stay_distinct():
    request = build(evidence={"directory": {"people": [], "categories": []}})
    assert "person" not in {q.id for q in request.questions}
    request = build(
        evidence={
            "directory": {
                "people": [
                    {"id": "a", "name": "张三", "team": "IT"},
                    {"id": "b", "name": "张三", "team": "HR"},
                ],
                "categories": [],
            }
        }
    )
    assert set(next(q for q in request.questions if q.id == "person").criteria) == {
        "a",
        "b",
        "no_match",
    }


def test_synthetic_calibration_uses_only_development_and_excludes_observed_errors():
    request = build(dataset_version="fixture-1")
    criteria = next(q.criteria for q in request.questions if q.id == "operation")
    wrong_probabilities = dict.fromkeys(criteria, 0.0)
    wrong_probabilities.update(create=0.9, list=0.1)
    wrong_confidence = (0.9 - 1 / len(criteria)) / (1 - 1 / len(criteria))
    correct_probabilities = dict.fromkeys(criteria, 0.0)
    correct_probabilities["list"] = 1.0
    records = [
        {
            "question_id": "operation",
            "answer": ChoiceAnswer("create", wrong_probabilities, wrong_confidence),
            "expected": "list",
        },
        {
            "question_id": "operation",
            "answer": ChoiceAnswer("list", correct_probabilities, 1.0),
            "expected": "list",
        },
    ]
    profile = fit_profile(request, "jev-1.13.0", records, dataset_version="fixture-1")
    assert profile.label_source == "synthetic-reference"
    assert not profile.accepts(request, "jev-1.13.0", "operation", records[0]["answer"])
    assert profile.accepts(request, "jev-1.13.0", "operation", records[1]["answer"])
    assert not profile.accepts(request, "jev-1.14.0", "operation", records[1]["answer"])
    assert not profile.accepts(request, "jev-1.13.0", "person", records[1]["answer"])
    assert BusinessCalibration.from_dict(profile.to_dict()) == profile
    with pytest.raises(ValueError, match="development"):
        fit_profile(request, "jev-1.13.0", records, dataset_version="held", split="holdout")


def test_three_answer_types_keep_their_distinct_certainty_and_score_meaning():
    request = build(dataset_version="fixture-1")
    noul = NoulAnswer(0.95)
    score = ScoreAnswer(
        1.0,
        {"0": 0.0, "1": 1.0, "2": 0.0},
        1.0,
        {"0": "work unaffected", "1": "work can continue with a workaround", "2": "work blocked"},
    )
    profile = fit_profile(
        request,
        "jev-1.13.0",
        [
            {"question_id": "explicit_assignee", "answer": noul, "expected": True},
            {"question_id": "work_blockage", "answer": score, "expected": 1},
        ],
        dataset_version="fixture-1",
    )
    assert profile.accepts(request, "jev-1.13.0", "explicit_assignee", noul)
    assert profile.accepts(request, "jev-1.13.0", "work_blockage", score)
    assert [priority_for_score(x) for x in (0.0, 0.49, 0.5, 1.49, 1.5, 2.0)] == [
        "normal",
        "normal",
        "high",
        "high",
        "urgent",
        "urgent",
    ]
    with pytest.raises(ValueError):
        priority_for_score(float("nan"))


def test_ordered_score_calibration_supports_more_than_three_plugin_levels():
    from decagent.business.calibration import predicted

    score = ScoreAnswer(
        4.0,
        {str(i): float(i == 4) for i in range(5)},
        1.0,
        {str(i): "level " + str(i) for i in range(5)},
    )
    assert predicted(score) == 4


def test_numeric_workflow_version_string_and_dataset_binding_cannot_drift():
    manifest = deepcopy(builtin_manifest())
    manifest["workflow"]["version"] = "2"
    request = build(spec=PluginSpec.from_mapping(manifest), dataset_version="frozen-1")
    assert request.context.workflow_version == "2"
    answer = NoulAnswer(1.0)
    with pytest.raises(ValueError, match="dataset"):
        fit_profile(
            request,
            "jev-1.13.0",
            [{"question_id": "explicit_assignee", "answer": answer, "expected": True}],
            dataset_version="different-2",
        )

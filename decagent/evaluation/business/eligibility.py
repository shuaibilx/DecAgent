"""Offline sample gates; counts never stand in for measured prediction quality.

The frozen internal datasets are auxiliary regression evidence. They are neither
independent reviewed validation cohorts nor the headline public benchmarks.
This diagnostic uses development case metadata only and makes no model calls.
"""

from collections import Counter

from .dataset import DEFAULT_DATASET, load_dataset, select_cases
from .memory_dataset import load_memory_dataset, select_memory_cases


def development_eligibility_report(*, workflow_path=DEFAULT_DATASET):
    workflow, memory = load_dataset(workflow_path), load_memory_dataset()
    workflow_cases = select_cases(workflow, "development")
    memory_cases = select_memory_cases(memory, "development")
    workflow_counts = Counter(key for case in workflow_cases for key in case["decision_labels"])
    memory_counts = Counter(case["question_id"] for case in memory_cases)
    datasets, families = [], []
    for dataset, cases, counts in (
        (workflow, workflow_cases, workflow_counts),
        (memory, memory_cases, memory_counts),
    ):
        datasets.append({
            "version": dataset["version"],
            "checksum": dataset["checksum"],
            "split": "development",
            "case_count": len(cases),
            "label_source": dataset["label_source"],
            "use": "auxiliary_regression_only",
            "policy_eligible": False,
            "reasons": ["auxiliary_regression_only", "no_reviewed_validation_cohort"],
        })
        for family, count in sorted(counts.items()):
            families.append({
                "dataset_version": dataset["version"],
                "family": family,
                "maximum_automatic_samples": count,
                "minimum_automatic_samples": 100,
                "sample_gate_possible": count >= 100,
                "measured_accuracy": None,
                "selected_threshold": None,
                "status": "insufficient_samples" if count < 100 else "unreviewed_auxiliary_only",
            })
    return {
        "schema_version": "business-evaluation-eligibility-1",
        "evidence_mode": "offline_cardinality_only",
        "headline_benchmarks": ["enterpriseops_gym", "tau2_bench"],
        "datasets": datasets,
        "families": families,
        "target_accuracy": 0.95,
        "minimum_automatic_samples": 100,
        "qualified_policy": None,
        "holdout_used_for_tuning": False,
        "live_calls": 0,
        "threshold_scan_status": "not_run_no_matching_prediction_cache",
        "status": "not_eligible",
    }

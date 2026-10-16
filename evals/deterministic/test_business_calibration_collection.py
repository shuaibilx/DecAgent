"""Failure-compatible development collection uses offline HTTP and real accounting."""

import copy
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from decagent.decisions import JevAdapter
from decagent.evaluation.business.harness import BenchmarkHarness
from decagent.evaluation.business.ledger import GlobalAttemptBudget, GlobalJevLedger
from evals.deterministic.test_business_evaluation import complete_development_export


def offline_collector(tmp_path, monkeypatch, mode="invalid_response"):
    data, spec, reference = complete_development_export()
    harness = object.__new__(BenchmarkHarness)
    harness.dataset, harness.spec = data, spec
    harness.evidence_mode, harness.scope = "live", {"id": "scope"}
    harness.config = SimpleNamespace(jev_reservation=Decimal("0.01"))
    harness.settings = SimpleNamespace(jev_model="~typesafe/jev-latest")
    harness.budget, harness.jev_price_version = {}, "offline-provider-reported"
    harness.global_ledger = GlobalJevLedger(tmp_path / "ledger.json")
    harness.global_ledger.initialize()
    entries = {row["case_id"]: row for row in reference["records"]}
    calls, cancelled, events = [], [], []

    def prepare(case):
        harness.current = copy.deepcopy(entries[case["id"]])
        return harness.current["instruction"], None, None

    harness.prepare_case = prepare

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda statement, params: cancelled.append(params[0]))

    harness.database = SimpleNamespace(
        create_business_task=lambda scope, params, budget: {"id": harness.current["task_id"]},
        claim=lambda task, owner, seconds: {"id": task, "lease_generation": 1},
        connect=connect,
        event=lambda task, kind, payload: events.append((task, kind, payload)),
    )

    class PreparedServices:
        def __init__(self, *args):
            self.evidence = copy.deepcopy(harness.current["evidence"])

        def run_phase(self, name, state):
            assert name == "prepare"
            return {"evidence": self.evidence}

        def close(self):
            pass

    monkeypatch.setattr("decagent.evaluation.business.harness.BusinessServices", PreparedServices)

    def adapter(services, attribution):
        entry = copy.deepcopy(harness.current)

        def response(request):
            calls.append(entry["case_id"])
            payload = {
                "model": "jev-1.13.0",
                "answers": entry["evaluation"]["answers"],
                "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.001},
            }
            status = 200
            if len(calls) == 2:
                score = next(a for a in payload["answers"].values() if a["type"] == "score")
                score["score"] = 2 if score["score"] == 0 else 0
                if mode == "unknown_cost":
                    payload["usage"].pop("cost")
                elif mode == "unknown_tokens":
                    payload["usage"]["output_tokens"] = None
                elif mode == "missing_model":
                    payload["model"] = None
                elif mode == "http_error":
                    status = 503
            return httpx.Response(status, json=payload)

        return JevAdapter(
            api_key="offline-test-key",
            model="~typesafe/jev-latest",
            provider="openrouter",
            client=httpx.Client(transport=httpx.MockTransport(response)),
            budget=GlobalAttemptBudget(
                harness.global_ledger, reservation="0.01", attribution=attribution
            ),
            cache_ttl=0,
            max_retries=0,
        )

    harness._adapter = adapter
    return harness, calls, cancelled, events


def test_collector_v2_continues_known_cost_score_failure_once_per_case(tmp_path, monkeypatch):
    harness, calls, cancelled, events = offline_collector(tmp_path, monkeypatch)
    saved = []
    export = harness.collect_development(save=lambda value: saved.append(copy.deepcopy(value)))
    assert export["schema_version"] == "business-calibration-export-2"
    assert export["collection_complete"] is True and export["complete"] is True
    assert export["stop_reason"] is None
    assert len(calls) == len(set(calls)) == len(export["records"]) == 40
    assert len(cancelled) == len(events) == 40
    assert len(harness.global_ledger.snapshot()["attempts"]) == 40
    failed = export["records"][1]["evaluation"]
    assert failed["error"] == failed["attempts"][0]["error"] == "invalid_response"
    assert failed["answers"] == {} and failed["cached"] is False
    assert export["collection_summary"] == {
        "valid_records": 39,
        "invalid_response_records": 1,
        "failed_or_unaccounted_records": 0,
        "disabled_family_count": len(failed["question_versions"]),
        "disabled_families": sorted(failed["question_versions"]),
        "family_invalid_response_counts": {name: 1 for name in sorted(failed["question_versions"])},
    }
    assert saved[1]["collection_complete"] is False
    assert saved[1]["collection_summary"]["invalid_response_records"] == 1
    assert saved[-1] == export


@pytest.mark.parametrize("mode", ["unknown_cost", "unknown_tokens", "missing_model", "http_error"])
def test_collector_other_failure_or_unknown_usage_stops_without_retry(tmp_path, monkeypatch, mode):
    harness, calls, cancelled, events = offline_collector(tmp_path, monkeypatch, mode)
    export = harness.collect_development()
    assert len(calls) == len(export["records"]) == len(cancelled) == len(events) == 2
    assert export["collection_complete"] is False
    assert export["complete"] is False and export["stop_reason"]
    assert export["collection_summary"]["invalid_response_records"] == 0
    assert export["collection_summary"]["failed_or_unaccounted_records"] == 1


def test_collector_over_cap_snapshot_stops_before_dispatch(tmp_path, monkeypatch):
    harness, calls, _, _ = offline_collector(tmp_path, monkeypatch)
    harness._ledger_snapshot = lambda: {
        "blocked_unknown_or_pending": False,
        "within_cap": False,
        "remaining_usd": "0.9",
    }
    export = harness.collect_development()
    assert not calls and not export["records"]
    assert export["stop_reason"] == "global_jev_budget_or_unknown_usage"


def test_probe_remains_explicitly_incomplete_under_v2(tmp_path, monkeypatch):
    harness, calls, _, _ = offline_collector(tmp_path, monkeypatch)
    export = harness.collect_development(max_cases=2)
    assert len(calls) == 2
    assert export["collection_complete"] is False and export["complete"] is False
    assert export["stop_reason"] == "explicit_probe_limit"

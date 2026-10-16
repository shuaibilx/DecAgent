"""Experiment controls have no real providers or human ground-truth claims."""

import importlib
import json
from dataclasses import replace

import pytest


def api():
    try:
        return importlib.import_module("decagent.evaluation")
    except ImportError as exc:
        pytest.fail(f"Benchmark tooling is not implemented: {exc}")


def manifest(**overrides):
    e = api()
    values = dict(
        run_id="fixture-run",
        dataset_version="fixture-1",
        dataset_sha256="a" * 64,
        sample_ids=("s1", "s2"),
        strategies=("rule", "main-llm", "jev"),
        primary_model=e.ModelBinding("qwen-test", "qwen-test-20261001"),
        judges={
            "rule": e.ModelBinding("rule", "rule-v1"),
            "main-llm": e.ModelBinding("qwen-test", "qwen-test-20261001"),
            "jev": e.ModelBinding("~typesafe/jev-latest", "jev-1.13.0"),
        },
        authorization_sha256="b" * 64,
        tools_sha256="c" * 64,
        hard_policy_sha256="d" * 64,
        acceptance_sha256="e" * 64,
        max_calls=2,
        max_tokens=400,
        max_usd="0.04",
        paid=True,
        price=e.PriceBasis(
            "fixture-prices", "https://example.org/prices", "2026-10-10", "100", "100"
        ),
    )
    values.update(overrides)
    return e.RunManifest(**values)


def authorized_store(tmp_path, **overrides):
    e = api()
    m = manifest(**overrides)
    receipt = e.RunAuthorization(m.sha256, "human-owner", "owner-confirmation-1")
    return e.ProgressStore.create(tmp_path / "progress.json", m, authorization=receipt)


def test_paid_run_needs_exact_confirmed_manifest_and_nonzero_limits(tmp_path):
    e = api()
    m = manifest()
    with pytest.raises(ValueError, match="authorization"):
        e.ProgressStore.create(tmp_path / "progress.json", m)
    with pytest.raises(ValueError, match="authorization"):
        e.ProgressStore.create(
            tmp_path / "progress.json",
            m,
            authorization=e.RunAuthorization("f" * 64, "owner", "confirm"),
        )
    for override in ({"max_calls": 0}, {"max_usd": "0"}, {"price": None}):
        with pytest.raises(ValueError):
            manifest(**override)


def test_retries_reserve_before_provider_and_unknown_usage_keeps_reservation(tmp_path):
    e = api()
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    called = []

    def failed_provider(ticket):
        disk = json.loads((tmp_path / "progress.json").read_text())
        assert disk["attempts"][ticket.attempt_id]["status"] == "reserved"
        called.append(ticket.attempt_id)
        raise TimeoutError("secret provider message must not be persisted")

    with pytest.raises(TimeoutError):
        e.BudgetedCalls(store, "jev", "s1").call("judge", 100, 100, failed_provider)
    reopened = e.ProgressStore.open(tmp_path / "progress.json")
    assert reopened.snapshot()["budget"]["committed_usd"] == "0.02"
    assert reopened.snapshot()["attempts"][called[0]]["status"] == "unknown"
    assert "secret provider" not in (tmp_path / "progress.json").read_text()
    ticket = reopened.reserve("jev", "s1", "judge", 100, 100)
    reopened.settle(
        ticket.attempt_id,
        cost_usd="0.02",
        input_tokens=100,
        output_tokens=100,
        actual_model="jev-1.13.0",
    )
    with pytest.raises(e.BudgetExhausted):
        reopened.reserve("jev", "s1", "judge", 1, 1)
    assert len(reopened.snapshot()["attempts"]) == 2
    with pytest.raises(e.BudgetExhausted):
        reopened.start_sample("jev", "s2")


def test_progress_resume_preserves_all_cells_and_never_reissues_unknown_attempt(tmp_path):
    e = api()
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    store.reserve("jev", "s1", "judge", 100, 100)
    resumed = e.ProgressStore.open(tmp_path / "progress.json")
    resumed.recover_interrupted()
    snapshot = resumed.snapshot()
    assert snapshot["samples"]["jev/s1"]["status"] == "unknown"
    assert snapshot["samples"]["jev/s2"]["status"] == "pending"
    assert len(snapshot["samples"]) == 6
    with pytest.raises(ValueError, match="reconciliation"):
        resumed.start_sample("jev", "s1")
    assert snapshot["budget"]["committed_usd"] == "0.02"


def test_settlement_is_idempotent_and_cannot_erase_real_charges(tmp_path):
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    ticket = store.reserve("jev", "s1", "judge", 100, 100)
    args = dict(cost_usd="0.01", input_tokens=10, output_tokens=20, actual_model="jev-1.13.0")
    store.settle(ticket.attempt_id, **args)
    store.settle(ticket.attempt_id, **args)
    assert store.snapshot()["budget"]["committed_usd"] == "0.01"
    with pytest.raises(ValueError, match="conflicting"):
        store.settle(ticket.attempt_id, **{**args, "cost_usd": "0"})
    assert store.snapshot()["budget"]["committed_usd"] == "0.01"


def test_manifest_binding_and_resolved_model_protect_comparability(tmp_path):
    e = api()
    m = manifest()
    assert replace(m, hard_policy_sha256="f" * 64).sha256 != m.sha256
    with pytest.raises(ValueError):
        e.ModelBinding("~typesafe/jev-latest", "~typesafe/jev-latest")
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    t = store.reserve("jev", "s1", "judge", 1, 1)
    store.settle(
        t.attempt_id, cost_usd="0.0002", input_tokens=1, output_tokens=1, actual_model="jev-1.14.0"
    )
    assert store.snapshot()["samples"]["jev/s1"]["status"] == "model_version_changed"
    with pytest.raises(ValueError):
        store.finish_sample("jev", "s1", {"status": "succeeded"})


def test_known_cost_with_unknown_tokens_retains_reserved_token_capacity(tmp_path):
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    t = store.reserve("jev", "s1", "judge", 100, 100)
    store.settle(
        t.attempt_id,
        cost_usd="0.01",
        input_tokens=None,
        output_tokens=None,
        actual_model="jev-1.13.0",
    )
    assert store.snapshot()["budget"]["committed_tokens"] == 200


def test_snapshot_and_manifest_are_immutable_to_caller(tmp_path):
    store = authorized_store(tmp_path)
    snapshot = store.snapshot()
    snapshot["manifest"]["max_calls"] = 1000
    assert store.snapshot()["manifest"]["max_calls"] == 2


def test_unknown_settlement_reconciliation_does_not_double_release_tokens(tmp_path):
    store = authorized_store(tmp_path)
    store.start_sample("jev", "s1")
    t = store.reserve("jev", "s1", "judge", 100, 100)
    store.settle(
        t.attempt_id, cost_usd=None, input_tokens=10, output_tokens=20, actual_model="jev-1.13.0"
    )
    store.settle(
        t.attempt_id, cost_usd="0.01", input_tokens=10, output_tokens=20, actual_model="jev-1.13.0"
    )
    assert store.snapshot()["budget"]["committed_tokens"] == 30
    assert store.snapshot()["budget"]["committed_usd"] == "0.01"


def test_run_pending_stops_at_cap_and_keeps_unfinished_cells(tmp_path):
    e = api()
    store = authorized_store(tmp_path)

    def execute(strategy, sample, calls):
        calls.call(
            "judge",
            100,
            100,
            lambda t: e.CallOutcome("review", t.expected_actual_model, "0.02", 100, 100),
        )
        return {"status": "completed", "route": "review"}

    e.run_pending(store, execute)
    snapshot = store.snapshot()
    assert sum(c["status"] == "completed" for c in snapshot["samples"].values()) == 2
    assert sum(c["status"] == "pending" for c in snapshot["samples"].values()) == 4
    assert snapshot["budget"]["calls"] == 2


def test_each_actual_model_can_bind_distinct_price_basis(tmp_path):
    e = api()
    prices = {
        "jev-1.13.0": e.PriceBasis(
            "jev-fixture", "https://example.org/jev", "2026-10-10", "100", "100"
        ),
        "qwen-test-20261001": e.PriceBasis(
            "qwen-fixture", "https://example.org/qwen", "2026-10-10", "1", "2"
        ),
        "rule-v1": e.PriceBasis("rules-free", "https://example.org/rules", "2026-10-10", "0", "0"),
    }
    store = authorized_store(tmp_path, model_prices=prices)
    store.start_sample("jev", "s1")
    primary = store.reserve("jev", "s1", "primary", 100, 100)
    judge = store.reserve("jev", "s1", "judge", 100, 100)
    assert primary.reserved_usd == "0.0003"
    assert judge.reserved_usd == "0.02"


def test_two_instances_cannot_overreserve_shared_progress(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    e = api()
    store = authorized_store(tmp_path, max_calls=1)
    store.start_sample("jev", "s1")

    def reserve():
        try:
            e.ProgressStore.open(store.path).reserve("jev", "s1", "judge", 100, 100)
            return "reserved"
        except e.BudgetExhausted:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(outcomes) == ["exhausted", "reserved"]
    assert store.snapshot()["budget"]["calls"] == 1

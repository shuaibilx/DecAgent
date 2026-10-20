"""Readiness authorization is durable and never sends a request by default."""

import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest

from decagent.models.primary import ModelTurn, ModelUsage, PrimaryResponseRejected, ToolProposal
from decagent.models.validation_feedback import ValidationIssue
from decagent.storage.budget import BudgetExceeded
from evals.deterministic.test_business_plan_compiler import read_plan

MODULE = "decagent.evaluation.business.readiness"
FINGERPRINT = "a" * 64


@pytest.fixture(autouse=True)
def isolated_authorization_anchor(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    # Only tests may redirect the fixed project authorization anchor. Neither
    # Settings, environment variables nor CLI arguments can select it.
    monkeypatch.setattr(
        module, "AUTHORIZATION_ANCHOR", tmp_path / "fixed-project-anchor.sqlite", raising=False
    )


def test_readiness_module_exists():
    assert importlib.util.find_spec(MODULE) is not None, "Persistent readiness gate is missing"


def ledger(path, **kwargs):
    return importlib.import_module(MODULE).SqliteReadinessLedger.open(
        path,
        model="qwen3.8-flash",
        endpoint_host="maas.qianwenaiapi.com",
        diagnostic_fingerprint=kwargs.pop("diagnostic_fingerprint", FINGERPRINT),
        **kwargs,
    )


def reserve(target, key="request", *, tokens=100, purpose="plan"):
    return target.reserve_model(
        key, 1, "primary", tokens, Decimal(0), model_family="primary", purpose=purpose
    )


def finish(target, receipt, tokens=10, **kwargs):
    target.mark_dispatched(receipt)
    target.settle_model(receipt, tokens, Decimal(0), response_state="responded", **kwargs)


def test_authorization_caps_survive_restart_and_duplicate_settlement(tmp_path):
    path = tmp_path / "readiness.sqlite"
    target = ledger(path)
    for index in range(3):
        receipt = reserve(target, str(index))
        finish(target, receipt)
        with pytest.raises(ValueError):
            target.settle_model(receipt, 10, Decimal(0), response_state="responded")
        target.close()
        target = ledger(path)
    with pytest.raises(BudgetExceeded):
        reserve(target, "four")
    assert target.snapshot()["max_calls"] == 3
    assert len(target.snapshot()["attempts"]) == 3
    target.close()


@pytest.mark.parametrize("state", ["reserved", "dispatching", "unknown"])
def test_uncertain_or_crashed_request_blocks_all_followup(tmp_path, state):
    path = tmp_path / "readiness.sqlite"
    target = ledger(path)
    receipt = reserve(target)
    if state != "reserved":
        target.mark_dispatched(receipt)
    if state == "unknown":
        target.settle_model(receipt, None, None, response_state="unknown")
    target.close()
    reopened = ledger(path)
    with pytest.raises(BudgetExceeded):
        reserve(reopened, "followup")
    assert reopened.snapshot()["blocked"] is True
    reopened.close()


def test_identity_drift_is_rejected_and_original_records_preserved(tmp_path):
    path = tmp_path / "readiness.sqlite"
    target = ledger(path)
    finish(target, reserve(target))
    target.close()
    with pytest.raises(ValueError, match="identity"):
        ledger(path, diagnostic_fingerprint="b" * 64)
    target = ledger(path)
    assert len(target.snapshot()["attempts"]) == 1
    target.close()


def test_unknown_usage_with_free_price_still_blocks(tmp_path):
    target = ledger(tmp_path / "readiness.sqlite")
    receipt = reserve(target)
    target.mark_dispatched(receipt)
    target.settle_model(receipt, None, Decimal(0), response_state="responded")
    with pytest.raises(BudgetExceeded):
        reserve(target, "followup")
    assert target.snapshot()["attempts"][0]["status"] == "unknown"
    target.close()


def test_unknown_attempt_is_not_reported_as_zero_aggregate_tokens(tmp_path):
    target = ledger(tmp_path / "readiness.sqlite")
    receipt = reserve(target)
    target.mark_dispatched(receipt)
    target.settle_model(receipt, None, None, response_state="unknown")

    snapshot = target.snapshot()

    assert snapshot["attempts"][0]["actual_tokens"] is None
    assert snapshot["actual_tokens"] is None
    assert snapshot["known_actual_tokens"] == 0
    assert snapshot["actual_tokens_complete"] is False
    target.close()


def test_total_token_budget_is_fixed_and_cannot_be_raised_after_known_settlement(tmp_path):
    target = ledger(tmp_path / "readiness.sqlite")
    finish(target, reserve(target, tokens=60000), tokens=60000)
    with pytest.raises(BudgetExceeded):
        reserve(target, "too-large", tokens=40001)
    assert target.snapshot()["max_total_tokens"] == 100000
    finish(target, reserve(target, "exact-boundary", tokens=40000), tokens=40000)
    with pytest.raises(BudgetExceeded):
        reserve(target, "exhausted", tokens=1)
    target.close()


def test_request_input_cap_is_separate_from_new_total_authorization(tmp_path):
    target = ledger(tmp_path / "readiness.sqlite")
    with pytest.raises(BudgetExceeded):
        reserve(target, tokens=65536 + 8192 + 1)
    finish(target, reserve(target, "maximum-input", tokens=65536 + 8192), tokens=100)
    assert target.snapshot()["max_request_input_tokens"] == 65536
    assert target.snapshot()["max_output_tokens"] == 8192
    target.close()


def test_overbound_provider_usage_is_saved_and_stops_future_requests(tmp_path):
    target = ledger(tmp_path / "readiness.sqlite")
    receipt = reserve(target)
    target.mark_dispatched(receipt)
    with pytest.raises(BudgetExceeded):
        target.settle_model(receipt, 101, Decimal(0), response_state="responded")
    assert target.snapshot()["attempts"][0]["actual_tokens"] == 101
    with pytest.raises(BudgetExceeded):
        reserve(target, "followup")
    target.close()


def test_only_one_format_repair_is_allowed_across_process_restart(tmp_path):
    path = tmp_path / "readiness.sqlite"
    target = ledger(path)
    finish(target, reserve(target))
    finish(target, reserve(target, "repair", purpose="format_repair"))
    target.close()
    target = ledger(path)
    with pytest.raises(BudgetExceeded):
        reserve(target, "repair-2", purpose="format_repair")
    target.close()


def test_concurrent_reservations_cannot_exceed_authorization(tmp_path):
    path = tmp_path / "readiness.sqlite"
    ledger(path).close()

    def worker(index):
        target = ledger(path)
        try:
            receipt = reserve(target, str(index))
            finish(target, receipt)
            return True
        except BudgetExceeded:
            return False
        finally:
            target.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(worker, range(12)))
    assert 1 <= sum(results) <= 3
    target = ledger(path)
    assert len(target.snapshot()["attempts"]) == sum(results)
    target.close()


def settings(data_dir=None):
    return SimpleNamespace(
        data_dir=data_dir,
        main_model="qwen3.8-flash",
        main_base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        main_api_key="secret-never-record",
        jev_api_key="",
        github_token="",
    )


def test_default_offline_never_constructs_provider_or_creates_ledger(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline readiness must never construct a provider")

    monkeypatch.setattr(module.PrimaryModel, "from_settings", forbidden)
    path = tmp_path / "readiness.sqlite"
    report = module.run_readiness(settings(), ledger_path=path, diagnostic_fingerprint=FINGERPRINT)
    assert report["status"] == "offline"
    assert report["business_tool_calls"] == 0
    assert not path.exists()


def test_offline_cli_default_snapshot_exposes_the_frozen_new_authorization():
    module = importlib.import_module(MODULE)
    report = module.run_readiness(
        settings(), ledger_path="unused.sqlite", diagnostic_fingerprint=FINGERPRINT
    )
    assert report["limits"] == {
        "max_calls": 3,
        "max_format_repairs": 1,
        "max_request_input_tokens": 65536,
        "max_output_tokens": 8192,
        "max_total_tokens": 100000,
        "total_tokens_include_input_and_output": True,
    }


class ScriptedModel:
    schema_bytes = 0

    def __init__(self, outcomes):
        self.outcomes, self.calls = iter(outcomes), 0

    def respond(self, messages, *, stream=False):
        self.calls += 1
        value = next(self.outcomes)
        if isinstance(value, Exception):
            raise value
        return value


def plan_turn():
    return ModelTurn(
        "",
        (ToolProposal("call", "decagent_submit_plan", read_plan()),),
        ModelUsage(10, 5, 15),
        "qwen3.8-flash",
    )


def live_run(tmp_path, monkeypatch, model):
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(module.PrimaryModel, "from_settings", lambda *a, **kw: model)
    return module.run_readiness(
        settings(tmp_path),
        ledger_path=tmp_path / "readiness.sqlite",
        diagnostic_fingerprint=FINGERPRINT,
        live=True,
        evidence_path=tmp_path / "evidence.json",
    )


def test_read_only_plan_compilation_is_persisted_with_accounted_requests(tmp_path, monkeypatch):
    model = ScriptedModel([plan_turn()])
    report = live_run(tmp_path, monkeypatch, model)
    assert report["status"] == "passed"
    assert report["business_tool_calls"] == 0
    assert report["plan_node_types"] == ["read_tool", "complete"]
    assert report["plan_commands"] == ["directory"]
    assert report["ledger"]["attempts"][0]["actual_tokens"] == 15
    stored = json.loads((tmp_path / "evidence.json").read_text("utf-8"))
    assert stored["compiled_plan"]["entry"] == "directory"
    assert model.calls == 1


def test_known_rejection_records_safe_issues_and_allows_only_budgeted_repair(tmp_path, monkeypatch):
    model = ScriptedModel(
        [
            PrimaryResponseRejected(
                reason="tool_schema",
                usage=ModelUsage(10, 5, 15),
                validation_issues=(ValidationIssue("schema_required", ("nodes",)),),
            ),
            plan_turn(),
        ]
    )
    report = live_run(tmp_path, monkeypatch, model)
    assert report["status"] == "passed"
    attempts = report["ledger"]["attempts"]
    assert len(attempts) == 2
    assert attempts[1]["purpose"] == "format_repair"
    assert attempts[0]["validation_issues"] == [{"code": "schema_required", "path": ["nodes"]}]
    assert report["format_repair_used"] is True


def test_arbitrary_transport_exception_is_unknown_sanitized_and_never_retried(
    tmp_path, monkeypatch
):
    model = ScriptedModel([RuntimeError("secret-never-record upstream body"), plan_turn()])
    report = live_run(tmp_path, monkeypatch, model)
    assert report["status"] == "stopped"
    assert report["ledger"]["blocked"] is True
    assert model.calls == 1
    serialized = (tmp_path / "evidence.json").read_text("utf-8")
    assert "secret-never-record" not in serialized
    assert "upstream body" not in serialized


def test_unknown_usage_does_not_allow_planner_repair(tmp_path, monkeypatch):
    model = ScriptedModel([PrimaryResponseRejected(reason="tool_schema"), plan_turn()])
    report = live_run(tmp_path, monkeypatch, model)
    assert report["status"] == "stopped"
    assert model.calls == 1
    assert report["ledger"]["attempts"][0]["status"] == "unknown"


def test_diagnostic_fingerprint_tracks_actual_packaged_sources():
    module = importlib.import_module(MODULE)
    fingerprint = module.diagnostic_fingerprint()
    assert len(fingerprint) == 64
    assert fingerprint == module.diagnostic_fingerprint()


def test_existing_evidence_cannot_cause_an_unrecorded_live_request(tmp_path, monkeypatch):
    model = ScriptedModel([plan_turn()])
    (tmp_path / "evidence.json").write_text("old evidence", "utf-8")
    with pytest.raises(FileExistsError):
        live_run(tmp_path, monkeypatch, model)
    assert model.calls == 0
    assert (tmp_path / "evidence.json").read_text("utf-8") == "old evidence"


def test_restart_after_pass_reuses_evidence_without_another_request(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([plan_turn()])
    live_run(tmp_path, monkeypatch, model)
    repeated = module.run_readiness(
        settings(tmp_path),
        ledger_path=tmp_path / "readiness.sqlite",
        diagnostic_fingerprint=FINGERPRINT,
        live=True,
    )
    assert repeated["status"] == "passed"
    assert repeated["reused_passed_evidence"] is True
    assert model.calls == 1


def test_dispatch_guard_rejects_mutated_settings_before_network(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: FINGERPRINT)
    configuration = settings(tmp_path)
    model = ScriptedModel([plan_turn()])

    def factory(*args, **kwargs):
        configuration.main_model = "other-model"
        return model

    monkeypatch.setattr(module.PrimaryModel, "from_settings", factory)
    report = module.run_readiness(
        configuration,
        ledger_path=tmp_path / "readiness.sqlite",
        diagnostic_fingerprint=FINGERPRINT,
        live=True,
    )
    assert report["status"] == "stopped"
    assert model.calls == 0
    assert report["ledger"]["attempts"][0]["dispatch_state"] == "not_sent"


def test_same_authorization_cannot_reset_by_changing_ledger_path(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([RuntimeError("unknown transport"), plan_turn()])
    live_run(tmp_path, monkeypatch, model)
    with pytest.raises(ValueError, match="path"):
        module.run_readiness(
            settings(tmp_path),
            ledger_path=tmp_path / "new.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 1
    assert not (tmp_path / "new.sqlite").exists()


def test_zero_attempt_authorization_still_rejects_new_ledger_path(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: FINGERPRINT)
    model = ScriptedModel([plan_turn()])

    def factory(*args, **kwargs):
        raise ValueError("Configuration could not construct client")

    monkeypatch.setattr(module.PrimaryModel, "from_settings", factory)
    first = module.run_readiness(
        settings(tmp_path),
        ledger_path=tmp_path / "old.sqlite",
        diagnostic_fingerprint=FINGERPRINT,
        live=True,
    )
    assert first["ledger"]["attempts"] == []
    monkeypatch.setattr(module.PrimaryModel, "from_settings", lambda *a, **kw: model)
    with pytest.raises(ValueError, match="path"):
        module.run_readiness(
            settings(tmp_path),
            ledger_path=tmp_path / "new.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 0


def test_missing_previously_initialized_ledger_fails_closed(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([RuntimeError("unknown transport"), plan_turn()])
    live_run(tmp_path, monkeypatch, model)
    (tmp_path / "readiness.sqlite").unlink()
    with pytest.raises(ValueError, match="missing"):
        module.run_readiness(
            settings(tmp_path),
            ledger_path=tmp_path / "readiness.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 1


def test_data_dir_and_ledger_path_cannot_jointly_refresh_authorization(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([RuntimeError("unknown transport"), plan_turn()])
    live_run(tmp_path, monkeypatch, model)
    changed_state = tmp_path / "different-configured-data-dir"
    monkeypatch.setenv("DECAGENT_DATA_DIR", str(changed_state))
    with pytest.raises(ValueError, match="path"):
        module.run_readiness(
            settings(changed_state),
            ledger_path=changed_state / "new.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 1
    assert not (changed_state / "new.sqlite").exists()


def test_moved_ledger_cannot_refresh_authorization_under_new_data_dir(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([RuntimeError("unknown transport"), plan_turn()])
    live_run(tmp_path, monkeypatch, model)
    moved = tmp_path / "moved.sqlite"
    (tmp_path / "readiness.sqlite").rename(moved)
    with pytest.raises(ValueError, match="path"):
        module.run_readiness(
            settings(tmp_path / "different-state"),
            ledger_path=moved,
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    with pytest.raises(ValueError, match="missing"):
        module.run_readiness(
            settings(tmp_path / "different-state"),
            ledger_path=tmp_path / "readiness.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 1


def test_fixed_anchor_is_not_derived_from_configured_data_dir(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    live_run(tmp_path, monkeypatch, ScriptedModel([RuntimeError("unknown transport")]))
    assert module.AUTHORIZATION_ANCHOR.is_file()
    assert not (tmp_path / "qwen-readiness-authorizations.sqlite").exists()


def test_report_explicitly_references_the_old_unknown_evidence():
    module = importlib.import_module(MODULE)
    report = module.run_readiness(
        settings(), ledger_path="unused.sqlite", diagnostic_fingerprint=FINGERPRINT
    )
    assert (
        "evals/benchmarks/evidence/qwen-plan-format-repair-20261019.json"
        in (report["historical_evidence"])
    )


def test_expected_diagnostic_must_match_actual_sources_before_provider(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([plan_turn()])
    monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: "b" * 64)
    monkeypatch.setattr(module.PrimaryModel, "from_settings", lambda *a, **kw: model)
    with pytest.raises(ValueError, match="fingerprint"):
        module.run_readiness(
            settings(tmp_path),
            ledger_path=tmp_path / "readiness.sqlite",
            diagnostic_fingerprint=FINGERPRINT,
            live=True,
        )
    assert model.calls == 0


def test_source_change_after_client_creation_stops_before_dispatch(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    model = ScriptedModel([plan_turn()])
    monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: FINGERPRINT)

    def factory(*args, **kwargs):
        monkeypatch.setattr(module, "diagnostic_fingerprint", lambda: "b" * 64)
        return model

    monkeypatch.setattr(module.PrimaryModel, "from_settings", factory)
    report = module.run_readiness(
        settings(tmp_path),
        ledger_path=tmp_path / "readiness.sqlite",
        diagnostic_fingerprint=FINGERPRINT,
        live=True,
    )
    assert model.calls == 0
    assert report["status"] == "stopped"
    assert report["ledger"]["attempts"][0]["dispatch_state"] == "not_sent"


def test_overreservation_rejection_preserves_safe_feedback_before_budget_error(
    tmp_path, monkeypatch
):
    model = ScriptedModel(
        [
            PrimaryResponseRejected(
                reason="tool_schema",
                usage=ModelUsage(99999, 5, 100004),
                validation_issues=(ValidationIssue("schema_type", ("nodes",)),),
            ),
            plan_turn(),
        ]
    )
    report = live_run(tmp_path, monkeypatch, model)
    assert report["status"] == "stopped"
    assert report["ledger"]["blocked"] is True
    row = report["ledger"]["attempts"][0]
    assert row["actual_tokens"] == 100004
    assert row["validation_issues"] == [{"code": "schema_type", "path": ["nodes"]}]
    assert model.calls == 1

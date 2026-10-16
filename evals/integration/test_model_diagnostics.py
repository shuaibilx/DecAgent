"""Default diagnostics persist their dispatch intent before an unpaid injected provider."""

from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings
from decagent.gateway import initialize_owner
from decagent.models.primary import ModelTurn, ModelUsage
from decagent.runtime.application import create_workbench
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import TokenPrice
from decagent.runtime.model_diagnostics import ModelDiagnostics

pytestmark = pytest.mark.integration


def test_selected_main_diagnostic_is_bounded_persistent_and_replay_safe(database, tmp_path):
    calls = []

    class Model:
        def respond(self, messages, *, stream=False):
            with database.connect() as conn:
                record = conn.execute("SELECT * FROM app.model_diagnostics").fetchone()
                assert record["status"] == "dispatched" and record["result"] is None
            calls.append(messages)
            return ModelTurn("ok", (), ModelUsage(8, 2, 10), "offline-model")

    config = RuntimeConfig(main_price=TokenPrice("unpaid-test", Decimal(0), Decimal(0)))
    run = ModelDiagnostics(
        database, Settings(data_dir=tmp_path, main_api_key="fake"), config, primary=Model()
    )
    first = run("main", "a" * 64)
    assert first["status"] == "verified" and first["actual_model"] == "offline-model"
    assert run("main", "a" * 64) == first and len(calls) == 1
    run.settings = Settings(data_dir=tmp_path)
    assert run("main", "a" * 64) == first and len(calls) == 1
    with pytest.raises(ValueError, match="different"):
        run("jev", "a" * 64)


def test_lost_response_keeps_reservation_and_does_not_charge_again(database, tmp_path):
    calls = []

    class Model:
        def respond(self, messages, *, stream=False):
            calls.append(True)
            raise TimeoutError("Injected after provider dispatch")

    config = RuntimeConfig(unknown_main_reservation=Decimal("0.02"))
    run = ModelDiagnostics(
        database, Settings(data_dir=tmp_path, main_api_key="fake"), config, primary=Model()
    )
    result = run("main", "b" * 64)
    assert result["status"] == "unknown" and result["held_usd"] == "0.02"
    assert run("main", "b" * 64) == result and len(calls) == 1


def test_only_selected_jev_is_called_and_usage_is_settled(database, tmp_path):
    calls = []

    def respond(request):
        with database.connect() as conn:
            assert (
                conn.execute("SELECT status FROM app.model_diagnostics").fetchone()["status"]
                == "dispatched"
            )
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13-20260917",
                "answers": {"memory_read_needed": {"type": "noul", "noul": 0.1}},
                "usage": {"input_tokens": 100, "output_tokens": 10, "cost": 0.00001},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = ModelDiagnostics(
            database,
            Settings(
                data_dir=tmp_path,
                jev_api_key="fake",
                jev_provider="openrouter",
                jev_model="~typesafe/jev-latest",
            ),
            RuntimeConfig(jev_reservation=Decimal("0.02")),
            jev_client=client,
        )
        result = run("jev", "c" * 64)
        assert result["status"] == "verified" and Decimal(result["cost_usd"]) == Decimal("0.00001")
        assert run("jev", "c" * 64) == result and len(calls) == 1
        assert str(calls[0].url) == "https://openrouter.ai/api/alpha/decisions"


def test_default_gateway_needs_explicit_confirmation_and_replays_same_key(
    database, tmp_path, monkeypatch
):
    calls = []

    class Model:
        def respond(self, messages, *, stream=False):
            calls.append(True)
            return ModelTurn("ok", (), ModelUsage(8, 2, 10), "offline-model")

    monkeypatch.setattr(
        "decagent.runtime.model_diagnostics.PrimaryModel.from_settings", lambda *a, **k: Model()
    )
    settings = Settings(database_url=database.url, data_dir=tmp_path, main_api_key="fake")
    token = initialize_owner(database, tmp_path / "owner.token").read_text().strip()
    client = TestClient(
        create_workbench(
            settings,
            database=database,
            runtime_config=RuntimeConfig(
                main_price=TokenPrice("unpaid-test", Decimal(0), Decimal(0))
            ),
            broker_client=lambda *a: {},
        )
    )
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "selected-main"}
    assert (
        client.post(
            "/api/v1/diagnostics", json={"mode": "live", "target": "main"}, headers=headers
        ).status_code
        == 422
    )
    assert not calls
    body = {"mode": "live", "target": "main", "confirm_external_request": True}
    first = client.post("/api/v1/diagnostics", json=body, headers=headers)
    assert first.status_code == 200 and first.json()["result"]["status"] == "verified"
    assert client.post("/api/v1/diagnostics", json=body, headers=headers).json() == first.json()
    assert len(calls) == 1 and "fake" not in first.text

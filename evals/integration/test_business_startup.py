"""Opening the business workbench does not assemble repair integrations."""

import pytest
from fastapi.testclient import TestClient

from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration


def test_configured_repair_clients_stay_lazy_for_business_startup(database, tmp_path, monkeypatch):
    import decagent.runtime.application as application

    def forbidden(*args, **kwargs):
        raise AssertionError("Repair client was initialized for business startup")

    monkeypatch.setattr(application.httpx, "Client", forbidden)
    monkeypatch.setattr(application, "HttpBrokerClient", forbidden)
    settings = Settings(database_url=database.url, data_dir=tmp_path, github_token="fixture-key")
    app = application.create_workbench(
        settings, database=database, runtime_config=RuntimeConfig(broker_token="fixture-broker")
    )
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

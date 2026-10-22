"""Business readiness distinguishes API liveness from working dependencies."""

import pytest
from fastapi.testclient import TestClient

from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.config import Settings
from decagent.gateway.app import create_app
from decagent.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration


def test_api_liveness_stays_ok_when_business_mcp_is_not_ready(
    database, tmp_path, monkeypatch
):
    import decagent.business.readiness as readiness

    PluginRegistry(database).register(builtin_manifest())
    settings = Settings(
        database_url=database.url,
        data_dir=tmp_path,
        public_url="http://127.0.0.1:8000",
    )
    runtime_config = RuntimeConfig(conditional_workflows_enabled=True)
    client = TestClient(create_app(database, settings, runtime_config=runtime_config))

    monkeypatch.setattr(readiness, "_mcp_ready", lambda _url: True)
    assert client.get("/health").status_code == 200
    assert client.get("/readyz").status_code == 200

    monkeypatch.setattr(readiness, "_mcp_ready", lambda _url: False)
    assert client.get("/health").status_code == 200
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {
            "database": True,
            "business_plugin": True,
            "mcp": False,
            "conditional_workflows": True,
        },
    }

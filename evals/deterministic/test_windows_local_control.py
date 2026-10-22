"""Windows local roles share the container database without leaking credentials."""

import os
from urllib.parse import urlsplit

import pytest


@pytest.fixture(autouse=True)
def preserve_environment(monkeypatch):
    for name in (
        "DECAGENT_DATABASE_URL",
        "DECAGENT_DATA_DIR",
        "DECAGENT_BROKER_URL",
        "DECAGENT_TICKETS_MCP_URL",
        "DECAGENT_MAIN_API_KEY",
        "DECAGENT_JEV_API_KEY",
        "DECAGENT_GITHUB_TOKEN",
        "DECAGENT_BROKER_TOKEN",
        "PYTHONUTF8",
    ):
        monkeypatch.setenv(name, os.getenv(name, ""))


def test_local_configuration_uses_loopback_and_preserves_existing_model_key(monkeypatch, tmp_path):
    from ops.local.start import load_local_environment

    secrets = tmp_path / ".decagent/secrets"
    secrets.mkdir(parents=True)
    (secrets / "postgres_password").write_text("fixture:@ password", encoding="utf-8")
    (secrets / "main_api_key").write_text("file-fixture", encoding="utf-8")
    monkeypatch.setenv("DECAGENT_MAIN_API_KEY", "existing-fixture")
    for name in (
        "DECAGENT_LOCAL_DATABASE_URL",
        "DECAGENT_LOCAL_DATA_DIR",
        "DECAGENT_LOCAL_POSTGRES_PORT",
        "DECAGENT_LOCAL_BROKER_PORT",
        "DECAGENT_LOCAL_MCP_PORT",
        "DECAGENT_LOCAL_BROKER_URL",
        "DECAGENT_SECRETS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DECAGENT_LOCAL_BROKER_URL", "")
    load_local_environment(tmp_path)
    parsed = urlsplit(os.environ["DECAGENT_DATABASE_URL"])
    assert parsed.hostname == "127.0.0.1" and parsed.port == 55432
    assert parsed.username == "decagent" and parsed.path == "/decagent"
    assert parsed.password == "fixture%3A%40%20password"
    assert os.environ["DECAGENT_MAIN_API_KEY"] == "existing-fixture"
    assert os.environ["DECAGENT_BROKER_URL"] == "http://127.0.0.1:8002"
    assert os.environ["DECAGENT_TICKETS_MCP_URL"] == "http://127.0.0.1:8001/mcp"
    assert os.environ["DECAGENT_DATA_DIR"] == str(tmp_path / ".decagent/local-control")


def test_invalid_local_port_stops_before_role_start(monkeypatch, tmp_path):
    from ops.local.start import load_local_environment

    monkeypatch.setenv("DECAGENT_LOCAL_POSTGRES_PORT", "0")
    monkeypatch.delenv("DECAGENT_LOCAL_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="port"):
        load_local_environment(tmp_path)


def test_local_business_mcp_port_and_repair_broker_are_distinct(monkeypatch, tmp_path):
    from decagent.runtime.config import RuntimeConfig
    from ops.local.start import load_local_environment

    monkeypatch.setenv("DECAGENT_LOCAL_DATABASE_URL", "postgresql://fixture@127.0.0.1/test")
    monkeypatch.setenv("DECAGENT_LOCAL_MCP_PORT", "8853")
    monkeypatch.setenv("DECAGENT_LOCAL_BROKER_PORT", "8854")
    load_local_environment(tmp_path)
    assert os.environ["DECAGENT_TICKETS_MCP_URL"] == "http://127.0.0.1:8853/mcp"
    assert os.environ["DECAGENT_BROKER_URL"] == "http://127.0.0.1:8854"
    assert RuntimeConfig.from_env().tickets_mcp_url == "http://127.0.0.1:8853/mcp"


def test_local_mcp_cannot_reuse_repair_broker_port(monkeypatch, tmp_path):
    from ops.local.start import load_local_environment

    monkeypatch.setenv("DECAGENT_LOCAL_DATABASE_URL", "postgresql://fixture@127.0.0.1/test")
    monkeypatch.setenv("DECAGENT_LOCAL_MCP_PORT", "8853")
    monkeypatch.setenv("DECAGENT_LOCAL_BROKER_PORT", "8853")
    with pytest.raises(ValueError, match="must differ"):
        load_local_environment(tmp_path)


def test_runtime_config_rejects_non_mcp_endpoint():
    from decagent.runtime.config import RuntimeConfig

    with pytest.raises(ValueError, match="business MCP URL"):
        RuntimeConfig(tickets_mcp_url="http://127.0.0.1:8001/unrelated")


def test_business_server_cli_uses_shared_employee_credential_directory(monkeypatch, tmp_path):
    from decagent import cli
    from decagent.config import Settings

    settings = Settings(
        database_url="postgresql://fixture@127.0.0.1/test",
        data_dir=tmp_path / "control",
        public_url="http://127.0.0.1:8000",
    )
    database = object()
    captured = {}
    app = object()
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr("decagent.storage.database.Database", lambda url: database)

    def create_app(actual_database, employee_auth, **kwargs):
        captured.update(database=actual_database, auth=employee_auth, options=kwargs)
        return app

    monkeypatch.setattr("decagent.business.mcp_server.create_mcp_app", create_app)
    monkeypatch.setattr(
        "uvicorn.run", lambda actual_app, **kwargs: captured.update(app=actual_app, uvicorn=kwargs)
    )
    cli.main(["business-server", "--port", "8853"])
    assert captured["database"] is database
    assert captured["auth"].credential_dir == tmp_path / "control" / "demo-auth"
    assert captured["auth"].origin == settings.public_url
    assert captured["options"] == {"host": "127.0.0.1", "port": 8853}
    assert captured["app"] is app
    assert captured["uvicorn"] == {"host": "127.0.0.1", "port": 8853}

    resource_url = "http://business-mcp:8001/mcp"
    cli.main(
        [
            "business-server",
            "--port",
            "8854",
            "--resource-url",
            resource_url,
        ]
    )
    assert captured["options"] == {
        "host": "127.0.0.1",
        "port": 8854,
        "resource_url": resource_url,
    }

    monkeypatch.setenv("DECAGENT_LOCAL_MCP_PORT", "8855")
    cli.main(["business-server"])
    assert captured["options"] == {"host": "127.0.0.1", "port": 8855}

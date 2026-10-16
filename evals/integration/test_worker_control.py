"""Control secret loading and Compose boundaries without real provider credentials."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ops.control.export_owner import export_owner
from ops.control.prepare_secrets import write_new
from ops.control.start import SECRETS, load_secret_files


def test_secret_files_are_private_exclusive_and_process_values_take_precedence(
    tmp_path, monkeypatch
):
    for variable, filename in SECRETS.items():
        monkeypatch.delenv(variable, raising=False)
        path = tmp_path / filename
        write_new(path, "offline-fixture-credential")
        monkeypatch.setenv(variable + "_FILE", str(path))
        with pytest.raises(FileExistsError):
            write_new(path, "replacement")
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv("DECAGENT_MAIN_API_KEY", "process-override")
    monkeypatch.delenv("DECAGENT_DATABASE_URL", raising=False)
    password = tmp_path / "postgres_password"
    write_new(password, "offline:a@b")
    monkeypatch.setenv("DECAGENT_POSTGRES_PASSWORD_FILE", str(password))
    load_secret_files()
    assert os.environ["DECAGENT_MAIN_API_KEY"] == "process-override"
    assert os.environ["DECAGENT_JEV_API_KEY"] == "offline-fixture-credential"
    assert "offline%3Aa%40b" in os.environ["DECAGENT_DATABASE_URL"]


def test_compose_control_services_have_no_socket_and_models_stay_in_control_services(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    envfile = tmp_path / "empty.env"
    envfile.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "DECAGENT_RUNNER_IMAGE": "sha256:" + "a" * 64,
        "DECAGENT_SECRETS_DIR": str(tmp_path),
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(envfile),
            "--file",
            "compose.yaml",
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    for role in ("api", "worker", "migrate"):
        assert services[role]["read_only"] is True
        assert all("docker.sock" not in volume["source"] for volume in services[role]["volumes"])
        assert not any(name.endswith("API_KEY") for name in services[role]["environment"])
    assert {secret["source"] for secret in services["api"]["secrets"]} == {
        "postgres_password",
        "broker_token",
        "github_token",
        "main_api_key",
        "jev_api_key",
    }
    assert {secret["source"] for secret in services["worker"]["secrets"]} == {
        "postgres_password",
        "broker_token",
        "github_token",
        "main_api_key",
        "jev_api_key",
    }
    assert not services["postgres"].get("ports")
    assert not services["executor-broker"].get("ports")
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert not services["egress-proxy"].get("secrets")
    assert {secret["source"] for secret in services["migrate"]["secrets"]} == {"postgres_password"}
    assert Path("ops/control/Dockerfile").is_file()


def test_owner_export_captures_output_and_refuses_existing_file(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="dca_" + "x" * 64 + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("ops.control.prepare_secrets._protect_file", lambda _path: None)
    target = tmp_path / "owner.token"
    assert export_owner(target) == target
    assert capsys.readouterr().out == ""
    assert calls[0][1]["capture_output"] is True
    assert "dca_" not in " ".join(calls[0][0])
    with pytest.raises(FileExistsError):
        export_owner(target)
    assert len(calls) == 1

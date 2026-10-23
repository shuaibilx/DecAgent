import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from decagent.business.mcp_contract import (
    COMPOSE_INTERNAL_MCP_ENDPOINT as APP_COMPOSE_INTERNAL_MCP_ENDPOINT,
)
from ops.deploy.sop_contract import (
    COMPOSE_INTERNAL_MCP_ENDPOINT as DEPLOY_COMPOSE_INTERNAL_MCP_ENDPOINT,
)
from ops.deploy.sop_preflight import validate_compose_config


def _service(
    *, environment=None, command=None, ports=None, networks=None, volumes=None, healthcheck=None
):
    return {
        "environment": environment or {},
        "command": command or [],
        "ports": ports or [],
        "networks": networks or {"sop-internal": None},
        "volumes": volumes or [],
        "healthcheck": healthcheck or {},
    }


@pytest.fixture
def valid_config():
    return {
        "name": "decagent-sop",
        "services": {
            "postgres": {**_service(), "image": "postgres:17-alpine@sha256:" + "a" * 64},
            "migrate": _service(command=["migrate"]),
            "bootstrap": _service(command=["sop-bootstrap"]),
            "api": _service(
                environment={
                    "DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED": "true",
                    "DECAGENT_TICKETS_MCP_URL": "http://business-mcp:8001/mcp",
                    "DECAGENT_GUEST_TRIAL_ENABLED": "false",
                },
                ports=[{"target": 8000, "published": "8000", "host_ip": "127.0.0.1"}],
                networks={"sop-internal": None, "sop-egress": None},
                healthcheck={
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3)",
                    ]
                },
            ),
            "worker": _service(
                command=["worker", "--kind", "business"],
                environment={
                    "DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED": "true",
                    "DECAGENT_TICKETS_MCP_URL": "http://business-mcp:8001/mcp",
                },
                networks={"sop-internal": None, "sop-egress": None},
                healthcheck={
                    "test": [
                        "CMD",
                        "python",
                        "/opt/decagent/start.py",
                        "readiness",
                        "--role",
                        "worker",
                    ]
                },
            ),
            "business-mcp": _service(
                command=[
                    "business-server",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8001",
                    "--resource-url",
                    "http://business-mcp:8001/mcp",
                ]
            ),
        },
        "networks": {
            "sop-internal": {"internal": True},
            "sop-egress": {"internal": False},
        },
    }


def test_sop_topology_is_valid_and_does_not_publish_internal_services(valid_config):
    validate_compose_config(valid_config)


def test_private_preflight_copy_does_not_require_application_package(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    deploy_package = tmp_path / "ops" / "deploy"
    deploy_package.mkdir(parents=True)
    for name in (
        "release_verifier.py",
        "sop_business_probe.py",
        "sop_contract.py",
        "sop_preflight.py",
        "sop_rollout.py",
    ):
        (deploy_package / name).write_bytes((project_root / "ops" / "deploy" / name).read_bytes())

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from ops.deploy import release_verifier, sop_business_probe, sop_preflight, "
            "sop_rollout; "
            "assert all((release_verifier, sop_business_probe, sop_preflight, sop_rollout))",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_private_deployment_compose_matches_sop_topology_without_build_context():
    project_root = Path(__file__).resolve().parents[2]
    public_config = yaml.safe_load((project_root / "compose.sop.yaml").read_text(encoding="utf-8"))
    private_path = project_root / "ops" / "deploy" / "compose.sop.private.yaml"
    assert private_path.is_file(), (
        "private deployment needs a self-contained image-only Compose file"
    )
    private_config = yaml.safe_load(private_path.read_text(encoding="utf-8"))

    expected_private_config = deepcopy(public_config)
    expected_private_config["x-sop-control"].pop("build")
    for service in expected_private_config["services"].values():
        service.pop("build", None)

    assert private_config == expected_private_config
    assert all("build" not in service for service in private_config["services"].values())


def test_deployment_and_application_mcp_endpoint_contracts_match():
    assert DEPLOY_COMPOSE_INTERNAL_MCP_ENDPOINT == APP_COMPOSE_INTERNAL_MCP_ENDPOINT


@pytest.mark.parametrize("env_file_kind", ["empty", "example"])
def test_sop_compose_uses_available_loopback_port_by_default(tmp_path, env_file_kind):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI is unavailable")

    project_root = Path(__file__).resolve().parents[2]
    if env_file_kind == "empty":
        env_file = tmp_path / "empty.env"
        env_file.write_text("", encoding="utf-8")
    else:
        env_file = project_root / "ops" / "deploy" / "sop.env.example"

    result = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(project_root),
            "--env-file",
            str(env_file),
            "--file",
            str(project_root / "compose.sop.yaml"),
            "config",
            "--format",
            "json",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr

    config = json.loads(result.stdout)
    summary = validate_compose_config(config)
    assert summary["api_binding"] == "127.0.0.1:8001 -> 8000"
    assert config["services"]["api"]["environment"]["DECAGENT_PUBLIC_URL"] == (
        "http://127.0.0.1:8001"
    )


def test_preflight_rejects_public_api_binding(valid_config):
    valid_config["services"]["api"]["ports"][0]["host_ip"] = "0.0.0.0"

    with pytest.raises(ValueError, match="loopback"):
        validate_compose_config(valid_config)


@pytest.mark.parametrize("service_name", ["postgres", "worker", "business-mcp"])
def test_preflight_rejects_published_internal_services(valid_config, service_name):
    valid_config["services"][service_name]["ports"] = [
        {
            "target": 5432 if service_name == "postgres" else 8001,
            "published": "5432",
            "host_ip": "127.0.0.1",
        }
    ]

    with pytest.raises(ValueError, match="must not publish"):
        validate_compose_config(valid_config)


@pytest.mark.parametrize("service_name", ["api", "worker"])
def test_preflight_requires_conditional_workflows_and_same_mcp_resource(valid_config, service_name):
    config = deepcopy(valid_config)
    config["services"][service_name]["environment"][
        "DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED"
    ] = "false"

    with pytest.raises(ValueError, match="conditional workflows"):
        validate_compose_config(config)


def test_preflight_rejects_mcp_resource_identity_mismatch(valid_config):
    valid_config["services"]["worker"]["environment"]["DECAGENT_TICKETS_MCP_URL"] = (
        "http://other-mcp:8001/mcp"
    )

    with pytest.raises(ValueError, match="MCP resource identity"):
        validate_compose_config(valid_config)


def test_preflight_rejects_repository_repair_worker_and_docker_socket(valid_config):
    valid_config["services"]["worker"]["command"] = ["worker", "--kind", "repository_repair"]

    with pytest.raises(ValueError, match="business worker"):
        validate_compose_config(valid_config)

    valid_config["services"]["worker"]["command"] = ["worker", "--kind", "business"]
    valid_config["services"]["worker"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]

    with pytest.raises(ValueError, match="Docker socket"):
        validate_compose_config(valid_config)


def test_preflight_rejects_unreviewed_repair_service(valid_config):
    valid_config["services"]["repository-repair"] = _service(
        command=["worker", "--kind", "repository_repair"]
    )

    with pytest.raises(ValueError, match="service set"):
        validate_compose_config(valid_config)


def test_preflight_requires_business_readiness_checks_for_api_and_worker(valid_config):
    valid_config["services"]["api"]["healthcheck"] = {
        "test": ["CMD", "python", "-c", "health only"]
    }

    with pytest.raises(ValueError, match="API business readiness"):
        validate_compose_config(valid_config)

    valid_config["services"]["api"]["healthcheck"]["test"] = [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=3)",
    ]
    valid_config["services"]["worker"]["healthcheck"] = {"test": ["CMD", "true"]}

    with pytest.raises(ValueError, match="business worker readiness"):
        validate_compose_config(valid_config)

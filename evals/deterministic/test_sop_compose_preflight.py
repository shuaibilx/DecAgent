from copy import deepcopy

import pytest

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

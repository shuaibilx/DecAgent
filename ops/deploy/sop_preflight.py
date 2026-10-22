"""Safely preflight the isolated local SOP Compose topology."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REQUIRED_SERVICES = {"postgres", "migrate", "bootstrap", "business-mcp", "api", "worker"}
_MCP_RESOURCE_URL = "http://business-mcp:8001/mcp"
_PRIVATE_SERVICES = {"postgres", "migrate", "bootstrap", "business-mcp"}


def _environment(service):
    value = service.get("environment") or {}
    if not isinstance(value, dict):
        raise ValueError("SOP service environment must be a mapping")
    return value


def _network_names(service):
    networks = service.get("networks") or {}
    if isinstance(networks, list):
        return set(networks)
    if isinstance(networks, dict):
        return set(networks)
    raise ValueError("SOP service networks must be a list or mapping")


def _command(service):
    command = service.get("command") or []
    if isinstance(command, str):
        return command.split()
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return command
    raise ValueError("SOP service command must be an argument list")


def _mounts_docker_socket(service):
    for mount in service.get("volumes") or []:
        if isinstance(mount, str):
            value = mount
        elif isinstance(mount, dict):
            value = " ".join(str(mount.get(key, "")) for key in ("source", "target"))
        else:
            continue
        if "docker.sock" in value.casefold():
            return True
    return False


def validate_compose_config(config):
    """Raise ``ValueError`` unless the rendered compose file stays SOP-isolated."""
    if not isinstance(config, dict):
        raise ValueError("Rendered SOP Compose configuration must be a mapping")
    project_name = config.get("name")
    if not isinstance(project_name, str) or not project_name.startswith("decagent-sop"):
        raise ValueError("SOP Compose project name must use the decagent-sop prefix")
    services = config.get("services")
    if not isinstance(services, dict) or set(services) != _REQUIRED_SERVICES:
        raise ValueError("SOP Compose service set must match the reviewed business topology")

    networks = config.get("networks") or {}
    if not isinstance(networks, dict):
        raise ValueError("SOP Compose networks must be a mapping")
    internal_network = networks.get("sop-internal") or {}
    egress_network = networks.get("sop-egress") or {}
    if internal_network.get("internal") is not True:
        raise ValueError("SOP internal network must not provide external routing")
    if egress_network.get("internal") is True:
        raise ValueError("SOP model egress network must support outbound connections")

    for name, service in services.items():
        if service.get("privileged") is True or _mounts_docker_socket(service):
            raise ValueError("SOP services must not have privileged access or a Docker socket")
        service_networks = _network_names(service)
        if name in _PRIVATE_SERVICES and service_networks != {"sop-internal"}:
            raise ValueError("SOP internal services must stay on the private network")
        if name in {"api", "worker"} and service_networks != {
            "sop-internal",
            "sop-egress",
        }:
            raise ValueError("SOP API and worker need private and outbound networks")
        if name != "api" and service.get("ports"):
            raise ValueError(f"SOP service {name} must not publish host ports")

    api_ports = services["api"].get("ports") or []
    if len(api_ports) != 1:
        raise ValueError("SOP API must expose exactly one loopback port")
    api_port = api_ports[0]
    if (
        not isinstance(api_port, dict)
        or api_port.get("host_ip") != "127.0.0.1"
        or api_port.get("target") != 8000
    ):
        raise ValueError("SOP API must bind only to loopback on container port 8000")

    database_image = services["postgres"].get("image")
    if not isinstance(database_image, str) or "@sha256:" not in database_image:
        raise ValueError("SOP PostgreSQL image must be pinned by digest")
    if _command(services["worker"]) != ["worker", "--kind", "business"]:
        raise ValueError("SOP must run the business worker, not repository repair")
    if _command(services["migrate"]) != ["migrate"] or _command(services["bootstrap"]) != [
        "sop-bootstrap"
    ]:
        raise ValueError("SOP migration and business bootstrap commands must be explicit")
    expected_mcp_command = [
        "business-server",
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
        "--resource-url",
        _MCP_RESOURCE_URL,
    ]
    if _command(services["business-mcp"]) != expected_mcp_command:
        raise ValueError("SOP MCP must bind internally with the reviewed resource identity")

    for service_name in ("api", "worker"):
        environment = _environment(services[service_name])
        if environment.get("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED") != "true":
            raise ValueError("SOP API and worker must enable conditional workflows")
        if environment.get("DECAGENT_TICKETS_MCP_URL") != _MCP_RESOURCE_URL:
            raise ValueError("SOP MCP resource identity must match API and worker configuration")
    if _environment(services["api"]).get("DECAGENT_GUEST_TRIAL_ENABLED") != "false":
        raise ValueError("Guest trial entry must remain disabled until its release gate passes")

    return {
        "status": "passed",
        "project": project_name,
        "services": sorted(_REQUIRED_SERVICES),
        "api_binding": f"127.0.0.1:{api_port.get('published', 'configured')} -> 8000",
        "mcp_resource": _MCP_RESOURCE_URL,
        "conditional_workflows": True,
        "guest_trial": False,
        "docker_socket": False,
    }


def main(argv=None):
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", default="compose.sop.yaml")
    parser.add_argument("--env-file", default="ops/deploy/sop.env.example")
    args = parser.parse_args(argv)
    compose_file = (root / args.compose_file).resolve()
    env_file = (root / args.env_file).resolve()
    if not compose_file.is_file() or not env_file.is_file():
        raise SystemExit("SOP Compose file or isolated environment example is missing")
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(root),
                "--env-file",
                str(env_file),
                "--file",
                str(compose_file),
                "config",
                "--format",
                "json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Docker Compose v2 is required for SOP preflight") from exc
    if result.returncode != 0:
        raise SystemExit(
            f"Docker Compose rejected the SOP configuration (exit {result.returncode})"
        )
    try:
        config = json.loads(result.stdout)
        summary = validate_compose_config(config)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"SOP Compose preflight failed: {exc}") from None
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

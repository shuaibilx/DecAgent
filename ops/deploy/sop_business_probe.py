"""Business readiness checks for the isolated SOP Compose deployment."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from ops.deploy.sop_contract import SOP_REQUIRED_SERVICES


def _running_healthy(services, name):
    service = services.get(name)
    if not isinstance(service, dict):
        return False
    state = service.get("State")
    health = state.get("Health") if isinstance(state, dict) else None
    return (
        isinstance(state, dict)
        and state.get("Status") == "running"
        and isinstance(health, dict)
        and health.get("Status") == "healthy"
    )


def assess_business_readiness(services, api_liveness, api_readiness):
    """Return a redacted summary; API liveness alone never marks the stack ready."""
    services = services if isinstance(services, dict) else {}
    expected_worker = ["worker", "--kind", "business"]
    worker = services.get("worker")
    worker_command = (
        (worker or {}).get("Config", {}).get("Cmd") if isinstance(worker, dict) else None
    )
    components = {
        "api_liveness": isinstance(api_liveness, dict)
        and api_liveness.get("status") == "ok",
        "api_readiness": isinstance(api_readiness, dict)
        and api_readiness.get("status") == "ready",
        "postgres": _running_healthy(services, "postgres"),
        "mcp": _running_healthy(services, "business-mcp"),
        "api_container": _running_healthy(services, "api"),
        "business_worker": _running_healthy(services, "worker")
        and worker_command == expected_worker,
        "migrations": _completed_successfully(services, "migrate"),
        "bootstrap": _completed_successfully(services, "bootstrap"),
    }
    # A missing or unexpected service is a topology failure, even if the named
    # components happen to be healthy.
    topology_matches = set(services) == SOP_REQUIRED_SERVICES
    return {
        "status": "ready" if topology_matches and all(components.values()) else "not_ready",
        "components": components,
    }


def _completed_successfully(services, name):
    service = services.get(name)
    state = service.get("State") if isinstance(service, dict) else None
    return (
        isinstance(state, dict)
        and state.get("Status") == "exited"
        and state.get("ExitCode") == 0
    )


def _run(command, *, cwd, env):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("SOP business probe could not inspect the Compose project")
    return result.stdout.strip()


def _http_json(url):
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def probe_running_sop(root, compose_file, env_file, *, env=None):
    """Inspect a Compose stack and probe loopback API liveness/readiness."""
    root = Path(root).resolve()
    compose_file = Path(compose_file).resolve()
    env_file = Path(env_file).resolve()
    environment = env or {}
    prefix = [
        "docker",
        "compose",
        "--project-directory",
        str(root),
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
    ]
    containers = {}
    for name in sorted(SOP_REQUIRED_SERVICES):
        container_id = _run([*prefix, "ps", "--all", "-q", name], cwd=root, env=environment)
        if not container_id:
            continue
        inspected = json.loads(_run(["docker", "inspect", container_id], cwd=root, env=environment))
        if len(inspected) != 1:
            continue
        containers[name] = inspected[0]

    api = containers.get("api")
    port_bindings = (api or {}).get("HostConfig", {}).get("PortBindings", {})
    bindings = port_bindings.get("8000/tcp") or []
    if len(bindings) == 1 and bindings[0].get("HostIp") == "127.0.0.1":
        base_url = "http://127.0.0.1:" + str(bindings[0].get("HostPort", ""))
        try:
            liveness = _http_json(base_url + "/health")
        except (OSError, URLError, TimeoutError, ValueError):
            liveness = {"status": "unavailable"}
        try:
            readiness = _http_json(base_url + "/readyz")
        except (OSError, URLError, TimeoutError, ValueError):
            readiness = {"status": "unavailable"}
    else:
        liveness = {"status": "unavailable"}
        readiness = {"status": "unavailable"}

    return assess_business_readiness(containers, liveness, readiness)


def main(argv=None):
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", default="compose.sop.yaml")
    parser.add_argument("--env-file", default="ops/deploy/sop.env.example")
    args = parser.parse_args(argv)
    import os

    try:
        report = probe_running_sop(
            root, root / args.compose_file, root / args.env_file, env=os.environ.copy()
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        report = {"status": "not_ready", "components": {}}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())

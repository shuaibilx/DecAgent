"""Boot a unique SOP Compose stack with empty model keys and remove it afterward."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4


def _run(command, *, cwd, env, capture=False, check=True):
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Disposable SOP smoke command failed with exit {result.returncode}")
    return result


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _container_info(container_id, *, cwd, env):
    output = _run(
        ["docker", "inspect", container_id], cwd=cwd, env=env, capture=True
    ).stdout
    return json.loads(output)[0]


def _diagnostic_payload(output):
    try:
        payload = json.loads(output.strip())
    except (json.JSONDecodeError, AttributeError):
        payload = None
    return payload if isinstance(payload, dict) else {}


def _safe_health_diagnostic(container):
    state = container.get("State", {})
    health = state.get("Health") or {}
    output = (health.get("Log") or [{}])[-1].get("Output", "")
    payload = _diagnostic_payload(output)
    components = payload.get("components")
    return {
        "container_state": state.get("Status"),
        "health": health.get("Status"),
        "probe_status": payload.get("status"),
        "components": components if isinstance(components, dict) else {},
    }


def _safe_probe_output(output):
    payload = _diagnostic_payload(output)
    components = payload.get("components")
    if not isinstance(components, dict):
        components = {}
    return {
        "probe_status": payload.get("status")
        if payload.get("status") in {"ready", "not_ready"}
        else None,
        "components": {
            name: value
            for name, value in components.items()
            if isinstance(name, str) and type(value) is bool
        },
    }


def _safe_command_diagnostic(exit_code, stderr):
    exception_type = "CommandFailed"
    for line in reversed((stderr or "").splitlines()):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))(?::|$)", line.strip())
        if match:
            exception_type = match.group(1)
            break
    return {"exit_code": exit_code, "error_type": exception_type}


def _safe_probe_report(report):
    return _safe_probe_output(json.dumps(report, ensure_ascii=False))


def _wait_for_readiness(
    probe,
    *,
    timeout_seconds,
    poll_interval_seconds=2,
    monotonic=time.monotonic,
    sleep=time.sleep,
):
    deadline = monotonic() + timeout_seconds
    latest = {"probe_status": None, "components": {}}
    while True:
        try:
            latest = _safe_probe_report(probe())
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            latest = {"probe_status": "not_ready", "components": {}}
        if latest["probe_status"] == "ready":
            return {"status": "ready", "components": latest["components"]}
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {
                "status": latest["probe_status"] or "not_ready",
                "components": latest["components"],
            }
        sleep(min(poll_interval_seconds, remaining))


def _start_stack(compose):
    """Wait for configured Compose health checks before the first host probe."""
    compose("up", "--build", "--detach", "--wait", "--wait-timeout", "180")


def main():
    root = Path(__file__).resolve().parents[2]
    instance = "decagent-sop-smoke-" + uuid4().hex[:12]
    image_tag = "decagent-sop:offline-" + instance.removeprefix("decagent-sop-smoke-")
    api_port = _port()
    inherited = {key: value for key, value in os.environ.items() if not key.startswith("DECAGENT_")}

    with tempfile.TemporaryDirectory(prefix=instance + "-") as directory:
        target = Path(directory).resolve()
        secret_dir = target / "secrets"
        secret_dir.mkdir()
        for filename in ("postgres_password", "main_api_key", "jev_api_key"):
            secret = secret_dir / filename
            secret.write_text(
                secrets.token_urlsafe(48) + "\n" if filename == "postgres_password" else "\n",
                encoding="utf-8",
            )
        env_file = target / "sop.env"
        env_file.write_text(
            "\n".join(
                (
                    f"DECAGENT_SOP_COMPOSE_PROJECT={instance}",
                    f"DECAGENT_SOP_IMAGE={image_tag}",
                    f"DECAGENT_SOP_API_PORT={api_port}",
                    f"DECAGENT_SOP_PUBLIC_URL=http://127.0.0.1:{api_port}",
                    f"DECAGENT_SOP_SECRETS_DIR={secret_dir.as_posix()}",
                    "DECAGENT_MAIN_API_KEY=",
                    "DECAGENT_JEV_API_KEY=",
                    "DECAGENT_GUEST_TRIAL_ENABLED=false",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        env = {**inherited}
        prefix = [
            "docker",
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "--file",
            str(root / "compose.sop.yaml"),
        ]

        def compose(*args, capture=False, check=True):
            return _run(
                [*prefix, *args], cwd=root, env=env, capture=capture, check=check
            )

        existing = _run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=com.docker.compose.project=" + instance,
            ],
            cwd=root,
            env=env,
            capture=True,
        )
        if existing.stdout.strip():
            raise ValueError("Disposable SOP Compose project already exists")

        business_probe = {"status": "not_ready", "components": {}}
        try:
            _run(
                [
                    sys.executable,
                    "-m",
                    "ops.deploy.sop_preflight",
                    "--compose-file",
                    str(root / "compose.sop.yaml"),
                    "--env-file",
                    str(env_file),
                ],
                cwd=root,
                env=env,
                capture=True,
            )
            compose("config", "--quiet")
            _start_stack(compose)
            services = {}
            for service_name in (
                "postgres",
                "migrate",
                "bootstrap",
                "business-mcp",
                "api",
                "worker",
            ):
                container_id = compose(
                    "ps", "--all", "-q", service_name, capture=True
                ).stdout.strip()
                if not container_id:
                    raise RuntimeError("SOP smoke service did not create a container")
                services[service_name] = _container_info(container_id, cwd=root, env=env)

            api_payload = json.loads(
                urlopen(f"http://127.0.0.1:{api_port}/health", timeout=5).read().decode("utf-8")
            )
            if api_payload.get("status") != "ok":
                raise RuntimeError("SOP API health endpoint did not report healthy")
            if services["business-mcp"]["State"].get("Health", {}).get("Status") != "healthy":
                raise RuntimeError("SOP business MCP readiness health check failed")
            from ops.deploy.sop_business_probe import probe_running_sop

            business_probe = _wait_for_readiness(
                lambda: probe_running_sop(
                    root,
                    root / "compose.sop.yaml",
                    env_file,
                    env=env,
                ),
                timeout_seconds=240,
            )
            if business_probe["status"] != "ready":
                raise RuntimeError("SOP business readiness probe did not pass")

            # Prove that a healthy API process cannot hide an unavailable worker.
            compose("stop", "worker")
            degraded_probe = probe_running_sop(
                root,
                root / "compose.sop.yaml",
                env_file,
                env=env,
            )
            if (
                degraded_probe["components"].get("api_liveness") is not True
                or degraded_probe["components"].get("business_worker") is not False
                or degraded_probe["status"] != "not_ready"
            ):
                raise RuntimeError("SOP business probe failed to detect a stopped worker")
            compose("start", "worker")
            recovered_probe = _wait_for_readiness(
                lambda: probe_running_sop(
                    root,
                    root / "compose.sop.yaml",
                    env_file,
                    env=env,
                ),
                timeout_seconds=180,
            )
            if recovered_probe["status"] != "ready":
                raise RuntimeError("SOP business readiness did not recover after worker restart")

            expected_api_bindings = {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(api_port)}]
            }
            if services["api"]["HostConfig"].get("PortBindings") != expected_api_bindings:
                raise RuntimeError("SOP API host binding is not loopback-only")
            for name in ("postgres", "business-mcp", "worker"):
                if services[name]["HostConfig"].get("PortBindings"):
                    raise RuntimeError("SOP database, MCP, or worker published a host port")
            for name in ("api", "worker"):
                container_env = dict(
                    entry.split("=", 1) for entry in services[name]["Config"]["Env"] if "=" in entry
                )
                if container_env.get("DECAGENT_CONDITIONAL_WORKFLOWS_ENABLED") != "true":
                    raise RuntimeError("SOP conditional workflows are not enabled in the container")
                if container_env.get("DECAGENT_TICKETS_MCP_URL") != "http://business-mcp:8001/mcp":
                    raise RuntimeError("SOP container has a mismatched MCP resource identity")
                if container_env.get("DECAGENT_GUEST_TRIAL_ENABLED", "false") != "false":
                    raise RuntimeError("SOP guest trial unexpectedly enabled in this smoke")
            if services["worker"]["Config"].get("Cmd") != ["worker", "--kind", "business"]:
                raise RuntimeError("SOP container is not running the business worker")

            for name, container in services.items():
                if any(
                    "/var/run/docker.sock" in mount.get("Destination", "")
                    for mount in container.get("Mounts", [])
                ):
                    raise RuntimeError("SOP service received the Docker socket")
                network_names = set(container["NetworkSettings"]["Networks"])
                if name in {"postgres", "migrate", "bootstrap", "business-mcp"} and not all(
                    network.endswith("_sop-internal") for network in network_names
                ):
                    raise RuntimeError("SOP internal service joined an unexpected network")

            print(
                json.dumps(
                    {
                        "project": instance,
                        "status": "passed",
                        "services": sorted(services),
                        "api": "healthy on loopback",
                        "business_readiness": "ready for database, MCP, API and business worker",
                        "business_mcp": "ready with internal resource identity",
                        "worker": "business kind running",
                        "worker_fault_injection": (
                            "healthy API liveness; worker outage detected and recovered"
                        ),
                        "conditional_workflows": True,
                        "guest_trial": False,
                        "model_requests": 0,
                        "docker_socket": False,
                    },
                    sort_keys=True,
                )
            )
        except Exception as exc:
            diagnostics = {"business_readiness": business_probe}
            for service_name in ("api", "business-mcp", "worker"):
                container_id = compose(
                    "ps", "--all", "-q", service_name, capture=True, check=False
                ).stdout.strip()
                if container_id:
                    diagnostics[service_name] = _safe_health_diagnostic(
                        _container_info(container_id, cwd=root, env=env)
                    )
            worker_probe = compose(
                "exec",
                "-T",
                "worker",
                "python",
                "/opt/decagent/start.py",
                "readiness",
                "--role",
                "worker",
                capture=True,
                check=False,
            )
            diagnostics["worker_readiness_probe"] = _safe_probe_output(worker_probe.stdout)
            diagnostics["worker_readiness_command"] = _safe_command_diagnostic(
                worker_probe.returncode,
                worker_probe.stderr,
            )
            raise RuntimeError(
                "Disposable SOP business smoke failed: "
                + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
            ) from exc
        finally:
            cleanup = compose("down", "--volumes", "--remove-orphans", capture=True, check=False)
            if cleanup.returncode != 0:
                raise RuntimeError("Disposable SOP Compose cleanup did not complete")
            project_label = "label=com.docker.compose.project=" + instance
            artifacts = (
                ("containers", ["docker", "ps", "-aq", "--filter", project_label]),
                ("volumes", ["docker", "volume", "ls", "-q", "--filter", project_label]),
                ("networks", ["docker", "network", "ls", "-q", "--filter", project_label]),
            )
            for name, command in artifacts:
                remaining = _run(
                    command, cwd=root, env=env, capture=True
                )
                if remaining.stdout.strip():
                    raise RuntimeError(f"Disposable SOP Compose {name} remain after cleanup")

            image = _run(
                ["docker", "image", "ls", "-q", image_tag],
                cwd=root,
                env=env,
                capture=True,
            )
            if image.stdout.strip():
                removal = _run(
                    ["docker", "image", "rm", image_tag],
                    cwd=root,
                    env=env,
                    capture=True,
                    check=False,
                )
                if removal.returncode != 0:
                    raise RuntimeError("Disposable SOP smoke image removal failed")
                remaining_image = _run(
                    ["docker", "image", "ls", "-q", image_tag],
                    cwd=root,
                    env=env,
                    capture=True,
                )
                if remaining_image.stdout.strip():
                    raise RuntimeError("Disposable SOP smoke image remains after cleanup")


if __name__ == "__main__":
    main()

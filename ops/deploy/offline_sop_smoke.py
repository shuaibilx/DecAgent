"""Boot a unique SOP Compose stack with empty model keys and remove it afterward."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
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
            compose("up", "--build", "--detach", "--wait", "--wait-timeout", "240")
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
                        "business_mcp": "ready with internal resource identity",
                        "worker": "business kind running",
                        "conditional_workflows": True,
                        "guest_trial": False,
                        "model_requests": 0,
                        "docker_socket": False,
                    },
                    sort_keys=True,
                )
            )
        except Exception:
            raise
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

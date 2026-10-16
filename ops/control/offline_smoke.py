"""Boot a unique Compose instance with empty provider secrets; never call a paid API."""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from decagent.gateway.auth import _protect_file


def main():
    root = Path(__file__).resolve().parents[2]
    instance = "decagent-offline-" + uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=instance + "-") as directory:
        target = Path(directory).resolve()
        if not target.name.startswith(instance + "-"):
            raise ValueError("Unexpected temporary workspace")
        for filename in (
            "postgres_password",
            "broker_token",
            "github_token",
            "main_api_key",
            "jev_api_key",
        ):
            path = target / filename
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                _protect_file(path)
                value = (
                    secrets.token_urlsafe(48)
                    if filename in {"postgres_password", "broker_token"}
                    else ""
                )
                stream.write(value + "\n")
        envfile = target / "empty.env"
        envfile.write_text("", encoding="utf-8")
        runner = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "decagent-executor-runner:supervised",
                "--format",
                "{{.Id}}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        env = {
            **os.environ,
            "DECAGENT_COMPOSE_PROJECT": instance,
            "DECAGENT_INSTANCE_PREFIX": instance,
            "DECAGENT_SECRETS_DIR": str(target),
            "DECAGENT_RUNNER_IMAGE": runner,
            "DECAGENT_CONTROL_IMAGE": "decagent-control:offline-smoke",
            "DECAGENT_EXECUTOR_IMAGE": "decagent-executor:offline-smoke",
            "DECAGENT_API_PORT": "18020",
            "DECAGENT_PUBLIC_URL": "http://127.0.0.1:18020",
            "DECAGENT_DECISION_STRATEGY": "rule",
        }
        prefix = [
            "docker",
            "compose",
            "--env-file",
            str(envfile),
            "--file",
            str(root / "compose.yaml"),
        ]

        def compose(*args, capture=False):
            return subprocess.run(
                [*prefix, *args],
                cwd=root,
                env=env,
                check=True,
                capture_output=capture,
                text=True,
                encoding="utf-8",
            )

        existing = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + instance],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        if existing.stdout.strip():
            raise ValueError("Temporary Compose project already exists")
        try:
            compose("config", "--quiet")
            compose("build", "api", "executor-broker", "egress-proxy")
            compose("up", "--detach", "--wait", "--wait-timeout", "180")
            compose("--profile", "setup", "run", "--rm", "owner-init")
            doctor = compose(
                "exec", "-T", "api", "python", "/opt/decagent/start.py", "doctor", capture=True
            )
            status = json.loads(doctor.stdout)
            if (
                status["paid_requests"] != 0
                or status["main_model_configured"]
                or status["jev_configured"]
            ):
                raise ValueError("Offline smoke unexpectedly has provider credentials")
            task_list = compose(
                "exec",
                "-T",
                "--env",
                "DECAGENT_PUBLIC_URL=http://127.0.0.1:8000",
                "api",
                "python",
                "/opt/decagent/start.py",
                "tasks",
                capture=True,
            )
            if json.loads(task_list.stdout) != []:
                raise ValueError("Offline smoke did not create a fresh database")
            for role in ("api", "worker"):
                name = instance + "-" + role
                info = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=True,
                )
                container = json.loads(info.stdout)[0]
                if any(
                    mount["Destination"] == "/var/run/docker.sock" for mount in container["Mounts"]
                ):
                    raise ValueError("Control container has a Docker socket")
                if not container["HostConfig"]["ReadonlyRootfs"]:
                    raise ValueError("Control container root filesystem is writable")
            print(
                json.dumps(
                    {
                        "instance": instance,
                        "api": "healthy",
                        "worker": "running",
                        "owner_init": "verified",
                        "authenticated_cli": "verified",
                        "docker_socket_in_api_or_worker": False,
                        "paid_requests": 0,
                    }
                )
            )
        except Exception:
            compose("logs", "--tail", "50")
            raise
        finally:
            # Only this unique, freshly created fixture's services and volumes are removed.
            compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()

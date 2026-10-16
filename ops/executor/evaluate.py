"""Fresh Compose primitive boot. Only resources created by this invocation are removed.

Run from the DecAgent repository: python ops/executor/evaluate.py
This evaluates infrastructure; it does not run the graph/API/worker product flow.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

PROJECT = "decagent-executor-compose"


def command(*args, env=None, capture=False):
    return subprocess.run(
        ["docker", *args],
        check=True,
        env=env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def evaluate():
    root = Path(__file__).resolve().parents[2]
    if Path.cwd().resolve() != root:
        raise ValueError("Run the evaluator from the DecAgent repository root")
    existing = command(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}", capture=True
    ).strip()
    volumes = command(
        "volume",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        capture=True,
    ).strip()
    if existing or volumes:
        raise ValueError("Evaluator resources already exist; inspect them before choosing cleanup")
    command("build", "-f", "ops/runner/Dockerfile", "-t", "decagent-executor-runner:test", ".")
    image = command(
        "image", "inspect", "decagent-executor-runner:test", "--format", "{{.Id}}", capture=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="decagent-executor-secrets-") as temporary:
        secret_directory = Path(temporary)
        for name in ("broker_token", "postgres_password"):
            (secret_directory / name).write_text(secrets.token_urlsafe(32), encoding="ascii")
        for name in ("github_token", "main_api_key", "jev_api_key"):
            (secret_directory / name).write_text("", encoding="ascii")
        environment = os.environ | {
            "DECAGENT_EXECUTOR_TEST_SECRETS_DIR": temporary,
            "DECAGENT_SECRETS_DIR": temporary,
            "DECAGENT_RUNNER_IMAGE": image,
        }
        compose = [
            "compose",
            "-p",
            PROJECT,
            "-f",
            "compose.yaml",
            "-f",
            "ops/executor/compose.test.yaml",
        ]
        try:
            command(
                *compose,
                "up",
                "--build",
                "-d",
                "postgres",
                "migrate",
                "egress-proxy",
                "executor-broker",
                env=environment,
            )
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                inspected = json.loads(
                    command("inspect", "decagent-executor-compose-broker", capture=True)
                )[0]
                if inspected["State"].get("Health", {}).get("Status") == "healthy":
                    probe = (
                        "import json,pathlib,urllib.request; "
                        "token=pathlib.Path('/run/secrets/broker_token').read_text().strip(); "
                        "r=urllib.request.Request('http://127.0.0.1:8081/health',"
                        "headers={'Authorization':'Bearer '+token}); "
                        "print(urllib.request.urlopen(r,timeout=5).read().decode())"
                    )
                    result = json.loads(
                        command(
                            "exec",
                            "decagent-executor-compose-broker",
                            "python",
                            "-c",
                            probe,
                            capture=True,
                        )
                    )
                    assert result["ready"] and result["diagnostics"]["boundary"]["passed"]
                    print(
                        json.dumps(
                            {
                                "compose_primitive_boot": "passed",
                                "runner_image": image,
                                "diagnostics": result["diagnostics"],
                            },
                            indent=2,
                        )
                    )
                    return result
                time.sleep(1)
            command(*compose, "logs", "--tail", "30", "executor-broker", env=environment)
            raise TimeoutError("Compose broker did not pass startup boundary checks")
        finally:
            # Project absence was checked before creation; remove only this new test stack.
            command(*compose, "down", "--volumes", env=environment)
            # A startup interruption can leave a disposable probe's independent network/volumes.
            label = "label=decagent.executor.instance=decagent-executor-compose-proxy"
            for _kind, query, remove in [
                ("container", ["ps", "-aq"], ["rm", "-f"]),
                ("network", ["network", "ls", "-q"], ["network", "rm"]),
                ("volume", ["volume", "ls", "-q"], ["volume", "rm"]),
            ]:
                resources = command(*query, "--filter", label, capture=True).splitlines()
                for resource in resources:
                    command(*remove, resource)


if __name__ == "__main__":
    evaluate()

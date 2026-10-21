"""Run real Docker executor/repair integration tests on a disposable GHA Linux runner."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def require_github_hosted_linux(env: dict[str, str]) -> None:
    if not (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("RUNNER_OS") == "Linux"
    ):
        raise RuntimeError("Docker boundary tests require a GitHub-hosted Linux runner")


def project_name(run_id: str, run_attempt: str) -> str:
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", run_attempt):
        raise ValueError("GitHub run identity must contain only decimal digits")
    return f"decagent-executor-ci-{run_id}-{run_attempt}"


def compose_command(project: str, *args: str) -> list[str]:
    if not re.fullmatch(r"decagent-executor-ci-[0-9]+-[0-9]+", project):
        raise ValueError("Unexpected Docker Compose project identity")
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        "compose.yaml",
        "--file",
        "ops/executor/compose.test.yaml",
        *args,
    ]


def run(
    command: list[str], *, env: dict[str, str], check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture,
    )


def assert_project_is_unused(project: str, instance_prefix: str, env: dict[str, str]) -> None:
    for command in (
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    ):
        result = run(command, env=env, capture=True)
        if result.stdout and result.stdout.strip():
            raise RuntimeError("The disposable Docker Compose project already has resources")

    for suffix in ("pg", "proxy", "broker"):
        result = run(
            ["docker", "inspect", f"{instance_prefix}-{suffix}"],
            env=env,
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            raise RuntimeError("A disposable executor test container name is already in use")


def wait_for_broker(project: str, broker_name: str, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", broker_name],
            env=env,
            check=False,
            capture=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "healthy":
            return
        time.sleep(1)
    raise TimeoutError(f"Disposable executor broker did not become healthy for {project}")


def execute() -> None:
    env = os.environ.copy()
    require_github_hosted_linux(env)
    if Path.cwd().resolve() != ROOT:
        raise RuntimeError("Run from the DecAgent repository root")

    project = project_name(env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", ""))
    instance_prefix = project
    assert_project_is_unused(project, instance_prefix, env)

    runner_tag = f"{project}-runner:ci"
    runner_build = ["docker", "build", "--file", "ops/runner/Dockerfile", "--tag", runner_tag, "."]
    run(runner_build, env=env)
    inspected = run(
        ["docker", "image", "inspect", runner_tag, "--format", "{{.Id}}"],
        env=env,
        capture=True,
    )
    runner_image = inspected.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", runner_image):
        raise RuntimeError("The isolated runner image did not produce an immutable image ID")

    with tempfile.TemporaryDirectory(prefix="decagent-executor-ci-secrets-") as temporary:
        secret_dir = Path(temporary)
        os.chmod(secret_dir, 0o700)
        password = secrets.token_hex(32)
        secret_values = {
            "broker_token": secrets.token_urlsafe(32),
            "postgres_password": password,
            "github_token": "",
            "main_api_key": "",
            "jev_api_key": "",
        }
        for name, value in secret_values.items():
            path = secret_dir / name
            path.write_text(value, encoding="ascii")
            os.chmod(path, 0o600)

        env.update(
            {
                "DECAGENT_INSTANCE_PREFIX": instance_prefix,
                "DECAGENT_EXECUTOR_TEST_SECRETS_DIR": temporary,
                "DECAGENT_SECRETS_DIR": temporary,
                "DECAGENT_RUNNER_IMAGE": runner_image,
                "DECAGENT_EXECUTOR_IMAGE": f"{project}-executor:ci",
            }
        )
        compose = compose_command(project)
        run([*compose, "config", "--quiet"], env=env)
        started = False
        try:
            started = True
            run(
                [
                    *compose,
                    "up",
                    "--build",
                    "--detach",
                    "postgres",
                    "migrate",
                    "egress-proxy",
                    "executor-broker",
                ],
                env=env,
            )
            broker_name = f"{instance_prefix}-broker"
            wait_for_broker(project, broker_name, env)

            run(
                [
                    *compose,
                    "exec",
                    "--no-TTY",
                    "postgres",
                    "psql",
                    "--username=decagent",
                    "--dbname=decagent",
                    "--command=CREATE DATABASE \"executor-test-only\"",
                ],
                env=env,
            )

            test_database_url = (
                f"postgresql://decagent:{password}@decagent-executor-pg:5432/executor-test-only"
            )
            test_env = {
                "DECAGENT_TEST_DATABASE_URL": test_database_url,
                "DECAGENT_EXECUTOR_INTEGRATION": "1",
                "DECAGENT_REPAIR_INTEGRATION": "1",
                "DECAGENT_RUNNER_IMAGE": runner_image,
                "DECAGENT_PROXY_CONTAINER": f"{instance_prefix}-proxy",
                "DECAGENT_EXECUTOR_TEST_PG_CONTAINER": f"{instance_prefix}-pg",
                "DECAGENT_REPAIR_TESTHOST": broker_name,
                "PYTHONPATH": "/workspace",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            exec_args = [*compose, "exec", "--no-TTY"]
            for key, value in test_env.items():
                exec_args.extend(["--env", f"{key}={value}"])
            run(
                [
                    *exec_args,
                    "executor-broker",
                    "python",
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "/workspace/evals/integration/test_executor_docker.py",
                    "/workspace/evals/integration/test_repair_docker.py",
                    "-q",
                    "-rs",
                    "--durations=10",
                ],
                env=env,
            )
        finally:
            if started:
                # The GitHub-hosted runner is ephemeral; leave its anonymous CI volume
                # for runner teardown and never issue a volume-deleting Compose command.
                run([*compose, "down"], env=env, check=False)


if __name__ == "__main__":
    execute()

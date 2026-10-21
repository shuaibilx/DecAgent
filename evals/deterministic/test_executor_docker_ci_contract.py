from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ops.executor import run_docker_boundary_tests as driver

ROOT = Path(__file__).parents[2]


def test_executor_docker_fixture_isolates_names_and_test_database_alias():
    compose = yaml.safe_load((ROOT / "ops/executor/compose.test.yaml").read_text())

    assert compose["services"]["postgres"]["container_name"] == (
        "${DECAGENT_INSTANCE_PREFIX:-decagent-executor-compose}-pg"
    )
    assert "decagent-executor-pg" in compose["services"]["postgres"]["networks"][
        "control"
    ]["aliases"]
    assert compose["services"]["egress-proxy"]["container_name"] == (
        "${DECAGENT_INSTANCE_PREFIX:-decagent-executor-compose}-proxy"
    )
    assert compose["services"]["executor-broker"]["container_name"] == (
        "${DECAGENT_INSTANCE_PREFIX:-decagent-executor-compose}-broker"
    )
    assert ".:/workspace:ro" in compose["services"]["executor-broker"]["volumes"]


def test_executor_docker_ci_job_uses_ephemeral_runner_without_deploy_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["executor-boundary"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read"}
    assert "self-hosted" not in str(job).lower()
    assert "secrets." not in str(job)
    assert any(
        "ops/executor/run_docker_boundary_tests.py" in step.get("run", "")
        for step in job["steps"]
    )


def test_public_workflows_never_schedule_self_hosted_runners():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job_id, job in workflow.get("jobs", {}).items():
            assert "self-hosted" not in str(job.get("runs-on", "")).lower(), (
                path.name,
                job_id,
            )


def test_docker_boundary_driver_refuses_local_and_self_hosted_runners():
    for env in (
        {},
        {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted", "RUNNER_OS": "Linux"},
        {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows"},
    ):
        with pytest.raises(RuntimeError, match="GitHub-hosted Linux runner"):
            driver.require_github_hosted_linux(env)

    driver.require_github_hosted_linux(
        {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux"}
    )


def test_docker_boundary_driver_refuses_local_execution_before_docker(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(driver, "run", lambda *args, **kwargs: pytest.fail("Docker was invoked"))

    with pytest.raises(RuntimeError, match="GitHub-hosted Linux runner"):
        driver.execute()


def test_docker_boundary_compose_cleanup_does_not_remove_volumes():
    command = driver.compose_command("decagent-executor-ci-123-1", "down")

    assert command[-1] == "down"
    assert "--volumes" not in command


def test_docker_boundary_project_name_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="decimal digits"):
        driver.project_name("123;touch-pwned", "1")


def test_docker_boundary_preflight_refuses_existing_project_resources(monkeypatch):
    calls = []

    def fake_run(command, *, env, check=True, capture=False):
        calls.append(command)
        return SimpleNamespace(stdout="existing-container\n", returncode=0)

    monkeypatch.setattr(driver, "run", fake_run)

    with pytest.raises(RuntimeError, match="already has resources"):
        driver.assert_project_is_unused(
            "decagent-executor-ci-123-1", "decagent-executor-ci-123-1", {}
        )

    assert len(calls) == 1

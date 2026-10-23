from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_workflow(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_public_release_workflow_never_runs_on_local_runner_and_gates_private_cd():
    release = _load_workflow(ROOT / ".github/workflows/release.yml")
    ci = _load_workflow(ROOT / ".github/workflows/ci.yml")

    assert release["name"] == "Release image publishing and private CD trigger"
    assert release["on"] == {"push": {"tags": ["v*"]}}
    assert set(release["jobs"]) == {"quality", "publish", "dispatch_private_deploy"}
    assert release["jobs"]["quality"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["publish"]["needs"] == "quality"
    assert release["jobs"]["publish"]["runs-on"] == "ubuntu-latest"
    dispatch = release["jobs"]["dispatch_private_deploy"]
    assert dispatch["needs"] == "publish"
    assert dispatch["runs-on"] == "ubuntu-latest"
    assert dispatch["if"] == (
        "needs.publish.result == 'success' && "
        "vars.DECAGENT_PRIVATE_CD_ENABLED == 'true'"
    )
    assert dispatch["permissions"] == {"contents": "read"}
    token_action = next(
        step
        for step in dispatch["steps"]
        if step.get("uses", "").startswith("actions/create-github-app-token@")
    )
    assert token_action["uses"] == (
        "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
    )
    assert token_action["with"]["owner"] == "shuaibilx"
    assert token_action["with"]["repositories"] == "DecAgent-deploy"
    assert token_action["with"]["permission-actions"] == "write"
    dispatch_step = next(
        step
        for step in dispatch["steps"]
        if step.get("name") == "Dispatch fixed private deployment workflow"
    )
    assert "/actions/workflows/deploy-sop.yml/dispatches" in dispatch_step["run"]
    for field in ("source_repository", "tag", "sha", "run_id", "run_attempt", "image"):
        assert f'"{field}"' in dispatch_step["run"]
    assert "self-hosted" not in dispatch["runs-on"]
    assert "workflow_call" in ci["on"]
    assert ci["jobs"]

    for workflow in (release, ci):
        for job in workflow["jobs"].values():
            runner = job.get("runs-on")
            if runner is None:
                continue
            labels = runner if isinstance(runner, list) else [runner]
            assert labels == ["ubuntu-latest"]


def test_local_runner_installer_only_targets_the_private_deployment_repository():
    installer = (ROOT / "ops/deploy/install-local-runner.ps1").read_text(encoding="utf-8")
    bootstrap = (ROOT / "ops/deploy/start-runner.ps1").read_text(encoding="utf-8")

    assert "$Repository = 'shuaibilx/DecAgent-deploy'" in installer
    assert "$ApprovedRepository = 'shuaibilx/DecAgent-deploy'" in installer
    assert "if ($Repository -cne $ApprovedRepository)" in installer
    assert '& gh api "repos/$Repository" --jq .private' in installer
    assert installer.index('$repositoryPrivateOutput = & gh api') < installer.index(
        'New-Item -ItemType Directory -Path $runnerRoot -Force'
    )
    assert ".gitHubUrl -cne \"https://github.com/$Repository\"" in installer
    assert "DecAgentPrivateDeployRunner" in installer
    assert "--replace" not in installer
    assert "legacyRunnerRoot" not in installer
    private_target = "$runnerConfig.gitHubUrl -cne 'https://github.com/shuaibilx/DecAgent-deploy'"
    assert private_target in bootstrap


def test_private_deployment_uses_private_code_and_the_local_sop_install_root():
    installer = (ROOT / "ops/deploy/install-local-runner.ps1").read_text(encoding="utf-8")
    bootstrap = (ROOT / "ops/deploy/start-runner.ps1").read_text(encoding="utf-8")
    workflow_text = (ROOT / "ops/deploy/private-deploy-workflow.yml").read_text(
        encoding="utf-8"
    )
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["deploy"]["steps"]

    assert "$env:DECAGENT_LOCAL_PROJECT_DIR = $ProjectDirectory" in bootstrap
    assert "ops\\deploy\\sop.env" in installer
    assert ".decagent\\sop-secrets" in installer
    assert "broker_token" not in installer and "github_token" not in installer
    assert "DECAGENT_LOCAL_PROJECT_DIR" in workflow_text
    assert "--project-directory $localProjectDirectory" in workflow_text
    assert "--compose-file $composeFile" in workflow_text
    assert "--env-file $envFile" in workflow_text
    assert "& $runtimePython -m ops.deploy.sop_rollout" in workflow_text
    assert all(
        "shuaibilx/DecAgent" not in step.get("with", {}).get("repository", "")
        for step in steps
    )

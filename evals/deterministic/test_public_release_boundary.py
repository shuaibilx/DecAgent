from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_workflow(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_public_release_workflow_is_ghcr_only_and_cannot_dispatch_to_local_runner():
    release = _load_workflow(ROOT / ".github/workflows/release.yml")
    ci = _load_workflow(ROOT / ".github/workflows/ci.yml")

    assert release["name"] == "Release image publishing (GHCR only)"
    assert release["on"] == {"push": {"tags": ["v*"]}}
    assert set(release["jobs"]) == {"quality", "publish"}
    assert release["jobs"]["quality"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["publish"]["needs"] == "quality"
    assert release["jobs"]["publish"]["runs-on"] == "ubuntu-latest"
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

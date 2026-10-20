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

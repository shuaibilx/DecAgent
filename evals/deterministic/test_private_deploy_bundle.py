import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "ops" / "deploy" / "private_deploy_bundle.py"


def test_private_bundle_exporter_is_part_of_the_release_tooling():
    assert MODULE.is_file(), "private deployment must be generated from a reviewed allowlist"


def test_private_bundle_contains_only_allowlisted_runtime_and_deployment_files(tmp_path):
    assert MODULE.is_file(), "private deployment bundle exporter is missing"
    spec = importlib.util.spec_from_file_location("private_deploy_bundle", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    destination = tmp_path / "bundle"
    module.export_private_deploy_bundle(destination, project_root=ROOT)

    expected = {
        ".gitignore",
        ".github/workflows/deploy-sop.yml",
        "README.md",
        "bundle-manifest.json",
        "compose.sop.yaml",
        "ops/deploy/release_verifier.py",
        "ops/deploy/sop.env.example",
        "ops/deploy/sop_business_probe.py",
        "ops/deploy/sop_contract.py",
        "ops/deploy/sop_preflight.py",
        "ops/deploy/sop_rollout.py",
    }
    files = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert files == expected

    manifest = json.loads((destination / "bundle-manifest.json").read_text(encoding="utf-8"))
    for relative_path, digest in manifest["files"].items():
        content = (destination / relative_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest
    assert "decagent" not in {path.split("/")[0] for path in files}


def test_private_bundle_readme_documents_anonymous_public_package_pull(tmp_path):
    spec = importlib.util.spec_from_file_location("private_deploy_bundle", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "bundle"
    module.export_private_deploy_bundle(destination, project_root=ROOT)

    readme = (destination / "README.md").read_text(encoding="utf-8")

    assert "anonymous" in readme.lower()
    assert "Manage Actions access" not in readme
    assert "packages: read" not in readme
    assert "verified release workflow" in readme.lower()


def test_private_bundle_runs_rollout_help_without_application_dependencies(tmp_path):
    assert MODULE.is_file(), "private deployment bundle exporter is missing"
    spec = importlib.util.spec_from_file_location("private_deploy_bundle", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "bundle"
    module.export_private_deploy_bundle(destination, project_root=ROOT)

    result = subprocess.run(
        [sys.executable, "-B", "-S", "-m", "ops.deploy.sop_rollout", "--help"],
        cwd=destination,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "candidate-image" in result.stdout


def test_private_workflow_uses_the_project_python_runtime_at_python_311_or_newer():
    workflow = (ROOT / "ops" / "deploy" / "private-deploy-workflow.yml").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "ops" / "deploy" / "install-local-runner.ps1").read_text(
        encoding="utf-8"
    )

    assert "'.venv\\Scripts\\python.exe'" in workflow
    assert "sys.version_info >= (3, 11)" in workflow
    assert "& $runtimePython .\\ops\\deploy\\release_verifier.py" in workflow
    assert "& $runtimePython -m ops.deploy.sop_rollout" in workflow
    assert "'.venv\\Scripts\\python.exe'" in installer
    assert "sys.version_info >= (3, 11)" in installer


def test_private_workflow_pulls_the_public_control_image_without_package_credentials():
    workflow = (ROOT / "ops" / "deploy" / "private-deploy-workflow.yml").read_text(
        encoding="utf-8"
    )

    assert "packages: read" not in workflow
    assert "docker/login-action" not in workflow


def test_private_bundle_exporter_refuses_to_overwrite_nonempty_destination(tmp_path):
    assert MODULE.is_file(), "private deployment bundle exporter is missing"
    spec = importlib.util.spec_from_file_location("private_deploy_bundle", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "bundle"
    destination.mkdir()
    (destination / "README.md").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        module.export_private_deploy_bundle(destination, project_root=ROOT)

    assert (destination / "README.md").read_text(encoding="utf-8") == "keep"


def test_private_bundle_exporter_checks_destination_symlink_before_resolving(
    monkeypatch, tmp_path
):
    assert MODULE.is_file(), "private deployment bundle exporter is missing"
    spec = importlib.util.spec_from_file_location("private_deploy_bundle", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    alias = tmp_path / "alias"
    target = tmp_path / "target"
    target.mkdir()
    original_resolve = Path.resolve
    original_is_symlink = Path.is_symlink

    def resolve(path, *args, **kwargs):
        if path == alias:
            return target
        return original_resolve(path, *args, **kwargs)

    def is_symlink(path):
        if path == alias:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        module.export_private_deploy_bundle(alias, project_root=ROOT)

    assert not any(target.iterdir())

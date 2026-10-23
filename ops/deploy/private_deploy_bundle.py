"""Export the reviewed, application-independent private deployment bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PRIVATE_DEPLOY_FILES = {
    ".gitignore": None,
    ".github/workflows/deploy-sop.yml": "ops/deploy/private-deploy-workflow.yml",
    "compose.sop.yaml": "ops/deploy/compose.sop.private.yaml",
    "ops/deploy/release_verifier.py": "ops/deploy/release_verifier.py",
    "ops/deploy/sop.env.example": "ops/deploy/sop.env.example",
    "ops/deploy/sop_business_probe.py": "ops/deploy/sop_business_probe.py",
    "ops/deploy/sop_contract.py": "ops/deploy/sop_contract.py",
    "ops/deploy/sop_preflight.py": "ops/deploy/sop_preflight.py",
    "ops/deploy/sop_rollout.py": "ops/deploy/sop_rollout.py",
}

_PRIVATE_GITIGNORE = """ops/deploy/sop.env
.decagent/
__pycache__/
*.py[cod]
"""

_README = """# DecAgent private deployment controls

This private repository contains only the reviewed deployment workflow, image-only
SOP Compose file, standard-library provenance verifier, and rollout/preflight code.
It does not contain or check out the public application source. The public release
must pass its CI gate and publish a SHA-tagged image before this workflow can deploy.

The workflow is restricted to the `main` ref and the `decagent-local-production`
Environment. Configure that Environment to allow `main` only. The public
`decagent-control` GHCR package is pulled anonymously; no package access grant or
registry credential is needed. The verified release workflow runs the release
verifier and passes its verified digest to the rollout coordinator. The coordinator is an
internal helper and must not be invoked with an unverified image reference.

The self-hosted Runner
must be registered to this private repository only and use the `decagent-deploy`
label. The local SOP configuration and secret files remain outside Git:
`ops/deploy/sop.env` and `.decagent/sop-secrets/`. The first deploy bootstraps
only a pristine Compose project. Subsequent releases use the backup and compatibility-
aware rollout coordinator. A failed first install retains database state and permits
recovery only with the same image digest and Compose fingerprint.

Until a least-privilege GitHub App trigger is configured in the public release
workflow, dispatch this workflow manually with the release identity fields. Never
dispatch arbitrary scripts, image tags, or local paths.
"""


def export_private_deploy_bundle(destination: Path, *, project_root: Path | None = None) -> Path:
    """Write an exact allowlist into a new or empty destination without overwriting."""
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    requested_target = Path(destination)
    if requested_target.is_symlink():
        raise ValueError("Private deployment bundle destination cannot be a symlink")
    target = requested_target.resolve()
    if target == root or root in target.parents:
        raise ValueError("Private deployment bundle must be outside the application repository")
    if target.exists():
        if not target.is_dir():
            raise FileExistsError("Private deployment bundle destination is not empty")
        if any(target.iterdir()):
            raise FileExistsError("Private deployment bundle destination is not empty")
    else:
        target.mkdir(parents=True)

    manifest_files = {}
    for relative_destination, relative_source in PRIVATE_DEPLOY_FILES.items():
        output_path = target / relative_destination
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if relative_source is None:
            content = _PRIVATE_GITIGNORE.encode("utf-8")
        else:
            source_path = root / relative_source
            if source_path.is_symlink() or not source_path.is_file():
                raise FileNotFoundError("Reviewed private deployment input is missing")
            content = source_path.read_bytes()
        output_path.write_bytes(content)
        manifest_files[relative_destination] = hashlib.sha256(content).hexdigest()

    readme = target / "README.md"
    if readme.exists():
        raise FileExistsError("Private deployment bundle destination is not empty")
    readme.write_text(_README, encoding="utf-8", newline="\n")
    manifest_files["README.md"] = hashlib.sha256(readme.read_bytes()).hexdigest()
    manifest = {"format": 1, "files": dict(sorted(manifest_files.items()))}
    (target / "bundle-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target

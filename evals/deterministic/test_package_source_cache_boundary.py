"""A source release must not republish locally cached upstream benchmark data."""

import subprocess
import sys
import tarfile
from pathlib import Path


def test_sdist_explicitly_excludes_upstream_caches_even_without_vcs_discovery(tmp_path):
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
    (tmp_path / "README.md").write_text("# Package fixture\n", encoding="utf-8")
    package = tmp_path / "decagent"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in ("vendor_cache", "research"):
        directory = tmp_path / "evals" / "benchmarks" / name / "nested"
        directory.mkdir(parents=True)
        (directory / "upstream-task-labels.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "dist"
    process = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "sdist", "-d", str(target)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stderr
    with tarfile.open(next(target.glob("*.tar.gz"))) as archive:
        names = archive.getnames()
    assert any(name.endswith("/decagent/__init__.py") for name in names)
    assert not any("/evals/benchmarks/vendor_cache/" in name for name in names)
    assert not any("/evals/benchmarks/research/" in name for name in names)

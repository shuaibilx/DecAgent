"""Requirements inference and immutable repair acceptance contracts."""

import pytest

from decagent.tools.repair import infer_project


def test_requirements_plan_preserves_source_and_uses_workspace_environment():
    plan = infer_project({"requirements.txt": b"pytest==8.4.2\n", "test_bug.py": b""})
    assert not plan.blockers
    assert plan.python_version == "3.11"
    assert plan.toolchain == "pip"
    assert plan.setup[0].argv == ("python", "-m", "venv", ".venv")
    assert "requirements.txt" in plan.setup[1].argv
    assert plan.setup[1].source == "requirements.txt"
    assert plan.regression.argv == ("python", "-m", "pytest")


def test_uv_lock_requires_pyproject_and_pinned_uv():
    broken = infer_project({"uv.lock": b"version = 1"})
    assert "pyproject.toml" in " ".join(broken.blockers)
    files = {"uv.lock": b"version = 1", "pyproject.toml": b'[project]\nname="x"\n'}
    assert infer_project(files).toolchain == "uv"
    assert "--frozen" in infer_project(files).setup[0].argv
    assert infer_project(files, available_uv=None).blockers


@pytest.mark.parametrize(
    "files,reason",
    [
        ({"pyproject.toml": b'[project]\nrequires-python=">=3.13"\n'}, "Python"),
        (
            {
                "pyproject.toml": b'[project]\ndependencies=["requests"]\n',
                "requirements.txt": b"requests==2.32.4",
            },
            "conflict",
        ),
        ({".python-version": b"3.13", "requirements.txt": b"pytest"}, "Python"),
        ({"requirements.txt": b"--extra-index-url https://private.example/simple"}, "private"),
        ({"pyproject.toml": b"[broken"}, "parse"),
    ],
)
def test_conflicts_and_unsupported_environment_are_explicit(files, reason):
    assert reason.lower() in " ".join(infer_project(files).blockers).lower()


def test_gpu_request_blocks_instead_of_host_fallback():
    assert infer_project(
        {"requirements.txt": b"pytest"}, task_description="needs CUDA GPU"
    ).blockers


def test_pytest_ini_scope_is_inferred_and_semantic_options_blocked():
    files = {
        "requirements.txt": b"pytest",
        "pytest.ini": b"[pytest]\ntestpaths=tests\naddopts=-q\n",
    }
    plan = infer_project(files)
    assert not plan.blockers
    assert plan.regression.argv == ("python", "-m", "pytest", "tests")
    files["pytest.ini"] = b"[pytest]\naddopts=-k passing_only\n"
    assert infer_project(files).blockers


def test_conflicting_pytest_config_scopes_are_blocked():
    files = {
        "pytest.ini": b"[pytest]\ntestpaths=tests\n",
        "pyproject.toml": (b'[tool.pytest.ini_options]\ntestpaths=["other_tests"]\n'),
    }
    assert "conflict" in " ".join(infer_project(files).blockers).lower()


def test_uv_lock_python_conflict_and_parse_error_are_blocked():
    files = {
        "pyproject.toml": b'[project]\nname="fixture"\n',
        "uv.lock": b'version=1\nrequires-python=">=3.13"\n',
    }
    assert "Python" in " ".join(infer_project(files).blockers)
    files["uv.lock"] = b"not-valid-toml"
    assert "parse" in " ".join(infer_project(files).blockers)


def test_private_pyproject_dependency_is_blocked():
    files = {
        "pyproject.toml": b'[project]\ndependencies=["example @ https://user:password@example/a"]\n'
    }
    assert "private" in " ".join(infer_project(files).blockers)

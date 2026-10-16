"""Conservative, static preparation inference. No repository code is executed here."""

from __future__ import annotations

import configparser
import hashlib
import re
import shlex
import tomllib
from collections.abc import Mapping

from .models import Command, ProjectPlan


def _supports_python(spec: str, version: str) -> bool:
    current = tuple(int(item) for item in version.split("."))
    for token in spec.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<|~=)\s*(\d+)\.(\d+)(?:\.(\d+|\*))?\s*", token)
        if not match:
            return False
        operator, major, minor, patch = match.groups()
        target = (int(major), int(minor))
        checks = {
            ">=": current >= target,
            "<=": current <= target,
            ">": current > target,
            "<": current < target,
            "==": current == target,
            "!=": current != target,
            "~=": current >= target and current[0] == target[0],
        }
        if not checks[operator] or patch not in {None, "0", "*"}:
            return False
    return True


def infer_project(
    files: Mapping[str, bytes],
    *,
    python_version: str = "3.11",
    available_uv: str | None = "0.8.22",
    task_description: str = "",
) -> ProjectPlan:
    blockers: list[str] = []
    setup: list[Command] = []
    configuration: dict = {}
    regression = Command(("python", "-m", "pytest"), "controlled pytest default")
    declarations = tuple(
        (path, hashlib.sha256(content).hexdigest())
        for path, content in sorted(files.items())
        if path in {"pyproject.toml", "uv.lock", ".python-version", "setup.py", "setup.cfg"}
        or re.fullmatch(r"requirements[^/]*\.txt", path)
    )
    if re.search(
        r"\b(gpu|cuda|production credentials|interactive service)\b", task_description, re.I
    ):
        blockers.append(
            "Unsupported environment: GPU, production credentials or interactive service"
        )
    if "pyproject.toml" in files:
        try:
            configuration = tomllib.loads(files["pyproject.toml"].decode("utf-8-sig"))
        except (ValueError, UnicodeError) as error:
            blockers.append(f"Cannot parse pyproject.toml: {error}")
    project = configuration.get("project", {})
    dependency_text = " ".join(str(item) for item in project.get("dependencies", []))
    if re.search(r"git\+|https?://[^/\s]+@|\b(?:cuda|nvidia)\b", dependency_text):
        blockers.append("Unsupported private credentials or GPU dependency in pyproject.toml")
    requested = project.get("requires-python")
    if requested and (
        not isinstance(requested, str) or not _supports_python(requested, python_version)
    ):
        blockers.append(f"Python requirement {requested!r} unsupported by runner {python_version}")
    if ".python-version" in files:
        pinned = files[".python-version"].decode("utf-8-sig").strip()
        if not pinned.startswith(python_version + ".") and pinned != python_version:
            blockers.append(f"Python version conflict: {pinned} versus runner {python_version}")
    requirements = sorted(path for path in files if re.fullmatch(r"requirements[^/]*\.txt", path))
    for path in requirements:
        content = files[path].decode("utf-8-sig", "replace")
        if re.search(
            r"(?:--(?:extra-)?index-url|git\+|https?://[^/\s]+@|\b(?:cuda|nvidia)\b)", content
        ):
            blockers.append(f"Unsupported private credentials/index or GPU dependency in {path}")
    if requirements and project.get("dependencies"):
        blockers.append("Dependency declaration conflict: requirements and pyproject dependencies")
    toolchain = "pip"
    if "uv.lock" in files:
        toolchain = "uv"
        try:
            lock = tomllib.loads(files["uv.lock"].decode("utf-8-sig"))
            lock_python = lock.get("requires-python")
            if lock_python and not _supports_python(lock_python, python_version):
                blockers.append("Python requirement in uv.lock conflicts with runner")
        except (ValueError, UnicodeError, TypeError) as error:
            blockers.append(f"Cannot parse uv.lock: {error}")
        if "pyproject.toml" not in files:
            blockers.append("uv.lock requires pyproject.toml")
        if available_uv is None:
            blockers.append("Pinned uv unavailable; cannot honor uv.lock")
        if requirements:
            blockers.append("Dependency declaration conflict: uv.lock and requirements")
        setup.append(Command(("uv", "sync", "--frozen"), "uv.lock", "setup", 600))
    else:
        setup.append(
            Command(("python", "-m", "venv", ".venv"), "isolated environment", "setup", 600)
        )
        if requirements:
            for path in requirements:
                setup.append(
                    Command(
                        ("python", "-m", "pip", "--python", ".venv", "install", "-r", path),
                        path,
                        "setup",
                        600,
                    )
                )
        elif "pyproject.toml" in files or "setup.py" in files:
            setup.append(
                Command(
                    ("python", "-m", "pip", "--python", ".venv", "install", "."),
                    "pyproject.toml" if "pyproject.toml" in files else "setup.py",
                    "setup",
                    600,
                )
            )
    configurations = [
        ("pyproject.toml", configuration.get("tool", {}).get("pytest", {}).get("ini_options", {}))
    ]
    for name, section in (
        ("pytest.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
        ("tox.ini", "pytest"),
    ):
        if name not in files:
            continue
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(files[name].decode("utf-8-sig"))
            if parser.has_section(section):
                configurations.append((name, dict(parser[section])))
        except (ValueError, UnicodeError, configparser.Error) as error:
            blockers.append(f"Cannot parse pytest configuration {name}: {error}")
    chosen_paths = None
    for source, options in configurations:
        testpaths = options.get("testpaths", [])
        if isinstance(testpaths, str):
            testpaths = testpaths.split()
        if testpaths:
            if not isinstance(testpaths, list) or any(
                not isinstance(path, str)
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or path.startswith("/")
                or ".." in path.split("/")
                for path in testpaths
            ):
                blockers.append("Unsupported pytest testpaths")
            elif chosen_paths is not None and chosen_paths != testpaths:
                blockers.append("Pytest configuration scope conflict")
            else:
                chosen_paths = testpaths
                regression = Command(("python", "-m", "pytest", *testpaths), source + " testpaths")
        addopts = options.get("addopts", "")
        try:
            arguments = shlex.split(addopts) if isinstance(addopts, str) else addopts
            if any(arg not in {"-q", "-v", "--disable-warnings"} for arg in arguments):
                blockers.append("Repository pytest addopts require explicit controlled acceptance")
        except (TypeError, ValueError):
            blockers.append("Cannot parse pytest addopts")
        if set(options) & {
            "python_files",
            "python_classes",
            "python_functions",
            "norecursedirs",
            "required_plugins",
            "usefixtures",
            "filterwarnings",
        }:
            blockers.append(
                "Custom pytest collection/plugins require explicit controlled acceptance"
            )
    return ProjectPlan(
        python_version, toolchain, tuple(setup), regression, declarations, tuple(blockers)
    )

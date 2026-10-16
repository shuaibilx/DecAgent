"""Conservative flags for test changes; flags describe review risks, not business correctness."""

import ast
import re
from collections.abc import Mapping


def is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")


def _shape(content: bytes):
    try:
        tree = ast.parse(content.decode("utf-8-sig"))
    except (SyntaxError, UnicodeError):
        return None
    assertions = {
        ast.dump(node, include_attributes=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
    }
    cases = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }
    return assertions, cases


def analyze_test_changes(
    baseline: Mapping[str, bytes],
    current: Mapping[str, bytes],
) -> tuple[tuple[str, str], ...]:
    flags = []
    for path, old in sorted(baseline.items()):
        if not is_test(path):
            continue
        new = current.get(path)
        if new == old:
            continue
        if new is None:
            flags.append((path, "test_file_deleted"))
            continue
        before, after = _shape(old), _shape(new)
        if after is None:
            flags.append((path, "test_syntax_changed"))
            continue
        if before:
            if not before[0] <= after[0]:
                flags.append((path, "assertion_changed_or_removed"))
            if not before[1] <= after[1]:
                flags.append((path, "test_scope_reduced"))
        for marker in ("skip", "xfail"):
            pattern = rf"\b(?:pytest\.)?(?:mark\.)?{marker}(?:if)?\b"
            if len(re.findall(pattern, new.decode("utf-8-sig"))) > len(
                re.findall(pattern, old.decode("utf-8-sig"))
            ):
                flags.append((path, marker + "_added"))
    for path in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"):
        if baseline.get(path) != current.get(path):
            flags.append((path, "test_configuration_changed"))
    return tuple(flags)

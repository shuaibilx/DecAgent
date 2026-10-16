"""Portable path checks. Executors must also prevent TOCTOU link replacement."""

from __future__ import annotations

import re
import stat
from pathlib import Path, PurePosixPath

SENSITIVE_DIRECTORIES = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".azure",
        ".gcloud",
        ".gnupg",
        ".kube",
        ".waku",
        ".decagent",
        "control-data",
        "secrets",
    }
)
SENSITIVE_FILES = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credential",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "docker.sock",
    }
)
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.I)


def normalize_relative_path(path: str) -> str:
    if type(path) is not str or not path or len(path) > 4096:
        raise ValueError("Path must be a bounded nonempty string")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Control characters in path")
    portable = path.replace("\\", "/")
    if portable.startswith("/") or ":" in portable:
        raise ValueError("Absolute paths and alternate data streams are forbidden")
    components = portable.split("/")
    if ".." in components:
        raise ValueError("Parent traversal is forbidden")
    for component in components:
        if component in ("", "."):
            continue
        if component.endswith((".", " ")) or WINDOWS_DEVICE.fullmatch(component):
            raise ValueError("Ambiguous or reserved path component")
    result = str(PurePosixPath(portable))
    if sensitive_path(result):
        raise ValueError("Sensitive path excluded")
    return result


def sensitive_path(path: str) -> bool:
    parts = path.replace("\\", "/").casefold().split("/")
    return any(
        part in SENSITIVE_DIRECTORIES
        or part in SENSITIVE_FILES
        or part.startswith(".env")
        or part.endswith(SENSITIVE_SUFFIXES)
        for part in parts
    )


def safe_workspace_path(root: Path, path: str) -> Path:
    """Check lexical and resolved paths, including existing symlink parents."""
    relative = normalize_relative_path(path)
    anchor = Path(root).resolve(strict=True)
    if not anchor.is_dir():
        raise ValueError("Workspace root is not a directory")
    destination = (anchor / relative).resolve(strict=False)
    try:
        resolved_relative = destination.relative_to(anchor)
    except ValueError as error:
        raise ValueError("Resolved path escapes task workspace") from error
    normalize_relative_path(resolved_relative.as_posix())
    if destination.exists():
        mode = destination.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("Special files are forbidden")
    return destination

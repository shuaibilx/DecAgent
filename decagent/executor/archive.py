"""Validated in-memory snapshots; never extract a caller's archive on the host."""

from __future__ import annotations

import difflib
import hashlib
import io
import tarfile
from collections.abc import Mapping

from decagent.policy.paths import normalize_relative_path

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILES = 20000


class FilesSnapshot(dict[str, bytes]):
    """Source bytes with normalized Git executable bits, without host permissions."""

    def __init__(self, files=(), *, modes=None):
        super().__init__(files)
        self.modes = {
            path: 0o755 if (modes or {}).get(path, 0o644) & 0o111 else 0o644 for path in self
        }


def file_mode(files: Mapping[str, bytes], path: str) -> int:
    return 0o755 if getattr(files, "modes", {}).get(path, 0o644) & 0o111 else 0o644


def read_archive(data: bytes, *, max_bytes=MAX_ARCHIVE_BYTES, strip_root=False) -> dict[str, bytes]:
    if len(data) > MAX_ARCHIVE_BYTES or max_bytes < 1:
        raise ValueError("Archive size exceeds limit")
    result = FilesSnapshot()
    seen = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_FILES:
                    raise ValueError("Archive has too many entries")
                name = member.name.rstrip("/")
                name = normalize_relative_path(name)
                if strip_root:
                    parts = name.split("/", 1)
                    if len(parts) == 1 and member.isdir():
                        continue
                    if len(parts) != 2:
                        raise ValueError("Archive root missing")
                    name = normalize_relative_path(parts[1])
                if name == "." or name in seen:
                    if name == "." and member.isdir():
                        continue
                    raise ValueError("Duplicate or invalid archive member")
                seen.add(name)
                if not (member.isdir() or member.isfile()):
                    raise ValueError("Links and special files are forbidden")
                if member.isdir():
                    continue
                if member.size < 0 or member.size > max_bytes - total:
                    raise ValueError("Expanded archive size exceeds limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Archive member missing")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError("Invalid archive content length")
                result[name] = content
                result.modes[name] = 0o755 if member.mode & 0o111 else 0o644
                total += member.size
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError("Invalid archive") from error
    for name in result:
        parts = name.split("/")
        if any("/".join(parts[:i]) in result for i in range(1, len(parts))):
            raise ValueError("Archive file used as a directory")
    return result


def make_archive(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in sorted(files.items()):
            name = normalize_relative_path(name)
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = file_mode(files, name)
            entry.uid = entry.gid = 10001
            archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def snapshot_hash(files: Mapping[str, bytes]) -> str:
    return hashlib.sha256(make_archive(files)).hexdigest()


def workspace_diff(baseline: Mapping[str, bytes], current: Mapping[str, bytes]) -> str:
    chunks = []
    for path in sorted(set(baseline) | set(current)):
        old, new = baseline.get(path), current.get(path)
        old_mode, new_mode = file_mode(baseline, path), file_mode(current, path)
        if old == new and old_mode == new_mode:
            continue
        if old is not None and new is not None and old_mode != new_mode:
            chunks.append(f"diff --git a/{path} b/{path}\n")
            chunks.append(f"old mode 100{old_mode:o}\nnew mode 100{new_mode:o}\n")
        if b"\0" in (old or b"") or b"\0" in (new or b""):
            chunks.append(f"Binary file changed: {path}\n")
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or b"").decode("utf-8", "replace").splitlines(keepends=True),
                (new or b"").decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"baseline/{path}",
                tofile=f"workspace/{path}",
            )
        )
    return "".join(chunks)

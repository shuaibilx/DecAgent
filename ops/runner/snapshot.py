"""Bounded source snapshot helper. Never follow links or include environment caches."""

import os
import stat
import sys
import tarfile
from pathlib import Path

from decagent.policy.paths import normalize_relative_path

IGNORED = frozenset({".venv", ".cache", "__pycache__", ".pytest_cache", ".ruff_cache"})
MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 20000


def snapshot(root):
    total = 0
    count = 0
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for directory, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in IGNORED)
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("Snapshot contains directory link")
            for name in sorted(files):
                source = Path(directory) / name
                relative = normalize_relative_path(source.relative_to(root).as_posix())
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                        raise ValueError("Snapshot contains a link or special file")
                    total += metadata.st_size
                    count += 1
                    if total > MAX_BYTES or count > MAX_FILES:
                        raise ValueError("Snapshot exceeds source limits")
                    info = tarfile.TarInfo(relative)
                    info.size = metadata.st_size
                    info.mode = 0o755 if metadata.st_mode & 0o111 else 0o644
                    info.uid = info.gid = 10001
                    with os.fdopen(descriptor, "rb", closefd=False) as stream:
                        archive.addfile(info, stream)
                finally:
                    os.close(descriptor)


if __name__ == "__main__":
    snapshot(Path("/baseline" if sys.argv[1] == "baseline" else "/workspace"))

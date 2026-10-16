"""Trusted helper for serialized task file writes, with no-follow directory descriptors."""

import base64
import json
import os
import sys


def write_file(path, content):
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid path")
    descriptors = [os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptors[-1])
            except FileExistsError:
                pass
            descriptors.append(
                os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptors[-1],
                )
            )
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
            dir_fd=descriptors[-1],
        )
        try:
            import stat

            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Special file")
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    write_file(request["path"], base64.b64decode(request["content_base64"], validate=True))

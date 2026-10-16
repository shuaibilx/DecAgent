"""Create protected Compose secret files once; never replace a configured credential."""

import argparse
import os
import secrets
from pathlib import Path

from decagent.gateway.auth import _protect_file


def write_new(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _protect_file(path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path(".decagent/secrets"))
    args = parser.parse_args()
    from decagent.config import Settings

    settings = Settings.from_env()
    target = args.directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    credentials = {
        "postgres_password": secrets.token_urlsafe(32),
        "broker_token": secrets.token_urlsafe(48),
        "github_token": settings.github_token,
        "main_api_key": settings.main_api_key,
        "jev_api_key": settings.jev_api_key,
    }
    for filename, value in credentials.items():
        path = target / filename
        if not path.exists():
            write_new(path, value)
    print("Compose secret files are ready at " + str(target) + "; existing files were preserved")


if __name__ == "__main__":
    main()

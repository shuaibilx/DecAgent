"""Start a separate local PostgreSQL cluster for disposable integration cases.

No existing database, volume or container is removed. The private connection file
is replaced only after the new test cluster accepts authenticated connections.
"""

import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import psycopg

NAME = "decagent-conditional-postgres"
PORT = 55433
ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / ".decagent" / "conditional-tests"


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, encoding="utf-8"
    )


def main():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    env_file = PRIVATE / "postgres.env"
    existing = docker(
        "container",
        "inspect",
        "--format",
        '{{index .Config.Labels "decagent.purpose"}}',
        NAME,
        check=False,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != "conditional-tests" or not env_file.is_file():
            raise RuntimeError("Existing container cannot be verified as this task's test cluster")
        docker("start", NAME)
    else:
        if not env_file.exists():
            env_file.write_text(
                "POSTGRES_USER=conditional_test\nPOSTGRES_DB=conditional_tests\n"
                "POSTGRES_PASSWORD=" + secrets.token_urlsafe(36) + "\n",
                encoding="utf-8",
            )
        docker(
            "run",
            "-d",
            "--name",
            NAME,
            "--label",
            "decagent.purpose=conditional-tests",
            "--shm-size=1g",
            "--env-file",
            str(env_file),
            "-p",
            f"127.0.0.1:{PORT}:5432",
            "-v",
            "decagent-conditional-tests:/var/lib/postgresql/data",
            "postgres:17-alpine",
            "-c",
            "max_parallel_workers_per_gather=0",
            "-c",
            "max_parallel_maintenance_workers=0",
            "-c",
            "jit=off",
        )
    config = dict(line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines())
    url = (
        "postgresql://conditional_test:"
        + quote(config["POSTGRES_PASSWORD"], safe="")
        + f"@127.0.0.1:{PORT}/conditional_tests"
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("The isolated test cluster did not become ready") from None
            time.sleep(1)
    target = PRIVATE / "database-url"
    if target.is_file() and not (PRIVATE / "database-url.legacy-55432").exists():
        previous = target.read_text(encoding="utf-8")
        if ":55432/" in previous:
            (PRIVATE / "database-url.legacy-55432").write_text(previous, encoding="utf-8")
    temporary = PRIVATE / "database-url.pending"
    temporary.write_text(url, encoding="utf-8")
    os.replace(temporary, target)
    print(
        f"Isolated integration PostgreSQL is ready on 127.0.0.1:{PORT}; credentials remain private."
    )


if __name__ == "__main__":
    main()

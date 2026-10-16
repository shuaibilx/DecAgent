"""Broker startup entry point; readiness requires SQL and real network diagnostics."""

import os
from pathlib import Path

import uvicorn

from decagent.storage.database import Database

from .broker import Broker, create_app
from .docker import DockerEngine


def main():
    token_path = Path(os.environ.get("DECAGENT_BROKER_TOKEN_FILE", "/run/secrets/broker_token"))
    token = token_path.read_text(encoding="utf-8").strip()
    database_url = os.environ.get("DECAGENT_DATABASE_URL")
    if not database_url:
        password = Path("/run/secrets/postgres_password").read_text().strip()
        from urllib.parse import quote

        database_url = f"postgresql://decagent:{quote(password, safe='')}@postgres/decagent"
    database = Database(database_url)
    database.check_version()
    engine = DockerEngine(
        runner_image=os.environ["DECAGENT_RUNNER_IMAGE"],
        proxy_container=os.environ.get("DECAGENT_PROXY_CONTAINER", "decagent-egress-proxy"),
        control_containers=os.environ.get(
            "DECAGENT_CONTROL_CONTAINERS",
            "decagent-postgres,decagent-executor-broker",
        ).split(","),
    )
    broker = Broker(database, engine, Path(os.environ.get("DECAGENT_RECEIPTS_DIR", "/receipts")))
    broker.initialize()
    uvicorn.run(create_app(broker, token), host="0.0.0.0", port=8081, access_log=False)


if __name__ == "__main__":
    main()

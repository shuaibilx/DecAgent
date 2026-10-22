"""Temporary local guest-workbench server for a real browser smoke check.

Uses a disposable PostgreSQL database, no model calls, and only localhost.
Set DECAGENT_TEST_DATABASE_URL and run this module; Ctrl+C drops the fixture DB.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from psycopg import sql

from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from decagent.storage.database import Database

HOST, PORT = "127.0.0.1", 8025
WORKBENCH_PATH = "/"


def build_fixture():
    administrator = Database(os.environ["DECAGENT_TEST_DATABASE_URL"])
    name = "guest_browser_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(administrator.url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    PluginRegistry(database).register(builtin_manifest())
    settings = Settings(
        database_url=database.url,
        public_url=f"http://{HOST}:{PORT}",
        data_dir=Path("output/playwright/guest-browser-fixture").resolve(),
        guest_trial_enabled=True,
        guest_rate_limit_hmac_key=b"B" * 32,
    )
    callbacks = GatewayCallbacks(
        business_employee_read=lambda _identity, command, _params: (
            {"tickets": []} if command == "list" else {"ticket": None, "history": []}
        )
    )
    app = create_app(database, settings, callbacks=callbacks)

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    return administrator, name, app


if __name__ == "__main__":
    if not os.environ.get("DECAGENT_TEST_DATABASE_URL"):
        raise SystemExit("Set DECAGENT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    administrator, name, app = build_fixture()
    print(f"Guest browser fixture: http://{HOST}:{PORT}{WORKBENCH_PATH}", flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    finally:
        with administrator.connect(autocommit=True) as conn:
            statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
            conn.execute(statement)

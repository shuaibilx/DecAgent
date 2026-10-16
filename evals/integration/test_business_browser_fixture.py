"""Offline browser acceptance against real PostgreSQL, graph and Windows JSON CLI.

Run as a module with DECAGENT_TEST_DATABASE_URL. Creates an independent database;
uses a public fixture login and rule/literal requests, with no model credentials.
"""

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from psycopg import sql

from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore
from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from decagent.gateway import create_app
from decagent.gateway.auth import token_hash
from decagent.storage.database import Database

FIXTURE_TOKEN = "dca_" + "public-business-browser-fixture-" * 2
PUBLIC_URL = "http://127.0.0.1:8853"


def build_fixture():
    administrator = Database(os.environ["DECAGENT_TEST_DATABASE_URL"])
    name = "business_browser_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(administrator.url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.owner_credentials(token_hash) VALUES(%s)", (token_hash(FIXTURE_TOKEN),)
        )
    store = TicketStore(database.url)
    assert store.execute("seed", {}, "browser-directory")["ok"]
    scope = PluginRegistry(database).register(builtin_manifest())
    directory = store.execute("directory", {}, "directory")["data"]
    category = next(row["id"] for row in directory["categories"] if row["name"] == "Network")
    settings = Settings(
        database_url=database.url,
        public_url=PUBLIC_URL,
        data_dir=Path(".decagent/business-browser-fixture"),
    )

    def submit(instruction, *, strategy="hybrid", budget=None):
        return database.create_business_task(
            scope["id"],
            {
                "instruction": instruction,
                "strategy": strategy,
                "plugin_digest": scope["manifest_digest"],
                "provenance": {"owner_id": "owner", "authenticated": True},
            },
            budget,
        )

    tasks = []
    for label in ("Approve priority", "Edit priority", "Reject priority"):
        ticket = store.execute(
            "create", {"title": label, "category_id": category}, "fixture-" + label
        )["data"]["ticket"]
        task = submit(
            json.dumps(
                {
                    "command": "set_priority",
                    "params": {
                        "ticket_id": ticket["id"],
                        "expected_revision": 1,
                        "priority": "high",
                    },
                }
            )
        )
        tasks.append({"scenario": label, "id": str(task["id"])})
    ticket = store.execute(
        "create", {"title": "Clarify namesakes", "category_id": category}, "fixture-clarify"
    )["data"]["ticket"]
    task = submit("请分配给张伟，工单 " + ticket["id"], strategy="rule")
    tasks.append({"scenario": "Clarify namesakes", "id": str(task["id"])})
    task = submit(
        json.dumps(
            {"command": "create", "params": {"title": "Budget resume", "category_id": category}}
        ),
        budget={"max_calls": 0},
    )
    tasks.append({"scenario": "Budget resume", "id": str(task["id"])})

    # Initial waits and later replies use the actual production graph and CLI runner.
    worker = BusinessWorker(database, settings)
    while worker.run_once():
        pass
    app = create_app(database, settings)
    stop = threading.Event()

    def consume():
        while not stop.is_set():
            if not BusinessWorker(database, settings).run_once():
                stop.wait(0.2)

    @asynccontextmanager
    async def lifespan(_app):
        thread = threading.Thread(target=consume, daemon=True, name="business-browser-fixture")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(5)

    app.router.lifespan_context = lifespan
    return app, {
        "url": PUBLIC_URL,
        "public_fixture_token": FIXTURE_TOKEN,
        "scope_id": str(scope["id"]),
        "category_id": category,
        "scenarios": tasks,
        "paid_requests": 0,
        "label": "offline browser fixture",
    }


if __name__ == "__main__":
    os.environ.pop("DECAGENT_BUSINESS_CALIBRATION_FILE", None)
    app, metadata = build_fixture()
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8853)

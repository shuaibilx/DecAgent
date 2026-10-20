"""Temporary, offline employee workbench with a persisted conditional plan.

Requires DECAGENT_TEST_DATABASE_URL to point at a disposable PostgreSQL database.
The fixture creates and drops its own database, calls no models, and listens only
on 127.0.0.1:8854 for a manual browser refresh-persistence check.
"""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import uvicorn
from psycopg import sql

from decagent.business.employee_accounts import hash_employee_password
from decagent.business.plans.handlers import BusinessNodeHandlers
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import PlanRuntime
from decagent.business.registry import PluginRegistry
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from decagent.storage.database import Database
from evals.deterministic.test_business_plan_compiler import branching_plan
from evals.integration.business_support import FixtureTickets

HOST, PORT = "127.0.0.1", 8854
LOGIN, PASSWORD = "employee-it", "Offline-Conditional-Trace-Only-2026"


def build_fixture():
    administrator = Database(os.environ["DECAGENT_TEST_DATABASE_URL"])
    name = "conditional_browser_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(administrator.url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()
    backend = FixtureTickets(database, login=LOGIN)
    scope = PluginRegistry(database).list()[0]
    instruction = "检查当前业务目录；本示例故意不提供 ticket_id，以触发目录分支。"
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": instruction,
            "plugin_digest": scope["manifest_digest"],
            "provenance": {
                "employee_id": str(backend.identity.employee_id),
                "authenticated": True,
            },
        },
        graph_version="business-3",
        employee_id=backend.identity.employee_id,
        credential_ref=backend.identity.credential_ref,
    )
    owner = "conditional-browser-" + uuid4().hex
    lease = database.claim(task["id"], owner, seconds=120)
    repository = PlanRepository(database, task["id"], owner, lease["lease_generation"])
    repository.publish(
        branching_plan(),
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "ticket_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    )
    handlers = BusinessNodeHandlers(
        repository, SimpleNamespace(executor=SimpleNamespace(read=backend.read))
    )
    runtime = PlanRuntime(repository, handlers.execute)
    for _ in range(12):
        outcome = runtime.step()
        if outcome["status"] in {"complete", "stopped", "waiting"}:
            break
    if outcome["status"] != "complete":
        raise RuntimeError(f"Fixture plan did not complete deterministically: {outcome}")
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='succeeded',phase='finalize',lease_owner=NULL,"
            "lease_until=NULL WHERE id=%s",
            (task["id"],),
        )
    database.event(task["id"], "task.completed", {"fixture": "offline_browser_acceptance"})

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET password_hash=%s WHERE id=%s",
            (hash_employee_password(PASSWORD), backend.identity.employee_id),
        )
    settings = Settings(
        database_url=database.url,
        public_url=f"http://{HOST}:{PORT}",
        data_dir=Path(".decagent/conditional-browser-fixture"),
    )
    app = create_app(
        database,
        settings,
        callbacks=GatewayCallbacks(
            business_employee_read=lambda identity, command, params: backend.read(command, params)
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    return administrator, name, app, {
        "url": f"http://{HOST}:{PORT}",
        "login": LOGIN,
        "task_id": str(task["id"]),
        "paid_requests": 0,
        "expected_path": {"choose": "case 0", "directory": "completed", "read_ticket": "inactive"},
    }


if __name__ == "__main__":
    if not os.environ.get("DECAGENT_TEST_DATABASE_URL"):
        raise SystemExit("Set DECAGENT_TEST_DATABASE_URL to a disposable PostgreSQL database")
    administrator, name, app, metadata = build_fixture()
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    finally:
        with administrator.connect(autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )

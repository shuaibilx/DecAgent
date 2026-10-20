"""Offline employee workbench for semantic-wait and memory browser acceptance.

Creates its own temporary database, uses only synthetic ticket data and local
loopback HTTP, and issues no model requests. Requires a disposable PostgreSQL
database in DECAGENT_TEST_DATABASE_URL.
"""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from psycopg import sql

from decagent.business.employee_accounts import hash_employee_password
from decagent.business.memory import BusinessMemoryStore
from decagent.business.memory_control import BusinessMemoryControl
from decagent.business.plans.repository import PlanRepository
from decagent.config import Settings
from decagent.gateway import GatewayCallbacks, create_app
from decagent.storage.database import Database
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.business_support import FixtureTickets
from evals.integration.test_business_interventions import setup as semantic_setup
from evals.integration.test_business_interventions import waiting as create_waiting_decision
from evals.integration.test_business_policy_store import publish as publish_test_policy
from evals.integration.test_business_policy_store import resolve as resolve_test_policy
from evals.integration.test_business_policy_store import sample as policy_setup

HOST, PORT = "127.0.0.1", 8855
PASSWORD = "Offline-Workbench-Acceptance-2026"


def build_fixture():
    administrator = Database(os.environ["DECAGENT_TEST_DATABASE_URL"])
    name = "workbench_intervention_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    database = Database(administrator.url.rsplit("/", 1)[0] + "/" + name)
    database.migrate()

    create_semantic = semantic_setup.__wrapped__(database)
    semantic = create_semantic(kind="choice")
    pending = create_waiting_decision(database, semantic)
    employee = semantic["backend"]
    other_employee = FixtureTickets(database, login="employee-facilities")
    with database.connect() as conn:
        for actor in (employee, other_employee):
            conn.execute(
                "UPDATE business.employee_accounts SET password_hash=%s WHERE id=%s",
                (hash_employee_password(PASSWORD), actor.identity.employee_id),
            )

    memory_control = BusinessMemoryControl(database)
    memory_context = memory_control.context(employee.browser, semantic["task"]["id"])
    observation = database.event(
        semantic["task"]["id"], "business.preference_observed", {"value": "中文"}
    )
    memory = BusinessMemoryStore(database).propose_preference(
        employee.identity,
        memory_context,
        subject="response_language",
        value="中文",
        evidence_refs=[
            {
                "kind": "task_event",
                "task_id": str(semantic["task"]["id"]),
                "seq": observation["seq"],
            }
        ],
    )

    policy = policy_setup.__wrapped__(database)
    publish_test_policy(policy)
    resolve_test_policy(policy)
    policy_owner = "workbench-policy-" + uuid4().hex
    policy_task = database.claim(policy["task"]["id"], policy_owner, seconds=120)
    policy_repo = PlanRepository(
        database, policy_task["id"], policy_owner, policy_task["lease_generation"]
    )
    policy_repo.publish(read_plan())
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='queued',lease_owner=NULL,lease_until=NULL WHERE id=%s",
            (policy_task["id"],),
        )

    selected_candidate = next(
        candidate
        for candidate in semantic["evidence"].question.criteria
        if candidate != "no_match"
    )
    with database.connect() as conn:
        conn.execute("UPDATE business.people SET active=false WHERE id=%s", (selected_candidate,))

    settings = Settings(
        database_url=database.url,
        public_url=f"http://{HOST}:{PORT}",
        data_dir=Path(".decagent/workbench-intervention-fixture"),
    )
    app = create_app(
        database,
        settings,
        callbacks=GatewayCallbacks(
            business_employee_read=lambda identity, command, params: employee.read(command, params)
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    metadata = {
        "url": f"http://{HOST}:{PORT}",
        "login": employee.identity.login,
        "other_login": other_employee.identity.login,
        "password": PASSWORD,
        "task_id": str(semantic["task"]["id"]),
        "policy_task_id": str(policy["task"]["id"]),
        "task_instruction": semantic["task"]["authorization_snapshot"]["instruction"],
        "approval_id": pending["approval_id"],
        "stale_candidate_id": selected_candidate,
        "stale_candidate_name": semantic["evidence"].question.criteria[selected_candidate],
        "memory_id": str(memory["id"]),
        "memory_subject": memory["subject"],
        "paid_requests": 0,
    }
    return administrator, name, app, metadata


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

"""Isolated mixed-version records for conditional runtime migration regressions."""

import hashlib
import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from evals.integration.business_support import FixtureTickets


def create_mixed_business_tasks(database):
    """Only create data in the per-test database supplied by conftest."""
    with database.connect() as conn:
        has_workspaces = (
            conn.execute("SELECT to_regclass('business.workspaces') AS relation").fetchone()[
                "relation"
            ]
            is not None
        )
    if not has_workspaces:
        return _create_pre_workspace_business_tasks(database)

    backend = FixtureTickets(database)
    registered = PluginRegistry(database).list()[0]
    authorization = {
        "instruction": json.dumps({"command": "directory", "params": {}}),
        "strategy": "rule",
        "plugin_digest": registered["manifest_digest"],
        "provenance": {"authenticated": True, "employee_id": str(backend.identity.employee_id)},
    }
    tasks = {
        version: database.create_business_task(
            registered["id"],
            authorization,
            graph_version=version,
            employee_id=backend.identity.employee_id,
            credential_ref=backend.identity.credential_ref,
        )
        for version in ("business-2", "business-3")
    }
    directory = backend.read("directory", {})
    category = directory["categories"][0]["id"]
    operation = backend.prepare(
        str(uuid4()),
        "create",
        {"title": "Uncertain prepared operation fixture", "category_id": category},
    )
    # No commit was made: this models uncertain transport state, not a completed write.
    action = database.plan_action(
        tasks["business-2"]["id"],
        "legacy-unknown",
        "a" * 64,
        {
            "operation_id": operation["request_id"],
            "command": "create",
            "params": operation["params"],
        },
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.operations SET state='unknown',error_code='fixture_network_loss' "
            "WHERE id=%s",
            (operation["operation_id"],),
        )
        conn.execute(
            "UPDATE app.actions SET status='unknown',server_operation_id=%s,receipt=%s WHERE id=%s",
            (
                operation["operation_id"],
                Jsonb({"status": "unknown", "operation_id": operation["operation_id"]}),
                action["id"],
            ),
        )
        action = conn.execute("SELECT * FROM app.actions WHERE id=%s", (action["id"],)).fetchone()
        saved = conn.execute(
            "SELECT state,error_code FROM business.operations WHERE id=%s",
            (operation["operation_id"],),
        ).fetchone()
        operation = {**operation, **saved}
    return {"tasks": tasks, "action": action, "operation": operation, "backend": backend}


def _create_pre_workspace_business_tasks(database):
    """Seed v11/v15 rows using only columns present before migration 020."""
    backend = FixtureTickets(database)
    registered = PluginRegistry(database).register(builtin_manifest())
    authorization = {
        "instruction": json.dumps({"command": "directory", "params": {}}),
        "strategy": "rule",
        "plugin_digest": registered["manifest_digest"],
        "provenance": {
            "authenticated": True,
            "employee_id": str(backend.identity.employee_id),
        },
    }
    task_ids = {version: uuid4() for version in ("business-2", "business-3")}
    with database.connect() as conn:
        tasks = {}
        for version, task_id in task_ids.items():
            snapshot = {
                **authorization,
                "task_id": str(task_id),
                "kind": "business",
                "scope_id": str(registered["id"]),
                "plugin_id": registered["plugin_id"],
                "plugin_version": registered["plugin_version"],
            }
            tasks[version] = conn.execute(
                "INSERT INTO app.tasks"
                "(id,kind,business_scope_id,authorization_snapshot,budget,graph_version,"
                "employee_id,credential_ref) VALUES(%s,'business',%s,%s,'{}',%s,%s,%s) "
                "RETURNING *",
                (
                    task_id,
                    registered["id"],
                    Jsonb(snapshot),
                    version,
                    backend.identity.employee_id,
                    backend.identity.credential_ref,
                ),
            ).fetchone()

        operation_id = str(uuid4())
        request_id = str(uuid4())
        params = {"title": "Uncertain prepared operation fixture", "category_id": str(uuid4())}
        conn.execute(
            "INSERT INTO business.operations"
            "(id,employee_id,request_id,scope_id,command,params,effective_params,input_hash,"
            "contract_digest,source,state,confirmation_required) "
            "VALUES(%s,%s,%s,%s,'create',%s,%s,%s,%s,'external_mcp','unknown',true)",
            (
                operation_id,
                backend.identity.employee_id,
                request_id,
                registered["id"],
                Jsonb(params),
                Jsonb(params),
                hashlib.sha256(operation_id.encode()).hexdigest(),
                registered["manifest_digest"],
            ),
        )
    action = database.plan_action(
        tasks["business-2"]["id"],
        "legacy-unknown",
        "a" * 64,
        {
            "operation_id": operation_id,
            "command": "create",
            "params": params,
        },
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.actions SET status='unknown',server_operation_id=%s,receipt=%s WHERE id=%s",
            (
                operation_id,
                Jsonb({"status": "unknown", "operation_id": operation_id}),
                action["id"],
            ),
        )
        action = conn.execute("SELECT * FROM app.actions WHERE id=%s", (action["id"],)).fetchone()
        saved_operation = conn.execute(
            "SELECT state,error_code FROM business.operations WHERE id=%s", (operation_id,)
        ).fetchone()
    operation = {"operation_id": operation_id, **saved_operation}
    return {"tasks": tasks, "action": action, "operation": operation, "backend": backend}


@pytest.fixture
def mixed_business_tasks(database):
    return create_mixed_business_tasks(database)

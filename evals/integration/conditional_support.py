"""Isolated mixed-version records for conditional runtime migration regressions."""

import json
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.registry import PluginRegistry
from evals.integration.business_support import FixtureTickets


def create_mixed_business_tasks(database):
    """Only create data in the per-test database supplied by conftest."""
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


@pytest.fixture
def mixed_business_tasks(database):
    return create_mixed_business_tasks(database)

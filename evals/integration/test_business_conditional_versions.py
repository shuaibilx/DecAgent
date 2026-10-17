"""Migration fixtures preserve graph identity and uncertain prepared operations."""

import pytest

from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks
from evals.integration.database_support import database_name

pytestmark = pytest.mark.integration


def test_mixed_fixture_preserves_versions_and_unknown_operation(database, mixed_business_tasks):
    sample = mixed_business_tasks
    before = {version: task["id"] for version, task in sample["tasks"].items()}
    assert set(before) == {"business-2", "business-3"}
    assert database_name(database).startswith("case_")
    database.migrate()
    for version, task_id in before.items():
        saved = database.get_task(task_id)
        assert saved["graph_version"] == version
        assert saved["employee_id"] == sample["backend"].identity.employee_id
        assert not saved["business_migration_pending"]
    with database.connect() as conn:
        action = conn.execute(
            "SELECT status,server_operation_id FROM app.actions WHERE id=%s",
            (sample["action"]["id"],),
        ).fetchone()
        assert action == {
            "status": "unknown",
            "server_operation_id": sample["operation"]["operation_id"],
        }
        operation = conn.execute(
            "SELECT state FROM business.operations WHERE id=%s",
            (sample["operation"]["operation_id"],),
        ).fetchone()
        assert operation["state"] == "unknown"
        assert conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"] == 0
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM business.operation_receipts WHERE operation_id=%s",
                (sample["operation"]["operation_id"],),
            ).fetchone()["n"]
            == 0
        )

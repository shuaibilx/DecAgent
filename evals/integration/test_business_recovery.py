"""Crash after a committed resource write resumes the original journal and checkpoint."""

import json

import pytest

from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.business.tickets import builtin_manifest
from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from evals.integration.business_support import FixtureTickets

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("crash_point", ["backend_commit", "journal_success"])
def test_committed_comment_survives_lost_checkpoint_and_lease_takeover_without_duplicate(
    database, tmp_path, crash_point
):
    store = FixtureTickets(database)
    assert store.execute("seed", {}, "seed")["ok"]
    category = store.execute("directory", {}, "dir")["data"]["categories"][0]["id"]
    ticket = store.execute("create", {"title": "Fixture", "category_id": category}, "create")[
        "data"
    ]["ticket"]
    scope = PluginRegistry(database).register(builtin_manifest())
    saved = database.create_business_task(
        scope["id"],
        {
            "instruction": json.dumps(
                {
                    "command": "comment",
                    "params": {
                        "ticket_id": ticket["id"],
                        "expected_revision": 1,
                        "body": "One requested comment",
                    },
                }
            ),
            "strategy": "hybrid",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"owner_id": "owner", "authenticated": True},
        },
        employee_id=store.identity.employee_id,
        credential_ref=store.identity.credential_ref,
    )
    writes, interrupted = [], []

    class Backend:
        def __getattr__(self, name):
            return getattr(store, name)

        def commit(self, operation_id, *, grant=None):
            result = store.commit(operation_id, grant=grant)
            if result["command"] == "comment":
                writes.append(operation_id)
                if crash_point == "backend_commit" and not interrupted:
                    interrupted.append(True)
                    raise KeyboardInterrupt("Injected process crash after backend commit")
            return result

    class Services(BusinessServices):
        def _execute(self, data):
            result = super()._execute(data)
            if crash_point == "journal_success" and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt("Injected process crash after journal success")
            return result

    def factory(db, settings, config, task, owner, generation):
        return Services(db, settings, config, task, owner, generation, mcp_client=Backend())

    def instance(owner):
        return BusinessWorker(
            database, Settings(data_dir=tmp_path), services_factory=factory, owner=owner
        )

    with pytest.raises(KeyboardInterrupt, match="Injected process crash"):
        instance("first-worker").run_once()
    with database.connect() as conn:
        actions = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (saved["id"],)
        ).fetchall()
        assert len(actions) == 1 and actions[0]["status"] == (
            "running" if crash_point == "backend_commit" else "succeeded"
        )
        conn.execute(
            "UPDATE app.tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (saved["id"],),
        )
    assert instance("second-worker").run_once()
    task = database.get_task(saved["id"])
    assert task["state"] == "succeeded", database.events(saved["id"])[-1]
    assert task["lease_generation"] == 2
    assert len(writes) == 1
    current = store.execute("get", {"ticket_id": ticket["id"]}, "after")["data"]
    assert current["ticket"]["revision"] == 2
    assert len([row for row in current["history"] if row["command"] == "comment"]) == 1
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.actions WHERE task_id=%s", (saved["id"],)
            ).fetchone()["n"]
            == 1
        )
    assert instance("third-worker").run_once() is False

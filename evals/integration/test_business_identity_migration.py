"""An in-place v1 business upgrade preserves legacy unknown principals."""

import hashlib
import os
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from decagent.business.contracts import canonical
from decagent.business.tickets.store import TicketStore, _json
from decagent.storage import database as storage
from evals.integration.database_support import temporary_database

pytestmark = pytest.mark.integration


def _legacy_create(database, title, category, operation_id):
    """Materialize the v1 SQL layout without invoking a newer domain writer."""
    params = {"title": title, "category_id": category}
    with database.connect() as connection:
        ticket = connection.execute(
            "INSERT INTO business.tickets(id,title,category_id) VALUES(%s,%s,%s) RETURNING *",
            (uuid4(), title, category),
        ).fetchone()
        connection.execute(
            "INSERT INTO business.ticket_history(id,ticket_id,revision,command,details) "
            "VALUES(%s,%s,1,'create',%s)",
            (uuid4(), ticket["id"], Jsonb(params)),
        )
        result = _json({"ticket": ticket})
        connection.execute(
            "INSERT INTO business.operation_receipts(operation_id,input_hash,command,resource_id,"
            "revision,result) VALUES(%s,%s,'create',%s,1,%s)",
            (
                operation_id,
                hashlib.sha256(
                    canonical({"command": "create", "params": params}).encode()
                ).hexdigest(),
                ticket["id"],
                Jsonb(result),
            ),
        )
    return result["ticket"]["id"]


@pytest.fixture
def legacy_v6_database(monkeypatch):
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    with temporary_database(url, prefix="case_mcp_") as database:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "MIGRATIONS", storage.MIGRATIONS[:6])
            patch.setattr(storage, "SCHEMA_VERSION", 6)
            database.migrate()
        yield database


def test_identity_upgrade_preserves_legacy_ticket_receipt_history_and_task(
    legacy_v6_database,
):
    db = legacy_v6_database

    tickets = TicketStore(db.url)
    assert tickets.execute("seed", {}, "seed-legacy")["ok"]
    directory = tickets.execute("directory", {}, "directory")["data"]
    category = directory["categories"][0]["id"]
    requester = directory["people"][0]["id"]
    ticket_id = _legacy_create(db, "Legacy named request", category, "old-write")
    # A pre-v2 row may have a directory requester while still lacking an authenticated actor.
    with db.connect() as connection:
        connection.execute(
            "UPDATE business.tickets SET requester_id=%s WHERE id=%s", (requester, ticket_id)
        )
        connection.execute(
            "UPDATE business.operation_receipts SET "
            "result=jsonb_set(result,'{ticket,requester_id}',to_jsonb(%s::text)) "
            "WHERE operation_id='old-write'",
            (requester,),
        )
    anonymous_id = _legacy_create(db, "Legacy anonymous request", category, "old-anon")
    task_id, scope_id = uuid4(), uuid4()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO app.business_scopes"
            "(id,plugin_id,plugin_version,manifest_digest,manifest) VALUES(%s,%s,%s,%s,%s)",
            (scope_id, "legacy-tickets", "1.0.0", "a" * 64, Jsonb({})),
        )
        connection.execute(
            "INSERT INTO app.tasks"
            "(id,kind,business_scope_id,authorization_snapshot,graph_version) "
            "VALUES(%s,'business',%s,%s,'business-1')",
            (task_id, scope_id, Jsonb({"owner_id": "owner"})),
        )
        before = {
            "ticket": connection.execute(
                "SELECT * FROM business.tickets WHERE id=%s",
                (ticket_id,),
            ).fetchone(),
            "history": connection.execute(
                "SELECT * FROM business.ticket_history WHERE ticket_id=%s",
                (ticket_id,),
            ).fetchall(),
            "receipt": connection.execute(
                "SELECT * FROM business.operation_receipts WHERE operation_id='old-write'"
            ).fetchone(),
            "task": connection.execute(
                "SELECT * FROM app.tasks WHERE id=%s",
                (task_id,),
            ).fetchone(),
        }

    db.migrate()
    db.migrate()
    with db.connect() as connection:
        after = {
            "ticket": connection.execute(
                "SELECT * FROM business.tickets WHERE id=%s",
                (ticket_id,),
            ).fetchone(),
            "history": connection.execute(
                "SELECT * FROM business.ticket_history WHERE ticket_id=%s",
                (ticket_id,),
            ).fetchall(),
            "receipt": connection.execute(
                "SELECT * FROM business.operation_receipts WHERE operation_id='old-write'"
            ).fetchone(),
            "task": connection.execute(
                "SELECT * FROM app.tasks WHERE id=%s",
                (task_id,),
            ).fetchone(),
        }
        assert str(before["ticket"]["requester_id"]) == requester
        for kind in ("ticket", "receipt"):
            assert {field: after[kind][field] for field in before[kind]} == before[kind]
        preserved = set(before["task"]) - {"state", "lease_owner", "lease_until", "event_seq"}
        assert {key: after["task"][key] for key in preserved} == {
            key: before["task"][key] for key in preserved
        }
        assert after["task"]["state"] == "recovering"
        assert after["task"]["business_migration_pending"] is True
        assert after["task"]["lease_owner"] is None and after["task"]["lease_until"] is None
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM app.events WHERE task_id=%s "
                "AND kind='business.migration_required'",
                (task_id,),
            ).fetchone()["n"]
            == 1
        )
        assert len(after["history"]) == len(before["history"])
        for original, migrated in zip(before["history"], after["history"], strict=True):
            assert {field: migrated[field] for field in original} == original
        for table, identifier, column in (
            ("business.tickets", ticket_id, "employee_id"),
            ("business.ticket_history", before["history"][0]["id"], "employee_id"),
            ("business.operation_receipts", "old-write", "employee_id"),
            ("app.tasks", task_id, "employee_id"),
        ):
            assert (
                connection.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {}=%s").format(
                        sql.Identifier(column),
                        sql.Identifier(*table.split(".")),
                        sql.Identifier("operation_id" if table.endswith("receipts") else "id"),
                    ),
                    (identifier,),
                ).fetchone()[column]
                is None
            )
        assert (
            connection.execute(
                "SELECT operation_id FROM business.ticket_history WHERE ticket_id=%s",
                (ticket_id,),
            ).fetchone()["operation_id"]
            is None
        )
        assert (
            connection.execute(
                "SELECT source FROM business.ticket_history WHERE ticket_id=%s",
                (ticket_id,),
            ).fetchone()["source"]
            is None
        )
        assert connection.execute(
            "SELECT requester_id,employee_id FROM business.tickets WHERE id=%s",
            (anonymous_id,),
        ).fetchone() == {"requester_id": None, "employee_id": None}
        assert (
            connection.execute("SELECT count(*) AS n FROM business.employee_accounts").fetchone()[
                "n"
            ]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) AS n FROM business.employee_sessions").fetchone()[
                "n"
            ]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) AS n FROM business.mcp_credentials").fetchone()["n"]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) AS n FROM business.employee_audit").fetchone()["n"]
            == 0
        )

"""Business authorization is checked against current PostgreSQL identity and scope."""

from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.business.access import (
    AccessError,
    allowed_commands,
    authorize_write,
    get_history,
    get_receipt,
    get_ticket,
    list_tickets,
)
from decagent.business.tickets.store import TicketStore

pytestmark = pytest.mark.integration

_ACCOUNTS = (
    ("employee-it", "张伟", "IT Support", "employee"),
    ("employee-facilities", "张伟", "Facilities", "employee"),
    ("handler-it", "Zhang Wei", "IT Support", "handler"),
    ("handler-facilities", "Zhang Wei", "Facilities", "handler"),
    ("supervisor", "Chen Li", "Network", "supervisor"),
)


@pytest.fixture
def business(database):
    assert TicketStore(database.url).execute("seed", {}, "seed-access-test")["ok"]
    with database.connect() as conn:
        people = {
            (row["name"], row["team"]): row["id"]
            for row in conn.execute("SELECT id,name,team FROM business.people").fetchall()
        }
        category = conn.execute(
            "SELECT id FROM business.categories ORDER BY name LIMIT 1"
        ).fetchone()["id"]
        actors = {}
        for login, name, team, role in _ACCOUNTS:
            actor_id = uuid4()
            person_id = people[name, team]
            conn.execute(
                "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                "VALUES(%s,%s,%s,%s,%s)",
                (actor_id, login, person_id, role, "test-only-hash-not-a-secret"),
            )
            actors[login] = {"id": actor_id, "person_id": person_id}
    return database, actors, category


def _ticket(database, category, requester, *, assignee=None, legacy=False):
    ticket_id = uuid4()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets"
            "(id,title,description,category_id,requester_id,assignee_id,status,employee_id) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                ticket_id,
                "private ticket",
                "private details",
                category,
                requester["person_id"],
                assignee["person_id"] if assignee else None,
                "assigned" if assignee else "open",
                None if legacy else requester["id"],
            ),
        )
        conn.execute(
            "INSERT INTO business.ticket_history(id,ticket_id,revision,command,details) "
            "VALUES(%s,%s,1,'create',%s)",
            (uuid4(), ticket_id, Jsonb({"private": "history"})),
        )
    return ticket_id


def _receipt(database, operation_id, employee_id, ticket_id):
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.operation_receipts"
            "(operation_id,input_hash,command,resource_id,revision,result,employee_id,"
            "contract_digest) "
            "VALUES(%s,%s,'create',%s,1,%s,%s,%s)",
            (
                operation_id,
                "a" * 64,
                ticket_id,
                Jsonb({"private": "receipt"}),
                employee_id,
                "b" * 64 if employee_id else None,
            ),
        )


def _assert_unavailable(call):
    with pytest.raises(AccessError) as exc:
        call()
    assert exc.value.code == "not_found"
    assert "private" not in str(exc.value)
    assert len(str(exc.value)) < 100


def test_ticket_history_and_list_scope_survive_person_filter_and_reassignment(business):
    database, actors, category = business
    requester = actors["employee-it"]
    other = actors["employee-facilities"]
    handler = actors["handler-it"]
    alternate_handler = actors["handler-facilities"]
    own_id = _ticket(database, category, requester, assignee=handler)
    other_id = _ticket(database, category, other, assignee=handler)

    with database.connect() as conn:
        assert get_ticket(conn, requester["id"], own_id)["id"] == own_id
        _assert_unavailable(lambda: get_ticket(conn, requester["id"], other_id))
        _assert_unavailable(lambda: get_history(conn, requester["id"], other_id))
        assert len(get_history(conn, requester["id"], own_id)) == 1
        assert [
            row["id"]
            for row in list_tickets(conn, requester["id"], {"person_id": str(handler["person_id"])})
        ] == [own_id]
        assert {row["id"] for row in list_tickets(conn, handler["id"], {})} == {
            own_id,
            other_id,
        }
        assert {row["id"] for row in list_tickets(conn, actors["supervisor"]["id"], {})} == {
            own_id,
            other_id,
        }

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.tickets SET assignee_id=%s WHERE id=%s",
            (alternate_handler["person_id"], own_id),
        )
    with database.connect() as conn:
        _assert_unavailable(lambda: get_ticket(conn, handler["id"], own_id))
        _assert_unavailable(lambda: get_history(conn, handler["id"], own_id))
        assert [row["id"] for row in list_tickets(conn, handler["id"], {})] == [other_id]
        assert get_ticket(conn, alternate_handler["id"], own_id)["id"] == own_id


def test_receipts_and_legacy_rows_do_not_become_other_employee_property(business):
    database, actors, category = business
    requester = actors["employee-it"]
    other = actors["employee-facilities"]
    supervisor = actors["supervisor"]
    own_id = _ticket(database, category, requester)
    old_id = _ticket(database, category, requester, legacy=True)
    _receipt(database, "own-operation", requester["id"], own_id)
    _receipt(database, "legacy-operation", None, old_id)

    with database.connect() as conn:
        assert get_receipt(conn, requester["id"], "own-operation")["resource_id"] == own_id
        _assert_unavailable(lambda: get_receipt(conn, other["id"], "own-operation"))
        _assert_unavailable(lambda: get_receipt(conn, requester["id"], "legacy-operation"))
        _assert_unavailable(lambda: get_ticket(conn, requester["id"], old_id))
        _assert_unavailable(lambda: get_history(conn, requester["id"], old_id))
        assert get_ticket(conn, supervisor["id"], old_id)["id"] == old_id
        assert get_receipt(conn, supervisor["id"], "legacy-operation")["resource_id"] == old_id
        assert get_receipt(conn, supervisor["id"], "no-such-operation") is None


def test_role_actions_bind_requester_and_validate_assignment_target(business):
    database, actors, category = business
    requester = actors["employee-it"]
    other = actors["employee-facilities"]
    handler = actors["handler-it"]
    supervisor = actors["supervisor"]
    ticket_id = _ticket(database, category, other, assignee=handler)
    legacy_id = _ticket(database, category, requester, legacy=True)

    with database.connect() as conn:
        prepared = authorize_write(
            conn,
            requester["id"],
            "create",
            {"title": "a request", "category_id": str(category)},
        )
        assert prepared["requester_id"] == requester["person_id"]
        assert allowed_commands(conn, requester["id"]) < allowed_commands(conn, supervisor["id"])
        assert "assign" not in allowed_commands(conn, requester["id"])
        for field, value in (
            ("requester_id", str(other["person_id"])),
            ("employee_id", str(supervisor["id"])),
            ("role", "supervisor"),
            ("approved", True),
        ):
            with pytest.raises(AccessError) as exc:
                authorize_write(
                    conn,
                    requester["id"],
                    "create",
                    {"title": "forged", "category_id": str(category), field: value},
                )
            assert exc.value.code == "invalid_input"
        for command, extras in (
            ("assign", {"person_id": str(handler["person_id"])}),
            ("set_priority", {"priority": "high"}),
            ("comment", {"body": "attempt"}),
            ("close", {}),
        ):
            with pytest.raises(AccessError):
                authorize_write(
                    conn,
                    requester["id"],
                    command,
                    {"ticket_id": str(ticket_id), "expected_revision": 1, **extras},
                )
        for command, extras in (
            ("start", {}),
            ("resolve", {"resolution": "Employee confirmed working"}),
            ("comment", {"body": "Handling this"}),
        ):
            assert authorize_write(
                conn,
                handler["id"],
                command,
                {"ticket_id": str(ticket_id), "expected_revision": 1, **extras},
            )["ticket_id"] == str(ticket_id)
        with pytest.raises(AccessError) as exc:
            authorize_write(
                conn,
                handler["id"],
                "close",
                {"ticket_id": str(ticket_id), "expected_revision": 1},
            )
        assert exc.value.code == "forbidden"
        assert authorize_write(
            conn,
            supervisor["id"],
            "assign",
            {
                "ticket_id": str(ticket_id),
                "expected_revision": 1,
                "person_id": str(handler["person_id"]),
            },
        )["person_id"] == str(handler["person_id"])
        assert (
            authorize_write(
                conn,
                supervisor["id"],
                "set_priority",
                {"ticket_id": str(ticket_id), "expected_revision": 1, "priority": "urgent"},
            )["priority"]
            == "urgent"
        )
        with pytest.raises(AccessError) as exc:
            authorize_write(
                conn,
                supervisor["id"],
                "assign",
                {
                    "ticket_id": str(ticket_id),
                    "expected_revision": 1,
                    "person_id": str(requester["person_id"]),
                },
            )
        assert exc.value.code == "invalid_reference"
        _assert_unavailable(
            lambda: authorize_write(
                conn,
                supervisor["id"],
                "comment",
                {"ticket_id": str(legacy_id), "expected_revision": 1, "body": "new write"},
            )
        )

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET active=false WHERE id=%s", (handler["id"],)
        )
    with database.connect() as conn:
        with pytest.raises(AccessError) as exc:
            authorize_write(
                conn,
                supervisor["id"],
                "assign",
                {
                    "ticket_id": str(ticket_id),
                    "expected_revision": 1,
                    "person_id": str(handler["person_id"]),
                },
            )
        assert exc.value.code == "invalid_reference"


def test_each_access_reloads_current_role_account_and_directory_state(business):
    database, actors, category = business
    supervisor = actors["supervisor"]
    requester = actors["employee-it"]
    ticket_id = _ticket(database, category, requester)
    command = {"ticket_id": str(ticket_id), "expected_revision": 1, "priority": "high"}
    with database.connect() as conn:
        assert authorize_write(conn, supervisor["id"], "set_priority", command) == command

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET role='employee',credential_revision=2 "
            "WHERE id=%s",
            (supervisor["id"],),
        )
    with database.connect() as conn:
        with pytest.raises(AccessError) as exc:
            authorize_write(conn, supervisor["id"], "set_priority", command)
        assert exc.value.code == "forbidden"
        _assert_unavailable(lambda: get_ticket(conn, supervisor["id"], ticket_id))

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET active=false WHERE id=%s", (requester["id"],)
        )
    with database.connect() as conn:
        with pytest.raises(AccessError) as exc:
            get_ticket(conn, requester["id"], ticket_id)
        assert exc.value.code == "unauthorized"

    with database.connect() as conn:
        conn.execute(
            "UPDATE business.people SET active=false WHERE id=%s", (supervisor["person_id"],)
        )
    with database.connect() as conn:
        with pytest.raises(AccessError) as exc:
            allowed_commands(conn, supervisor["id"])
        assert exc.value.code == "unauthorized"

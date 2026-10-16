"""In-process MCP semantics for model/accounting regressions; network tests are separate."""

import hashlib
from uuid import uuid4

from decagent.business import access
from decagent.business.employee_accounts import _DEMO_ACCOUNTS
from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.mcp_client import MCPOutcomeUnknown
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.tickets import builtin_manifest
from decagent.business.tickets.store import TicketStore, _json


class FixtureTickets:
    """Test setup may approve its own fixtures; Agent dispatch uses separate MCP methods."""

    def __init__(self, database, *, login="supervisor", lose=False):
        self.database, self.lose, self.writes = database, lose, 0
        self.store = TicketStore(database.url)
        self.store.seed_directory("seed")
        PluginRegistry(database).register(builtin_manifest())
        with database.connect() as conn:
            people = {
                (p["name"], p["team"]): p["id"]
                for p in conn.execute("SELECT * FROM business.people").fetchall()
            }
            for name, person_name, team, role in _DEMO_ACCOUNTS:
                conn.execute(
                    "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                    "VALUES(%s,%s,%s,%s,'fixture-only-account-hash') ON CONFLICT(login) DO NOTHING",
                    (uuid4(), name, people[person_name, team], role),
                )
            account = conn.execute(
                "SELECT * FROM business.employee_accounts WHERE login=%s", (login,)
            ).fetchone()
            ref, token_hash, browser_hash = (
                uuid4(),
                hashlib.sha256(uuid4().bytes).hexdigest(),
                hashlib.sha256(uuid4().bytes).hexdigest(),
            )
            conn.execute(
                "INSERT INTO business.mcp_credentials"
                "(id,employee_id,account_revision,token_hash,expires_at) "
                "VALUES(%s,%s,%s,%s,now()+interval '1 hour')",
                (ref, account["id"], account["credential_revision"], token_hash),
            )
            conn.execute(
                "INSERT INTO business.employee_sessions"
                "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
                "VALUES(%s,%s,%s,%s,now()+interval '1 hour')",
                (browser_hash, account["id"], account["credential_revision"], "a" * 64),
            )
        args = (
            account["id"],
            account["person_id"],
            login,
            account["role"],
            account["credential_revision"],
        )
        self.identity = EmployeeIdentity(*args, "mcp_token", token_hash, ref)
        self.browser = EmployeeIdentity(*args, "employee_session", browser_hash)
        self.operations = OperationService(database)

    def read(self, command, params):
        with self.database.connect() as conn:
            if command == "directory":
                return self.store._read(conn, command, params)
            if command == "get":
                return _json(
                    {
                        "ticket": access.get_ticket(
                            conn, self.identity.employee_id, params["ticket_id"]
                        ),
                        "history": access.get_history(
                            conn, self.identity.employee_id, params["ticket_id"]
                        ),
                    }
                )
            if command == "list":
                return _json(
                    {"tickets": access.list_tickets(conn, self.identity.employee_id, params)}
                )
            receipt = access.get_receipt(conn, self.identity.employee_id, params["operation_id"])
            return {"receipt": self.store._receipt(receipt) if receipt else None}

    def prepare(self, request_id, command, params, *, grant=None):
        return self.operations.prepare(self.identity, request_id, command, params, grant=grant)

    def commit(self, operation_id, *, grant=None):
        with self.database.connect() as conn:
            existed = conn.execute(
                "SELECT operation_id FROM business.operation_receipts WHERE operation_id=%s",
                (operation_id,),
            ).fetchone()
        result = self.operations.commit(self.identity, operation_id, grant=grant)
        if result["state"] == "succeeded" and not existed:
            self.writes += 1
            if self.lose:
                self.lose = False
                raise MCPOutcomeUnknown(operation_id)
        return result

    def query_operation(self, operation_id):
        return self.operations.get(self.identity, operation_id)

    def execute(self, command, params, operation_id):
        """Fixture creation only; never passed to the production execution interface."""
        try:
            return self._fixture_execute(command, params, operation_id)
        except access.AccessError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}

    def _fixture_execute(self, command, params, operation_id):
        if command == "seed":
            return self.store.seed_directory(operation_id)
        if self.store.spec.commands[command].effect == "read":
            return {"ok": True, "data": self.read(command, params)}
        operation = self.prepare(operation_id, command, params)
        if operation["state"] == "awaiting_approval":
            self.operations.reply(
                self.browser, operation["operation_id"], operation["input_hash"], "approve"
            )
        committed = self.operations.commit(self.identity, operation["operation_id"])
        if committed["state"] != "succeeded":
            return {
                "ok": False,
                "error": {"code": committed["error_code"], "message": "Fixture denied"},
            }
        return {"ok": True, "data": committed["result"]}


def bind_task(database, task, backend):
    with database.connect() as conn:
        return conn.execute(
            "UPDATE app.tasks SET employee_id=%s,credential_ref=%s WHERE id=%s RETURNING *",
            (backend.identity.employee_id, backend.identity.credential_ref, task["id"]),
        ).fetchone()

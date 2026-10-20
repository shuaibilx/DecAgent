"""Persistent ticket lifecycle; every successful write and receipt commit together."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.codec import error
from decagent.business.contracts import PluginSpec, canonical, validate_schema
from decagent.business.tickets import builtin_manifest


class TicketError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items() if key != "workspace_id"}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


class TicketStore:
    def __init__(self, database_url):
        if not database_url:
            raise ValueError("A configured business database connection is required")
        self.database_url = database_url
        self.spec = PluginSpec.from_mapping(builtin_manifest())

    def seed_directory(self, operation_id="seed-directory-v1"):
        """Explicit local administration; never exposed in the MCP tool catalog."""
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise ValueError("Invalid directory initialization key")
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,46123))", (operation_id,)
            )
            previous = conn.execute(
                "SELECT * FROM business.operation_receipts WHERE operation_id=%s", (operation_id,)
            ).fetchone()
            if previous:
                if previous["command"] != "seed":
                    raise ValueError("Directory key conflicts with an existing business receipt")
                return {
                    "ok": True,
                    "data": {
                        **previous["result"],
                        "receipt": self._receipt(previous),
                    },
                }
            result = self._write(conn, "seed", {})
            receipt = conn.execute(
                "INSERT INTO business.operation_receipts(operation_id,input_hash,command,result) "
                "VALUES(%s,%s,'seed',%s) RETURNING *",
                (
                    operation_id,
                    hashlib.sha256(
                        canonical({"command": "seed", "params": {}}).encode()
                    ).hexdigest(),
                    Jsonb(result),
                ),
            ).fetchone()
            return {"ok": True, "data": {**result, "receipt": self._receipt(receipt)}}

    def directory(self, params=None):
        params = {} if params is None else params
        self.spec.validate("directory", params)
        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            return {"ok": True, "data": self._read(conn, "directory", params)}

    def execute(self, command, params, operation_id):
        """Compatibility for local directory setup; domain writes require employee MCP."""
        if command == "seed" and params == {}:
            return self.seed_directory(operation_id)
        if command == "directory":
            return self.directory(params)
        return error("invalid_command", "Use authenticated MCP operations for business actions")

    @staticmethod
    def _receipt(row):
        receipt = {
            key: row[key]
            for key in (
                "operation_id",
                "input_hash",
                "command",
                "resource_id",
                "revision",
                "result",
            )
        }
        if row.get("employee_id") is not None:
            receipt.update({key: row[key] for key in ("employee_id", "contract_digest")})
        return _json(receipt)

    def execute_in_transaction(self, conn, employee_id, operation_id, *, grant=None):
        """Run only a locked, authorized operation on the caller's shared transaction."""
        operation = conn.execute(
            "SELECT * FROM business.operations WHERE id=%s AND employee_id=%s FOR UPDATE",
            (operation_id, employee_id),
        ).fetchone()
        if not operation or operation["state"] != "running":
            raise TicketError("not_found", "Running employee operation required")
        if operation["agent_task_id"] is not None:
            from decagent.business.grants import GrantAuthority
            from decagent.storage.database import Database

            GrantAuthority(Database(self.database_url)).validate(
                conn, grant, employee_id, operation=operation
            )
        elif grant is not None:
            raise TicketError("invalid_grant", "External operation cannot attach an Agent task")
        if operation["contract_digest"] != self.spec.digest:
            raise TicketError("contract_changed", "Business contract changed")
        scope = conn.execute(
            "SELECT enabled,manifest_digest,revision FROM app.business_scopes WHERE id=%s "
            "FOR SHARE",
            (operation["scope_id"],),
        ).fetchone()
        if not scope or any(
            (
                not scope["enabled"],
                scope["manifest_digest"] != self.spec.digest,
                scope["revision"] != operation["scope_revision"],
            )
        ):
            raise TicketError("contract_changed", "Business registration changed")
        params = access.authorize_write(
            conn, employee_id, operation["command"], operation["params"], lock=True
        )
        if _json(params) != operation["effective_params"]:
            raise TicketError("operation_conflict", "Saved employee input changed")
        expected_hash = hashlib.sha256(
            canonical(
                {
                    "employee_id": str(employee_id),
                    "command": operation["command"],
                    "params": _json(params),
                    "contract_digest": self.spec.digest,
                }
            ).encode("utf-8")
        ).hexdigest()
        if expected_hash != operation["input_hash"]:
            raise TicketError("operation_conflict", "Saved operation digest changed")
        approval = None
        if operation["confirmation_required"]:
            approval = conn.execute(
                "SELECT * FROM business.operation_approvals WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if not approval or any(
                (
                    approval["employee_id"] != employee_id,
                    approval["decision"] != "approve",
                    approval["input_hash"] != operation["input_hash"],
                    approval["contract_digest"] != operation["contract_digest"],
                    approval["expected_revision"] != operation["expected_revision"],
                    approval["policy_version"] != "business-approval-v2",
                    approval["consumed_at"] is not None,
                    approval["expires_at"] <= datetime.now(UTC),
                )
            ):
                raise TicketError("approval_expired", "Current specific approval required")
        previous = conn.execute(
            "SELECT * FROM business.operation_receipts WHERE operation_id=%s", (operation_id,)
        ).fetchone()
        if previous:
            if any(
                previous[key] != operation[key]
                for key in ("employee_id", "input_hash", "command", "contract_digest")
            ):
                raise TicketError("operation_conflict", "Receipt input binding changed")
            return {**previous["result"], "receipt": self._receipt(previous)}
        result = self._write(
            conn,
            operation["command"],
            params,
            employee_id=employee_id,
            operation_id=operation_id,
            source=operation["source"],
        )
        ticket = result["ticket"]
        receipt = conn.execute(
            "INSERT INTO business.operation_receipts(operation_id,input_hash,command,resource_id,"
            "revision,result,employee_id,contract_digest) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                operation_id,
                operation["input_hash"],
                operation["command"],
                ticket["id"],
                ticket["revision"],
                Jsonb(result),
                employee_id,
                operation["contract_digest"],
            ),
        ).fetchone()
        if approval:
            conn.execute(
                "UPDATE business.operation_approvals SET consumed_at=now() WHERE operation_id=%s",
                (operation_id,),
            )
        data = {**result, "receipt": self._receipt(receipt)}
        validate_schema(self.spec.commands[operation["command"]].result_schema, data)
        return data

    @staticmethod
    def _get(conn, ticket_id, lock=False):
        row = conn.execute(
            "SELECT * FROM business.tickets WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (ticket_id,),
        ).fetchone()
        if not row:
            raise TicketError("not_found", "Ticket not found")
        return row

    def _read(self, conn, command, params):
        if command == "directory":
            kind = params.get("kind", "all")
            query = params.get("query", "")
            # strpos treats %, _, and shell characters as ordinary data, never wildcard SQL.
            people = []
            categories = []
            if kind in {"all", "people"}:
                people = conn.execute(
                    "SELECT * FROM business.people WHERE active AND "
                    "strpos(lower(name),lower(%s))>0 AND (%s::text IS NULL OR team=%s) "
                    "ORDER BY name,team,id",
                    (query, params.get("team"), params.get("team")),
                ).fetchall()
            if kind in {"all", "categories"}:
                categories = conn.execute(
                    "SELECT * FROM business.categories WHERE strpos(lower(name),lower(%s))>0 "
                    "ORDER BY name,id",
                    (query,),
                ).fetchall()
            return _json({"people": people, "categories": categories})
        raise TicketError("invalid_command", "Only directory administration is available here")

    def _write(self, conn, command, params, *, employee_id=None, operation_id=None, source=None):
        if command == "seed":
            namespace = UUID("2fb6c147-0c0a-4c46-aaf2-5ea71f7c2e85")
            people = [
                ("Zhang Wei", "IT Support"),
                ("Zhang Wei", "Facilities"),
                ("张伟", "IT Support"),
                ("张伟", "Facilities"),
                ("Chen Li", "Network"),
                ("王芳", "Access Management"),
            ]
            for name, team in people:
                conn.execute(
                    "INSERT INTO business.people(id,name,team) VALUES(%s,%s,%s) "
                    "ON CONFLICT(id) DO NOTHING",
                    (uuid5(namespace, name + ":" + team), name, team),
                )
            categories = ["IT Access", "Network", "Facilities", "Hardware", "Software"]
            for name in categories:
                conn.execute(
                    "INSERT INTO business.categories(id,name) VALUES(%s,%s) "
                    "ON CONFLICT(id) DO NOTHING",
                    (uuid5(namespace, "category:" + name), name),
                )
            return {"people": len(people), "categories": len(categories)}
        if command == "create":
            if not params["title"].strip():
                raise TicketError("invalid_input", "Ticket title must not be blank")
            ticket = conn.execute(
                "INSERT INTO business.tickets"
                "(id,title,description,category_id,requester_id,priority,employee_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    params["title"],
                    params.get("description", ""),
                    params["category_id"],
                    params.get("requester_id"),
                    params.get("priority", "normal"),
                    employee_id,
                ),
            ).fetchone()
        else:
            current = self._get(conn, params["ticket_id"], lock=True)
            if current["revision"] != params["expected_revision"]:
                raise TicketError("revision_conflict", "Ticket changed; fetch its latest revision")
            transitions = {
                "assign": ({"open", "assigned", "in_progress"}, "assigned"),
                "start": ({"assigned"}, "in_progress"),
                "resolve": ({"in_progress"}, "resolved"),
                "close": ({"resolved"}, "closed"),
                "reopen": ({"resolved", "closed"}, "open"),
            }
            if command in transitions:
                allowed, next_status = transitions[command]
                if current["status"] not in allowed:
                    raise TicketError("invalid_transition", "Command is invalid in current status")
            else:
                next_status = current["status"]
            assignee = current["assignee_id"]
            resolution = current["resolution"]
            priority = current["priority"]
            if command == "assign":
                person = conn.execute(
                    "SELECT id FROM business.people WHERE id=%s AND active", (params["person_id"],)
                ).fetchone()
                if not person:
                    raise TicketError("invalid_reference", "Active directory person not found")
                assignee = params["person_id"]
            if command == "resolve":
                if not params["resolution"].strip():
                    raise TicketError("invalid_input", "Provided resolution must not be blank")
                resolution = params["resolution"]
            if command == "reopen":
                assignee, resolution = None, None
            if command == "set_priority":
                priority = params["priority"]
            if command == "comment" and not params["body"].strip():
                raise TicketError("invalid_input", "Comment must not be blank")
            ticket = conn.execute(
                "UPDATE business.tickets SET status=%s,assignee_id=%s,resolution=%s,priority=%s,"
                "revision=revision+1,updated_at=now() WHERE id=%s AND revision=%s RETURNING *",
                (next_status, assignee, resolution, priority, current["id"], current["revision"]),
            ).fetchone()
        conn.execute(
            "INSERT INTO business.ticket_history(id,ticket_id,revision,command,details,"
            "employee_id,operation_id,source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                ticket["id"],
                ticket["revision"],
                command,
                Jsonb(_json(params)),
                employee_id,
                operation_id,
                source,
            ),
        )
        return {"ticket": _json(ticket)}

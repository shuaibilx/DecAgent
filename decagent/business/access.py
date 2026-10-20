"""Server-side role and resource checks for synthetic employee business actions."""

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest
from decagent.business.workspaces import LEGACY_WORKSPACE_ID, parse_workspace_id

_SPEC = PluginSpec.from_mapping(builtin_manifest())
_BASE = {"directory", "get", "list", "receipt", "create", "comment", "close", "reopen"}


class AccessError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def principal(conn, actor_employee_id, *, workspace_id=LEGACY_WORKSPACE_ID, lock=False):
    workspace_id = parse_workspace_id(workspace_id)
    row = conn.execute(
        "SELECT a.id,a.person_id,a.role,a.credential_revision,a.workspace_id "
        "FROM business.employee_accounts a JOIN business.people p ON p.id=a.person_id "
        "AND p.workspace_id=a.workspace_id WHERE a.id=%s AND a.workspace_id=%s "
        "AND a.active AND p.active" + (" FOR UPDATE OF a,p" if lock else ""),
        (actor_employee_id, workspace_id),
    ).fetchone()
    if not row:
        raise AccessError("unauthorized", "Employee account unavailable")
    return row


def allowed_commands(conn, actor_employee_id, *, workspace_id=LEGACY_WORKSPACE_ID):
    actor = principal(conn, actor_employee_id, workspace_id=workspace_id)
    if actor["role"] == "supervisor":
        return set(_SPEC.commands)
    return _BASE | ({"start", "resolve"} if actor["role"] == "handler" else set())


def _visible(actor, ticket):
    return actor["role"] == "supervisor" or (
        ticket["employee_id"] is not None
        and (
            ticket["employee_id"] == actor["id"]
            or (actor["role"] == "handler" and ticket["assignee_id"] == actor["person_id"])
        )
    )


def _ticket(conn, actor, ticket_id, *, lock=False):
    row = conn.execute(
        "SELECT * FROM business.tickets WHERE id=%s AND workspace_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (ticket_id, actor["workspace_id"]),
    ).fetchone()
    if not row or not _visible(actor, row):
        raise AccessError("not_found", "Ticket unavailable")
    return row


def authorize_write(
    conn, actor_employee_id, command, params, *, workspace_id=LEGACY_WORKSPACE_ID, lock=False
):
    actor = principal(conn, actor_employee_id, workspace_id=workspace_id, lock=lock)
    try:
        _SPEC.validate(command, params)
        if _SPEC.commands[command].effect != "write":
            raise ValueError("Write command required")
    except (ValueError, KeyError, TypeError) as exc:
        raise AccessError("invalid_input", "Input does not match business schema") from exc
    if command in {"assign", "set_priority"} and actor["role"] != "supervisor":
        raise AccessError("forbidden", "Role does not permit this action")
    effective = dict(params)
    if command == "create":
        effective["requester_id"] = actor["person_id"]
        return effective
    ticket = _ticket(conn, actor, params["ticket_id"], lock=lock)
    if ticket["employee_id"] is None:
        raise AccessError("not_found", "Ticket unavailable for new operations")
    own = ticket["employee_id"] == actor["id"]
    assigned = ticket["assignee_id"] == actor["person_id"]
    if actor["role"] != "supervisor" and (
        (command in {"close", "reopen"} and not own)
        or (command in {"start", "resolve"} and not (actor["role"] == "handler" and assigned))
    ):
        raise AccessError("forbidden", "Role does not permit this action")
    if command == "assign":
        target = conn.execute(
            "SELECT a.id FROM business.employee_accounts a JOIN business.people p "
            "ON p.id=a.person_id AND p.workspace_id=a.workspace_id "
            "WHERE a.person_id=%s AND a.workspace_id=%s AND a.active AND p.active "
            "AND a.role IN ('handler','supervisor')" + (" FOR UPDATE OF a,p" if lock else ""),
            (params["person_id"], actor["workspace_id"]),
        ).fetchone()
        if not target:
            raise AccessError("invalid_reference", "Assignable employee unavailable")
    return effective


def get_ticket(conn, actor_employee_id, ticket_id, *, workspace_id=LEGACY_WORKSPACE_ID):
    return _ticket(conn, principal(conn, actor_employee_id, workspace_id=workspace_id), ticket_id)


def get_history(conn, actor_employee_id, ticket_id, *, workspace_id=LEGACY_WORKSPACE_ID):
    get_ticket(conn, actor_employee_id, ticket_id, workspace_id=workspace_id)
    return conn.execute(
        "SELECT * FROM business.ticket_history WHERE ticket_id=%s AND workspace_id=%s "
        "ORDER BY revision",
        (ticket_id, workspace_id),
    ).fetchall()


def list_tickets(conn, actor_employee_id, params, *, workspace_id=LEGACY_WORKSPACE_ID):
    actor = principal(conn, actor_employee_id, workspace_id=workspace_id)
    _SPEC.validate("list", params)
    return conn.execute(
        "SELECT * FROM business.tickets WHERE workspace_id=%s AND "
        "(%s='supervisor' OR (employee_id IS NOT NULL AND "
        "(employee_id=%s OR (%s='handler' AND assignee_id=%s)))) "
        "AND (%s::text IS NULL OR status=%s) "
        "AND (%s::uuid IS NULL OR assignee_id=%s::uuid) "
        "ORDER BY created_at,id LIMIT %s",
        (
            actor["workspace_id"],
            actor["role"],
            actor["id"],
            actor["role"],
            actor["person_id"],
            params.get("status"),
            params.get("status"),
            params.get("person_id"),
            params.get("person_id"),
            params.get("limit", 100),
        ),
    ).fetchall()


def get_receipt(conn, actor_employee_id, operation_id, *, workspace_id=LEGACY_WORKSPACE_ID):
    actor = principal(conn, actor_employee_id, workspace_id=workspace_id)
    row = conn.execute(
        "SELECT * FROM business.operation_receipts WHERE operation_id=%s AND workspace_id=%s",
        (operation_id, actor["workspace_id"]),
    ).fetchone()
    if row and row["employee_id"] != actor["id"] and actor["role"] != "supervisor":
        raise AccessError("not_found", "Operation unavailable")
    return row

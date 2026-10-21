"""Isolated anonymous browser sessions for synthetic business workspaces."""

import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4, uuid5

from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.workspaces import GUEST_INACTIVITY_SECONDS

COOKIE_NAME = "decagent_guest_session"
CSRF_COOKIE_NAME = "decagent_guest_csrf"
_GUEST_PREFIX = "dcg_"
_SESSION_SECONDS = 7 * 24 * 60 * 60
_INACTIVITY_SECONDS = GUEST_INACTIVITY_SECONDS
_PEOPLE = (
    ("Zhang Wei", "IT Support"),
    ("Zhang Wei", "Facilities"),
    ("张伟", "IT Support"),
    ("张伟", "Facilities"),
    ("Chen Li", "Network"),
    ("王芳", "Access Management"),
)
_CATEGORIES = ("IT Access", "Network", "Facilities", "Hardware", "Software")
_ACCOUNTS = (
    ("employee-it", "张伟", "IT Support", "employee"),
    ("employee-facilities", "张伟", "Facilities", "employee"),
    ("handler-it", "Zhang Wei", "IT Support", "handler"),
    ("handler-facilities", "Zhang Wei", "Facilities", "handler"),
    ("handler-access", "王芳", "Access Management", "handler"),
    ("supervisor", "Chen Li", "Network", "supervisor"),
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedGuestSession:
    token: str
    csrf_token: str
    expires_at: datetime
    identity: EmployeeIdentity
    created: bool


@dataclass(frozen=True)
class GuestRoleSwitch:
    csrf_token: str
    expires_at: datetime
    identity: EmployeeIdentity


class GuestAuth:
    """Resolve guest identity only from an unguessable, database-backed cookie."""

    def __init__(self, database, origin, secure, *, session_seconds=_SESSION_SECONDS):
        self.database = database
        self.origin = origin
        self.secure = secure
        self.session_seconds = session_seconds

    def valid_origin(self, request):
        if request.headers.get("Origin") != self.origin:
            return False
        if self.secure:
            return request.url.scheme == "https"
        origin = urlsplit(self.origin)
        if origin.scheme != "http" or not _is_loopback(origin.hostname):
            return False
        if request.url.scheme != "http" or not _is_loopback(request.url.hostname):
            return False
        return True

    @staticmethod
    def is_passive_activity(request):
        path = request.url.path
        return (
            request.method in {"HEAD", "OPTIONS"}
            or path in {"/api/v3/guest/status", "/api/v3/guest/session"}
            or path.endswith("/events")
        )

    def authenticate_browser(self, request):
        if "authorization" in request.headers:
            return None
        token = request.cookies.get(COOKIE_NAME, "")
        if not token.startswith(_GUEST_PREFIX) or len(token) > 512:
            return None
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT s.token_hash,s.workspace_id,s.account_revision,a.id,a.person_id,"
                "a.login,a.role,a.credential_revision "
                "FROM business.guest_sessions s "
                "JOIN business.workspaces w ON w.id=s.workspace_id AND w.active "
                "JOIN business.employee_sessions e ON e.token_hash=s.token_hash "
                "AND e.workspace_id=s.workspace_id AND e.employee_id=s.employee_id "
                "AND e.account_revision=s.account_revision AND e.csrf_hash=s.csrf_hash "
                "AND e.revoked_at IS NULL AND e.expires_at>now() "
                "JOIN business.employee_accounts a ON a.id=s.employee_id "
                "AND a.workspace_id=s.workspace_id "
                "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND s.last_activity_at>now()-%s*interval '1 second' "
                "AND a.active=true AND s.account_revision=a.credential_revision",
                (_hash(token), _INACTIVITY_SECONDS),
            ).fetchone()
        if not row:
            return None
        return EmployeeIdentity(
            row["id"],
            row["person_id"],
            row["login"],
            row["role"],
            row["credential_revision"],
            "employee_session",
            row["token_hash"],
            workspace_id=row["workspace_id"],
        )

    def valid_clear_replay(self, request):
        """Allow a lost clear response to be retried without restoring access."""
        if (
            request.method != "DELETE"
            or request.url.path != "/api/v3/guest/workspace"
            or not self.valid_origin(request)
        ):
            return False
        token = request.cookies.get(COOKIE_NAME, "")
        csrf = request.headers.get("X-CSRF-Token", "")
        if (
            not token.startswith(_GUEST_PREFIX)
            or len(token) > 512
            or not csrf
            or len(csrf) > 512
        ):
            return False
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT t.csrf_hash FROM business.guest_workspace_tombstones t "
                "JOIN business.workspaces w ON w.id=t.workspace_id "
                "AND w.kind='guest' AND NOT w.active "
                "WHERE t.token_hash=%s",
                (_hash(token),),
            ).fetchone()
        return bool(row and hmac.compare_digest(row["csrf_hash"], _hash(csrf)))

    def valid_mutation(self, request, identity):
        if identity.source != "employee_session" or not self.valid_origin(request):
            return False
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or len(csrf) > 512:
            return False
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT s.csrf_hash FROM business.guest_sessions s "
                "JOIN business.workspaces w ON w.id=s.workspace_id AND w.active "
                "JOIN business.employee_sessions e ON e.token_hash=s.token_hash "
                "AND e.workspace_id=s.workspace_id AND e.employee_id=s.employee_id "
                "AND e.account_revision=s.account_revision AND e.csrf_hash=s.csrf_hash "
                "AND e.revoked_at IS NULL AND e.expires_at>now() "
                "JOIN business.employee_accounts a ON a.id=s.employee_id "
                "AND a.workspace_id=s.workspace_id "
                "WHERE s.token_hash=%s AND s.workspace_id=%s AND s.employee_id=%s "
                "AND s.account_revision=%s AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND s.last_activity_at>now()-%s*interval '1 second' "
                "AND a.active=true AND a.credential_revision=s.account_revision",
                (
                    identity.token_hash,
                    identity.workspace_id,
                    identity.employee_id,
                    identity.account_revision,
                    _INACTIVITY_SECONDS,
                ),
            ).fetchone()
        return bool(row and hmac.compare_digest(row["csrf_hash"], _hash(csrf)))

    def issue_or_restore(self, request):
        token = request.cookies.get(COOKIE_NAME, "")
        identity = self.authenticate_browser(request)
        if identity is not None:
            csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
            with self.database.connect() as conn:
                row = conn.execute(
                    "SELECT csrf_hash,expires_at FROM business.guest_sessions "
                    "WHERE token_hash=%s AND workspace_id=%s AND revoked_at IS NULL "
                    "AND expires_at>now() FOR UPDATE",
                    (identity.token_hash, identity.workspace_id),
                ).fetchone()
                if row and csrf and hmac.compare_digest(row["csrf_hash"], _hash(csrf)):
                    return IssuedGuestSession(token, csrf, row["expires_at"], identity, False)
                csrf = secrets.token_urlsafe(32)
                if row:
                    conn.execute(
                        "UPDATE business.guest_sessions SET csrf_hash=%s "
                        "WHERE token_hash=%s AND workspace_id=%s AND revoked_at IS NULL "
                        "AND expires_at>now()",
                        (_hash(csrf), identity.token_hash, identity.workspace_id),
                    )
                    conn.execute(
                        "UPDATE business.employee_sessions SET csrf_hash=%s "
                        "WHERE token_hash=%s AND workspace_id=%s AND revoked_at IS NULL "
                        "AND expires_at>now()",
                        (_hash(csrf), identity.token_hash, identity.workspace_id),
                    )
                    return IssuedGuestSession(token, csrf, row["expires_at"], identity, False)

        token = _GUEST_PREFIX + secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        workspace_id = uuid4()
        slug = "guest-" + workspace_id.hex
        employee_id = uuid5(workspace_id, "account:employee-it")
        person_ids = {
            (name, team): uuid5(workspace_id, f"person:{name}:{team}")
            for name, team in _PEOPLE
        }
        with self.database.connect() as conn:
            conn.execute(
                "INSERT INTO business.workspaces(id,slug,kind) VALUES(%s,%s,'guest')",
                (workspace_id, slug),
            )
            for name, team in _PEOPLE:
                conn.execute(
                    "INSERT INTO business.people(id,name,team,workspace_id) "
                    "VALUES(%s,%s,%s,%s)",
                    (person_ids[(name, team)], name, team, workspace_id),
                )
            for category in _CATEGORIES:
                conn.execute(
                    "INSERT INTO business.categories(id,name,workspace_id) VALUES(%s,%s,%s)",
                    (uuid5(workspace_id, "category:" + category), category, workspace_id),
                )
            for key, name, team, role in _ACCOUNTS:
                account_id = uuid5(workspace_id, "account:" + key)
                login = f"g-{workspace_id.hex[:12]}-{key}"
                conn.execute(
                    "INSERT INTO business.employee_accounts"
                    "(id,login,person_id,role,password_hash,workspace_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        account_id,
                        login,
                        person_ids[(name, team)],
                        role,
                        "guest_session_only$" + secrets.token_urlsafe(24),
                        workspace_id,
                    ),
                )
            account = conn.execute(
                "SELECT id,person_id,login,role,credential_revision FROM "
                "business.employee_accounts WHERE id=%s AND workspace_id=%s",
                (employee_id, workspace_id),
            ).fetchone()
            conn.execute(
                "INSERT INTO business.guest_sessions"
                "(token_hash,workspace_id,employee_id,account_revision,csrf_hash,expires_at) "
                "VALUES(%s,%s,%s,%s,%s,now()+%s*interval '1 second')",
                (
                    _hash(token),
                    workspace_id,
                    employee_id,
                    account["credential_revision"],
                    _hash(csrf),
                    self.session_seconds,
                ),
            )
            conn.execute(
                "INSERT INTO business.employee_sessions"
                "(token_hash,employee_id,workspace_id,account_revision,csrf_hash,expires_at) "
                "SELECT %s,%s,%s,%s,%s,expires_at FROM business.guest_sessions "
                "WHERE token_hash=%s AND workspace_id=%s",
                (
                    _hash(token),
                    employee_id,
                    workspace_id,
                    account["credential_revision"],
                    _hash(csrf),
                    _hash(token),
                    workspace_id,
                ),
            )
            session = conn.execute(
                "SELECT expires_at FROM business.guest_sessions WHERE token_hash=%s",
                (_hash(token),),
            ).fetchone()
        identity = EmployeeIdentity(
            account["id"],
            account["person_id"],
            account["login"],
            account["role"],
            account["credential_revision"],
            "employee_session",
            _hash(token),
            workspace_id=workspace_id,
        )
        return IssuedGuestSession(token, csrf, session["expires_at"], identity, True)

    def switch_role(self, identity, role_key):
        role = next((item for item in _ACCOUNTS if item[0] == role_key), None)
        if role is None:
            raise ValueError("Guest role is not available")
        with self.database.connect() as conn:
            session = conn.execute(
                "SELECT s.workspace_id,s.employee_id,s.expires_at "
                "FROM business.guest_sessions s "
                "JOIN business.workspaces w ON w.id=s.workspace_id AND w.active "
                "JOIN business.employee_sessions e ON e.token_hash=s.token_hash "
                "AND e.workspace_id=s.workspace_id AND e.employee_id=s.employee_id "
                "AND e.account_revision=s.account_revision AND e.csrf_hash=s.csrf_hash "
                "AND e.revoked_at IS NULL AND e.expires_at>now() "
                "JOIN business.employee_accounts a ON a.id=s.employee_id "
                "AND a.workspace_id=s.workspace_id AND a.active "
                "AND a.credential_revision=s.account_revision "
                "WHERE s.token_hash=%s AND s.workspace_id=%s AND s.employee_id=%s "
                "AND s.account_revision=%s AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND s.last_activity_at>now()-%s*interval '1 second' "
                "FOR UPDATE OF s,e",
                (
                    identity.token_hash,
                    identity.workspace_id,
                    identity.employee_id,
                    identity.account_revision,
                    _INACTIVITY_SECONDS,
                ),
            ).fetchone()
            if not session:
                return None
            workspace_id = session["workspace_id"]
            target_login = f"g-{workspace_id.hex[:12]}-{role_key}"
            account = conn.execute(
                "SELECT a.id,a.person_id,a.login,a.role,a.credential_revision "
                "FROM business.employee_accounts a WHERE a.workspace_id=%s "
                "AND a.login=%s AND a.active FOR SHARE",
                (workspace_id, target_login),
            ).fetchone()
            if not account:
                return None
            csrf = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE business.guest_sessions SET employee_id=%s,account_revision=%s,"
                "csrf_hash=%s WHERE token_hash=%s AND workspace_id=%s",
                (
                    account["id"],
                    account["credential_revision"],
                    _hash(csrf),
                    identity.token_hash,
                    workspace_id,
                ),
            )
            conn.execute(
                "UPDATE business.employee_sessions SET employee_id=%s,account_revision=%s,"
                "csrf_hash=%s WHERE token_hash=%s AND workspace_id=%s",
                (
                    account["id"],
                    account["credential_revision"],
                    _hash(csrf),
                    identity.token_hash,
                    workspace_id,
                ),
            )
        switched_identity = EmployeeIdentity(
            account["id"],
            account["person_id"],
            account["login"],
            account["role"],
            account["credential_revision"],
            "employee_session",
            identity.token_hash,
            workspace_id=workspace_id,
        )
        return GuestRoleSwitch(csrf, session["expires_at"], switched_identity)

    def revoke(self, identity):
        with self.database.connect() as conn:
            session = conn.execute(
                "SELECT token_hash,workspace_id,employee_id FROM business.guest_sessions "
                "WHERE token_hash=%s AND workspace_id=%s AND employee_id=%s "
                "AND revoked_at IS NULL AND expires_at>now() "
                "AND last_activity_at>now()-%s*interval '1 second' FOR UPDATE",
                (
                    identity.token_hash,
                    identity.workspace_id,
                    identity.employee_id,
                    _INACTIVITY_SECONDS,
                ),
            ).fetchone()
            if session is None:
                return False
            conn.execute(
                "UPDATE business.guest_sessions SET revoked_at=now() "
                "WHERE token_hash=%s AND workspace_id=%s AND revoked_at IS NULL",
                (session["token_hash"], session["workspace_id"]),
            )
            conn.execute(
                "UPDATE business.employee_sessions SET revoked_at=now() "
                "WHERE token_hash=%s AND workspace_id=%s AND revoked_at IS NULL",
                (session["token_hash"], session["workspace_id"]),
            )
            return True

    def touch(self, identity):
        with self.database.connect() as conn:
            session = conn.execute(
                "UPDATE business.guest_sessions SET last_activity_at=now(),"
                "expires_at=now()+%s*interval '1 second' WHERE token_hash=%s "
                "AND workspace_id=%s AND revoked_at IS NULL AND expires_at>now() "
                "AND last_activity_at>now()-%s*interval '1 second' "
                "AND EXISTS (SELECT 1 FROM business.workspaces w "
                "WHERE w.id=business.guest_sessions.workspace_id AND w.active) "
                "RETURNING token_hash,workspace_id,employee_id,account_revision,"
                "csrf_hash,expires_at",
                (
                    _SESSION_SECONDS,
                    identity.token_hash,
                    identity.workspace_id,
                    _INACTIVITY_SECONDS,
                ),
            ).fetchone()
            if session is None:
                return None
            employee = conn.execute(
                "UPDATE business.employee_sessions SET expires_at=%s "
                "WHERE token_hash=%s AND workspace_id=%s AND employee_id=%s "
                "AND account_revision=%s AND csrf_hash=%s AND revoked_at IS NULL "
                "RETURNING expires_at",
                (
                    session["expires_at"],
                    session["token_hash"],
                    session["workspace_id"],
                    session["employee_id"],
                    session["account_revision"],
                    session["csrf_hash"],
                ),
            ).fetchone()
            if employee is None:
                raise RuntimeError("Guest and employee session renewal diverged")
            return session["expires_at"]

    def clear_workspace(self, identity):
        with self.database.connect() as conn:
            workspace = conn.execute(
                "SELECT id,active FROM business.workspaces WHERE id=%s AND kind='guest' "
                "FOR UPDATE",
                (identity.workspace_id,),
            ).fetchone()
            if workspace is None:
                return False
            if not workspace["active"]:
                return True
            session = conn.execute(
                "SELECT csrf_hash FROM business.guest_sessions WHERE token_hash=%s "
                "AND workspace_id=%s",
                (identity.token_hash, workspace["id"]),
            ).fetchone()
            if session is None:
                return False
            conn.execute(
                "INSERT INTO business.guest_workspace_tombstones"
                "(token_hash,workspace_id,csrf_hash) VALUES(%s,%s,%s) "
                "ON CONFLICT(token_hash) DO NOTHING",
                (identity.token_hash, workspace["id"], session["csrf_hash"]),
            )
            conn.execute(
                "UPDATE business.workspaces SET active=false WHERE id=%s",
                (workspace["id"],),
            )
            conn.execute(
                "UPDATE business.guest_sessions SET revoked_at=coalesce(revoked_at,now()) "
                "WHERE workspace_id=%s",
                (workspace["id"],),
            )
            conn.execute(
                "UPDATE business.employee_sessions SET revoked_at=coalesce(revoked_at,now()) "
                "WHERE workspace_id=%s AND token_hash IN ("
                "SELECT token_hash FROM business.guest_sessions WHERE workspace_id=%s)",
                (workspace["id"], workspace["id"]),
            )
            conn.execute(
                "UPDATE business.mcp_credentials SET revoked_at=coalesce(revoked_at,now()) "
                "WHERE workspace_id=%s",
                (workspace["id"],),
            )
            conn.execute(
                "UPDATE business.employee_accounts SET active=false "
                "WHERE workspace_id=%s AND active=true",
                (workspace["id"],),
            )
            return True

    def role_options(self):
        return [
            {"key": key, "name": name, "team": team, "role": role}
            for key, name, team, role in _ACCOUNTS
        ]

    @staticmethod
    def current_role_key(identity):
        if identity.workspace_id is None:
            return None
        prefix = f"g-{identity.workspace_id.hex[:12]}-"
        if not identity.login.startswith(prefix):
            return None
        role_key = identity.login[len(prefix) :]
        return role_key if role_key in {item[0] for item in _ACCOUNTS} else None

    def csrf_for(self, identity, csrf_cookie):
        if identity.source != "employee_session" or not csrf_cookie or len(csrf_cookie) > 512:
            return None
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT s.csrf_hash FROM business.guest_sessions s "
                "JOIN business.workspaces w ON w.id=s.workspace_id AND w.active "
                "JOIN business.employee_sessions e ON e.token_hash=s.token_hash "
                "AND e.workspace_id=s.workspace_id AND e.employee_id=s.employee_id "
                "AND e.account_revision=s.account_revision AND e.csrf_hash=s.csrf_hash "
                "AND e.revoked_at IS NULL AND e.expires_at>now() "
                "WHERE s.token_hash=%s AND s.workspace_id=%s AND s.employee_id=%s "
                "AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND s.last_activity_at>now()-%s*interval '1 second'",
                (
                    identity.token_hash,
                    identity.workspace_id,
                    identity.employee_id,
                    _INACTIVITY_SECONDS,
                ),
            ).fetchone()
        if row and hmac.compare_digest(row["csrf_hash"], _hash(csrf_cookie)):
            return csrf_cookie
        return None


def _is_loopback(hostname):
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False

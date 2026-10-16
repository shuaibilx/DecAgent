"""Database-backed synthetic employee browser and MCP authentication."""

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

COOKIE_NAME = "decagent_employee_session"
_BROWSER_PREFIX = "dce_"
_MCP_PREFIX = "dcm_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmployeeIdentity:
    employee_id: UUID
    person_id: UUID
    login: str
    role: str
    account_revision: int
    source: str
    token_hash: str
    credential_ref: UUID | None = None

    def public(self):
        return {
            "employee_id": str(self.employee_id),
            "person_id": str(self.person_id),
            "login": self.login,
            "role": self.role,
            "source": self.source,
        }


class EmployeeAuth:
    """Only server-side rows establish employee identity and credential audience."""

    def __init__(
        self, database, origin, secure, credential_dir, *, session_seconds=43200, mcp_seconds=3600
    ):
        from decagent.gateway.auth import credential_path

        self.database = database
        self.origin = origin
        self.secure = secure
        self.credential_dir = credential_path(credential_dir)
        self.session_seconds = session_seconds
        self.mcp_seconds = mcp_seconds

    def valid_origin(self, request):
        return request.headers.get("Origin") == self.origin and (
            not self.secure or request.url.scheme == "https"
        )

    def login(self, login: str, password: str):
        from decagent.business.employee_accounts import (
            get_employee_by_login,
            verify_employee_password,
        )

        account = get_employee_by_login(self.database, login)
        if not account or not account["active"]:
            return None
        if not verify_employee_password(password, account["password_hash"]):
            return None
        token = _BROWSER_PREFIX + secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        with self.database.connect() as conn:
            row = conn.execute(
                "INSERT INTO business.employee_sessions"
                "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
                "SELECT %s,id,credential_revision,%s,now()+%s*interval '1 second' "
                "FROM business.employee_accounts WHERE id=%s AND active=true "
                "AND credential_revision=%s RETURNING expires_at",
                (
                    _hash(token),
                    _hash(csrf),
                    self.session_seconds,
                    account["id"],
                    account["credential_revision"],
                ),
            ).fetchone()
        return (token, csrf, row["expires_at"]) if row else None

    def authenticate_browser(self, request):
        if "authorization" in request.headers:
            return None
        token = request.cookies.get(COOKIE_NAME, "")
        if not token.startswith(_BROWSER_PREFIX) or len(token) > 512:
            return None
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT s.token_hash,a.id,a.person_id,a.login,a.role,a.credential_revision "
                "FROM business.employee_sessions s "
                "JOIN business.employee_accounts a ON a.id=s.employee_id "
                "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND a.active=true AND s.account_revision=a.credential_revision",
                (_hash(token),),
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
        )

    def valid_mutation(self, request, identity):
        if identity.source != "employee_session" or not self.valid_origin(request):
            return False
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or len(csrf) > 512:
            return False
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT s.csrf_hash FROM business.employee_sessions s "
                "JOIN business.employee_accounts a ON a.id=s.employee_id "
                "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() "
                "AND a.active=true AND s.account_revision=a.credential_revision",
                (identity.token_hash,),
            ).fetchone()
        return bool(row and hmac.compare_digest(row["csrf_hash"], _hash(csrf)))

    def revoke_browser(self, identity):
        if identity.source != "employee_session":
            raise ValueError("Employee browser session required")
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE business.employee_sessions SET revoked_at=now() WHERE token_hash=%s",
                (identity.token_hash,),
            )

    def issue_mcp(self, identity):
        from decagent.gateway.auth import _protect_file

        if identity.source != "employee_session":
            raise ValueError("Employee browser session required")
        credential_ref = uuid4()
        token = _MCP_PREFIX + secrets.token_urlsafe(48)
        path = self._path(credential_ref)
        created = False
        try:
            with self.database.connect() as conn:
                session = conn.execute(
                    "SELECT s.employee_id FROM business.employee_sessions s "
                    "JOIN business.employee_accounts a ON a.id=s.employee_id "
                    "WHERE s.token_hash=%s AND s.employee_id=%s "
                    "AND s.revoked_at IS NULL AND s.expires_at>now() AND a.active "
                    "AND s.account_revision=a.credential_revision "
                    "AND a.credential_revision=%s FOR UPDATE OF s,a",
                    (identity.token_hash, identity.employee_id, identity.account_revision),
                ).fetchone()
                if not session:
                    raise ValueError("Employee session unavailable")
                row = conn.execute(
                    "INSERT INTO business.mcp_credentials"
                    "(id,token_hash,employee_id,account_revision,expires_at) "
                    "SELECT %s,%s,id,credential_revision,now()+%s*interval '1 second' "
                    "FROM business.employee_accounts WHERE id=%s AND active=true "
                    "AND credential_revision=%s RETURNING expires_at",
                    (
                        credential_ref,
                        _hash(token),
                        self.mcp_seconds,
                        identity.employee_id,
                        identity.account_revision,
                    ),
                ).fetchone()
                if not row:
                    raise ValueError("Employee account unavailable")
                self._protect_directory()
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created = True
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                    _protect_file(path)
                    output.write(token + "\n")
                    output.flush()
                    os.fsync(output.fileno())
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
        return {
            "credential_ref": str(credential_ref),
            "credential_path": str(path),
            "expires_at": row["expires_at"].isoformat(),
        }

    def authenticate_mcp_token(self, token):
        if type(token) is not str or not token.startswith(_MCP_PREFIX) or len(token) > 512:
            return None
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT c.id AS credential_ref,c.token_hash,a.id,a.person_id,a.login,a.role,"
                "a.credential_revision FROM business.mcp_credentials c "
                "JOIN business.employee_accounts a ON a.id=c.employee_id "
                "WHERE c.token_hash=%s AND c.revoked_at IS NULL AND c.expires_at>now() "
                "AND a.active=true AND c.account_revision=a.credential_revision",
                (_hash(token),),
            ).fetchone()
        if not row:
            return None
        return EmployeeIdentity(
            row["id"],
            row["person_id"],
            row["login"],
            row["role"],
            row["credential_revision"],
            "mcp_token",
            row["token_hash"],
            row["credential_ref"],
        )

    def revoke_mcp(self, identity, credential_ref):
        if identity.source != "employee_session":
            raise ValueError("Employee browser session required")
        ref = UUID(str(credential_ref))
        with self.database.connect() as conn:
            row = conn.execute(
                "UPDATE business.mcp_credentials SET revoked_at=COALESCE(revoked_at,now()) "
                "WHERE id=%s AND employee_id=%s RETURNING id",
                (ref, identity.employee_id),
            ).fetchone()
        if not row:
            return False
        self._path(ref).unlink(missing_ok=True)
        return True

    def resolve_credential_ref(self, task_id):
        """Trusted broker use only: resolve a saved task principal to its protected token."""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT t.kind,t.employee_id AS task_employee_id,t.credential_ref,"
                "c.employee_id AS credential_employee_id,c.token_hash,c.account_revision,"
                "c.expires_at,c.revoked_at,a.active,a.credential_revision "
                "FROM app.tasks t LEFT JOIN business.mcp_credentials c ON c.id=t.credential_ref "
                "LEFT JOIN business.employee_accounts a ON a.id=c.employee_id WHERE t.id=%s",
                (UUID(str(task_id)),),
            ).fetchone()
        if (
            not row
            or row["kind"] != "business"
            or not row["credential_ref"]
            or not row["task_employee_id"]
            or row["task_employee_id"] != row["credential_employee_id"]
            or not row["active"]
            or row["account_revision"] != row["credential_revision"]
            or row["revoked_at"] is not None
            or row["expires_at"] is None
            or row["expires_at"] <= datetime.now(UTC)
        ):
            raise ValueError("Employee credential unavailable for task")
        path = self._path(row["credential_ref"])
        from decagent.gateway.auth import credential_path

        credential_path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Employee credential unavailable for task")
        with path.open("r", encoding="utf-8") as input_file:
            token = input_file.read(513).rstrip("\n")
        if (
            not token.startswith(_MCP_PREFIX)
            or len(token) > 512
            or not hmac.compare_digest(_hash(token), row["token_hash"])
        ):
            raise ValueError("Employee credential unavailable for task")
        return token

    def _path(self, credential_ref):
        return self.credential_dir / f"mcp-{UUID(str(credential_ref))}.token"

    def _protect_directory(self):
        from decagent.business.employee_accounts import (
            _assert_private_windows_directory,
            _replace_windows_directory_acl,
        )
        from decagent.gateway.auth import _current_windows_sid, credential_path

        credential_path(self.credential_dir)
        self.credential_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential_path(self.credential_dir)
        if os.name == "nt":
            _replace_windows_directory_acl(self.credential_dir, _current_windows_sid())
            _assert_private_windows_directory(self.credential_dir)
        else:
            self.credential_dir.chmod(0o700)

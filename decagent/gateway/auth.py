"""Single-owner credentials and expiring browser sessions; no registration or rotation."""

import csv
import hashlib
import hmac
import io
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from decagent.policy import Provenance, Source

COOKIE_NAME = "decagent_session"


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _protect_file(path: Path):
    if os.name != "nt":
        path.chmod(0o600)
        return
    # Restrict the empty file before writing any credential. No shell interpolation.
    sid = _current_windows_sid()
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)", "*S-1-5-18:(F)"],
        check=True,
        capture_output=True,
    )


def _current_windows_sid() -> str:
    """Return the caller SID without relying on localized account names."""
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    sid = next(csv.reader(io.StringIO(identity.stdout)))[1]
    if not sid.startswith("S-1-"):
        raise ValueError("Cannot establish owner ACL")
    return sid


def credential_path(path):
    """Reject links and Windows reparse points before resolving credential paths."""
    candidate = Path(path).expanduser().absolute()
    for part in (candidate, *candidate.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Credential path must not contain a link or reparse point")
    return candidate


def initialize_owner(database, credential_path: Path | str) -> Path:
    """Return only the protected file path; refuse overwriting either credential or file."""
    path = Path(credential_path).expanduser().resolve()
    created = False
    try:
        with database.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7731410)")
            if conn.execute("SELECT singleton FROM app.owner_credentials").fetchone():
                raise ValueError("Owner is already initialized; existing credential is preserved")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                _protect_file(path)
                token = "dca_" + secrets.token_urlsafe(48)
                output.write(token + "\n")
                output.flush()
                os.fsync(output.fileno())
            conn.execute(
                "INSERT INTO app.owner_credentials(singleton,token_hash) VALUES(true,%s)",
                (token_hash(token),),
            )
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return path


@dataclass(frozen=True)
class OwnerIdentity:
    session_id: str
    bearer: bool
    owner_id: str = "owner"

    @property
    def provenance(self):
        return Provenance(Source.OWNER, self.owner_id, self.session_id, True)


class OwnerAuth:
    def __init__(self, database, public_url, *, session_seconds=43200):
        self.database = database
        parts = urlsplit(public_url)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password:
            raise ValueError("Invalid workbench origin")
        if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Remote workbench requires HTTPS")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError("Public URL must be an exact origin without a path")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.secure = parts.scheme == "https"
        self.session_seconds = session_seconds

    def valid_owner_token(self, token):
        if type(token) is not str or not 32 <= len(token) <= 512:
            return False
        with self.database.connect() as conn:
            record = conn.execute("SELECT token_hash FROM app.owner_credentials").fetchone()
        return bool(record and hmac.compare_digest(record["token_hash"], token_hash(token)))

    def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not self.valid_owner_token(token):
                return None
            return OwnerIdentity("bearer:" + token_hash(token), True)
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or len(token) > 512:
            return None
        with self.database.connect() as conn:
            record = conn.execute(
                "SELECT token_hash FROM app.owner_sessions WHERE token_hash=%s "
                "AND revoked_at IS NULL AND expires_at>now()",
                (token_hash(token),),
            ).fetchone()
        return OwnerIdentity(record["token_hash"], False) if record else None

    def valid_origin(self, request):
        return request.headers.get("Origin") == self.origin and (
            not self.secure or request.url.scheme == "https"
        )

    def valid_mutation(self, request, identity):
        if identity.bearer:
            # Browser cross-origin fetches can never use the bearer mutation exception.
            origin = request.headers.get("Origin")
            return origin is None or self.valid_origin(request)
        if not self.valid_origin(request):
            return False
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or len(csrf) > 512:
            return False
        with self.database.connect() as conn:
            record = conn.execute(
                "SELECT csrf_hash FROM app.owner_sessions WHERE token_hash=%s "
                "AND revoked_at IS NULL AND expires_at>now()",
                (identity.session_id,),
            ).fetchone()
        return bool(record and hmac.compare_digest(record["csrf_hash"], token_hash(csrf)))

    def session(self, token):
        if not self.valid_owner_token(token):
            return None
        session, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        with self.database.connect() as conn:
            row = conn.execute(
                "INSERT INTO app.owner_sessions(token_hash,csrf_hash,expires_at) "
                "VALUES(%s,%s,now()+%s*interval '1 second') RETURNING expires_at",
                (token_hash(session), token_hash(csrf), self.session_seconds),
            ).fetchone()
        return session, csrf, row["expires_at"]

    def revoke(self, identity):
        if not identity.bearer:
            with self.database.connect() as conn:
                conn.execute(
                    "UPDATE app.owner_sessions SET revoked_at=now() WHERE token_hash=%s",
                    (identity.session_id,),
                )

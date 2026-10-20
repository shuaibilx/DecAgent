"""Explicit initialization of synthetic employee accounts."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from pathlib import Path
from uuid import uuid4

from decagent.gateway.auth import _current_windows_sid, _protect_file, credential_path

_PBKDF2_ITERATIONS = 600_000
_SEED_LOCK = 7731422
_WINDOWS_ACL_READ = (
    "$ErrorActionPreference='Stop';"
    "$acl=Get-Acl -LiteralPath $env:DECAGENT_ACL_CHECK_PATH;"
    "$rules=@($acl.GetAccessRules($true,$true,"
    "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object {"
    "[pscustomobject]@{sid=$_.IdentityReference.Value;inherited=$_.IsInherited;"
    "type=$_.AccessControlType.ToString();rights=$_.FileSystemRights.ToString()}});"
    "[pscustomobject]@{protected=$acl.AreAccessRulesProtected;rules=$rules}"
    " | ConvertTo-Json -Compress -Depth 4"
)
_DEMO_ACCOUNTS = (
    ("employee-it", "张伟", "IT Support", "employee"),
    ("employee-facilities", "张伟", "Facilities", "employee"),
    ("handler-it", "Zhang Wei", "IT Support", "handler"),
    ("handler-facilities", "Zhang Wei", "Facilities", "handler"),
    ("handler-access", "王芳", "Access Management", "handler"),
    ("supervisor", "Chen Li", "Network", "supervisor"),
)


def hash_employee_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_employee_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, rounds, encoded_salt, encoded_digest = password_hash.split("$")
        if algorithm != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 5_000_000:
            return False
        salt = base64.b64decode(encoded_salt, validate=True)
        expected = base64.b64decode(encoded_digest, validate=True)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (AttributeError, TypeError, ValueError):
        return False


def get_employee_by_login(database, login: str):
    """Return a trusted account row for server-side authentication or None."""
    with database.connect() as conn:
        return conn.execute(
            "SELECT id,login,person_id,role,password_hash,active,credential_revision,workspace_id "
            "FROM business.employee_accounts WHERE login=%s",
            (login,),
        ).fetchone()


def get_employee_by_id(database, employee_id):
    """Reload current account state for sessions and credential checks."""
    with database.connect() as conn:
        return conn.execute(
            "SELECT id,login,person_id,role,password_hash,active,credential_revision,workspace_id "
            "FROM business.employee_accounts WHERE id=%s",
            (employee_id,),
        ).fetchone()


def _windows_acl_environment(path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DECAGENT_ACL_CHECK_PATH"] = str(path)
    # A process launched from PowerShell 7 can pass its module path into Windows
    # PowerShell 5.1; its Security module then fails to load due to duplicate types.
    environment["PSModulePath"] = str(
        Path(environment.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
    )
    return environment


def _read_windows_acl(path: Path) -> dict:
    """Read the directory DACL in SID form so localized names cannot affect checks."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL_READ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=_windows_acl_environment(path),
    )
    return json.loads(result.stdout)


def _replace_windows_directory_acl(path: Path, owner_sid: str) -> None:
    """Atomically replace all prior grants before any password is written."""
    import ctypes
    from ctypes import wintypes

    if not re.fullmatch(r"S-\d+(?:-\d+)+", owner_sid):
        raise ValueError("Invalid Windows owner SID")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{owner_sid})"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # DACL only; changing owner or SACL would require unnecessary privileges.
        if not set_security(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _windows_acl_is_private(acl: dict, owner_sid: str) -> bool:
    """Require a protected DACL granting only this account and LocalSystem."""
    if not isinstance(acl, dict) or acl.get("protected") is not True:
        return False
    rules = acl.get("rules")
    if not isinstance(rules, list):
        return False
    full_control = set()
    allowed = {owner_sid, "S-1-5-18"}
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        sid = rule.get("sid")
        if sid not in allowed or rule.get("inherited") is not False or rule.get("type") != "Allow":
            return False
        if rule.get("rights") == "FullControl":
            full_control.add(sid)
    return full_control == allowed


def _assert_private_windows_directory(path: Path) -> None:
    if not _windows_acl_is_private(_read_windows_acl(path), _current_windows_sid()):
        raise ValueError("Demo credential directory ACL is not private; remove explicit grants")


def seed_employee_accounts(database, credential_dir):
    """Create only missing demo accounts, returning names without credential material."""
    directory = credential_path(credential_dir)
    created_paths = []
    created = []
    preserved = []
    try:
        with database.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SEED_LOCK,))
            people = conn.execute("SELECT id,name,team,active FROM business.people").fetchall()
            by_name_team = {}
            for person in people:
                by_name_team.setdefault((person["name"], person["team"]), []).append(person)
            accounts = {
                row["login"]: row
                for row in conn.execute(
                    "SELECT login,person_id FROM business.employee_accounts FOR UPDATE"
                ).fetchall()
            }
            expected = []
            for login, name, team, role in _DEMO_ACCOUNTS:
                matches = by_name_team.get((name, team), [])
                if len(matches) != 1:
                    raise ValueError(
                        "Demo directory is missing or ambiguous; run decagent seed-tickets first"
                    )
                person_id = matches[0]["id"]
                prior = accounts.get(login)
                if prior and prior["person_id"] != person_id:
                    raise ValueError(
                        f"Demo account {login} is bound to a different directory person"
                    )
                if not prior and not matches[0]["active"]:
                    raise ValueError(f"Demo directory person for {login} is inactive")
                expected.append((login, person_id, role, prior is not None))
            if directory.is_symlink():
                raise ValueError("Demo credential directory must not be a symlink")
            if directory.exists() and not directory.is_dir():
                raise ValueError("Demo credential directory must be a directory")
            if any(not exists for _, _, _, exists in expected):
                for login, _, _, exists in expected:
                    path = directory / f"{login}.password"
                    if not exists and (path.exists() or path.is_symlink()):
                        raise FileExistsError(f"Demo credential file already exists: {path}")
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.exists():
                if os.name == "nt":
                    _replace_windows_directory_acl(directory, _current_windows_sid())
                    _assert_private_windows_directory(directory)
                elif any(not exists for _, _, _, exists in expected):
                    directory.chmod(0o700)
            for login, person_id, role, exists in expected:
                if exists:
                    preserved.append(login)
                    continue
                password = secrets.token_urlsafe(48)
                password_hash = hash_employee_password(password)
                path = directory / f"{login}.password"
                _write_password_file(path, password)
                created_paths.append(path)
                conn.execute(
                    "INSERT INTO business.employee_accounts(id,login,person_id,role,password_hash) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (uuid4(), login, person_id, role, password_hash),
                )
                created.append(login)
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return {"created": created, "preserved": preserved}


def _write_password_file(path: Path, password: str) -> None:
    """Protect a new file before any password bytes are written."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            _protect_file(path)
            output.write(password + "\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise

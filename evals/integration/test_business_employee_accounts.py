"""Explicit employee seeding keeps six directory identities and private credentials."""

import json
import os
import subprocess

import pytest

from decagent.business import employee_accounts
from decagent.business.employee_accounts import (
    hash_employee_password,
    seed_employee_accounts,
    verify_employee_password,
)
from decagent.business.tickets.store import TicketStore
from decagent.cli import main
from decagent.config import Settings

pytestmark = pytest.mark.integration

EXPECTED = {
    "employee-it": ("张伟", "IT Support", "employee"),
    "employee-facilities": ("张伟", "Facilities", "employee"),
    "handler-it": ("Zhang Wei", "IT Support", "handler"),
    "handler-facilities": ("Zhang Wei", "Facilities", "handler"),
    "handler-access": ("王芳", "Access Management", "handler"),
    "supervisor": ("Chen Li", "Network", "supervisor"),
}


def _seed_directory(database):
    assert TicketStore(database.url).execute("seed", {}, "seed-employee-test")["ok"]


def _accounts(database):
    with database.connect() as conn:
        return conn.execute(
            "SELECT a.id,a.login,a.person_id,a.role,a.password_hash,a.active,"
            "a.credential_revision,p.name,p.team FROM business.employee_accounts a "
            "JOIN business.people p ON p.id=a.person_id ORDER BY a.login"
        ).fetchall()


def test_accounts_seed_creates_exact_distinct_directory_accounts_and_private_passwords(
    database, monkeypatch, tmp_path, capsys, caplog
):
    _seed_directory(database)
    settings = Settings(database_url=database.url, data_dir=tmp_path / ".decagent")
    monkeypatch.setattr(Settings, "from_env", lambda: settings)

    main(["business", "accounts", "seed"])

    output = capsys.readouterr().out
    assert json.loads(output) == {"created": list(EXPECTED), "preserved": []}
    rows = _accounts(database)
    assert len(rows) == 6
    assert len({row["person_id"] for row in rows}) == 6
    assert {row["login"]: (row["name"], row["team"], row["role"]) for row in rows} == EXPECTED
    with database.connect() as conn:
        directory = conn.execute("SELECT id,name,team FROM business.people").fetchall()
    assert len(directory) == 6
    assert sum(row["name"] == "张伟" for row in directory) == 2
    assert sum(row["name"] == "Zhang Wei" for row in directory) == 2

    secrets = {}
    for row in rows:
        path = settings.data_dir / "demo-auth" / f"{row['login']}.password"
        password = path.read_text(encoding="utf-8").strip()
        secrets[row["login"]] = password
        assert len(password) >= 40
        assert verify_employee_password(password, row["password_hash"])
        assert password not in row["password_hash"]
        assert password not in output and password not in caplog.text
        if os.name == "nt":
            acl = subprocess.run(["icacls", str(path)], check=True, capture_output=True, text=True)
            assert "(I)" not in acl.stdout
        else:
            assert path.stat().st_mode & 0o777 == 0o600
    assert len(set(secrets.values())) == 6
    assert len({row["password_hash"] for row in rows}) == 6
    if os.name != "nt":
        assert (settings.data_dir / "demo-auth").stat().st_mode & 0o777 == 0o700


def test_repeated_seed_preserves_account_identity_password_and_disabled_state(database, tmp_path):
    _seed_directory(database)
    directory = tmp_path / ".decagent" / "demo-auth"
    assert seed_employee_accounts(database, directory) == {
        "created": list(EXPECTED),
        "preserved": [],
    }
    before = {row["login"]: row for row in _accounts(database)}
    original_files = {login: (directory / f"{login}.password").read_bytes() for login in EXPECTED}
    changed_hash = hash_employee_password("changed after initial seed")
    with database.connect() as conn:
        conn.execute(
            "UPDATE business.employee_accounts SET password_hash=%s,"
            "credential_revision=credential_revision+1,active=false WHERE login='employee-it'",
            (changed_hash,),
        )
        conn.execute(
            "UPDATE business.people SET active=false WHERE id=%s",
            (before["employee-it"]["person_id"],),
        )

    assert seed_employee_accounts(database, directory) == {
        "created": [],
        "preserved": list(EXPECTED),
    }

    after = {row["login"]: row for row in _accounts(database)}
    for login in EXPECTED:
        assert after[login]["id"] == before[login]["id"]
        assert after[login]["person_id"] == before[login]["person_id"]
        assert (directory / f"{login}.password").read_bytes() == original_files[login]
    assert after["employee-it"]["password_hash"] == changed_hash
    assert after["employee-it"]["credential_revision"] == 2
    assert after["employee-it"]["active"] is False


def test_accounts_seed_requires_directory_before_creating_credentials(database, tmp_path):
    directory = tmp_path / ".decagent" / "demo-auth"
    with pytest.raises(ValueError, match="seed-tickets"):
        seed_employee_accounts(database, directory)
    assert _accounts(database) == []
    assert not directory.exists()


def test_account_queries_return_exact_login_or_id_and_no_other_person(database, tmp_path):
    _seed_directory(database)
    seed_employee_accounts(database, tmp_path / ".decagent" / "demo-auth")
    by_login = employee_accounts.get_employee_by_login(database, "employee-it")
    assert by_login["login"] == "employee-it"
    assert by_login["role"] == "employee"
    assert by_login["active"] is True
    assert by_login["credential_revision"] == 1
    assert verify_employee_password(
        (tmp_path / ".decagent" / "demo-auth" / "employee-it.password").read_text().strip(),
        by_login["password_hash"],
    )
    by_id = employee_accounts.get_employee_by_id(database, by_login["id"])
    assert by_id == by_login
    assert employee_accounts.get_employee_by_login(database, "not-an-account") is None
    assert (
        employee_accounts.get_employee_by_id(database, "00000000-0000-0000-0000-000000000000")
        is None
    )


def test_accounts_seed_refuses_existing_password_file_without_database_changes(database, tmp_path):
    _seed_directory(database)
    directory = tmp_path / ".decagent" / "demo-auth"
    directory.mkdir(parents=True)
    path = directory / "employee-it.password"
    path.write_text("previous credential", encoding="utf-8")

    with pytest.raises(FileExistsError):
        seed_employee_accounts(database, directory)

    assert path.read_text(encoding="utf-8") == "previous credential"
    assert _accounts(database) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL only")
def test_seed_rejects_preexisting_directory_with_explicit_broad_acl(
    database, tmp_path, monkeypatch
):
    _seed_directory(database)
    directory = tmp_path / ".decagent" / "demo-auth"
    directory.mkdir(parents=True)
    monkeypatch.setattr(employee_accounts, "_replace_windows_directory_acl", lambda *_args: None)
    monkeypatch.setattr(
        employee_accounts,
        "_read_windows_acl",
        lambda _path: {
            "protected": True,
            "rules": [
                {
                    "sid": "S-1-1-0",
                    "inherited": False,
                    "type": "Allow",
                    "rights": "ReadAndExecute",
                }
            ],
        },
        raising=False,
    )

    with pytest.raises(ValueError, match="explicit|ACL|private"):
        seed_employee_accounts(database, directory)

    assert _accounts(database) == []
    assert list(directory.iterdir()) == []

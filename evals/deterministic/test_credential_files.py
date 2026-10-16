"""Credential storage rejects links and removes pre-existing broad Windows ACLs."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from decagent.business.employee_accounts import _replace_windows_directory_acl
from decagent.gateway.auth import _current_windows_sid


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL semantics")
def test_private_directory_replaces_explicit_everyone_grant(tmp_path):
    directory = tmp_path / "credentials"
    directory.mkdir()
    subprocess.run(
        ["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)(F)"],
        check=True,
        capture_output=True,
    )
    _replace_windows_directory_acl(directory, _current_windows_sid())
    env = {**os.environ, "DECAGENT_TEST_ACL_PATH": str(directory)}
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "$acl=[System.IO.Directory]::GetAccessControl($env:DECAGENT_TEST_ACL_PATH); "
            "@($acl.Access | ForEach-Object { "
            "$sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]); "
            "$sid.Value }) | ConvertTo-Json -Compress",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    sids = json.loads(result.stdout)
    assert len(sids) == 2
    assert "S-1-5-18" in sids
    assert "S-1-1-0" not in sids


def test_employee_auth_rejects_linked_credential_directory(tmp_path):
    from decagent.business.employee_auth import EmployeeAuth

    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "credentials"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Local account cannot create symlinks")
    with pytest.raises(ValueError, match="link"):
        EmployeeAuth(None, "http://127.0.0.1:8000", False, link)
    assert list(target.iterdir()) == []


def test_employee_auth_rejects_linked_ancestor(tmp_path):
    from decagent.business.employee_auth import EmployeeAuth

    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "runtime"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Local account cannot create symlinks")
    with pytest.raises(ValueError, match="link"):
        EmployeeAuth(None, "http://127.0.0.1:8000", False, Path(link) / "credentials")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_employee_auth_rejects_junction_directory(tmp_path):
    from decagent.business.employee_auth import EmployeeAuth

    target = tmp_path / "outside"
    target.mkdir()
    junction = tmp_path / "runtime"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="reparse point"):
        EmployeeAuth(None, "http://127.0.0.1:8000", False, junction / "credentials")
    assert not (target / "credentials").exists()

"""Employee bootstrap credentials use independently salted password hashes."""

from decagent.business import employee_accounts


def test_password_hashes_are_salted_and_verify_without_storing_plaintext():
    first = employee_accounts.hash_employee_password("private password")
    second = employee_accounts.hash_employee_password("private password")

    assert first != second
    assert first.startswith("pbkdf2_sha256$")
    assert "private password" not in first
    assert employee_accounts.verify_employee_password("private password", first)
    assert not employee_accounts.verify_employee_password("incorrect", first)


def test_private_directory_acl_rejects_explicit_broad_access():
    owner_sid = "S-1-5-21-1001"
    rules = {
        "protected": True,
        "rules": [
            {"sid": owner_sid, "inherited": False, "type": "Allow", "rights": "FullControl"},
            {"sid": "S-1-5-18", "inherited": False, "type": "Allow", "rights": "FullControl"},
            {"sid": "S-1-1-0", "inherited": False, "type": "Allow", "rights": "ReadAndExecute"},
        ],
    }
    assert not employee_accounts._windows_acl_is_private(rules, owner_sid)


def test_private_directory_acl_accepts_only_employee_and_system():
    owner_sid = "S-1-5-21-1001"
    rules = {
        "protected": True,
        "rules": [
            {"sid": owner_sid, "inherited": False, "type": "Allow", "rights": "FullControl"},
            {"sid": "S-1-5-18", "inherited": False, "type": "Allow", "rights": "FullControl"},
        ],
    }
    assert employee_accounts._windows_acl_is_private(rules, owner_sid)

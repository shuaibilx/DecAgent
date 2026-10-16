"""Authorized live-test owner replies are restricted to the registered fixture."""

from ops.live_repository import approved_fixture_action


def test_fixture_authorization_rejects_test_rewrite_and_arbitrary_commands():
    assert approved_fixture_action({"tool": "write_file", "params": {"path": "range_logic.py"}})
    assert not approved_fixture_action({"tool": "write_file", "params": {"path": "test_range.py"}})
    assert not approved_fixture_action(
        {
            "tool": "run_command",
            "params": {"cwd": ".", "argv": ["python", "-c", "exit(0)"]},
        }
    )
    assert not approved_fixture_action(
        {
            "tool": "run_command",
            "params": {"cwd": ".", "argv": ["rm", "-rf", "."]},
        }
    )

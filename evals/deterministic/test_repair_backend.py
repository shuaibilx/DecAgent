"""Broker-only adapter always authorizes before a fenced remote operation."""

import base64
from uuid import uuid4

import pytest

from decagent.executor.archive import make_archive, snapshot_hash
from decagent.executor.protocol import ScopeRequest
from decagent.tools.repair import BrokerRepairBackend, Command, GovernedAction


def test_broker_adapter_authentication_scope_and_execute_receipt():
    scope = ScopeRequest(task_id=str(uuid4()), owner="repair-test", lease_generation=1)
    actions, calls = [], []
    action_id = str(uuid4())

    def authorize(action, intent):
        actions.append((action, intent))
        return GovernedAction(action_id, "a" * 64)

    files = {"logic.py": b"X = 1\n"}

    def call(path, payload):
        calls.append((path, payload))
        assert payload["task_id"] == scope.task_id
        assert payload["lease_generation"] == 1
        if path == "/v1/workspaces/snapshot":
            return {
                "archive_base64": base64.b64encode(make_archive(files)).decode(),
                "workspace_hash": snapshot_hash(files),
            }
        if path == "/v1/executions":
            assert payload["action_id"] == action_id
            return {
                "execution_id": payload["execution_id"],
                "state": "completed",
                "exit_code": 0,
                "output": "done",
                "truncated": False,
            }
        if path == "/v1/workspaces/write":
            assert payload["action_id"] == action_id
            assert payload["action_digest"] == "a" * 64
        if path == "/v1/workspaces/diff":
            return {"diff": "complete patch"}
        return {"path": "logic.py"}

    backend = BrokerRepairBackend(call, scope, authorize)
    assert backend.read("logic.py") == files["logic.py"]
    backend.write("logic.py", b"X = 2\n")
    assert backend.diff() == "complete patch"
    completed = backend.execute(Command(("python", "-m", "pytest"), "original regression"))
    assert completed.output == "done"
    assert actions[-1][1].source == "original regression"
    assert len(actions) == len(calls)


def test_denied_action_never_reaches_broker():
    scope = ScopeRequest(task_id=str(uuid4()), owner="repair-test", lease_generation=1)
    calls = []

    def deny(_action, _intent):
        raise PermissionError("code trust required")

    backend = BrokerRepairBackend(lambda *args: calls.append(args), scope, deny)
    with pytest.raises(PermissionError, match="trust"):
        backend.execute(Command(("python", "-m", "pip", "install", "."), "pyproject", "setup"))
    assert not calls


def test_snapshot_checksum_mismatch_is_rejected():
    scope = ScopeRequest(task_id=str(uuid4()), owner="repair-test", lease_generation=1)
    backend = BrokerRepairBackend(
        lambda *_: {
            "archive_base64": base64.b64encode(make_archive({"x.py": b"x"})).decode(),
            "workspace_hash": "b" * 64,
        },
        scope,
        lambda *_: GovernedAction(str(uuid4()), "a" * 64),
    )
    with pytest.raises(ValueError, match="snapshot"):
        backend.snapshot()

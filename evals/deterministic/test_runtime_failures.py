"""Missing repair evidence stays visible and can be corrected before source effects."""

from types import SimpleNamespace

import httpx
import pytest

from decagent.executor.protocol import ScopeRequest
from decagent.policy import Action
from decagent.runtime.operations import WaitingRequired
from decagent.runtime.services import ReproductionUnavailable, RuntimeServices


def unprepared_evidence(files):
    service = object.__new__(RuntimeServices)
    service._prepared = True
    service._latest = lambda kind: None
    service._files = lambda: files
    service.plan = SimpleNamespace(regression=SimpleNamespace(argv=("python", "-m", "pytest")))
    return service


def test_source_edit_requires_reproducible_tests_and_does_not_write():
    service = unprepared_evidence({"logic.py": b"def f(): return 1\n"})
    with pytest.raises(ReproductionUnavailable, match="failing test"):
        service.before_execute(Action("write_file", {"path": "logic.py", "content": "x=2"}))


def test_missing_reproduction_exports_unverified_diff():
    service = unprepared_evidence({})
    events = []
    service.task_id = "task"
    service.database = SimpleNamespace(event_once=lambda *args: events.append(args))
    service.backend = SimpleNamespace(diff=lambda: "reviewable unverified changes")
    result = service.verify_and_finalize()
    assert result["status"] == "unverified"
    assert result["verification"] is None
    assert result["diff"] == "reviewable unverified changes"
    assert result["reasons"]
    assert events[0][2:] == ("task.result", result)


@pytest.mark.parametrize(
    "tool,params",
    [
        ("read_file", {"path": "absent.py", "max_bytes": 100}),
        ("edit_file", {"path": "absent.py", "old": "x", "new": "y", "expected_matches": 1}),
    ],
)
def test_missing_file_returns_failed_observation(tool, params):
    service = unprepared_evidence({})
    service.fence = lambda: None
    result = service.execute(Action(tool, params), {}, "unused-read-id")
    assert result["status"] == "failed" and result["reason"]


def test_broker_command_response_loss_is_unknown_instead_of_failed():
    service = unprepared_evidence({})
    service.fence = lambda: None
    service.scope = ScopeRequest(
        task_id="00000000-0000-0000-0000-000000000001", owner="worker", lease_generation=1
    )
    calls = []

    def lost_response(path, payload):
        calls.append(path)
        raise httpx.ReadTimeout("Injected broker response lost after dispatch")

    service.call = lost_response
    result = service.execute(
        Action(
            "run_command",
            {
                "argv": ["python", "-c", "print(1)"],
                "cwd": ".",
                "timeout_seconds": 10,
                "max_output_bytes": 1000,
                "purpose": "command",
            },
        ),
        {"id": "00000000-0000-0000-0000-000000000002", "digest": "a" * 64},
        "00000000-0000-0000-0000-000000000003",
    )
    assert result["status"] == "unknown" and calls == ["/v1/executions"]
    assert result["execution_id"] == "00000000-0000-0000-0000-000000000003"


def test_controller_command_response_loss_pauses_for_reconciliation():
    service = unprepared_evidence({})

    def lost_response(path, payload):
        raise httpx.ReadTimeout("Injected controller response loss")

    service.call = lost_response
    with pytest.raises(WaitingRequired) as captured:
        service._controller_broker("/v1/executions/status", {"execution_id": "fixed-id"})
    assert captured.value.payload["kind"] == "reconciliation"
    assert captured.value.payload["execution_id"] == "fixed-id"


def test_context_limit_is_loaded_and_must_be_positive(monkeypatch):
    from decagent.runtime.config import RuntimeConfig

    monkeypatch.setenv("DECAGENT_CONTEXT_TOKENS", "8192")
    assert RuntimeConfig.from_env().context_tokens == 8192
    with pytest.raises(ValueError, match="Context"):
        RuntimeConfig(context_tokens=0)

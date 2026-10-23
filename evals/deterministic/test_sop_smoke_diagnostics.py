import json

from ops.deploy.offline_sop_smoke import (
    _safe_command_diagnostic,
    _safe_health_diagnostic,
    _safe_probe_output,
    _wait_for_readiness,
)


def test_smoke_failure_diagnostic_keeps_only_component_status_and_discards_raw_output():
    report = {
        "status": "not_ready",
        "components": {"database": True, "mcp": False},
        "secret": "must not be copied into diagnostics",
    }
    container = {
        "State": {
            "Status": "running",
            "Health": {
                "Status": "unhealthy",
                "Log": [{"Output": json.dumps(report)}],
            },
        }
    }

    result = _safe_health_diagnostic(container)

    assert result == {
        "container_state": "running",
        "health": "unhealthy",
        "probe_status": "not_ready",
        "components": {"database": True, "mcp": False},
    }


def test_smoke_failure_probe_summary_discards_untrusted_response_fields():
    result = _safe_probe_output(
        json.dumps(
            {
                "status": "not_ready",
                "components": {"database": True, "mcp": False},
                "connection_string": "must not be copied into diagnostics",
            }
        )
    )

    assert result == {
        "probe_status": "not_ready",
        "components": {"database": True, "mcp": False},
    }


def test_smoke_command_diagnostic_keeps_exit_and_exception_type_only():
    result = _safe_command_diagnostic(
        1, "Traceback (most recent call last):\nRuntimeError: sensitive response content"
    )

    assert result == {"exit_code": 1, "error_type": "RuntimeError"}


def test_smoke_waits_for_readiness_and_returns_first_ready_report():
    reports = iter(
        [
            {"status": "not_ready", "components": {"business_worker": False}},
            {"status": "ready", "components": {"business_worker": True}},
        ]
    )
    slept = []

    result = _wait_for_readiness(
        lambda: next(reports),
        timeout_seconds=10,
        poll_interval_seconds=2,
        monotonic=lambda: 0,
        sleep=slept.append,
    )

    assert result["status"] == "ready"
    assert slept == [2]


def test_smoke_readiness_timeout_returns_only_last_redacted_report():
    ticks = iter([0, 1, 2, 3])
    result = _wait_for_readiness(
        lambda: {
            "status": "not_ready",
            "components": {"business_worker": False},
            "secret": "must not be returned",
        },
        timeout_seconds=1,
        poll_interval_seconds=1,
        monotonic=lambda: next(ticks),
        sleep=lambda _: None,
    )

    assert result == {
        "status": "not_ready",
        "components": {"business_worker": False},
    }


def test_disposable_smoke_waits_for_compose_health_before_probing_api():
    from ops.deploy.offline_sop_smoke import _start_stack

    commands = []

    _start_stack(lambda *args: commands.append(args))

    assert commands == [
        ("up", "--build", "--detach", "--wait", "--wait-timeout", "180")
    ]

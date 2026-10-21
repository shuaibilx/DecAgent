"""Small allowlisted diagnostics for public evaluation failures.

Only stage identifiers, Python exception class names, and bounded HTTP status
codes are retained. Exception messages, request/response bodies, arguments, and
credentials are deliberately excluded.
"""

from __future__ import annotations

import re

_EXCEPTION_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
FAILURE_STAGES = frozenset(
    {
        "model_callback",
        "model_response_validation",
        "model_worker_model_call",
        "model_worker_request_pre_send",
        "model_worker_provider_response",
        "model_worker_target",
        "model_worker_response",
        "tool_gateway",
        "environment_setup",
        "agent_execution",
        "official_scoring",
        "model_resource_cleanup",
        "environment_cleanup",
        "route_supervisor",
        "route_deadline",
    }
)


def safe_exception_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    try:
        name = getattr(error, "diagnostic_exception_type", None)
    except Exception:
        name = None
    if type(name) is not str or _EXCEPTION_TYPE.fullmatch(name) is None:
        name = type(error).__name__
    return name if _EXCEPTION_TYPE.fullmatch(name) is not None else "UnknownException"


def safe_failure_stage(error: BaseException | None, *, default="model_callback") -> str:
    """Keep an explicitly attached stage only when it belongs to the allowlist."""
    if type(default) is not str or default not in FAILURE_STAGES:
        default = "model_callback"
    if error is None:
        return default
    try:
        stage = getattr(error, "failure_stage", None)
    except Exception:
        return default
    return stage if type(stage) is str and stage in FAILURE_STAGES else default


def safe_http_status(error: BaseException | None) -> int | None:
    if error is None:
        return None
    try:
        value = getattr(error, "status_code", None)
        if type(value) is int and 100 <= value <= 599:
            return value
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    except Exception:
        return None
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def validate_diagnostic(*, failure_stage, exception_type, http_status=None):
    if failure_stage is not None and (
        type(failure_stage) is not str or failure_stage not in FAILURE_STAGES
    ):
        raise ValueError("Failure stage must use an allowlisted diagnostic code")
    if exception_type is not None and (
        type(exception_type) is not str
        or _EXCEPTION_TYPE.fullmatch(exception_type) is None
    ):
        raise ValueError("Failure exception type must be a sanitized class name")
    if http_status is not None and (
        type(http_status) is not int or not 100 <= http_status <= 599
    ):
        raise ValueError("Failure HTTP status must be a valid status code")

import pytest

from decagent.evaluation.business.public_diagnostics import (
    safe_chained_exception_type,
    safe_chained_http_status,
    safe_diagnostic_code,
    safe_exception_type,
    safe_failure_stage,
    safe_http_status,
    validate_diagnostic,
)


def test_failure_stage_with_wrong_type_is_rejected_as_invalid_diagnostic():
    with pytest.raises(ValueError, match="allowlisted"):
        validate_diagnostic(failure_stage=["model_callback"], exception_type=None)


def test_diagnostics_keep_only_exception_class_and_valid_http_status():
    class ProviderError(Exception):
        status_code = 503

    error = ProviderError("private response body and secret")
    assert safe_exception_type(error) == "ProviderError"
    assert safe_http_status(error) == 503
    assert "private response body" not in repr(
        (safe_exception_type(error), safe_http_status(error))
    )


def test_invalid_http_status_is_not_retained():
    error = RuntimeError("private")
    error.status_code = 999
    assert safe_http_status(error) is None


def test_worker_failure_diagnostics_keep_only_allowlisted_stage_and_type():
    class WorkerFailure(RuntimeError):
        failure_stage = "model_worker_target"
        diagnostic_exception_type = "ConnectTimeout"

    error = WorkerFailure("private provider detail and token")
    diagnostic = (safe_failure_stage(error), safe_exception_type(error))
    validate_diagnostic(failure_stage=diagnostic[0], exception_type=diagnostic[1])
    assert diagnostic == ("model_worker_target", "ConnectTimeout")
    assert "private provider detail" not in repr(diagnostic)


def test_worker_failure_diagnostic_rejects_untrusted_attributes():
    class UntrustedFailure(RuntimeError):
        failure_stage = "arbitrary-stage"
        diagnostic_exception_type = "Provider error with details"

    error = UntrustedFailure("secret")
    assert safe_failure_stage(error) == "model_callback"
    assert safe_exception_type(error) == "UntrustedFailure"


def test_diagnostic_code_is_allowlisted_across_wrappers_without_messages():
    class CallbackFailure(ValueError):
        diagnostic_code = "malformed_response"

    try:
        try:
            raise CallbackFailure("private callback body")
        except CallbackFailure as cause:
            raise RuntimeError("private bridge detail") from cause
    except RuntimeError as wrapped:
        diagnostic = safe_diagnostic_code(wrapped)

    assert diagnostic == "malformed_response"
    assert "private" not in repr(diagnostic)


def test_diagnostic_code_rejects_untrusted_text():
    class UntrustedFailure(RuntimeError):
        diagnostic_code = "private token=secret"

    assert safe_diagnostic_code(UntrustedFailure("private")) is None


def test_chained_diagnostics_follow_implicit_context_without_reading_messages():
    class UpstreamHttpFailure(Exception):
        status_code = 504

    try:
        try:
            raise UpstreamHttpFailure("private upstream response body")
        except UpstreamHttpFailure:
            raise RuntimeError("private wrapper message")  # noqa: B904 - exercise implicit context
    except RuntimeError as wrapped:
        diagnostic = (
            safe_chained_exception_type(wrapped),
            safe_chained_http_status(wrapped),
        )

    assert diagnostic == ("UpstreamHttpFailure", 504)
    assert "private upstream response body" not in repr(diagnostic)
    assert "private wrapper message" not in repr(diagnostic)


def test_failure_stage_propagates_from_a_wrapped_safe_cause():
    class OutputValidationFailure(ValueError):
        failure_stage = "tool_output_validation"

    try:
        try:
            raise OutputValidationFailure("private invalid response detail")
        except OutputValidationFailure as exc:
            raise RuntimeError("private wrapper detail") from exc
    except RuntimeError as wrapped:
        stage = safe_failure_stage(wrapped, default="tool_gateway")

    assert stage == "tool_output_validation"
    assert "private invalid response detail" not in repr(stage)


def test_suppressed_exception_context_is_not_exposed_in_diagnostics():
    class UpstreamHttpFailure(Exception):
        status_code = 504

    try:
        try:
            raise UpstreamHttpFailure("private")
        except UpstreamHttpFailure:
            raise RuntimeError("wrapper") from None
    except RuntimeError as wrapped:
        diagnostic = (
            safe_chained_exception_type(wrapped),
            safe_chained_http_status(wrapped),
        )

    assert diagnostic == ("RuntimeError", None)

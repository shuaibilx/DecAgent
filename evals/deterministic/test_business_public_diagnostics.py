import pytest

from decagent.evaluation.business.public_diagnostics import (
    safe_exception_type,
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

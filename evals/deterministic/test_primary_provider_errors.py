"""Provider failures are classified without retaining provider payloads."""

import traceback

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from openai import APIConnectionError, APIStatusError, APITimeoutError

from decagent.models.primary import PrimaryModel
from decagent.policy import Redactor

PRIVATE_MARKER = "provider-response-secret-marker"


class FailingClient:
    def __init__(self, failure, *, stream=False):
        self.failure = failure
        self.stream_mode = stream

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        raise self.failure

    def stream(self, messages):
        yield AIMessageChunk(content="partial")
        raise self.failure


def _request():
    return httpx.Request("POST", "https://provider.example/v1/chat/completions")


def _status_error(status_code):
    response = httpx.Response(status_code, request=_request(), text=PRIVATE_MARKER)
    return APIStatusError(
        PRIVATE_MARKER,
        response=response,
        body={"error": {"message": PRIVATE_MARKER, "api_key": PRIVATE_MARKER}},
    )


def _invoke_failure(error, *, stream=False):
    with pytest.raises(Exception) as caught:
        PrimaryModel(FailingClient(error, stream=stream)).respond(
            [{"role": "user", "content": "diagnostic"}], stream=stream
        )
    return caught.value


@pytest.mark.parametrize(
    ("failure_factory", "category", "retryable"),
    [
        (lambda: APITimeoutError(request=_request()), "timeout", True),
        (
            lambda: APIConnectionError(message=PRIVATE_MARKER, request=_request()),
            "connection",
            True,
        ),
    ],
)
def test_connection_failures_are_transient_and_sanitized(failure_factory, category, retryable):
    failure = _invoke_failure(failure_factory())

    assert (failure.category, failure.retryable, failure.status_code) == (
        category,
        retryable,
        None,
    )
    assert PRIVATE_MARKER not in str(failure)
    assert PRIVATE_MARKER not in repr(failure)
    assert PRIVATE_MARKER not in repr(vars(failure))
    assert failure.__cause__ is None and failure.__context__ is None


@pytest.mark.parametrize("status_code", [408, 429, 500, 503, 599])
def test_transient_http_statuses_are_classified_without_response_body(status_code):
    failure = _invoke_failure(_status_error(status_code))

    assert failure.category == "transient_http"
    assert failure.retryable is True
    assert failure.status_code == status_code
    assert PRIVATE_MARKER not in str(failure)
    assert PRIVATE_MARKER not in repr(failure)
    assert PRIVATE_MARKER not in repr(vars(failure))
    assert failure.__cause__ is None and failure.__context__ is None


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_permanent_http_statuses_are_not_retryable(status_code):
    failure = _invoke_failure(_status_error(status_code))

    assert failure.category == "permanent_http"
    assert failure.retryable is False
    assert failure.status_code == status_code
    assert PRIVATE_MARKER not in str(failure)
    assert PRIVATE_MARKER not in repr(vars(failure))


def test_other_http_status_is_safe_and_not_assumed_retryable():
    failure = _invoke_failure(_status_error(404))

    assert failure.category == "provider_error"
    assert failure.retryable is False
    assert failure.status_code == 404
    assert PRIVATE_MARKER not in str(failure)
    assert PRIVATE_MARKER not in repr(vars(failure))


def test_error_request_id_is_redacted_before_it_can_reach_persistent_usage():
    response = httpx.Response(
        503,
        request=_request(),
        headers={"x-request-id": PRIVATE_MARKER},
        text="provider error",
    )
    error = APIStatusError("provider error", response=response, body={})
    with pytest.raises(Exception) as caught:
        PrimaryModel(
            FailingClient(error), Redactor(known_secrets=(PRIVATE_MARKER,))
        ).respond([{"role": "user", "content": "diagnostic"}])

    assert caught.value.server_request_id is None


def test_retry_after_farther_than_one_day_is_preserved_as_a_stop_signal():
    response = httpx.Response(
        429, request=_request(), headers={"retry-after": "172800"}, text="rate limited"
    )
    error = APIStatusError("rate limited", response=response, body={})
    failure = _invoke_failure(error)

    assert failure.retry_after_seconds is not None
    assert failure.retry_after_seconds > 30


def test_streaming_connection_failure_is_classified_and_does_not_leak_payload():
    original = _status_error(429)

    with pytest.raises(Exception) as caught:
        PrimaryModel(FailingClient(original, stream=True)).respond(
            [{"role": "user", "content": "diagnostic"}], stream=True
        )

    failure = caught.value
    assert failure.category == "transient_http"
    assert failure.status_code == 429
    assert PRIVATE_MARKER not in "".join(traceback.format_exception(failure))
    assert failure.__cause__ is None and failure.__context__ is None

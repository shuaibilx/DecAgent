"""Normalize LangChain chat completions; tool execution belongs to the governed runtime."""

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError

from decagent.policy import Redactor, validate_tool
from decagent.policy.canonical import primitive
from decagent.policy.tools import SCHEMAS
from decagent.storage.safe_record import exact_safe_record

from .validation_feedback import ModelValidationError, bounded_validation_issues


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None = None


_REJECTION_MESSAGES = {
    "tool_schema": "Malformed tool proposal: Unregistered tool or Missing required parameter",
    "tool_identity": "Missing or duplicate tool call identity",
    "invalid_tool_calls": "Malformed model response: invalid tool calls",
    "content_type": "Provider must return textual chat-completion content",
    "usage_invalid": "Invalid model usage",
    "usage_missing": "Completed model response has unknown billing usage",
    "unsafe_tool_proposal": "Tool proposal contains suspected credentials or unsupported bounds",
    "malformed_response": "Malformed model response",
    "empty_stream": "Provider returned an empty stream",
}
_PREFLIGHT_MESSAGES = {
    "context_limit": "Context must be compacted before model input",
    "input_invalid": "Invalid model input before provider dispatch",
}
_PROVIDER_FAILURE_MESSAGES = {
    "timeout": "Primary provider request timed out",
    "connection": "Primary provider connection failed",
    "transient_http": "Primary provider returned a transient HTTP error",
    "permanent_http": "Primary provider returned a permanent HTTP error",
    "provider_error": "Primary provider request failed",
}
_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", re.ASCII)
_REQUEST_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/_-]{0,254}", re.ASCII)


def _valid_usage_values(values):
    return (
        all(type(value) is int and value >= 0 for value in values)
        and values[2] == values[0] + values[1]
    )


def _response_usage(metadata):
    if metadata is None or (type(metadata) is dict and not metadata):
        return None, False
    if type(metadata) is not dict:
        return None, True
    values = tuple(metadata.get(key) for key in ("input_tokens", "output_tokens", "total_tokens"))
    if not _valid_usage_values(values):
        return None, True
    cached_counts = []
    for details_key, count_key in (
        ("input_token_details", "cache_read"),
        ("prompt_tokens_details", "cached_tokens"),
    ):
        details = metadata.get(details_key)
        if details is not None:
            if type(details) is not dict:
                return None, True
            if count_key in details:
                cached = details[count_key]
                if type(cached) is not int or not 0 <= cached <= values[0]:
                    return None, True
                cached_counts.append(cached)
    if len(set(cached_counts)) > 1:
        return None, True
    cached = cached_counts[0] if cached_counts else None
    return ModelUsage(*values, cached_input_tokens=cached), False


def _safe_model_identifier(value, redactor=None):
    if type(value) is not str or _MODEL_IDENTIFIER.fullmatch(value) is None:
        return None
    secrets = redactor.known_secrets if redactor is not None else ()
    if Redactor(known_secrets=secrets).redact_text(value) != value:
        return None
    return value


def _safe_request_identifier(value, redactor=None):
    if type(value) is not str or _REQUEST_IDENTIFIER.fullmatch(value) is None:
        return None
    secrets = redactor.known_secrets if redactor is not None else ()
    if Redactor(known_secrets=secrets).redact_text(value) != value:
        return None
    return value


class PrimaryResponseRejected(ValueError):
    """A completed rejection with safe billing metadata and bounded validation locations."""

    def __init__(
        self, *, reason, usage=None, model=None, request_id=None, validation_issues=()
    ):
        if type(reason) is not str or reason not in _REJECTION_MESSAGES:
            raise ValueError("Unsupported primary rejection reason") from None
        self.reason = reason
        self.usage = None
        if type(usage) is ModelUsage:
            values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
            if _valid_usage_values(values):
                self.usage = ModelUsage(*values, cached_input_tokens=usage.cached_input_tokens)
        self.model = _safe_model_identifier(model)
        self.server_request_id = _safe_request_identifier(request_id)
        self.validation_issues = (
            bounded_validation_issues(validation_issues) if reason == "tool_schema" else ()
        )
        super().__init__(_REJECTION_MESSAGES[reason])


class PrimaryRequestNotDispatched(ValueError):
    """Trusted local validation failed before the provider client was called."""

    def __init__(self, *, reason):
        if type(reason) is not str or reason not in _PREFLIGHT_MESSAGES:
            raise ValueError("Unsupported primary preflight reason") from None
        self.reason = reason
        self.usage = ModelUsage(0, 0, 0)
        self.model = None
        super().__init__(_PREFLIGHT_MESSAGES[reason])


class PrimaryProviderFailure(RuntimeError):
    """Sanitized provider failure metadata; original exceptions and bodies are discarded."""

    def __init__(
        self, *, category, status_code=None, retry_after_seconds=None, server_request_id=None
    ):
        if type(category) is not str or category not in _PROVIDER_FAILURE_MESSAGES:
            raise ValueError("Unsupported primary provider failure category") from None
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            status_code = None
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = (
            retry_after_seconds
            if type(retry_after_seconds) in {int, float}
            and 0 <= retry_after_seconds <= 86400
            else None
        )
        self.server_request_id = _safe_request_identifier(server_request_id)
        self.retryable = category in {"timeout", "connection", "transient_http"}
        super().__init__(_PROVIDER_FAILURE_MESSAGES[category])


def _retry_after_seconds(response):
    headers = getattr(response, "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        delay = float(raw.strip())
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            delay = (deadline - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if delay < 0:
        return None
    # The routing policy only distinguishes short waits from "do not retry".
    # Saturate long valid dates instead of discarding them and retrying early.
    return min(delay, 86400)


def _classify_provider_failure(error, redactor=None):
    """Map SDK failures to bounded metadata without retaining provider payloads."""
    if isinstance(error, APITimeoutError):
        return PrimaryProviderFailure(category="timeout")
    if isinstance(error, APIConnectionError):
        return PrimaryProviderFailure(category="connection")
    if isinstance(error, APIStatusError):
        status_code = error.status_code
        if status_code in {400, 401, 403}:
            category = "permanent_http"
        elif status_code in {408, 429} or 500 <= status_code <= 599:
            category = "transient_http"
        else:
            category = "provider_error"
        headers = getattr(error.response, "headers", None)
        request_id = (
            headers.get("x-request-id") or headers.get("request-id")
            if headers is not None
            else None
        )
        return PrimaryProviderFailure(
            category=category,
            status_code=status_code,
            retry_after_seconds=_retry_after_seconds(error.response),
            server_request_id=_safe_request_identifier(request_id, redactor),
        )
    return None


@dataclass(frozen=True)
class ToolProposal:
    id: str
    name: str
    params: Any


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tools: tuple[ToolProposal, ...]
    usage: ModelUsage | None
    model: str | None
    server_request_id: str | None = None

    def assistant_message(self):
        message = {"role": "assistant", "content": self.text}
        if self.tools:
            message["tool_calls"] = [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "arguments": json.dumps(primitive(t.params), ensure_ascii=False),
                    },
                }
                for t in self.tools
            ]
        return message

    def tool_result(self, call_id, result):
        if call_id not in {t.id for t in self.tools}:
            raise ValueError("Result references an unknown tool call")
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }


def tool_schemas():
    tools = []
    for name, fields in SCHEMAS.items():
        properties = {}
        required = []
        for key, rule in fields.items():
            kind = {str: "string", int: "integer", tuple: "array"}[rule.kind]
            properties[key] = {"type": kind}
            if kind == "array":
                properties[key].update(items={"type": "string"}, maxItems=rule.maximum)
            elif kind == "integer":
                properties[key].update(minimum=rule.minimum, maximum=rule.maximum)
            else:
                properties[key].update(minLength=rule.minimum, maxLength=rule.maximum)
            if rule.required:
                required.append(key)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"Propose a scoped {name} action for governance.",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


class PrimaryModel:
    def __init__(
        self,
        client,
        redactor=None,
        *,
        tool_definitions=None,
        validator=None,
        requested_model=None,
        endpoint=None,
        tool_choice=None,
    ):
        if (tool_definitions is None) != (validator is None):
            raise ValueError("Capability definitions and validator must be injected together")
        definitions = tool_schemas() if tool_definitions is None else tool_definitions
        self.schema_bytes = len(json.dumps(definitions, ensure_ascii=False).encode("utf-8"))
        self.client = (
            client.bind_tools(definitions)
            if tool_choice is None
            else client.bind_tools(definitions, tool_choice=tool_choice)
        )
        self.validate_tool = validator or validate_tool
        self.redactor = redactor or Redactor()
        self.requested_model = _safe_model_identifier(requested_model, self.redactor)
        self.endpoint = endpoint
        self.max_request_timeout = getattr(client, "request_timeout", None) or 60
        self._request_timeout_lock = threading.RLock()
        self._closed = False

    @classmethod
    def from_settings(
        cls, settings, max_output_tokens=2048, *, tool_definitions=None, validator=None
    ):
        return cls.from_profile(
            model=settings.main_model,
            base_url=settings.main_base_url,
            api_key=settings.main_api_key,
            max_output_tokens=max_output_tokens,
            tool_definitions=tool_definitions,
            validator=validator,
            known_secrets=(
                settings.main_api_key,
                settings.jev_api_key,
                settings.github_token,
                getattr(settings, "fallback_api_key", ""),
            ),
            usage_include=False,
        )

    @classmethod
    def from_profile(
        cls,
        *,
        model,
        base_url,
        api_key,
        max_output_tokens=2048,
        tool_definitions=None,
        validator=None,
        known_secrets=(),
        usage_include=False,
        temperature=None,
        top_p=None,
        seed=None,
        timeout=60,
        tool_choice=None,
        http_client=None,
    ):
        if not api_key:
            raise ValueError("DECAGENT_MAIN_API_KEY is required for model requests")
        options = {}
        if usage_include:
            options["extra_body"] = {"usage": {"include": True}}
        request_parameters = {}
        if temperature is not None:
            request_parameters["temperature"] = temperature
        if top_p is not None:
            request_parameters["top_p"] = top_p
        if seed is not None:
            request_parameters["seed"] = seed
        client = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            use_responses_api=False,
            max_tokens=max_output_tokens,
            timeout=timeout,
            max_retries=0,
            include_response_headers=True,
            http_client=http_client,
            **request_parameters,
            **options,
        )
        return cls(
            client,
            Redactor(known_secrets=known_secrets),
            tool_definitions=tool_definitions,
            validator=validator,
            requested_model=model,
            endpoint=base_url,
            tool_choice=tool_choice,
        )

    @classmethod
    def from_evaluation_profile(
        cls,
        profile,
        *,
        api_key,
        known_secrets=(),
        tool_definitions=None,
        validator=None,
        http_client=None,
    ):
        """Build a general-model client whose wire parameters match a frozen cohort."""
        from decagent.evaluation.business.model_profiles import ModelProfile

        if not isinstance(profile, ModelProfile) or profile.role == "jev":
            raise ValueError("A general-model evaluation profile is required")
        if profile.provider_routing != "direct_endpoint":
            raise ValueError("This model client cannot apply the frozen provider-routing policy")
        parameters = set(profile.unsupported_parameters)
        return cls.from_profile(
            model=profile.model,
            base_url=profile.endpoint,
            api_key=api_key,
            max_output_tokens=profile.max_output_tokens,
            tool_definitions=tool_definitions,
            validator=validator,
            known_secrets=known_secrets,
            temperature=(
                None if "temperature" in parameters else float(profile.temperature)
            ),
            top_p=None if "top_p" in parameters else float(profile.top_p),
            seed=None if "seed" in parameters else profile.seed,
            timeout=profile.timeout_seconds,
            tool_choice=profile.tool_choice,
            http_client=http_client,
        )

    @contextmanager
    def _with_request_timeout(self, timeout_seconds):
        if timeout_seconds is None:
            yield
            return
        if (
            type(timeout_seconds) not in {int, float}
            or timeout_seconds <= 0
            or timeout_seconds > self.max_request_timeout
        ):
            raise ValueError("Per-request deadline must be positive and within the model timeout")
        binding = self.client
        chat_model = getattr(binding, "bound", None)
        root_client = getattr(chat_model, "root_client", None)
        if chat_model is None or not callable(getattr(root_client, "with_options", None)):
            raise PrimaryRequestNotDispatched(reason="input_invalid") from None
        with self._request_timeout_lock:
            original_root = chat_model.root_client
            original_client = chat_model.client
            bounded_root = original_root.with_options(timeout=float(timeout_seconds))
            try:
                chat_model.root_client = bounded_root
                chat_model.client = bounded_root.chat.completions
                yield
            finally:
                chat_model.root_client = original_root
                chat_model.client = original_client

    def respond(self, messages, stream=False, *, timeout_seconds=None):
        """Run one provider attempt with an optional hard HTTP transport timeout."""
        with self._with_request_timeout(timeout_seconds):
            return self._respond(messages, stream=stream)

    def close(self):
        """Close the owned synchronous provider client after its route finishes."""
        with self._request_timeout_lock:
            if self._closed:
                return
            self._closed = True
            bound = getattr(self.client, "bound", None)
            root_client = getattr(bound, "root_client", None)
            close = getattr(root_client, "close", None)
            if callable(close):
                close()

    def _respond(self, messages, stream=False):
        # No provider retry is hidden here: runtime reserves and records every attempt.
        try:
            exceeds_context = len(messages) > self.redactor.max_items
        except Exception:
            raise PrimaryRequestNotDispatched(reason="input_invalid") from None
        if exceeds_context:
            raise PrimaryRequestNotDispatched(reason="context_limit") from None
        try:
            safe_messages = self.redactor.sanitize(messages, surface="model")
        except Exception:
            raise PrimaryRequestNotDispatched(reason="input_invalid") from None
        if stream:
            combined = None
            usage, usage_invalid, model = None, False, None
            model_seen, model_conflict = False, False
            request_id, request_id_seen, request_id_conflict = None, False, False
            provider_failure = None
            try:
                chunks = iter(self.client.stream(safe_messages))
            except Exception as error:
                if isinstance(error, (PrimaryResponseRejected, PrimaryRequestNotDispatched)):
                    provider_failure = ValueError("Provider streaming request failed")
                else:
                    provider_failure = _classify_provider_failure(error, self.redactor) or error
            if provider_failure is not None:
                raise provider_failure from None
            while True:
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                except Exception as error:
                    if isinstance(error, (PrimaryResponseRejected, PrimaryRequestNotDispatched)):
                        provider_failure = ValueError("Provider streaming request failed")
                    else:
                        provider_failure = _classify_provider_failure(error, self.redactor) or error
                if provider_failure is not None:
                    raise provider_failure from None
                if not isinstance(chunk, AIMessageChunk):
                    raise ValueError("Unsupported streaming message") from None
                chunk_usage, invalid = _response_usage(chunk.usage_metadata)
                usage_invalid = usage_invalid or invalid
                if chunk_usage is not None:
                    usage = (
                        chunk_usage
                        if usage is None
                        else ModelUsage(
                            usage.input_tokens + chunk_usage.input_tokens,
                            usage.output_tokens + chunk_usage.output_tokens,
                            usage.total_tokens + chunk_usage.total_tokens,
                            cached_input_tokens=(
                                usage.cached_input_tokens + chunk_usage.cached_input_tokens
                                if usage.cached_input_tokens is not None
                                and chunk_usage.cached_input_tokens is not None
                                else None
                            ),
                        )
                    )
                metadata = chunk.response_metadata
                if type(metadata) is dict and "id" in metadata:
                    candidate_id = _safe_model_identifier(metadata["id"], self.redactor)
                    request_id_conflict = (
                        request_id_conflict
                        or candidate_id is None
                        or (request_id_seen and request_id != candidate_id)
                    )
                    request_id, request_id_seen = candidate_id, True
                if type(metadata) is dict and "model_name" in metadata:
                    candidate = _safe_model_identifier(metadata["model_name"], self.redactor)
                    model_conflict = (
                        model_conflict
                        or candidate is None
                        or (model_seen and model != candidate)
                    )
                    model, model_seen = candidate, True
                    metadata = {
                        key: value
                        for key, value in metadata.items()
                        if key not in {"model_name", "id"}
                    }
                # LangChain addition can coerce usage and concatenate model identifiers.
                # Preserve those fields independently instead of trusting the merged DTO.
                try:
                    content_chunk = chunk.model_copy(
                        update={"usage_metadata": None, "response_metadata": metadata}
                    )
                    combined = content_chunk if combined is None else combined + content_chunk
                except Exception:
                    raise ValueError("Malformed streaming message") from None
            if combined is None:
                raise PrimaryResponseRejected(reason="empty_stream") from None
            response = combined
            if model_conflict:
                model = None
            if request_id_conflict:
                request_id = None
            if usage_invalid:
                usage = None
        else:
            provider_failure = None
            try:
                response = self.client.invoke(safe_messages)
            except Exception as error:
                if isinstance(error, (PrimaryResponseRejected, PrimaryRequestNotDispatched)):
                    provider_failure = ValueError("Provider request failed")
                else:
                    provider_failure = _classify_provider_failure(error, self.redactor) or error
            if provider_failure is not None:
                raise provider_failure from None
            if not isinstance(response, AIMessage) or isinstance(response, AIMessageChunk):
                raise ValueError("Malformed model response") from None
            usage, usage_invalid = _response_usage(response.usage_metadata)
            metadata = response.response_metadata
            model = _safe_model_identifier(
                metadata.get("model_name") if type(metadata) is dict else None, self.redactor
            )
            response_headers = metadata.get("headers") if type(metadata) is dict else None
            request_identifier = metadata.get("id") if type(metadata) is dict else None
            if type(response_headers) is dict:
                # Prefer the HTTP trace ID; the compatible body ID remains a fallback.
                header_request_id = next(
                    (
                        value
                        for key, value in response_headers.items()
                        if type(key) is str
                        and key.casefold() in {"x-request-id", "request-id"}
                        and value
                    ),
                    None,
                )
                request_identifier = header_request_id or request_identifier
            request_id = _safe_request_identifier(
                request_identifier, self.redactor
            )
        if type(response.tool_calls) is not list or type(response.invalid_tool_calls) is not list:
            raise PrimaryResponseRejected(
                reason="malformed_response", usage=usage, model=model,
                request_id=request_id,
            ) from None
        if response.invalid_tool_calls:
            raise PrimaryResponseRejected(
                reason="invalid_tool_calls", usage=usage, model=model,
                request_id=request_id,
            ) from None
        if type(response.content) is not str:
            raise PrimaryResponseRejected(
                reason="content_type", usage=usage, model=model, request_id=request_id
            ) from None
        if usage_invalid:
            raise PrimaryResponseRejected(
                reason="usage_invalid", model=model, request_id=request_id
            ) from None
        proposals, seen = [], set()
        for call in response.tool_calls:
            if type(call) is not dict:
                raise PrimaryResponseRejected(
                    reason="malformed_response", usage=usage, model=model,
                    request_id=request_id,
                ) from None
            identity = call.get("id")
            if type(identity) is not str or not identity or identity in seen:
                raise PrimaryResponseRejected(
                    reason="tool_identity", usage=usage, model=model,
                    request_id=request_id,
                ) from None
            seen.add(identity)
            try:
                params = self.validate_tool(call["name"], call["args"])
            except ModelValidationError as error:
                raise PrimaryResponseRejected(
                    reason="tool_schema", usage=usage, model=model,
                    request_id=request_id,
                    validation_issues=error.validation_issues,
                ) from None
            except Exception:
                raise PrimaryResponseRejected(
                    reason="tool_schema", usage=usage, model=model,
                    request_id=request_id,
                ) from None
            try:
                exact_safe_record(primitive(params), self.redactor)
            except Exception:
                raise PrimaryResponseRejected(
                    reason="unsafe_tool_proposal", usage=usage, model=model,
                    request_id=request_id,
                ) from None
            proposals.append(ToolProposal(identity, call["name"], params))
        return ModelTurn(
            self.redactor.redact_text(response.content),
            tuple(proposals),
            usage,
            model,
            request_id,
        )

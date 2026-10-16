"""Normalize LangChain chat completions; tool execution belongs to the governed runtime."""

import json
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

from decagent.policy import Redactor, validate_tool
from decagent.policy.canonical import primitive
from decagent.policy.tools import SCHEMAS
from decagent.storage.safe_record import exact_safe_record


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


_REJECTION_MESSAGES = {
    "tool_schema": "Malformed tool proposal: Unregistered tool or Missing required parameter",
    "tool_identity": "Missing or duplicate tool call identity",
    "invalid_tool_calls": "Malformed model response: invalid tool calls",
    "content_type": "Provider must return textual chat-completion content",
    "usage_invalid": "Invalid model usage",
    "unsafe_tool_proposal": "Tool proposal contains suspected credentials or unsupported bounds",
    "malformed_response": "Malformed model response",
    "empty_stream": "Provider returned an empty stream",
}
_PREFLIGHT_MESSAGES = {
    "context_limit": "Context must be compacted before model input",
    "input_invalid": "Invalid model input before provider dispatch",
}
_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", re.ASCII)


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
    return ModelUsage(*values), False


def _safe_model_identifier(value, redactor=None):
    if type(value) is not str or _MODEL_IDENTIFIER.fullmatch(value) is None:
        return None
    secrets = redactor.known_secrets if redactor is not None else ()
    if Redactor(known_secrets=secrets).redact_text(value) != value:
        return None
    return value


class PrimaryResponseRejected(ValueError):
    """A completed response was rejected, with only independently safe billing metadata."""

    def __init__(self, *, reason, usage=None, model=None):
        if type(reason) is not str or reason not in _REJECTION_MESSAGES:
            raise ValueError("Unsupported primary rejection reason") from None
        self.reason = reason
        self.usage = None
        if type(usage) is ModelUsage:
            values = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
            if _valid_usage_values(values):
                self.usage = ModelUsage(*values)
        self.model = _safe_model_identifier(model)
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
    def __init__(self, client, redactor=None, *, tool_definitions=None, validator=None):
        if (tool_definitions is None) != (validator is None):
            raise ValueError("Capability definitions and validator must be injected together")
        definitions = tool_schemas() if tool_definitions is None else tool_definitions
        self.schema_bytes = len(json.dumps(definitions, ensure_ascii=False).encode("utf-8"))
        self.client = client.bind_tools(definitions)
        self.validate_tool = validator or validate_tool
        self.redactor = redactor or Redactor()

    @classmethod
    def from_settings(
        cls, settings, max_output_tokens=2048, *, tool_definitions=None, validator=None
    ):
        if not settings.main_api_key:
            raise ValueError("DECAGENT_MAIN_API_KEY is required for model requests")
        client = ChatOpenAI(
            model=settings.main_model,
            base_url=settings.main_base_url,
            api_key=settings.main_api_key,
            use_responses_api=False,
            max_tokens=max_output_tokens,
            timeout=60,
            max_retries=0,
        )
        return cls(
            client,
            Redactor(
                known_secrets=(
                    settings.main_api_key,
                    settings.jev_api_key,
                    settings.github_token,
                )
            ),
            tool_definitions=tool_definitions,
            validator=validator,
        )

    def respond(self, messages, stream=False):
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
            try:
                for chunk in self.client.stream(safe_messages):
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
                            )
                        )
                    metadata = chunk.response_metadata
                    if type(metadata) is dict and "model_name" in metadata:
                        candidate = _safe_model_identifier(metadata["model_name"], self.redactor)
                        model_conflict = (
                            model_conflict
                            or candidate is None
                            or (model_seen and model != candidate)
                        )
                        model, model_seen = candidate, True
                        metadata = {
                            key: value for key, value in metadata.items() if key != "model_name"
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
            except (PrimaryResponseRejected, PrimaryRequestNotDispatched):
                # A client exception cannot certify completion or non-dispatch.
                raise ValueError("Provider streaming request failed") from None
            if combined is None:
                raise PrimaryResponseRejected(reason="empty_stream") from None
            response = combined
            if model_conflict:
                model = None
            if usage_invalid:
                usage = None
        else:
            try:
                response = self.client.invoke(safe_messages)
            except (PrimaryResponseRejected, PrimaryRequestNotDispatched):
                raise ValueError("Provider request failed") from None
            if not isinstance(response, AIMessage) or isinstance(response, AIMessageChunk):
                raise ValueError("Malformed model response") from None
            usage, usage_invalid = _response_usage(response.usage_metadata)
            metadata = response.response_metadata
            model = _safe_model_identifier(
                metadata.get("model_name") if type(metadata) is dict else None, self.redactor
            )
        if type(response.tool_calls) is not list or type(response.invalid_tool_calls) is not list:
            raise PrimaryResponseRejected(
                reason="malformed_response", usage=usage, model=model
            ) from None
        if response.invalid_tool_calls:
            raise PrimaryResponseRejected(
                reason="invalid_tool_calls", usage=usage, model=model
            ) from None
        if type(response.content) is not str:
            raise PrimaryResponseRejected(reason="content_type", usage=usage, model=model) from None
        if usage_invalid:
            raise PrimaryResponseRejected(reason="usage_invalid", model=model) from None
        proposals, seen = [], set()
        for call in response.tool_calls:
            if type(call) is not dict:
                raise PrimaryResponseRejected(
                    reason="malformed_response", usage=usage, model=model
                ) from None
            identity = call.get("id")
            if type(identity) is not str or not identity or identity in seen:
                raise PrimaryResponseRejected(
                    reason="tool_identity", usage=usage, model=model
                ) from None
            seen.add(identity)
            try:
                params = self.validate_tool(call["name"], call["args"])
            except Exception:
                raise PrimaryResponseRejected(
                    reason="tool_schema", usage=usage, model=model
                ) from None
            try:
                exact_safe_record(primitive(params), self.redactor)
            except Exception:
                raise PrimaryResponseRejected(
                    reason="unsafe_tool_proposal", usage=usage, model=model
                ) from None
            proposals.append(ToolProposal(identity, call["name"], params))
        return ModelTurn(
            self.redactor.redact_text(response.content),
            tuple(proposals),
            usage,
            model,
        )

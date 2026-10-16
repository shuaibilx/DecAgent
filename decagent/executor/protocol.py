"""Small internal broker contract. Callers cannot configure Docker or task credentials."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decagent.policy.paths import normalize_relative_path


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    task_id: str
    owner: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    lease_generation: int = Field(ge=1)

    @field_validator("task_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Identifiers must be canonical UUIDs")
        return value


class ExecutionRequest(ScopeRequest):
    action_id: str
    execution_id: str
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    argv: list[str] = Field(min_length=1, max_length=100)
    cwd: str = "."
    kind: Literal["command", "setup"] = "command"
    purpose: str = Field(default="command", min_length=1, max_length=200)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_output_bytes: int = Field(default=65536, ge=32, le=1_048_576)

    @field_validator("action_id", "execution_id")
    @classmethod
    def other_identifier(cls, value: str) -> str:
        return cls.identifier(value)

    @field_validator("cwd")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        return normalize_relative_path(value)

    @field_validator("argv")
    @classmethod
    def bounded_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\0" in item or len(item) > 16384 for item in value):
            raise ValueError("Invalid argument")
        if sum(len(item) for item in value) > 65536:
            raise ValueError("Arguments exceed limit")
        return value

    @model_validator(mode="after")
    def bounded_timeout(self):
        maximum = 600 if self.kind == "setup" else 120
        if self.timeout_seconds is None:
            object.__setattr__(self, "timeout_seconds", maximum)
        if self.timeout_seconds > maximum:
            raise ValueError("Timeout exceeds command class limit")
        return self

    @property
    def payload_hash(self) -> str:
        data = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(data).hexdigest()


class WorkspaceRequest(ScopeRequest):
    archive_base64: str = Field(min_length=1, max_length=72_000_000)


class FileRequest(ScopeRequest):
    path: str
    content_base64: str | None = Field(default=None, max_length=1_400_000)

    @field_validator("path")
    @classmethod
    def relative_file(cls, value: str) -> str:
        return normalize_relative_path(value)


class WriteRequest(FileRequest):
    action_id: str
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_base64: str = Field(min_length=0, max_length=1_400_000)

    @field_validator("action_id")
    @classmethod
    def action_identifier(cls, value: str) -> str:
        return cls.identifier(value)


class ExecutionScope(ScopeRequest):
    execution_id: str

    @field_validator("execution_id")
    @classmethod
    def execution_identifier(cls, value: str) -> str:
        return cls.identifier(value)


class TaskControlRequest(BaseModel):
    """Owner control operation, including recovery after the worker lease has expired."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    task_id: str
    owner: str | None = None
    lease_generation: int = Field(ge=0)

    @field_validator("task_id")
    @classmethod
    def task_identifier(cls, value: str) -> str:
        return ScopeRequest.identifier(value)

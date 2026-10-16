"""Typed repair values. Command provenance is retained for governance and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from decagent.storage.evidence import EvidenceRef


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    source: str
    kind: Literal["command", "setup"] = "command"
    timeout_seconds: int = 120


@dataclass(frozen=True)
class ProjectPlan:
    python_version: str
    toolchain: str
    setup: tuple[Command, ...]
    regression: Command
    declarations: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    state: str
    exit_code: int | None
    output: str
    truncated: bool = False


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckReport(FrozenModel):
    collected: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    xfailed: tuple[str, ...] = ()
    exit_code: int
    failure_details: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.exit_code == 0
            and bool(self.collected)
            and not self.failed
            and not self.errors
            and not self.skipped
            and not self.xfailed
        )


class CheckEvidence(FrozenModel):
    report: CheckReport
    execution_id: str
    log_ref: EvidenceRef
    classification: Literal["related_failure", "environment_error", "unreproduced", "passed"]
    command: tuple[str, ...] = ()
    runtime_environment: tuple[tuple[str, str], ...] = ()


class ReproductionBundle(FrozenModel):
    version: int = 1
    task_id: str
    issue: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    baseline_tree: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_digest: str = Field(pattern=r"^(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}$")
    python_version: str
    toolchain: str
    dependencies: tuple[tuple[str, str], ...]
    setup_commands: tuple[tuple[str, ...], ...]
    reproduction_command: tuple[str, ...]
    regression_command: tuple[str, ...]
    checker_sources: tuple[tuple[str, str], ...]
    regression_sources: tuple[tuple[str, str], ...]
    configuration_sources: tuple[tuple[str, str], ...] = ()
    checker_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    regression_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    related_cases: tuple[str, ...]
    baseline: CheckEvidence
    original_regression_cases: tuple[str, ...]
    previous_bundle: str | None = None
    acceptance_approval: str | None = None


class VerificationRecord(FrozenModel):
    task_id: str
    base_sha: str
    repair_sha: str
    tree_hash: str
    bundle_ref: EvidenceRef
    checker_hash: str
    runner_digest: str
    independent: CheckEvidence
    regression: CheckEvidence
    valid: bool
    reasons: tuple[str, ...]
    test_changes: tuple[tuple[str, str], ...] = ()

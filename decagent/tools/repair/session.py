"""Repair orchestration over governed capabilities, without any host process fallback."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from decagent.executor.archive import snapshot_hash
from decagent.policy import Approval, ApprovalKind, valid_confirmation
from decagent.storage.evidence import EvidenceRef

from .backend import RepairBackend
from .bundles import BundleStore
from .changes import analyze_test_changes, is_test
from .check_runner import MARKER, _validate
from .inference import infer_project
from .models import (
    CheckEvidence,
    CheckReport,
    Command,
    ExecutionResult,
    ProjectPlan,
    ReproductionBundle,
    VerificationRecord,
)


def material_hash(sources: tuple[tuple[str, str], ...], targets=()) -> str:
    return hashlib.sha256(
        json.dumps({"sources": sorted(sources), "targets": targets}, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ValidationCommand(Command):
    checker_hash: str = ""
    code_source: Literal["workspace", "baseline"] = "workspace"
    collect_only: bool = False


def validation_command(
    sources, targets, *, collect_only=False, code_source="workspace", purpose="baseline"
):
    sources = tuple(sorted(sources))
    payload = {
        "sources": dict(sources),
        "targets": list(targets),
        "collect_only": collect_only,
        "code_source": code_source,
    }
    _validate(payload)
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    if len(encoded) > 16000:
        raise ValueError("Fixed material exceeds supported broker argument size")
    return ValidationCommand(
        ("python", "-P", "-m", "decagent_check", encoded),
        "immutable reproduction material:" + purpose,
        checker_hash=material_hash(sources, targets),
        code_source=code_source,
        collect_only=collect_only,
    )


def frozen_test_sources(files: Mapping[str, bytes]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path, content.decode("utf-8-sig"))
        for path, content in sorted(files.items())
        if is_test(path)
        or path.endswith(".py")
        and any(part in {"test", "tests"} for part in path.split("/")[:-1])
    )


def targets_for_command(command: tuple[str, ...], sources) -> tuple[str, ...]:
    if command[:3] != ("python", "-m", "pytest"):
        raise ValueError("Only controlled Python pytest regression is supported")
    scope = command[3:]
    tests = tuple(path for path, _ in sources if is_test(path))
    if any(target.startswith("-") or ".." in target.split("/") for target in scope):
        raise ValueError("Regression options require acceptance confirmation")
    if not scope:
        return tests
    return tuple(
        path
        for path in tests
        if any(path == target or path.startswith(target.rstrip("/") + "/") for target in scope)
    )


class RepairSession:
    def __init__(
        self,
        task_id: str,
        backend: RepairBackend,
        store: BundleStore,
        *,
        runner_digest: str,
        record_sink: Callable[[VerificationRecord], None] | None = None,
    ):
        self.task_id, self.backend, self.store = task_id, backend, store
        self.runner_digest, self.record_sink = runner_digest, record_sink
        self.preparation: tuple[ExecutionResult, ...] = ()
        self.dependency_snapshot: tuple[tuple[str, str], ...] = ()

    def prepare(self, plan: ProjectPlan) -> tuple[ExecutionResult, ...]:
        if plan.blockers:
            raise ValueError("Preparation blocked: " + "; ".join(plan.blockers))
        runs = []
        for command in plan.setup:
            result = self.backend.execute(command)
            runs.append(result)
            if result.state != "completed" or result.exit_code != 0 or result.truncated:
                self.preparation = tuple(runs)
                raise ValueError("Environment preparation failed; no defect reproduction claimed")
        dependencies = self.backend.execute(
            Command(
                ("python", "-m", "pip", "--python", ".venv", "freeze", "--all"),
                "installed dependency snapshot",
            )
        )
        if (
            dependencies.state != "completed"
            or dependencies.exit_code != 0
            or dependencies.truncated
        ):
            raise ValueError("Cannot snapshot prepared dependencies")
        reference = self.store.evidence.write(dependencies.output, "repair:dependencies")
        self.dependency_snapshot = (("installed-dependencies", reference.sha256),)
        self.preparation = tuple(runs)
        return self.preparation

    def _check(
        self,
        sources,
        targets,
        *,
        related=(),
        collect_only=False,
        code_source="workspace",
        purpose="baseline",
    ):
        command = validation_command(
            sources,
            targets,
            collect_only=collect_only,
            code_source=code_source,
            purpose=purpose,
        )
        result = self.backend.execute(command)
        log_ref = self.store.evidence.write(result.output, "repair:check-log")
        report = CheckReport(exit_code=result.exit_code if result.exit_code is not None else 3)
        classification = "environment_error"
        environment = ()
        if result.state == "completed" and not result.truncated:
            lines = [
                line[len(MARKER) :]
                for line in result.output.splitlines()
                if line.startswith(MARKER)
            ]
            if len(lines) == 1:
                try:
                    data = json.loads(lines[0])
                    data.pop("log", None)
                    environment = tuple(sorted(data.pop("environment", {}).items()))
                    parsed = CheckReport.model_validate(data)
                    if parsed.exit_code == result.exit_code:
                        report = parsed
                        if report.errors or report.exit_code in {2, 3, 4}:
                            classification = "environment_error"
                        elif report.passed:
                            classification = "passed"
                        elif report.failed and set(related) & set(report.failed):
                            classification = "related_failure"
                        else:
                            classification = "unreproduced"
                except (ValueError, TypeError):
                    pass
        return CheckEvidence(
            report=report,
            execution_id=result.execution_id,
            log_ref=log_ref,
            classification=classification,
            command=command.argv,
            runtime_environment=environment,
        )

    def reproduce(
        self,
        *,
        base_sha: str,
        issue: str,
        related_cases: tuple[str, ...],
        reproduction_targets: tuple[str, ...],
        plan: ProjectPlan | None = None,
    ) -> EvidenceRef:
        if not related_cases:
            raise ValueError("Issue-related fixed test cases are required")
        files = self.backend.snapshot()
        plan = plan or infer_project(files)
        if plan.blockers:
            raise ValueError("Reproduction blocked: " + "; ".join(plan.blockers))
        sources = frozen_test_sources(files)
        targets = targets_for_command(plan.regression.argv, sources)
        baseline = self._check(sources, reproduction_targets, related=related_cases)
        regression = self._check(
            sources, targets, collect_only=True, purpose="regression_collection"
        )
        if snapshot_hash(self.backend.snapshot()) != snapshot_hash(files):
            raise ValueError("Baseline changed during reproduction; discard and repeat")
        bundle = ReproductionBundle(
            task_id=self.task_id,
            issue=issue,
            base_sha=base_sha,
            baseline_tree=snapshot_hash(files),
            runner_digest=self.runner_digest,
            python_version=plan.python_version,
            toolchain=plan.toolchain,
            dependencies=plan.declarations + self.dependency_snapshot,
            setup_commands=tuple(command.argv for command in plan.setup),
            reproduction_command=reproduction_targets,
            regression_command=plan.regression.argv,
            configuration_sources=tuple(
                (path, content.decode("utf-8-sig"))
                for path, content in sorted(files.items())
                if path in {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"}
            ),
            checker_sources=sources,
            regression_sources=sources,
            checker_hash=material_hash(sources, reproduction_targets),
            regression_hash=material_hash(sources, targets),
            related_cases=related_cases,
            baseline=baseline,
            original_regression_cases=regression.report.collected,
        )
        return self.store.save(bundle)

    def verify(self, reference: EvidenceRef, *, repair_sha: str) -> VerificationRecord:
        bundle = self.store.load(reference)
        if bundle.task_id != self.task_id or bundle.runner_digest != self.runner_digest:
            raise ValueError("Bundle task/environment does not match")
        if (
            material_hash(bundle.checker_sources, bundle.reproduction_command)
            != bundle.checker_hash
        ):
            raise ValueError("Frozen checker material hash changed")
        files = self.backend.snapshot()
        tree_hash = snapshot_hash(files)
        independent = self._check(
            bundle.checker_sources, bundle.reproduction_command, purpose="independent"
        )
        regression = self._check(
            bundle.regression_sources,
            targets_for_command(bundle.regression_command, bundle.regression_sources),
            purpose="regression",
        )
        reasons = []
        if bundle.baseline.classification != "related_failure":
            reasons.append("related_baseline_failure_missing")
        if not independent.report.passed or independent.classification != "passed":
            reasons.append("independent_check_not_passed")
        if independent.report.collected != bundle.baseline.report.collected:
            reasons.append("independent_collection_changed")
        if not regression.report.passed or regression.classification != "passed":
            reasons.append("regression_not_passed")
        if (
            not bundle.original_regression_cases
            or regression.report.collected != bundle.original_regression_cases
        ):
            reasons.append("regression_scope_changed_or_empty")
        if snapshot_hash(self.backend.snapshot()) != tree_hash:
            reasons.append("workspace_changed_during_verification")
        baseline_tests = {
            path: content.encode()
            for path, content in (*bundle.regression_sources, *bundle.configuration_sources)
        }
        changes = analyze_test_changes(baseline_tests, files)
        record = VerificationRecord(
            task_id=self.task_id,
            base_sha=bundle.base_sha,
            repair_sha=repair_sha,
            tree_hash=tree_hash,
            bundle_ref=reference,
            checker_hash=bundle.checker_hash,
            runner_digest=self.runner_digest,
            independent=independent,
            regression=regression,
            valid=not reasons,
            reasons=tuple(reasons),
            test_changes=changes,
        )
        if self.record_sink:
            self.record_sink(record)
        return record

    def publishable(self, record: VerificationRecord) -> bool:
        return (
            record.valid
            and record.task_id == self.task_id
            and record.runner_digest == self.runner_digest
            and snapshot_hash(self.backend.snapshot()) == record.tree_hash
        )

    def acceptance_digest(
        self,
        reference,
        sources,
        targets,
        owner_id,
        *,
        regression_sources=None,
        regression_command=None,
    ):
        subject = {
            "task_id": self.task_id,
            "owner_id": owner_id,
            "previous": reference.sha256,
            "sources": sorted(sources.items()),
            "targets": targets,
            "runner": self.runner_digest,
            "regression_sources": (
                sorted(regression_sources.items()) if regression_sources is not None else None
            ),
            "regression_command": regression_command,
        }
        return hashlib.sha256(json.dumps(subject, sort_keys=True).encode()).hexdigest()

    def change_acceptance(
        self,
        reference: EvidenceRef,
        sources: Mapping[str, str],
        targets: tuple[str, ...],
        approval: Approval,
        owner_id: str,
        *,
        related_cases: tuple[str, ...] = (),
        regression_sources: Mapping[str, str] | None = None,
        regression_command: tuple[str, ...] | None = None,
    ) -> EvidenceRef:
        expected = self.acceptance_digest(
            reference,
            sources,
            targets,
            owner_id,
            regression_sources=regression_sources,
            regression_command=regression_command,
        )
        if not valid_confirmation(approval, ApprovalKind.ACCEPTANCE, expected, owner_id):
            raise ValueError("Acceptance requires authenticated confirmation of exact proposal")
        original = self.store.load(reference)
        if original.task_id != self.task_id:
            raise ValueError("Acceptance bundle belongs to another task")
        material = tuple(sorted(sources.items()))
        baseline = self._check(material, targets, related=related_cases, code_source="baseline")
        regression_material = (
            tuple(sorted(regression_sources.items()))
            if regression_sources is not None
            else original.regression_sources
        )
        regression_argv = regression_command or original.regression_command
        regression_targets = targets_for_command(regression_argv, regression_material)
        regression_cases = original.original_regression_cases
        if regression_sources is not None or regression_command is not None:
            collected = self._check(
                regression_material,
                regression_targets,
                collect_only=True,
                code_source="baseline",
            )
            regression_cases = collected.report.collected
        updated = original.model_copy(
            update={
                "checker_sources": material,
                "checker_hash": material_hash(material, targets),
                "reproduction_command": targets,
                "related_cases": related_cases,
                "regression_sources": regression_material,
                "regression_command": regression_argv,
                "regression_hash": material_hash(regression_material, regression_targets),
                "original_regression_cases": regression_cases,
                "baseline": baseline,
                "previous_bundle": reference.sha256,
                "acceptance_approval": approval.id,
            }
        )
        return self.store.save(updated)

    def export_result(self, record: VerificationRecord | None = None) -> dict:
        verified = record is not None and self.publishable(record)
        return {
            "status": "verified" if verified else "unverified",
            "diff": self.backend.diff(),
            "verification": record.model_dump(mode="json") if record else None,
            "reasons": list(record.reasons) if record else ["independent_verification_incomplete"],
        }


def validate_validation_intent(action, intent):
    """Validate a controller-created capability; model DTOs must never become this object."""
    if type(intent) is not ValidationCommand or action.tool != "run_command":
        raise ValueError("Trusted fixed validation intent required")
    arguments = tuple(action.params.get("argv", ()))
    if (
        len(arguments) != 5
        or arguments[:4] != ("python", "-P", "-m", "decagent_check")
        or arguments != intent.argv
        or intent.kind != "command"
        or action.params.get("cwd", ".") != "."
        or action.params.get("timeout_seconds", 120) != intent.timeout_seconds
        or action.params.get("max_output_bytes", 65536) != 65536
    ):
        raise ValueError("Fixed validation command does not match intent")
    try:
        payload = json.loads(base64.b64decode(arguments[4], validate=True))
        _validate(payload)
        sources = tuple(sorted(payload["sources"].items()))
        if (
            material_hash(sources, payload["targets"]) != intent.checker_hash
            or payload.get("code_source", "workspace") != intent.code_source
            or payload["collect_only"] is not intent.collect_only
        ):
            raise ValueError("Fixed validation payload changed")
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Invalid fixed validation payload") from error

"""A repair never succeeds merely because editable tests pass."""

import json
from dataclasses import replace

import pytest

from decagent.policy import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    Provenance,
    Source,
)
from decagent.storage.evidence import EvidenceStore
from decagent.tools.repair import (
    BundleStore,
    ExecutionResult,
    RepairSession,
    analyze_test_changes,
)
from decagent.tools.repair.check_runner import MARKER

FILES = {
    "requirements.txt": b"pytest==8.4.2\n",
    "logic.py": b"def answer(): return 0\n",
    "test_logic.py": b"from logic import answer\ndef test_answer(): assert answer() == 42\n",
}
CASE = "test_logic.py::test_answer"


def result(*, failed=(), errors=(), collected=(CASE,), skipped=(), exit_code=0):
    report = {
        "collected": list(collected),
        "failed": list(failed),
        "errors": list(errors),
        "skipped": list(skipped),
        "xfailed": [],
        "failure_details": [],
        "exit_code": exit_code,
        "log": "fixed-check log",
    }
    return ExecutionResult("run-1", "completed", exit_code, MARKER + json.dumps(report))


class Backend:
    def __init__(self, runs):
        self.files = dict(FILES)
        self.runs = list(runs)
        self.commands = []

    def snapshot(self):
        return dict(self.files)

    def list_files(self):
        return tuple(self.files)

    def read(self, path):
        return self.files[path]

    def write(self, path, content):
        self.files[path] = content

    def diff(self):
        return "full patch"

    def execute(self, command):
        self.commands.append(command)
        return self.runs.pop(0)


def session(tmp_path, runs, record_sink=None):
    backend = Backend(runs)
    store = BundleStore(EvidenceStore(tmp_path / "evidence"), workspace_root=tmp_path / "repo")
    service = RepairSession(
        "task-1",
        backend,
        store,
        runner_digest="sha256:" + "b" * 64,
        record_sink=record_sink,
    )
    return backend, service


def reproduce(service):
    return service.reproduce(
        base_sha="a" * 40,
        issue="answer should be 42",
        related_cases=(CASE,),
        reproduction_targets=("test_logic.py",),
    )


def test_baseline_bundle_is_external_frozen_and_preserves_exact_material(tmp_path):
    backend, service = session(tmp_path, [result(failed=(CASE,), exit_code=1), result()])
    reference = reproduce(service)
    bundle = service.store.load(reference)
    assert bundle.baseline.classification == "related_failure"
    assert bundle.original_regression_cases == (CASE,)
    assert bundle.base_sha == "a" * 40
    assert bundle.runner_digest == "sha256:" + "b" * 64
    backend.write("test_logic.py", b"def test_answer(): pass\n")
    assert service.store.load(reference).checker_sources == bundle.checker_sources
    assert service.store.evidence.root != tmp_path / "repo"


def test_environment_failure_and_unrelated_failure_cannot_be_baseline_proof(tmp_path):
    _, service = session(tmp_path, [result(errors=("missing_dependency",), exit_code=2), result()])
    ref = reproduce(service)
    assert service.store.load(ref).baseline.classification == "environment_error"
    _, other = session(tmp_path, [result(failed=("other::test_other",), exit_code=1), result()])
    assert other.store.load(reproduce(other)).baseline.classification == "unreproduced"


def test_independent_and_original_regression_must_pass_on_current_tree(tmp_path):
    records = []
    backend, service = session(
        tmp_path,
        [
            result(failed=(CASE,), exit_code=1),
            result(),
            result(),
            result(),
        ],
        records.append,
    )
    ref = reproduce(service)
    backend.write("logic.py", b"def answer(): return 42\n")
    verified = service.verify(ref, repair_sha="c" * 40)
    assert verified.valid
    assert service.publishable(verified)
    assert records == [verified]
    assert verified.independent.report.collected == (CASE,)
    backend.write("logic.py", b"def answer(): return 0\n")
    assert not service.publishable(verified)
    assert service.export_result(verified)["status"] == "unverified"


@pytest.mark.parametrize("regression", [result(collected=(), exit_code=0), result(exit_code=1)])
def test_zero_tests_or_failed_regression_block_delivery(tmp_path, regression):
    backend, service = session(
        tmp_path,
        [
            result(failed=(CASE,), exit_code=1),
            result(),
            result(),
            regression,
        ],
    )
    ref = reproduce(service)
    backend.write("logic.py", b"def answer(): return 42\n")
    verification = service.verify(ref, repair_sha="c" * 40)
    assert not verification.valid
    assert not service.publishable(verification)
    assert service.export_result(verification)["diff"] == "full patch"


def test_mutable_test_changes_cannot_alter_runner_payload(tmp_path):
    backend, service = session(
        tmp_path,
        [
            result(failed=(CASE,), exit_code=1),
            result(),
            result(failed=(CASE,), exit_code=1),
            result(),
        ],
    )
    ref = reproduce(service)
    backend.write("test_logic.py", b"def test_answer(): pass\n")
    verification = service.verify(ref, repair_sha="c" * 40)
    assert not verification.valid
    assert any(reason == "assertion_changed_or_removed" for _, reason in verification.test_changes)
    assert service.store.load(ref).checker_hash == verification.checker_hash


@pytest.mark.parametrize(
    "replacement,kind",
    [
        (None, "test_file_deleted"),
        (b"import pytest\n@pytest.mark.skip\ndef test_answer(): assert True\n", "skip_added"),
        (b"import pytest\n@pytest.mark.xfail\ndef test_answer(): assert True\n", "xfail_added"),
        (b"def test_answer(): assert True\n", "assertion_changed_or_removed"),
        (b"def not_a_test(): pass\n", "test_scope_reduced"),
    ],
)
def test_weakening_signals_are_explicit(replacement, kind):
    current = dict(FILES)
    if replacement is None:
        current.pop("test_logic.py")
    else:
        current["test_logic.py"] = replacement
    assert ("test_logic.py", kind) in analyze_test_changes(FILES, current)


def test_acceptance_requires_authenticated_confirmation_bound_to_exact_proposal(tmp_path):
    _, service = session(tmp_path, [result(failed=(CASE,), exit_code=1), result()])
    reference = reproduce(service)
    proposal = {"test_new.py": "def test_new(): assert True\n"}
    expected = service.acceptance_digest(reference, proposal, ("test_new.py",), "owner")
    approval = Approval("acceptance-1", ApprovalKind.ACCEPTANCE, expected)
    with pytest.raises(ValueError, match="confirmation"):
        service.change_acceptance(reference, proposal, ("test_new.py",), approval, "owner")
    approval = replace(
        approval,
        status=ApprovalStatus.CONFIRMED,
        response_id="response-1",
        response_fingerprint="fingerprint",
        confirmation="accept updated requirement",
        provenance=Provenance(Source.OWNER, "owner", "session", True),
    )
    service.backend.runs.extend([result(failed=("test_new.py::test_new",), exit_code=1), result()])
    new_reference = service.change_acceptance(
        reference,
        proposal,
        ("test_new.py",),
        approval,
        "owner",
        related_cases=("test_new.py::test_new",),
    )
    assert service.store.load(new_reference).previous_bundle == reference.sha256
    assert service.store.load(reference).acceptance_approval is None


def test_evidence_root_inside_writable_repo_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        BundleStore(EvidenceStore(tmp_path / "repo" / "evidence"), workspace_root=tmp_path / "repo")


def test_persistence_rows_bind_same_checker_failure_and_pass(tmp_path):
    from decagent.tools.repair import verification_rows

    backend, service = session(
        tmp_path,
        [
            result(failed=(CASE,), exit_code=1),
            result(),
            result(),
            result(),
        ],
    )
    reference = reproduce(service)
    backend.write("logic.py", b"def answer(): return 42\n")
    record = service.verify(reference, repair_sha="c" * 40)
    rows = verification_rows(record, service.store.load(reference))
    assert [row["kind"] for row in rows] == ["baseline", "independent", "regression"]
    assert [row["status"] for row in rows] == ["failed", "passed", "passed"]
    assert rows[0]["checker_hash"] == rows[1]["checker_hash"]
    assert rows[0]["environment"]["related_failure"] is True
    assert all(row["test_count"] > 0 and row["environment"]["skip_count"] == 0 for row in rows)
    assert rows[1]["tree_hash"] == rows[2]["tree_hash"] == record.tree_hash
    assert all(row["command"][:4] == ["python", "-P", "-m", "decagent_check"] for row in rows)


def test_fixed_checker_digest_binds_targets_as_well_as_sources():
    from decagent.tools.repair import validation_command

    sources = (("test_a.py", "def test_one(): pass\ndef test_two(): pass\n"),)
    first = validation_command(sources, ("test_a.py::test_one",))
    second = validation_command(sources, ("test_a.py::test_two",))
    assert first.checker_hash != second.checker_hash


def test_setup_blocked_plan_never_executes(tmp_path):
    from decagent.tools.repair import infer_project

    backend, service = session(tmp_path, [])
    plan = infer_project({".python-version": b"3.13"})
    with pytest.raises(ValueError, match="blocked"):
        service.prepare(plan)
    assert not backend.commands


def test_preparation_failure_stops_before_hooks_or_reproduction(tmp_path):
    from decagent.tools.repair import infer_project

    backend, service = session(tmp_path, [ExecutionResult("setup", "completed", 1, "missing deps")])
    with pytest.raises(ValueError, match="preparation failed"):
        service.prepare(infer_project(FILES))
    assert len(backend.commands) == 1


def test_successful_preparation_records_dependency_snapshot(tmp_path):
    from decagent.tools.repair import infer_project

    _, service = session(
        tmp_path,
        [
            ExecutionResult("venv", "completed", 0, ""),
            ExecutionResult("pip", "completed", 0, ""),
            ExecutionResult("freeze", "completed", 0, "pytest==8.4.2\n"),
        ],
    )
    assert len(service.prepare(infer_project(FILES))) == 2
    assert service.dependency_snapshot[0][0] == "installed-dependencies"


def test_unchanged_test_configuration_is_not_flagged_as_new(tmp_path):
    backend, service = session(
        tmp_path,
        [
            result(failed=(CASE,), exit_code=1),
            result(),
            result(),
            result(),
        ],
    )
    backend.files["pyproject.toml"] = b'[project]\nname="fixture"\n'
    reference = reproduce(service)
    backend.write("logic.py", b"def answer(): return 42\n")
    record = service.verify(reference, repair_sha="c" * 40)
    assert ("pyproject.toml", "test_configuration_changed") not in record.test_changes


def test_acceptance_can_replace_regression_only_with_bound_owner_confirmation(tmp_path):
    _, service = session(tmp_path, [result(failed=(CASE,), exit_code=1), result()])
    reference = reproduce(service)
    sources = {"test_new.py": "def test_new(): assert True\n"}
    command = ("python", "-m", "pytest", "test_new.py")
    expected = service.acceptance_digest(
        reference,
        sources,
        ("test_new.py",),
        "owner",
        regression_sources=sources,
        regression_command=command,
    )
    approval = Approval(
        "acceptance-2",
        ApprovalKind.ACCEPTANCE,
        expected,
        ApprovalStatus.CONFIRMED,
        response_id="response",
        response_fingerprint="fingerprint",
        confirmation="changed contract",
        provenance=Provenance(Source.OWNER, "owner", "session", True),
    )
    with pytest.raises(ValueError, match="confirmation"):
        service.change_acceptance(reference, sources, ("test_new.py",), approval, "owner")
    service.backend.runs.extend(
        [
            result(
                failed=("test_new.py::test_new",), collected=("test_new.py::test_new",), exit_code=1
            ),
            result(collected=("test_new.py::test_new",)),
        ]
    )
    changed = service.change_acceptance(
        reference,
        sources,
        ("test_new.py",),
        approval,
        "owner",
        related_cases=("test_new.py::test_new",),
        regression_sources=sources,
        regression_command=command,
    )
    updated = service.store.load(changed)
    assert updated.regression_command == command
    assert updated.original_regression_cases == ("test_new.py::test_new",)
    assert service.store.load(reference).original_regression_cases == (CASE,)

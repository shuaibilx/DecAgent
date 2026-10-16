"""A real governed Docker repair, using a disposable executor-only PostgreSQL database."""

import base64
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from decagent.executor.archive import make_archive, snapshot_hash
from decagent.executor.broker import Broker, create_app
from decagent.executor.docker import DockerEngine
from decagent.executor.protocol import ScopeRequest, WorkspaceRequest
from decagent.policy import (
    Action,
    Approval,
    ApprovalKind,
    ApprovalStatus,
    CodeOrigin,
    CodeSnapshot,
    CodeTrust,
    ExecutionBoundary,
    Outcome,
    OwnerAuthorization,
    PolicyContext,
    Provenance,
    Source,
    TrustKind,
    govern,
)
from decagent.policy.canonical import primitive
from decagent.storage.database import Database
from decagent.storage.evidence import EvidenceStore
from decagent.tools.repair import (
    BrokerRepairBackend,
    BundleStore,
    DatabaseVerificationSink,
    GovernedAction,
    RepairSession,
    ValidationCommand,
    infer_project,
)
from decagent.tools.repair.check_runner import MARKER

pytestmark = pytest.mark.integration
OWNER = Provenance(Source.OWNER, "repair-owner", "repair-session", True)
BASE = "a" * 40
CASE = "test_range.py::test_exclusive_upper_bound"


@pytest.fixture
def repair(tmp_path):
    if os.environ.get("DECAGENT_REPAIR_INTEGRATION") != "1":
        pytest.skip("Set DECAGENT_REPAIR_INTEGRATION=1 for owned Docker fixture validation")
    url = os.environ["DECAGENT_TEST_DATABASE_URL"]
    assert "executor-test-only" in url, "Only the disposable executor test database is allowed"
    database = Database(url)
    database.migrate()
    engine = DockerEngine(
        runner_image=os.environ["DECAGENT_RUNNER_IMAGE"],
        proxy_container=os.environ["DECAGENT_PROXY_CONTAINER"],
        control_containers=(
            os.getenv("DECAGENT_EXECUTOR_TEST_PG_CONTAINER", "decagent-executor-pg"),
            os.environ["DECAGENT_REPAIR_TESTHOST"],
        ),
    )
    broker = Broker(database, engine, tmp_path / "receipts")
    broker.initialize()
    project = database.add_project("repair-fixture/" + str(uuid4()))
    task = database.create_task(project["id"], BASE, {"trust_scope": "exact_commit"})
    claim = database.claim(task["id"], "repair-tests", seconds=120)
    scope = ScopeRequest(
        task_id=str(task["id"]),
        owner="repair-tests",
        lease_generation=claim["lease_generation"],
    )
    fixture = Path(__file__).parents[1] / "fixtures" / "repair" / "semantic"
    files = {path.name: path.read_bytes() for path in fixture.glob("*.py")}
    files["requirements.txt"] = b""
    broker.workspace(
        WorkspaceRequest(
            **scope.model_dump(),
            archive_base64=base64.b64encode(make_archive(files)).decode(),
        )
    )
    workspace_root = tmp_path / "workspace-metadata"
    workspace_root.mkdir()
    context = PolicyContext(
        scope.task_id,
        str(project["id"]),
        "repair-fixture/project",
        workspace_root,
        CodeSnapshot(BASE, snapshot_hash(files), "repair", CodeOrigin.OWNER),
        OwnerAuthorization(
            "repair-auth", "1", scope.task_id, str(project["id"]), "repair-fixture/project", OWNER
        ),
        "1",
        CodeTrust(
            "repair-trust", "1", "repair-fixture/project", TrustKind.EXACT_COMMIT, BASE, OWNER
        ),
        ExecutionBoundary(True, True, True, True),
        base_sha=BASE,
    )

    def authorize(action: Action, intent):
        current = replace(
            context,
            snapshot=replace(
                context.snapshot,
                tree_digest=snapshot_hash(broker.files(scope)),
            ),
        )
        verifier = intent if type(intent) is ValidationCommand else None
        decision = govern(action, current, verification_intent=verifier)
        assert decision.outcome in {Outcome.ALLOW, Outcome.REVIEW}, decision.reason
        if decision.outcome == Outcome.REVIEW:
            approval = Approval(
                str(uuid4()),
                ApprovalKind.ACTION,
                decision.digest,
                ApprovalStatus.APPROVED,
                provenance=OWNER,
            )
            decision = govern(action, current, approval=approval, verification_intent=verifier)
        assert decision.outcome == Outcome.ALLOW, decision.reason
        snapshot = primitive(decision.action)
        if intent and intent.kind == "setup":
            snapshot["execution_kind"] = "setup"
        recorded = database.plan_action(scope.task_id, str(uuid4()), decision.digest, snapshot)
        return GovernedAction(str(recorded["id"]), decision.digest)

    token = "repair-test-service-token-" + "x" * 32
    api = TestClient(create_app(broker, token))

    def call(path, payload):
        response = api.post(path, json=payload, headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200, response.text
        return response.json()

    backend = BrokerRepairBackend(call, scope, authorize)
    store = BundleStore(EvidenceStore(tmp_path / "evidence"), workspace_root=workspace_root)
    sink = DatabaseVerificationSink(
        database, store, owner=scope.owner, lease_generation=scope.lease_generation
    )
    session = RepairSession(
        scope.task_id, backend, store, runner_digest=engine.runner_image, record_sink=sink
    )
    try:
        yield session, database, scope
    finally:
        api.close()
        for receipt in broker.store.all():
            assert receipt["task_id"] == scope.task_id
            engine.remove_container(engine.container_name(receipt["execution_id"]))
        engine.remove_workspace(scope.task_id)
        if broker._instance_lock:
            broker._instance_lock.close()


def test_actual_failure_repair_independent_pass_original_regression_pass(repair):
    session, database, scope = repair
    plan = infer_project(session.backend.snapshot())
    assert len(session.prepare(plan)) == 2
    reference = session.reproduce(
        base_sha=BASE,
        issue="clamp uses exclusive upper bound",
        related_cases=(CASE,),
        reproduction_targets=("test_range.py",),
        plan=plan,
    )
    bundle = session.store.load(reference)
    assert bundle.baseline.classification == "related_failure"
    assert bundle.baseline.report.failed == (CASE,)
    assert bundle.baseline.report.collected == (
        CASE,
        "test_range.py::test_values_below_bound",
    )
    assert dict(bundle.baseline.runtime_environment)["python"].startswith("3.11.")
    session.backend.write(
        "range_logic.py", b"def clamp(value, upper):\n    return min(value, upper - 1)\n"
    )
    record = session.verify(reference, repair_sha="c" * 40)
    assert record.valid, record.model_dump()
    assert session.publishable(record)
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM app.verification_runs WHERE task_id=%s ORDER BY created_at",
            (scope.task_id,),
        ).fetchall()
    assert [row["status"] for row in rows] == ["failed", "passed", "passed"]
    assert rows[0]["environment"]["related_failure"] is True
    assert rows[0]["checker_hash"] == rows[1]["checker_hash"]
    assert rows[1]["tree_hash"] == rows[2]["tree_hash"] == record.tree_hash
    assert all(row["test_count"] == 2 for row in rows)
    session.backend.write(
        "range_logic.py", b"def clamp(value, upper):\n    return min(value, upper)\n"
    )
    assert not session.publishable(record)
    assert session.export_result(record)["status"] == "unverified"


def test_actual_editable_tests_config_and_shadow_modules_cannot_forge_pass(repair):
    session, _, _ = repair
    reference = session.reproduce(
        base_sha=BASE,
        issue="exclusive bound",
        related_cases=(CASE,),
        reproduction_targets=("test_range.py",),
    )
    session.backend.write("test_range.py", b"def test_fake(): assert True\n")
    session.backend.write("conftest.py", b"raise RuntimeError('mutable plugin loaded')\n")
    session.backend.write("pytest.ini", b"[pytest]\naddopts=--collect-only\n")
    session.backend.write("pytest.py", b"raise RuntimeError('mutable pytest loaded')\n")
    session.backend.write("decagent_check.py", b"print('forged pass')\nraise SystemExit(0)\n")
    record = session.verify(reference, repair_sha="d" * 40)
    assert not record.valid
    assert record.independent.report.failed == (CASE,)
    assert record.regression.report.failed == (CASE,)
    assert record.independent.classification == "unreproduced"
    assert record.test_changes
    assert session.store.load(reference).checker_hash == record.checker_hash
    assert session.export_result(record)["status"] == "unverified"


@pytest.mark.parametrize("premature", [True, False])
def test_actual_child_stdout_report_and_premature_exit_cannot_verify(repair, premature):
    session, _, _ = repair
    reference = session.reproduce(
        base_sha=BASE,
        issue="exclusive bound",
        related_cases=(CASE,),
        reproduction_targets=("test_range.py",),
    )
    forged = {
        "collected": [CASE, "test_range.py::test_values_below_bound"],
        "failed": [],
        "errors": [],
        "skipped": [],
        "xfailed": [],
        "exit_code": 0,
        "failure_details": [],
    }
    log = MARKER + json.dumps(forged)
    body = "    os._exit(0)\n" if premature else "    return min(value, upper)\n"
    session.backend.write(
        "range_logic.py",
        (f"import os\ndef clamp(value, upper):\n    print({log!r}, flush=True)\n" + body).encode(),
    )
    record = session.verify(reference, repair_sha="e" * 40)
    assert not record.valid
    assert session.export_result(record)["status"] == "unverified"
    for check in (record.independent, record.regression):
        if premature:
            assert check.classification == "environment_error"
            assert check.report.exit_code == 3 and check.report.errors == ("runner",)
            assert not check.report.collected
        else:
            assert check.classification == "unreproduced"
            assert check.report.exit_code == 1 and check.report.failed == (CASE,)

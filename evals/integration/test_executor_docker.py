"""Real Docker/SQL boundary tests. Run explicitly inside the Linux broker test container.

Absent services are skipped and remain unverified; no fake network is substituted.
The launcher guide uses a unique disposable PostgreSQL instance and Docker resources.
"""

import base64
import hashlib
import json
import os
import time
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from decagent.executor.archive import FilesSnapshot, make_archive, read_archive, snapshot_hash
from decagent.executor.broker import Broker
from decagent.executor.docker import DockerEngine
from decagent.executor.protocol import (
    ExecutionRequest,
    ExecutionScope,
    FileRequest,
    ScopeRequest,
    TaskControlRequest,
    WorkspaceRequest,
    WriteRequest,
)
from decagent.storage.database import Database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DECAGENT_EXECUTOR_INTEGRATION") != "1",
        reason="Explicit disposable Docker/SQL executor fixture is unavailable; unverified",
    ),
]


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    url = os.environ["DECAGENT_TEST_DATABASE_URL"]
    assert "@decagent-executor-pg" in url, "Only the dedicated executor test DB may be used"
    database = Database(url)
    database.migrate()
    engine = DockerEngine(
        runner_image=os.environ["DECAGENT_RUNNER_IMAGE"],
        proxy_container=os.environ["DECAGENT_PROXY_CONTAINER"],
        control_containers=(
            os.getenv("DECAGENT_EXECUTOR_TEST_PG_CONTAINER", "decagent-executor-pg"),
            os.getenv("DECAGENT_REPAIR_TESTHOST", "decagent-executor-tests"),
        ),
    )
    value = Broker(database, engine, tmp_path_factory.mktemp("executor-receipts"))
    value.initialize()
    value.test_tasks = []
    yield value
    for receipt in value.store.all():
        engine.remove_container(engine.container_name(receipt["execution_id"]))
    for task_id in value.test_tasks:
        engine.remove_workspace(task_id)
    if value._instance_lock is not None:
        value._instance_lock.close()


def task(broker, files=None):
    project = broker.database.add_project("executor-fixture/project")
    created = broker.database.create_task(project["id"], "a" * 40, {"trust_scope": "exact_commit"})
    claim = broker.database.claim(created["id"], "executor-tests", seconds=120)
    scope = {
        "task_id": str(created["id"]),
        "owner": "executor-tests",
        "lease_generation": claim["lease_generation"],
    }
    broker.test_tasks.append(scope["task_id"])
    broker.workspace(
        WorkspaceRequest.model_validate(
            scope
            | {
                "archive_base64": base64.b64encode(
                    make_archive(
                        files if files is not None else {"main.py": b"print('baseline')\n"}
                    )
                ).decode()
            }
        )
    )
    return scope


def write_request(broker, scope, path, content):
    snapshot = {"tool": "write_file", "params": {"path": path, "content": content.decode("utf-8")}}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    action = broker.database.plan_action(scope["task_id"], str(uuid4()), digest, snapshot)
    return WriteRequest.model_validate(
        scope
        | {
            "path": path,
            "content_base64": base64.b64encode(content).decode(),
            "action_id": str(action["id"]),
            "action_digest": digest,
        }
    )


def execution(broker, scope, script, timeout=120, kind="command", max_output=65536):
    params = {
        "argv": ["python", "-c", script],
        "cwd": ".",
        "timeout_seconds": timeout,
        "max_output_bytes": max_output,
    }
    snapshot = {"tool": "run_command", "params": params}
    if kind == "setup":
        snapshot["execution_kind"] = "setup"
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    action = broker.database.plan_action(scope["task_id"], str(uuid4()), digest, snapshot)
    return ExecutionRequest.model_validate(
        scope
        | params
        | {
            "action_id": str(action["id"]),
            "action_digest": digest,
            "execution_id": str(uuid4()),
            "kind": kind,
        }
    )


def completed(broker, scope, request):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        receipt = broker.status(
            ExecutionScope.model_validate(scope | {"execution_id": request.execution_id})
        )
        if receipt["state"] not in {"reserved", "starting", "running"}:
            return receipt
        time.sleep(0.1)
    raise AssertionError("Execution did not reach terminal receipt")


def test_actual_startup_network_boundary(broker):
    assert broker.ready
    assert broker.diagnostics["boundary"]["passed"]
    checks = broker.diagnostics["boundary"]["checks"]
    assert checks["public_http_proxy"] and checks["public_https_connect"]
    assert checks["direct_public_ipv4_blocked"]
    assert checks["proxy_denies_host.docker.internal"]
    assert checks["proxy_denies_169.254.169.254"]


def test_actual_repeated_command_receipts_reach_bounded_human_escalation(broker):
    from decagent.runtime.progress import begin_replan, observe_progress, route_progress

    scope = task(broker)
    progress, routes = None, []
    for _ in range(6):
        request = execution(broker, scope, "print('same assertion failure'); raise SystemExit(1)")
        broker.execute(request)
        receipt = completed(broker, scope, request)
        assert receipt["state"] == "completed" and receipt["exit_code"] == 1
        progress = observe_progress(
            progress,
            {"tool": "run_command", "params": {"argv": request.argv, "cwd": request.cwd}},
            {**receipt, "status": "failed"},
            step_id=request.execution_id,
        )
        decision = route_progress(progress)
        routes.append(decision["route"])
        if decision["route"] == "replan":
            progress = begin_replan(progress)
    assert routes == ["continue", "replan", "continue", "replan", "continue", "escalate"]
    assert progress["replans"] == 2


def test_actual_receipt_limits_and_duplicate_never_restarts(broker):
    scope = task(broker)
    script = (
        "from pathlib import Path; import os; "
        "p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
        "print('ghp_'+'a'*30); print('uid',os.getuid()); "
        "print(open('/sys/fs/cgroup/pids.max').read().strip()); "
        "print(open('/sys/fs/cgroup/memory.max').read().strip()); "
        "print(open('/sys/fs/cgroup/cpu.max').read().strip()); raise SystemExit(3)"
    )
    request = execution(broker, scope, script)
    broker.execute(request)
    broker.execute(request)
    receipt = completed(broker, scope, request)
    assert receipt["exit_code"] == 3 and receipt["state"] == "completed"
    assert "[REDACTED]" in receipt["output"] and "ghp_" not in receipt["output"]
    assert "uid 10001" in receipt["output"] and "128" in receipt["output"]
    assert str(4 * 1024**3) in receipt["output"] and "200000 100000" in receipt["output"]
    assert broker.engine.snapshot(scope["task_id"])["count"] == b"1"
    assert broker.execute(request)["execution_id"] == receipt["execution_id"]
    inspected = broker.engine.inspect(broker.engine.container_name(request.execution_id))
    assert inspected["HostConfig"]["ReadonlyRootfs"]
    assert inspected["HostConfig"]["CapDrop"] == ["ALL"]
    assert all(mount["Type"] == "volume" for mount in inspected["Mounts"])
    assert all("docker.sock" not in mount["Destination"] for mount in inspected["Mounts"])
    assert not any("GITHUB" in item or "API_KEY" in item for item in inspected["Config"]["Env"])
    assert "ghp_" not in broker.engine.logs(broker.engine.container_name(request.execution_id))


@pytest.mark.parametrize("kind", ["command", "setup"])
def test_actual_timeout_kills_children(broker, kind):
    scope = task(broker)
    script = (
        "import subprocess,time; "
        "subprocess.Popen(['python','-c',\"import time; from pathlib import Path; "
        "time.sleep(3); Path('escaped-child').write_text('bad')\"]); time.sleep(60)"
    )
    request = execution(broker, scope, script, timeout=1, kind=kind)
    broker.execute(request)
    receipt = completed(broker, scope, request)
    assert receipt["state"] == "timed_out"
    time.sleep(3.1)
    assert "escaped-child" not in broker.engine.snapshot(scope["task_id"])
    assert not broker.engine.inspect(broker.engine.container_name(request.execution_id))["State"][
        "Running"
    ]


def test_actual_cancel_stops_entire_container_and_is_idempotent(broker):
    scope = task(broker)
    request = execution(
        broker,
        scope,
        "import subprocess,time; "
        "subprocess.Popen(['python','-c','import time; time.sleep(60)']); "
        "time.sleep(60)",
    )
    broker.execute(request)
    cancellation = ExecutionScope.model_validate(scope | {"execution_id": request.execution_id})
    assert broker.cancel(cancellation)["state"] == "cancelled"
    assert broker.cancel(cancellation)["state"] == "cancelled"
    assert not broker.engine.inspect(broker.engine.container_name(request.execution_id))["State"][
        "Running"
    ]


def pending_action(broker, request):
    with broker.database.connect() as connection:
        connection.execute(
            "UPDATE app.actions SET status='running',receipt=%s WHERE id=%s",
            (Jsonb({"execution_id": request.execution_id, "status": "unknown"}), request.action_id),
        )


def test_actual_task_stop_fences_lease_and_unknown_receipt_cannot_reconcile_as_pass(broker):
    scope = task(broker)
    request = execution(broker, scope, "import time; time.sleep(60)")
    pending_action(broker, request)
    broker.execute(request)
    control = TaskControlRequest.model_validate(scope)
    with pytest.raises(ValueError):
        broker.stop_task(control.model_copy(update={"owner": "other-owner"}))
    assert broker.engine.inspect(broker.engine.container_name(request.execution_id))["State"][
        "Running"
    ]
    result = broker.stop_task(control)
    assert result["stopped"] and result["state"] == "recovering"
    assert not broker.engine.inspect(broker.engine.container_name(request.execution_id))["State"][
        "Running"
    ]
    with broker.database.connect() as connection:
        row = connection.execute(
            "SELECT state,lease_until FROM app.tasks WHERE id=%s", (scope["task_id"],)
        ).fetchone()
    assert row["state"] == "recovering" and row["lease_until"] is None
    with pytest.raises(ValueError):
        broker.database.assert_lease(scope["task_id"], scope["owner"], scope["lease_generation"])
    assert broker.stop_task(control)["stopped"]
    assert broker.store.read(request.execution_id)["state"] in {"cancelled", "fenced"}
    reconciled = broker.reconcile_task(control)
    assert not reconciled["reconciled"]
    assert reconciled["unknown_action_ids"] == [request.action_id]


def test_actual_reconcile_completed_receipt_without_replaying_effect(broker):
    scope = task(broker)
    request = execution(
        broker,
        scope,
        "from pathlib import Path; p=Path('reconcile-count'); "
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )
    pending_action(broker, request)
    broker.execute(request)
    assert completed(broker, scope, request)["exit_code"] == 0
    control = TaskControlRequest.model_validate(scope)
    assert broker.reconcile_task(control)["reconciled"]
    assert broker.reconcile_task(control)["reconciled"]
    with broker.database.connect() as connection:
        row = connection.execute(
            "SELECT status,receipt FROM app.actions WHERE id=%s", (request.action_id,)
        ).fetchone()
    assert row["status"] == row["receipt"]["status"] == "succeeded"
    assert row["receipt"]["execution_id"] == request.execution_id
    assert broker.engine.snapshot(scope["task_id"])["reconcile-count"] == b"1"


def test_actual_lease_fence_and_exact_action_prevent_stale_execution(broker):
    scope = task(broker)
    request = execution(broker, scope, "import time; time.sleep(60)")
    with pytest.raises(ValueError, match="parameters"):
        broker.execute(request.model_copy(update={"argv": ["python", "-V"]}))
    broker.execute(request)
    with broker.database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET lease_generation=lease_generation+1 WHERE id=%s",
            (scope["task_id"],),
        )
    deadline = time.monotonic() + 5
    while broker.store.read(request.execution_id)["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert broker.store.read(request.execution_id)["state"] == "fenced"
    with pytest.raises(ValueError, match="stale"):
        broker.execute(request)


def test_actual_baseline_immutable_symlink_and_cross_task_paths_denied(broker):
    scope = task(broker)
    request = execution(
        broker,
        scope,
        "from pathlib import Path; "
        "Path('link').symlink_to('/baseline/main.py'); "
        "\ntry: Path('/baseline/main.py').write_text('changed')"
        "\nexcept OSError: print('baseline readonly')",
    )
    broker.execute(request)
    assert "baseline readonly" in completed(broker, scope, request)["output"]
    with pytest.raises(ValueError, match="link"):
        broker.write_file(write_request(broker, scope, "link", b"changed"))
    assert (
        broker.engine.snapshot(scope["task_id"], baseline=True)["main.py"] == b"print('baseline')\n"
    )
    with pytest.raises(ValueError):
        FileRequest.model_validate(scope | {"path": "../other/main.py"})


def test_actual_write_requires_exact_task_action_digest_path_and_content(broker):
    scope = task(broker)
    other_scope = task(broker)
    content = "print('authorized \u4fee\u590d')\n".encode()
    request = write_request(broker, scope, "main.py", content)
    changes = [
        other_scope,
        {"action_id": str(uuid4())},
        {"action_digest": "0" * 64},
        {"content_base64": base64.b64encode(b"different content").decode()},
        {"path": "other.py"},
    ]
    for change in changes:
        with pytest.raises(ValueError, match="action|parameters"):
            broker.write_file(request.model_copy(update=change))
    assert broker.engine.snapshot(scope["task_id"])["main.py"] == b"print('baseline')\n"
    assert broker.engine.snapshot(other_scope["task_id"])["main.py"] == b"print('baseline')\n"
    command = execution(broker, scope, "print('not a write action')")
    with pytest.raises(ValueError, match="parameters"):
        broker.write_file(
            request.model_copy(
                update={"action_id": command.action_id, "action_digest": command.action_digest}
            )
        )
    assert broker.write_file(request)["size"] == len(content)
    assert broker.engine.snapshot(scope["task_id"])["main.py"] == content


def test_actual_executable_permissions_survive_archive_workspace_and_snapshot(broker):
    files = FilesSnapshot(
        {
            "main.py": b"print('baseline')\n",
            "bin/run.sh": b"#!/bin/sh\necho permission-roundtrip\n",
        },
        modes={"main.py": 0o644, "bin/run.sh": 0o755},
    )
    scope = task(broker, files)
    baseline = broker.engine.snapshot(scope["task_id"], baseline=True)
    current = broker.engine.snapshot(scope["task_id"])
    assert baseline.modes == current.modes == files.modes
    assert snapshot_hash(baseline) == snapshot_hash(current) == snapshot_hash(files)
    transport = broker.snapshot(ScopeRequest.model_validate(scope))
    restored = read_archive(base64.b64decode(transport["archive_base64"], validate=True))
    assert restored.modes == files.modes
    assert snapshot_hash(restored) == transport["workspace_hash"]
    request = execution(
        broker,
        scope,
        "import subprocess; from pathlib import Path; "
        "subprocess.run(['./bin/run.sh'],check=True); Path('main.py').chmod(0o755)",
    )
    broker.execute(request)
    receipt = completed(broker, scope, request)
    assert receipt["exit_code"] == 0 and "permission-roundtrip" in receipt["output"]
    changed = broker.engine.snapshot(scope["task_id"])
    assert changed == files and changed.modes["main.py"] == 0o755
    assert snapshot_hash(changed) != snapshot_hash(files)
    result = broker.diff(ScopeRequest.model_validate(scope))
    assert "old mode 100644" in result["diff"] and "new mode 100755" in result["diff"]
    assert broker.engine.snapshot(scope["task_id"], baseline=True).modes["main.py"] == 0o644


def test_actual_output_is_bounded_before_daemon_logs(broker):
    scope = task(broker)
    request = execution(broker, scope, "print('x'*1000000); print('sk-'+'a'*30)", max_output=64)
    broker.execute(request)
    receipt = completed(broker, scope, request)
    assert receipt["truncated"] and "[TRUNCATED]" in receipt["output"]
    assert len(receipt["output"]) < 100
    assert "sk-" not in broker.engine.logs(broker.engine.container_name(request.execution_id))


def test_actual_different_execution_id_cannot_repeat_same_action(broker):
    scope = task(broker)
    request = execution(broker, scope, "from pathlib import Path; Path('once').write_text('1')")
    broker.execute(request)
    completed(broker, scope, request)
    with pytest.raises(ValueError, match="execution"):
        broker.execute(request.model_copy(update={"execution_id": str(uuid4())}))


def test_actual_dependency_caches_are_not_source_diff(broker):
    scope = task(broker)
    request = execution(
        broker,
        scope,
        "from pathlib import Path; "
        "Path('.venv').mkdir(); Path('.venv/generated').write_text('cache'); "
        "Path('main.py').write_text('changed\\n')",
    )
    broker.execute(request)
    completed(broker, scope, request)
    result = broker.diff(
        ExecutionScope.model_validate(scope | {"execution_id": request.execution_id})
    )
    assert "main.py" in result["diff"] and ".venv" not in result["diff"]


def test_actual_public_redirect_to_metadata_is_denied(broker):
    scope = task(broker)
    script = (
        "import urllib.request,urllib.error; "
        "url='https://httpbin.org/redirect-to?url=http%3A%2F%2F169.254.169.254%2F'; "
        "\ntry: urllib.request.urlopen(url,timeout=15); raise AssertionError('redirect accepted')"
        "\nexcept urllib.error.HTTPError as error: "
        "assert error.code == 403 and error.url == 'http://169.254.169.254/'; "
        "print('redirect was independently denied')"
    )
    request = execution(broker, scope, script)
    broker.execute(request)
    receipt = completed(broker, scope, request)
    assert receipt["exit_code"] == 0 and "redirect was independently denied" in receipt["output"]


def test_actual_proxy_failure_keeps_broker_execution_disabled(broker, tmp_path):
    name = "decagent-executor-unavailable-proxy"
    engine = DockerEngine(runner_image=os.environ["DECAGENT_RUNNER_IMAGE"], proxy_container=name)
    engine.create_container(
        name,
        {
            "Image": engine.runner_image,
            "User": "10001:10001",
            "Cmd": ["python", "-c", "import time; time.sleep(60)"],
            "Labels": {"decagent.executor.instance": name},
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "NetworkMode": "bridge",
            },
        },
    )
    engine.start(name)
    try:
        disabled = Broker(broker.database, engine, tmp_path)
        with pytest.raises(ValueError, match="boundary"):
            disabled.initialize()
        assert not disabled.ready
        value = ExecutionRequest.model_validate(
            {
                "task_id": str(uuid4()),
                "action_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "owner": "executor-tests",
                "lease_generation": 1,
                "action_digest": "a" * 64,
                "argv": ["python", "-V"],
            }
        )
        with pytest.raises(ValueError, match="boundary"):
            disabled.execute(value)
    finally:
        engine.remove_container(name)
        if disabled._instance_lock is not None:
            disabled._instance_lock.close()

"""Authorized verification of the real repository delivery and recovery path.

Run with the project Python from DecAgent's root. Settings.from_env reads credentials
only in the host control process; the PAT crosses docker exec stdin, never argv or
task environment. Default uses a scripted model; --real-models uses authorized Qwen/Jev.
Remote mutations have durable intents. An ambiguous mutation is never blindly retried.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decagent.config import Settings
from decagent.executor.archive import read_archive
from decagent.integrations.github import GitHub
from decagent.policy import Redactor

REPOSITORY = "shuaibilx/decagent-integration-test-20261010"
REPOSITORY_ID = 1412976452
SETUP_BRANCH = "decagent-fixture/semantic-20261010"
RUNNER = "sha256:caa1b714fa8f01202405776e8e93ae464fac4f92075db47458833e54b3b405cd"
HOST = "decagent-executor-tests"
CASE = "test_range.py::test_exclusive_upper_bound"
FIXED_SOURCE = (
    '"""Clamp to an exclusive upper bound."""\n\n\n'
    "def clamp(value, upper):\n    return min(value, upper - 1)\n"
)
ISSUE = (
    "Controlled zero-paid-model integration fixture: clamp(10, 10) must be 9 because "
    "the upper bound is exclusive, and clamp(3, 10) must remain 3. Repair range_logic.py, "
    "preserve the two original tests, independently verify and publish a draft PR. "
    "The primary replies in this run are scripted, not an actual model's repair."
)
REAL_ISSUE = (
    "Fix range_logic.py: clamp(10, 10) must return 9 because the upper bound is exclusive, "
    "and clamp(3, 10) must remain 3. Preserve test_range.py and requirements.txt. "
    "Read the existing source and tests, make the smallest source fix, then finish. "
    "The controller independently verifies the fixed baseline and regression before a draft PR."
)


def approved_fixture_action(action, task_id=None):
    """The owner's integration-test authorization covers only this fixed fixture."""
    tool, params = action["tool"], action["params"]
    if tool in {"write_file", "edit_file"}:
        return params.get("path") == "range_logic.py"
    if tool == "publish_draft":
        return bool(task_id) and params.get("branch") == "decagent/" + task_id
    if tool != "run_command" or params.get("cwd") != ".":
        return False
    argv = params["argv"]
    return (
        argv
        in (
            ["python", "-m", "venv", ".venv"],
            ["python", "-m", "pip", "--python", ".venv", "install", "-r", "requirements.txt"],
            ["python", "-m", "pip", "--python", ".venv", "freeze", "--all"],
            ["python", "-m", "pytest"],
            ["python", "-m", "pytest", "test_range.py"],
        )
        or (
            argv[:3] == ["python", "-m", "pytest"]
            and all(part in {"-q", "-v", "-x", "test_range.py"} for part in argv[3:])
        )
        or (
            argv[:4] == ["python", "-P", "-m", "decagent_check"]
            and params.get("purpose", "").startswith("immutable reproduction material:")
            and len(argv) == 5
        )
    )


class Journal:
    def __init__(self, path: Path, *, redactor=None):
        self.path = path
        self.redactor = redactor or Redactor(max_text_chars=1_048_576)
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        text = json.dumps(self.data, ensure_ascii=False, indent=2, default=str)
        if self.redactor.redact_text(text) != text:
            raise ValueError("Credential-like content cannot enter the live-test journal")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def event(self, kind, payload):
        self.data.setdefault("events", []).append(
            {"utc": datetime.now(UTC).isoformat(), "kind": kind, "payload": payload}
        )
        self.save()


def fixture(root: Path):
    directory = root / "evals/fixtures/repair/semantic"
    return {
        "range_logic.py": (directory / "range_logic.py").read_bytes(),
        "test_range.py": (directory / "test_range.py").read_bytes(),
        "requirements.txt": b"",
    }


def mutation(github, journal, key, endpoint, payload):
    entries = journal.data.setdefault("setup_mutations", {})
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if key in entries:
        saved = entries[key]
        if saved["input_hash"] != fingerprint:
            raise ValueError("Remote setup operation changed after its intent was recorded")
        if saved["state"] != "confirmed":
            raise RuntimeError(
                "Setup mutation unconfirmed; read-only reconciliation required: " + key
            )
        return saved["response"]
    entries[key] = {"state": "dispatched", "endpoint": endpoint, "input_hash": fingerprint}
    journal.save()
    try:
        response = github.post(endpoint, payload)
    except Exception:
        entries[key]["state"] = "unknown"
        journal.save()
        raise
    entries[key].update(state="confirmed", response=response)
    journal.save()
    return response


def prepare_fixture(github, journal, files):
    repository = github.get("")
    if repository["id"] != REPOSITORY_ID or not repository["private"]:
        raise ValueError("Live validation is restricted to the registered private test repository")
    if repository["default_branch"] != "main":
        raise ValueError("Default branch differs from the approved test repository receipt")
    main = github.ref("main")
    if not main:
        raise ValueError("The private repository requires its existing initialized main branch")
    journal.data.setdefault("main_before", main)
    if main != journal.data["main_before"]:
        raise ValueError("Default branch advanced; preserve prior intent and review the new base")
    expected = {path: hashlib.sha256(content).hexdigest() for path, content in files.items()}
    if journal.data.get("fixture_hashes", expected) != expected:
        raise ValueError("Controlled fixture changed after intent creation")
    journal.data["fixture_hashes"] = expected
    journal.save()
    head = github.ref(SETUP_BRANCH)
    if head is None:
        commit = github.commit(main)
        entries = []
        for path, content in sorted(files.items()):
            blob = mutation(
                github,
                journal,
                "blob:" + path,
                "git/blobs",
                {"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )["sha"]
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
        tree = mutation(
            github,
            journal,
            "tree",
            "git/trees",
            {"base_tree": commit["tree"]["sha"], "tree": entries},
        )["sha"]
        author = {
            "name": "DecAgent Integration Fixture",
            "email": "180500914+shuaibilx@users.noreply.github.com",
            "date": journal.data["created_utc"],
        }
        head = mutation(
            github,
            journal,
            "commit",
            "git/commits",
            {
                "message": "Add controlled exclusive-bound fixture\n\nDecAgent-Live-Fixture: "
                + journal.data["run_id"],
                "tree": tree,
                "parents": [main],
                "author": author,
                "committer": author,
            },
        )["sha"]
        mutation(
            github,
            journal,
            "ref",
            "git/refs",
            {"ref": "refs/heads/" + SETUP_BRANCH, "sha": head},
        )
    downloaded = read_archive(github.archive(head), strip_root=True)
    if any(downloaded.get(path) != content for path, content in files.items()):
        raise ValueError("Existing setup branch does not contain the exact controlled bug fixture")
    if github.ref("main") != journal.data["main_before"]:
        raise ValueError("Default branch unexpectedly changed during fixture setup")
    journal.data.update(setup_sha=head, setup_branch=SETUP_BRANCH, fixture_verified=True)
    journal.event("fixture_verified", {"sha": head, "files": expected, "main_unchanged": True})
    return head


def control(payload):
    """Trusted control harness in the socket-enabled test host; no task code runs here."""
    import psycopg
    from fastapi.testclient import TestClient
    from psycopg import sql

    from decagent.decisions import DecisionEngine, RuleStrategy
    from decagent.executor.broker import Broker, create_app
    from decagent.executor.docker import DockerEngine
    from decagent.gateway.auth import initialize_owner
    from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
    from decagent.runtime.application import create_workbench
    from decagent.runtime.config import RuntimeConfig
    from decagent.runtime.model_calls import TokenPrice
    from decagent.runtime.services import RuntimeServices
    from decagent.runtime.worker import Worker
    from decagent.storage.database import Database

    real = payload["intent"].get("model_mode") == "real_qwen_jev"
    faults = payload["intent"].get("fault_drill") is True
    expected = {"github_token", "intent", "fixture_sha", "repository"}
    if real:
        expected |= {"main_api_key", "jev_api_key", "main_base_url", "main_model", "jev_model"}
    if set(payload) != expected:
        raise ValueError("Only a fixed live-test control payload is accepted")
    if payload["repository"] != REPOSITORY or payload["intent"]["runner_digest"] != RUNNER:
        raise ValueError("Live-test scope or fixed runner changed")
    run_id = payload["intent"]["run_id"]
    if str(__import__("uuid").UUID(run_id)) != run_id:
        raise ValueError("Canonical live-test run identity is required")
    directory = Path("/tmp") / ("decagent-live-repository-" + run_id)
    directory.mkdir(mode=0o700, exist_ok=True)
    redactor = Redactor(
        known_secrets=tuple(
            payload.get(k, "") for k in ("github_token", "main_api_key", "jev_api_key")
        ),
        max_text_chars=1_048_576,
    )
    journal = Journal(directory / "control-receipt.json", redactor=redactor)
    journal.data.setdefault("run_id", run_id)
    journal.data.setdefault("repository", REPOSITORY)
    journal.data.setdefault("model_mode", "real_qwen_jev" if real else "scripted_primary_plus_rule")
    journal.save()
    original_url = os.environ["DECAGENT_TEST_DATABASE_URL"]
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(original_url)
    if parts.hostname != "decagent-executor-pg":
        raise ValueError("Only the dedicated executor PostgreSQL fixture may be used")
    db_name = "decagent_live_repository_" + run_id.replace("-", "")
    admin_url = urlunsplit(parts._replace(path="/postgres"))
    with psycopg.connect(admin_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (db_name,)
        ).fetchone()
        if not exists:
            journal.event("database_create_intent", {"database": db_name})
            connection.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(db_name)))
    url = urlunsplit(parts._replace(path="/" + db_name))
    database = Database(url)
    database.migrate()
    owner_path = directory / "owner.token"
    with database.connect() as connection:
        initialized = connection.execute("SELECT singleton FROM app.owner_credentials").fetchone()
    if not initialized:
        initialize_owner(database, owner_path)
    if not owner_path.exists() or owner_path.stat().st_mode & 0o077:
        raise ValueError("The live-test owner credential is missing or insufficiently protected")
    owner_token = owner_path.read_text(encoding="utf-8").strip()
    redactor.known_secrets = (*redactor.known_secrets, owner_token)
    settings = Settings(
        database_url=url,
        data_dir=directory / "application",
        github_token=payload["github_token"],
        main_api_key=payload.get("main_api_key", ""),
        jev_api_key=payload.get("jev_api_key", ""),
        main_base_url=payload.get("main_base_url", "https://api.openai.com/v1"),
        main_model=payload.get("main_model", "scripted-primary-offline"),
        jev_model=payload.get("jev_model", "jev-1.13.0"),
        jev_provider="openrouter" if real else "typesafe",
    )
    token = "live-test-internal-broker-" + run_id
    config = RuntimeConfig(
        strategy="jev" if real else "rule",
        runner_digest=RUNNER,
        broker_token=token,
        author_name="DecAgent Scripted Integration",
        author_email="180500914+shuaibilx@users.noreply.github.com",
        main_price=TokenPrice(
            "owner-confirmed-free-quota-20261011" if real else "scripted-offline-zero-v1",
            Decimal(0),
            Decimal(0),
        ),
        jev_reservation=Decimal("0.02"),
    )
    engine = DockerEngine(
        runner_image=RUNNER,
        proxy_container="decagent-executor-proxy",
        control_containers=("decagent-executor-pg", HOST),
    )
    broker = Broker(database, engine, directory / "broker-receipts")
    broker.initialize()
    journal.data["boundary"] = broker.diagnostics
    journal.event("broker_verified", {"passed": broker.ready, "runner": RUNNER})
    broker_api = TestClient(create_app(broker, token))

    def call(path, body):
        # Do not expose raw snapshots or headers in this operational test log.
        journal.event(
            "broker_request",
            {
                "path": path,
                **{k: body[k] for k in ("task_id", "action_id", "execution_id") if k in body},
            },
        )
        response = broker_api.post(path, json=body, headers={"Authorization": "Bearer " + token})
        if response.status_code != 200:
            raise RuntimeError(
                "Actual broker rejected request: " + redactor.redact_text(response.text)
            )
        result = response.json()
        if faults and path == "/v1/executions" and not journal.data.get("command_response_lost"):
            import time

            import httpx

            deadline = time.monotonic() + 120
            while result["state"] not in {"completed", "fenced", "timed_out"}:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Bounded command-loss injection could not observe completion"
                    )
                time.sleep(0.1)
                result = broker.status(
                    __import__(
                        "decagent.executor.protocol", fromlist=["ExecutionScope"]
                    ).ExecutionScope(
                        **{
                            k: body[k]
                            for k in ("task_id", "owner", "lease_generation", "execution_id")
                        }
                    )
                )
            journal.data["command_response_lost"] = True
            journal.event("fault.command_response_lost", {"execution_id": body["execution_id"]})
            raise httpx.ReadTimeout("Injected response loss after real command completion")
        return result

    class ScriptedPrimary:
        def respond(self, messages, *, stream=False):
            if any(message["role"] == "tool" for message in messages):
                return ModelTurn(
                    "Scripted fixture edit complete; independent verification must decide success.",
                    (),
                    ModelUsage(0, 0, 0),
                    "scripted-primary-offline",
                )
            return ModelTurn(
                "Scripted integration fixture repair proposal; no paid model request.",
                (
                    ToolProposal(
                        "semantic-fix",
                        "write_file",
                        {"path": "range_logic.py", "content": FIXED_SOURCE},
                    ),
                ),
                ModelUsage(0, 0, 0),
                "scripted-primary-offline",
            )

    factories = []

    class FaultGitHub(GitHub):
        def post(self, path, body):
            result = super().post(path, body)
            if path == "pulls" and not journal.data.get("pr_response_lost"):
                from decagent.integrations.github import GitHubError

                journal.data["pr_response_lost"] = True
                journal.event("fault.pr_response_lost", {"pr_url": result["html_url"]})
                raise GitHubError("Injected response loss after actual PR creation", unknown=True)
            return result

    fault_github = FaultGitHub(REPOSITORY, settings.github_token) if faults else None

    def services_factory(*args):
        factories.append(True)
        journal.data["runtime_rebuilds"] = journal.data.get("runtime_rebuilds", 0) + 1
        journal.event("runtime_services_rebuilt", {"invocation": journal.data["runtime_rebuilds"]})
        return RuntimeServices(
            *args,
            broker_client=call,
            primary=None if real else ScriptedPrimary(),
            decision_engine=None if real else DecisionEngine(RuleStrategy()),
            github=fault_github,
        )

    api = TestClient(
        create_workbench(settings, database=database, runtime_config=config, broker_client=call)
    )
    api.headers["Authorization"] = "Bearer " + owner_token

    def request(path, body, key):
        journal.event("gateway_mutation_intent", {"path": path, "key": key, "body": body})
        response = api.post(
            path, json=body, headers={"Idempotency-Key": "live:" + run_id + ":" + key}
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                "Authenticated Gateway rejected request: " + redactor.redact_text(response.text)
            )
        journal.event("gateway_receipt", {"path": path, "status": response.status_code})
        return response.json()

    succeeded = False
    try:
        project = request(
            "/api/v1/projects",
            {"repository": REPOSITORY, "base_branch": SETUP_BRANCH},
            "project",
        )
        if project["permissions"].get("status") != "verified":
            raise ValueError("Real GitHub repository owner diagnostic was not verified")
        task = request(
            "/api/v1/tasks",
            {
                "project_id": project["id"],
                "base_ref": payload["fixture_sha"],
                "issue": REAL_ISSUE if real else ISSUE,
                "trust_scope": {"kind": "exact_commit"},
                "publish_draft_pr": True,
                "budget": {
                    "max_calls": 30,
                    "max_tokens": 500000,
                    "max_active_seconds": 1800,
                    "max_cost": "0.80" if real else "0.000001",
                },
            },
            "task",
        )
        task_id = task["id"]
        journal.data.update(task_id=task_id, project_id=project["id"], database=db_name)
        journal.save()
        for iteration in range(40):
            status_response = api.get("/api/v1/tasks/" + task_id)
            status_response.raise_for_status()
            status = status_response.json()
            journal.event(
                "task_state",
                {
                    "iteration": iteration,
                    "state": status["state"],
                    "generation": status["lease_generation"],
                },
            )
            if status["state"] == "succeeded":
                succeeded = True
                break
            if status["state"] == "failed":
                raise RuntimeError(
                    "Production graph failed: "
                    + json.dumps(database.events(task_id)[-3:], default=str)
                )
            if status["state"] == "recovering":
                if not faults:
                    raise RuntimeError(
                        "Production graph requires reconciliation; no blind replay permitted"
                    )
                broker_api.close()
                if broker._instance_lock:
                    broker._instance_lock.close()
                broker = Broker(database, engine, directory / "broker-receipts")
                broker.initialize()
                broker_api = TestClient(create_app(broker, token))
                journal.event("fault.broker_restarted", {"generation": status["lease_generation"]})
                request(
                    "/api/v1/tasks/" + task_id + "/resume",
                    {},
                    "reconcile:" + str(status["lease_generation"]),
                )
            pending = [a for a in status["approvals"] if a["status"] == "pending"]
            if pending:
                for approval in pending:
                    if approval["kind"] == "clarification":
                        request(
                            f"/api/v1/tasks/{task_id}/responses",
                            {
                                "question_id": approval["id"],
                                "expected_digest": approval["digest"],
                                "content": (
                                    "Confirmed: the exclusive upper bound failure is the intended "
                                    "controlled defect; retain the below-bound regression."
                                ),
                            },
                            "reply:" + approval["id"],
                        )
                    elif approval["kind"] in {"action", "code_trust"}:
                        if approval["kind"] != "action":
                            raise RuntimeError(
                                "Unexpected additional code trust needs owner review"
                            )
                        with database.connect() as connection:
                            action = connection.execute(
                                "SELECT snapshot FROM app.actions WHERE id=%s",
                                (approval["action_id"],),
                            ).fetchone()["snapshot"]
                        if not approved_fixture_action(action, task_id):
                            raise RuntimeError("Action exceeds the fixed authorized fixture scope")
                        request(
                            "/api/v1/approvals/" + approval["id"] + "/reply",
                            {"expected_digest": approval["digest"], "decision": "approve"},
                            "reply:" + approval["id"],
                        )
                    else:
                        raise RuntimeError(
                            "Unplanned owner confirmation requires review: " + approval["kind"]
                        )
                request(
                    "/api/v1/tasks/" + task_id + "/resume",
                    {},
                    "resume:" + str(status["lease_generation"]),
                )
            worker = Worker(
                database,
                settings,
                config,
                owner="live-scripted-worker-" + run_id,
                services_factory=services_factory,
                heartbeat_seconds=5,
                lease_seconds=90,
            )
            if not worker.run_once():
                raise RuntimeError("The isolated live-test SQL queue has no claimable task")
        if not succeeded:
            raise RuntimeError("Controlled live test exceeded its bounded owner-reply loop")
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM app.verification_runs WHERE task_id=%s ORDER BY created_at,id",
                (task_id,),
            ).fetchall()
            actions = connection.execute(
                "SELECT * FROM app.actions WHERE task_id=%s ORDER BY created_at,id", (task_id,)
            ).fetchall()
            approvals = connection.execute(
                "SELECT * FROM app.approvals WHERE task_id=%s ORDER BY created_at,id", (task_id,)
            ).fetchall()
            usage = connection.execute(
                "SELECT * FROM app.usage_records WHERE task_id=%s ORDER BY created_at,id",
                (task_id,),
            ).fetchall()
            delivery = connection.execute(
                "SELECT * FROM app.deliveries WHERE task_id=%s", (task_id,)
            ).fetchone()
            checkpoints = connection.execute(
                "SELECT count(*) FROM checkpoint.checkpoints WHERE thread_id=%s", (task_id,)
            ).fetchone()["count"]
        selected = {row["kind"]: row for row in rows}
        assert selected["baseline"]["status"] == "failed"
        assert selected["baseline"]["environment"]["related_failure"] is True
        assert selected["independent"]["status"] == selected["regression"]["status"] == "passed"
        assert all(row["test_count"] == 2 for row in selected.values())
        assert journal.data["runtime_rebuilds"] > 1 and checkpoints > 1
        assert any(row["kind"] == "action" for row in approvals)
        assert any(row["kind"] == "clarification" for row in approvals)
        assert all(row["status"] == "settled" and row["actual_cost"] is not None for row in usage)
        if real:
            assert any(row["provider"] == "jev" for row in usage)
            assert sum(row["actual_cost"] for row in usage) <= Decimal("0.80")
        else:
            assert all(row["provider"] == "primary" and row["actual_cost"] == 0 for row in usage)
        assert delivery["status"] == "draft"
        if faults:
            assert journal.data.get("command_response_lost") is True
            assert journal.data.get("pr_response_lost") is True
        journal.data.update(
            status="verified",
            task_state="succeeded",
            runtime_rebuilds=journal.data["runtime_rebuilds"],
            checkpoints=checkpoints,
            verification_runs=rows,
            actions=actions,
            approvals=approvals,
            usage=usage,
            delivery=delivery,
            task_events=database.events(task_id),
            paid_requests=sum(row["provider"] == "jev" for row in usage) if real else 0,
        )
        inspected = []
        for receipt in broker.store.all():
            info = engine.inspect(engine.container_name(receipt["execution_id"]))
            env = info["Config"]["Env"]
            assert not any(
                any(secret and secret in item for secret in redactor.known_secrets)
                or "API_KEY=" in item
                or "GITHUB_TOKEN=" in item
                for item in env
            )
            assert all(mount["Type"] == "volume" for mount in info["Mounts"])
            assert not any("docker.sock" in mount["Destination"] for mount in info["Mounts"])
            inspected.append(
                {
                    "receipt": receipt,
                    "config": info["Config"],
                    "host_config": info["HostConfig"],
                    "mounts": info["Mounts"],
                    "state": info["State"],
                }
            )
        journal.data["actual_docker_executions"] = inspected
        journal.event(
            "actual_delivery_verified",
            {"pr_url": delivery["pr_url"], "paid_requests": journal.data["paid_requests"]},
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "task_id": task_id,
                    "pr_url": delivery["pr_url"],
                    "control_dir": str(directory),
                    "paid_requests": journal.data["paid_requests"],
                }
            )
        )
    except Exception as error:
        journal.data.update(
            status="blocked",
            error_type=type(error).__name__,
            error=redactor.redact_text(str(error)),
        )
        journal.save()
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "control_dir": str(directory),
                    "error_type": type(error).__name__,
                    "error": redactor.redact_text(str(error)),
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        api.close()
        broker_api.close()
        if fault_github:
            fault_github.client.close()
        if succeeded and journal.data.get("status") == "verified":
            for receipt in broker.store.all():
                engine.remove_container(engine.container_name(receipt["execution_id"]))
            engine.remove_workspace(journal.data["task_id"])
        if broker._instance_lock:
            broker._instance_lock.close()


def host(root, *, real=False, faults=False):
    settings = Settings.from_env()
    if not settings.github_token:
        raise ValueError("The explicitly authorized private repository requires the configured PAT")
    redactor = Redactor(
        known_secrets=(settings.github_token, settings.main_api_key, settings.jev_api_key),
        max_text_chars=1_048_576,
    )
    journal = Journal(
        root
        / (
            ".decagent/live-recovery-intent.json"
            if faults
            else (
                ".decagent/live-model-repair-intent.json"
                if real
                else ".decagent/live-repository-intent.json"
            )
        ),
        redactor=redactor,
    )
    if not journal.data:
        journal.data.update(
            run_id=str(uuid4()),
            created_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            runner_digest=RUNNER,
            setup_branch=SETUP_BRANCH,
            paid_requests=0,
            status="intent_recorded",
            model_mode="real_qwen_jev" if real else "scripted_primary_plus_rule",
            qwen_billing_basis="owner_confirmed_free_quota" if real else "scripted",
            jev_budget_usd="0.80" if real else "0",
            fault_drill=faults,
        )
        journal.save()
    if journal.data["repository"] != REPOSITORY or journal.data["runner_digest"] != RUNNER:
        raise ValueError("Existing live-test intent belongs to another scope")
    receipt = json.loads(
        (root / ".decagent/github-test-repository.json").read_text(encoding="utf-8")
    )
    if receipt["repository_id"] != REPOSITORY_ID or receipt["repository"] != REPOSITORY:
        raise ValueError(
            "Repository creation receipt differs from this explicitly authorized scope"
        )
    github = GitHub(REPOSITORY, settings.github_token, repository_id=REPOSITORY_ID)
    environment = {
        key: value
        for key, value in os.environ.items()
        if "TOKEN" not in key.upper()
        and "KEY" not in key.upper()
        and not key.startswith("DECAGENT_")
    }

    def docker(*arguments, input_text=None, check=True):
        result = subprocess.run(
            ["docker", *arguments],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=600,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(redactor.redact_text(result.stderr + result.stdout))
        return result

    try:
        image = docker(
            "image", "inspect", "decagent-executor-runner:supervised", "--format", "{{.Id}}"
        ).stdout.strip()
        if image != RUNNER:
            raise ValueError(
                "The tested runner tag no longer resolves to the fixed approved image .Id"
            )
        head = prepare_fixture(github, journal, fixture(root))
        if journal.data.get("status") == "verified":
            pull = github.pull(journal.data["pr_number"])
            assert pull["draft"] and not pull["merged"] and pull["state"] == "open"
            assert (
                pull["base"]["ref"] == SETUP_BRANCH
                and github.ref("main") == journal.data["main_before"]
            )
            print(
                json.dumps(
                    {
                        "status": "verified_existing_receipt",
                        "pr_url": pull["html_url"],
                        "paid_requests": journal.data.get("paid_requests", 0),
                    }
                )
            )
            return
        state = docker("inspect", HOST, "--format", "{{.State.Status}}").stdout.strip()
        if state != "running":
            docker("start", HOST)
        networks = json.loads(
            docker("inspect", HOST, "--format", "{{json .NetworkSettings.Networks}}").stdout
        )
        if "bridge" not in networks:
            journal.event("control_public_network_intent", {"container": HOST, "network": "bridge"})
            docker("network", "connect", "bridge", HOST)
            journal.data["added_control_public_bridge"] = True
            journal.save()
        package = root / "decagent"
        if any(
            path.is_symlink() or path.name == ".env" or ".decagent" in path.parts
            for path in package.rglob("*")
        ):
            raise ValueError("Unexpected secret or symlink inside explicit product source copy")
        docker("cp", str(package) + "/.", HOST + ":/opt/decagent/decagent/")
        docker(
            "exec",
            HOST,
            "mkdir",
            "-p",
            "/opt/decagent/ops",
            "/opt/decagent/evals/fixtures/repair/semantic",
        )
        docker(
            "cp",
            str(root / "ops/live_repository.py"),
            HOST + ":/opt/decagent/ops/live_repository.py",
        )
        docker(
            "cp",
            str(root / "evals/fixtures/repair/semantic") + "/.",
            HOST + ":/opt/decagent/evals/fixtures/repair/semantic/",
        )
        command_payload = {
            "github_token": settings.github_token,
            "intent": journal.data,
            "fixture_sha": head,
            "repository": REPOSITORY,
        }
        if real:
            if (
                settings.main_model != "qwen3.8-flash"
                or settings.main_base_url != "https://maas.qianwenaiapi.com/compatible-mode/v1"
                or settings.jev_provider != "openrouter"
                or settings.jev_model != "~typesafe/jev-latest"
                or not settings.main_api_key
                or not settings.jev_api_key
            ):
                raise ValueError("Real validation requires the owner's fixed model configuration")
            command_payload.update(
                main_api_key=settings.main_api_key,
                jev_api_key=settings.jev_api_key,
                main_base_url=settings.main_base_url,
                main_model=settings.main_model,
                jev_model=settings.jev_model,
            )
        journal.event(
            "control_dispatch_intent",
            {"host": HOST, "fixture_sha": head, "model_mode": journal.data["model_mode"]},
        )
        result = docker(
            "exec",
            "-i",
            "-w",
            "/opt/decagent",
            HOST,
            "python",
            "ops/live_repository.py",
            "--control",
            input_text=json.dumps(command_payload),
            check=False,
        )
        safe_stdout = redactor.redact_text(result.stdout)
        safe_stderr = redactor.redact_text(result.stderr)
        target = root / ".decagent/live-repository" / journal.data["run_id"]
        target.mkdir(parents=True, exist_ok=True)
        (target / "stdout.log").write_text(safe_stdout, encoding="utf-8")
        (target / "stderr.log").write_text(safe_stderr, encoding="utf-8")
        lines = [json.loads(line) for line in safe_stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise RuntimeError(
                "Control harness returned no receipt; prior dispatch must be reconciled"
            )
        control_receipt = lines[-1]
        location = control_receipt["control_dir"]
        if location != "/tmp/decagent-live-repository-" + journal.data["run_id"]:
            raise ValueError("Unexpected control receipt source directory")
        docker(
            "cp",
            HOST + ":" + location + "/control-receipt.json",
            str(target / "control-receipt.json"),
        )
        if control_receipt["status"] != "verified" or result.returncode:
            journal.data.update(status="blocked", control_receipt=control_receipt)
            journal.save()
            raise RuntimeError("Actual delivery test blocked; inspect ignored control receipt")
        saved = json.loads((target / "control-receipt.json").read_text(encoding="utf-8"))
        docker("cp", HOST + ":" + location + "/application/evidence", str(target / "evidence"))
        pull = github.pull(saved["delivery"]["pr_number"])
        assert pull["draft"] and not pull["merged"] and pull["state"] == "open"
        assert pull["base"]["ref"] == SETUP_BRANCH and pull["base"]["sha"] == head
        assert pull["head"]["sha"] == saved["delivery"]["head_sha"]
        assert github.ref("main") == journal.data["main_before"]
        repaired = read_archive(github.archive(pull["head"]["sha"]), strip_root=True)
        original = fixture(root)
        if not real:
            assert repaired["range_logic.py"] == FIXED_SOURCE.encode()
        assert repaired["test_range.py"] == original["test_range.py"]
        assert repaired["requirements.txt"] == b""
        journal.data.update(
            status="verified",
            pr_url=pull["html_url"],
            pr_number=pull["number"],
            task_id=saved["task_id"],
            main_after=github.ref("main"),
            evidence_dir=str(target),
            paid_requests=saved["paid_requests"],
        )
        journal.event(
            "github_draft_observed",
            {"pr_url": pull["html_url"], "draft": True, "merged": False, "main_unchanged": True},
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "pr_url": pull["html_url"],
                    "task_id": saved["task_id"],
                    "evidence_dir": str(target),
                    "paid_requests": journal.data["paid_requests"],
                }
            )
        )
    finally:
        github.close() if hasattr(github, "close") else github.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--real-models", action="store_true", help="Use authorized Qwen/Jev, Jev capped at $0.80"
    )
    parser.add_argument(
        "--fault-drill",
        action="store_true",
        help="Inject command/PR response loss with scripted model",
    )
    arguments = parser.parse_args()
    if arguments.control:
        control(json.load(sys.stdin))
    else:
        root = Path(__file__).resolve().parents[1]
        if Path.cwd().resolve() != root:
            raise ValueError("Run from the DecAgent project root to load its scoped Settings")
        try:
            if arguments.real_models and arguments.fault_drill:
                raise ValueError("Fault drill and paid model validation use separate intents")
            host(root, real=arguments.real_models, faults=arguments.fault_drill)
        except Exception as error:
            # Exceptions above have already been sanitized; avoid chained transport objects.
            print(
                json.dumps(
                    {"status": "blocked", "error_type": type(error).__name__, "error": str(error)}
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()

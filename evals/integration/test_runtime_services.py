"""Real PostgreSQL graph replay with an explicitly simulated, unpaid broker/model protocol."""

import base64
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from langgraph.types import Command
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.decisions import DecisionEngine, RuleStrategy
from decagent.executor.archive import make_archive, read_archive, snapshot_hash
from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
from decagent.policy import Action, ApprovalReply, Provenance, ReplyDecision, Source
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.graph import build_graph
from decagent.runtime.model_calls import TokenPrice
from decagent.runtime.services import RuntimeServices
from decagent.tools.repair.check_runner import MARKER

pytestmark = pytest.mark.integration


def test_graph_rebuild_prepares_freezes_repairs_and_verifies_without_replaying_commands(
    database, tmp_path
):
    project = database.add_project("fixture/serial-runtime")
    task = database.create_task(
        project["id"], "a" * 40, {}, budget={"max_calls": 30, "max_tokens": 100000, "max_cost": "1"}
    )
    owner = Provenance(Source.OWNER, "owner", "session", True)
    auth = {
        "id": "auth",
        "version": "1",
        "task_id": str(task["id"]),
        "project_id": str(project["id"]),
        "repository": project["repository"],
        "provenance": {
            "source": "owner",
            "owner_id": "owner",
            "session_id": "session",
            "authenticated": True,
        },
        "issue": "Fix upper bound to be exclusive",
        "publish_draft": False,
        "trust_scope": {"kind": "exact_commit", "branch": None},
    }
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET authorization_snapshot=%s WHERE id=%s", (Jsonb(auth), task["id"])
        )
    claimed = database.claim(task["id"], "runtime-fixture", seconds=120)
    settings = Settings(database_url=database.url, data_dir=tmp_path / "control")
    config = RuntimeConfig(
        strategy="rule",
        runner_digest="sha256:" + "a" * 64,
        main_price=TokenPrice("offline-fixture", Decimal(0), Decimal(0)),
    )
    original = {
        "logic.py": b"def bound(x): return min(x, 10)\n",
        "test_logic.py": b"from logic import bound\ndef test_bound(): assert bound(10) == 9\n",
    }

    class Repository:
        def archive(self, sha):
            assert sha == "a" * 40
            return make_archive({"repo/" + path: content for path, content in original.items()})

    class BrokerProtocol:
        files = {}
        executions = []

        def __call__(self, path, payload):
            assert payload["task_id"] == str(task["id"])
            if path == "/v1/workspaces":
                self.files = read_archive(base64.b64decode(payload["archive_base64"]))
                return {"task_id": str(task["id"]), "baseline_hash": snapshot_hash(self.files)}
            if path == "/v1/workspaces/snapshot":
                return {
                    "archive_base64": base64.b64encode(make_archive(self.files)).decode(),
                    "workspace_hash": snapshot_hash(self.files),
                }
            if path == "/v1/workspaces/diff":
                return {"diff": "Simulated protocol fixture diff"}
            if path == "/v1/workspaces/write":
                assert database.get_task(task["id"])["phase"] == "repair"
                self.files[payload["path"]] = base64.b64decode(payload["content_base64"])
                return {"path": payload["path"]}
            if path == "/v1/executions":
                self.executions.append(payload["execution_id"])
                assert len(set(self.executions)) == len(self.executions)
                exit_code, output = 0, "Protocol fixture preparation completed"
                if "decagent_check" in payload["argv"]:
                    fixed = json.loads(base64.b64decode(payload["argv"][-1]))
                    failed = b"min(x, 10)" in self.files["logic.py"] and not fixed["collect_only"]
                    exit_code = 1 if failed else 0
                    output = MARKER + json.dumps(
                        {
                            "collected": ["test_logic.py::test_bound"],
                            "failed": ["test_logic.py::test_bound"] if failed else [],
                            "errors": [],
                            "skipped": [],
                            "xfailed": [],
                            "exit_code": exit_code,
                            "failure_details": [],
                            "log": "Explicit simulated checker receipt",
                        }
                    )
                return {
                    "state": "completed",
                    "execution_id": payload["execution_id"],
                    "exit_code": exit_code,
                    "output": output,
                    "truncated": False,
                }
            raise AssertionError(path)

    class Model:
        def respond(self, messages, *, stream=False):
            if any(m["role"] == "tool" for m in messages):
                return ModelTurn(
                    "Ready for independent verification", (), ModelUsage(8, 4, 12), "fixture"
                )
            return ModelTurn(
                "",
                (
                    ToolProposal(
                        "fix1",
                        "write_file",
                        {"path": "logic.py", "content": "def bound(x): return min(x, 9)\n"},
                    ),
                ),
                ModelUsage(8, 4, 12),
                "fixture",
            )

    broker = BrokerProtocol()
    graph_config = {"configurable": {"thread_id": str(task["id"])}, "recursion_limit": 300}
    value = {
        "task_id": str(task["id"]),
        "messages": [{"role": "user", "content": auth["issue"]}],
        "iterations": 0,
        "queue": [],
    }
    with database.checkpointer() as saver:
        for _ in range(20):
            services = RuntimeServices(
                database,
                settings,
                config,
                claimed,
                "runtime-fixture",
                claimed["lease_generation"],
                broker_client=broker,
                primary=Model(),
                decision_engine=DecisionEngine(RuleStrategy()),
                github=Repository(),
            )
            result = build_graph(services, saver).invoke(value, graph_config)
            if not result.get("__interrupt__"):
                break
            pending = result["__interrupt__"][0].value
            assert pending["kind"] in {"action", "clarification"}
            approval = services.approvals.read(pending["approval_id"])
            if pending["kind"] == "action":
                action = pending["action"]
                # Controller snapshots carry execution_kind outside operative parameters.
                services.approvals.reply(
                    approval.id,
                    ApprovalReply(str(uuid4()), approval.digest, ReplyDecision.APPROVE, owner),
                    action=Action(action["tool"], action["params"]),
                    context=services.current_context(),
                )
            else:
                services.approvals.reply(
                    approval.id,
                    ApprovalReply(
                        str(uuid4()),
                        approval.digest,
                        ReplyDecision.CONFIRM,
                        owner,
                        confirmation="The failing bound test matches this defect",
                    ),
                    context=services.current_context(),
                )
            value = Command(resume={"approval_id": approval.id})
        else:
            raise AssertionError("Protocol fixture did not complete")
    assert result["verification"]["status"] == "verified"
    assert result["verification"]["verification"]["valid"] is True
    assert len(broker.executions) == 6
    assert len(set(broker.executions)) == 6
    assert database.get_task(task["id"])["state"] == "running"  # Worker owns terminal transition.
    with database.connect() as conn:
        memories = conn.execute(
            "SELECT * FROM app.memory_candidates WHERE source_task_id=%s", (task["id"],)
        ).fetchall()
    assert len(memories) == 1
    assert memories[0]["status"] == "pending_review"  # No calibrated automatic writing.
    assert memories[0]["source_pr"] is None
    assert memories[0]["applicability"]["_source_path"] == "logic.py"

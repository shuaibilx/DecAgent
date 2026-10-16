"""Conditional nodes retain a single operation across real MCP and crash boundaries."""

import importlib
import json

import pytest

from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import PlanRuntime
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from evals.deterministic.test_business_plan_contracts import literal, node, reference
from evals.integration.test_business_mcp_client import service as service

pytestmark = pytest.mark.integration


def create_plan(spec, params):
    source = reference("node_output", node_id="create", path=["receipt", "operation_id"])
    source["provenance"] = "write_receipt"
    return {
        "schema_version": "1",
        "plan_id": "create",
        "revision": 1,
        "registry_fingerprint": QuestionRegistry.from_spec(spec).fingerprint,
        "entry": "create",
        "goal": {
            "description": "Create requested ticket",
            "evidence": [
                {
                    "id": "created",
                    "kind": "write_receipt",
                    "description": "Committed receipt",
                    "source": source,
                },
            ],
        },
        "nodes": [
            node(
                "create",
                "governed_write",
                command={"plugin_id": spec.id, "command": "create"},
                inputs={key: literal(value) for key, value in params.items()},
                next=["done"],
            ),
            node("done", "complete", depends_on=["create"], evidence=["created"]),
        ],
    }


@pytest.mark.parametrize("crash_at", ["action_planned", "action_finished", "commit_response_lost"])
def test_crash_before_node_save_recovers_one_real_mcp_operation(service, tmp_path, crash_at):
    from decagent.business.mcp_client import MCPConnectionManager
    from evals.integration.test_business_mcp_worker import _browser

    api = importlib.import_module("decagent.business.plans.handlers")
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    issued = auth.issue_mcp(_browser(database, actor))
    scope = PluginRegistry(database).list()[0]
    params = {"title": "Conditional wire crash test", "category_id": category}
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": json.dumps({"command": "create", "params": params}),
            "plugin_digest": scope["manifest_digest"],
        },
        graph_version="business-3",
        employee_id=actor.employee_id,
        credential_ref=issued["credential_ref"],
    )
    task = database.claim(task["id"], "conditional-wire", seconds=120)
    repo = PlanRepository(database, task["id"], "conditional-wire", task["lease_generation"])
    repo.publish(create_plan(repo.spec, params))
    fired = []

    def crash(phase, action):
        if phase == crash_at and not fired:
            fired.append(str(action["id"]))
            raise SystemExit("Injected process exit")

    with MCPConnectionManager(url, auth) as manager:
        token = auth.resolve_credential_ref(task["id"])
        client = manager.client(auth.authenticate_mcp_token(token), token)
        if crash_at == "commit_response_lost":
            from decagent.business.mcp_client import MCPOutcomeUnknown

            real = client

            class LostCommitResponse:
                def __getattr__(self, name):
                    return getattr(real, name)

                def commit(self, *args, **kwargs):
                    real.commit(*args, **kwargs)
                    raise MCPOutcomeUnknown("Injected lost response after real commit")

            client = LostCommitResponse()
        services = BusinessServices(
            database,
            Settings(database_url=database.url, data_dir=tmp_path),
            RuntimeConfig(tickets_mcp_url=url),
            task,
            "conditional-wire",
            task["lease_generation"],
            mcp_client=client,
        )
        handlers = api.BusinessNodeHandlers(repo, services, after_action=crash)
        first = PlanRuntime(repo, handlers.execute)
        if crash_at == "commit_response_lost":
            waiting = first.step()
            assert waiting["status"] == "waiting"
            assert waiting["request"]["kind"] == "reconciliation"
            assert repo.nodes()[0]["status"] == "unknown"
            fired.append(waiting["request"]["action_id"])
            # The original receipt remains queryable after the overall active-time
            # boundary. This path must not issue another execution grant or commit.
            with database.connect() as conn:
                conn.execute("UPDATE app.tasks SET active_seconds=1800 WHERE id=%s", (task["id"],))
        else:
            with pytest.raises(SystemExit):
                first.step()
        handlers = api.BusinessNodeHandlers(repo, services)
        replacement = PlanRuntime(repo, handlers.execute)
        for _ in range(4):
            result = replacement.step()
            if result["status"] != "advanced":
                break
        assert result["status"] == "complete" and result["completion"]["valid"]
    with database.connect() as conn:
        actions = conn.execute(
            "SELECT * FROM app.actions WHERE task_id=%s", (task["id"],)
        ).fetchall()
        assert len(actions) == 1 and str(actions[0]["id"]) == fired[0]
        assert conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()["n"]
            == 1
        )
    assert repo.counters()["node_attempts"] == 2

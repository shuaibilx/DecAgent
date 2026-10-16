"""Production action journal uses MCP operation handles and verified receipts."""

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from mcp import ClientSession

from decagent.business.actions import assess_action
from decagent.business.execution import BusinessExecutor
from decagent.business.graph import GRAPH_VERSION, build_graph
from decagent.business.mcp_client import MCPConnectionManager, MCPOutcomeUnknown
from decagent.business.operations import OperationService
from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.config import Settings
from decagent.runtime.config import RuntimeConfig
from evals.integration.test_business_mcp_client import service as service

pytestmark = pytest.mark.integration


@pytest.fixture
def execution(service):
    database, url, auth, tokens, category = service
    token = tokens["employee-it"][0]
    actor = auth.authenticate_mcp_token(token)
    scope = PluginRegistry(database).list()[0]
    spec = PluginRegistry(database).get(scope["id"])
    task = database.create_business_task(
        scope["id"],
        {"instruction": "create", "plugin_digest": spec.digest},
    )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET employee_id=%s,credential_ref=%s WHERE id=%s",
            (actor.employee_id, actor.credential_ref, task["id"]),
        )
    leased = database.claim(task["id"], "mcp-worker", seconds=120)
    params = {"title": "VPN fails", "category_id": category}
    instruction = json.dumps({"command": "create", "params": params})
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(actor, token)
        assessment = assess_action(
            spec,
            str(task["id"]),
            str(scope["id"]),
            instruction,
            "create",
            params,
            {"directory": client.read("directory", {})},
        )
        executor = BusinessExecutor(
            database, spec, leased, "mcp-worker", leased["lease_generation"], mcp_client=client
        )
        yield database, executor, client, actor, assessment


def _counts(database):
    with database.connect() as conn:
        return tuple(
            conn.execute("SELECT count(*) AS n FROM business." + table).fetchone()["n"]
            for table in ("tickets", "ticket_history", "operation_receipts")
        )


def test_mcp_action_attaches_server_operation_and_verifies_one_write(execution):
    database, executor, _, _, assessment = execution
    action = executor.plan("one-write", assessment)
    result = executor.execute(action["id"], assessment["digest"])
    assert result["status"] == "succeeded"
    with database.connect() as conn:
        saved = conn.execute("SELECT * FROM app.actions WHERE id=%s", (action["id"],)).fetchone()
    assert saved["server_operation_id"] == result["data"]["receipt"]["operation_id"]
    assert result["verification"]["input_bound"] is True
    assert executor.execute(action["id"], assessment["digest"]) == result
    assert _counts(database) == (1, 1, 1)


def test_prepare_waits_for_employee_approval_and_never_claims_completion(execution):
    database, executor, _, actor, assessment = execution
    assessment = {**assessment, "outcome": "confirm"}
    action = executor.plan("needs-confirmation", assessment)
    pending = executor.execute(action["id"], assessment["digest"])
    assert pending["status"] == "awaiting_approval"
    assert _counts(database) == (0, 0, 0)
    with database.connect() as conn:
        saved = conn.execute("SELECT * FROM app.actions WHERE id=%s", (action["id"],)).fetchone()
        assert saved["status"] != "succeeded" and saved["receipt"] is None
        token_hash = hashlib.sha256(uuid4().bytes).hexdigest()
        conn.execute(
            "INSERT INTO business.employee_sessions"
            "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
            "VALUES(%s,%s,1,%s,now()+interval '1 hour')",
            (token_hash, actor.employee_id, "a" * 64),
        )
    browser = replace(actor, source="employee_session", token_hash=token_hash, credential_ref=None)
    OperationService(database).reply(
        browser, pending["operation_id"], pending["operation"]["input_hash"], "approve"
    )
    assert executor.execute(action["id"], assessment["digest"])["status"] == "succeeded"
    assert _counts(database) == (1, 1, 1)


def test_lost_mcp_commit_response_reconciles_original_id_without_duplicate(execution, monkeypatch):
    database, executor, client, _, assessment = execution
    original = client.commit

    def lost(operation_id, **kwargs):
        original(operation_id, **kwargs)
        raise MCPOutcomeUnknown(operation_id)

    monkeypatch.setattr(client, "commit", lost)
    action = executor.plan("lost-response", assessment)
    outcome = executor.execute(action["id"], assessment["digest"])
    assert outcome["status"] == "unknown"
    recovered = executor.reconcile(action["id"])
    assert recovered["status"] == "succeeded"
    assert _counts(database) == (1, 1, 1)


@pytest.mark.parametrize(
    ("stage", "tamper"),
    [
        ("business_commit_operation", "missing_structured"),
        ("business_get_operation", "missing_structured"),
        ("business_get_operation", "mismatched_receipt"),
    ],
)
def test_invalid_post_commit_result_preserves_unknown_and_original_receipt(
    execution, monkeypatch, stage, tamper
):
    from copy import deepcopy

    database, executor, _, _, assessment = execution
    original = ClientSession.call_tool
    corrupted = []

    async def invalid_result(self, name, *args, **kwargs):
        result = await original(self, name, *args, **kwargs)
        if name == stage and not corrupted:
            corrupted.append(True)
            if tamper == "missing_structured":
                return result.model_copy(update={"structured_content": None})
            payload = deepcopy(result.structured_content)
            payload["data"]["result"]["receipt"]["input_hash"] = "0" * 64
            return result.model_copy(
                update={
                    "structured_content": payload,
                    "content": [result.content[0].model_copy(update={"text": json.dumps(payload)})],
                }
            )
        return result

    monkeypatch.setattr(ClientSession, "call_tool", invalid_result)
    action = executor.plan("invalid-response", assessment)
    outcome = executor.execute(action["id"], assessment["digest"])
    assert outcome["status"] == "unknown"
    assert _counts(database) == (1, 1, 1)
    with database.connect() as conn:
        saved = conn.execute("SELECT * FROM app.actions WHERE id=%s", (action["id"],)).fetchone()
    operation_id = saved["server_operation_id"]
    assert operation_id and saved["status"] == "unknown"
    assert outcome["operation_id"] == operation_id
    monkeypatch.setattr(ClientSession, "call_tool", original)
    verified = executor.reconcile(action["id"])
    assert verified["status"] == "succeeded"
    assert verified["data"]["receipt"]["operation_id"] == operation_id
    assert _counts(database) == (1, 1, 1)


def test_production_graph_finishes_literal_goal_only_after_mcp_receipt(execution, tmp_path):
    database, executor, client, _, assessment = execution
    instruction = json.dumps({"command": "create", "params": assessment["snapshot"]["params"]})
    services = BusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(),
        executor.task,
        executor.owner,
        executor.generation,
        mcp_client=client,
    )
    try:
        with database.checkpointer() as saver:
            graph = build_graph(services, saver)
            result = graph.invoke(
                {
                    "data": {
                        "task_id": str(executor.task["id"]),
                        "graph_version": GRAPH_VERSION,
                        "instruction": instruction,
                        "strategy": "rule",
                        "steps": 0,
                        "messages": [],
                        "queue": [],
                        "observations": [],
                        "action_index": 0,
                    }
                },
                {"configurable": {"thread_id": str(executor.task["id"])}},
            )
        assert not result.get("__interrupt__")
        assert result["data"]["completion"]["valid"] is True
        assert len(result["data"]["observations"]) == 1
        assert result["data"]["observations"][0]["verification"]["server_verified"]
        assert _counts(database) == (1, 1, 1)
    finally:
        services.close()

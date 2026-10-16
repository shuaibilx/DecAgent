"""Explicit registered workflows enter the production harness without primary calls."""

import pytest

from decagent.business.registry import PluginRegistry
from decagent.business.workflow_registry import WorkflowRegistry
from decagent.decisions.contracts import digest
from decagent.gateway.auth import OwnerIdentity
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import BusinessTaskInput
from evals.deterministic.test_business_workflow_contracts import definition, report
from evals.integration.test_business_conditional_services import (
    MCPConnectionManager,
    drive,
    harness,
)
from evals.integration.test_business_mcp_client import service as service
from evals.integration.test_business_mcp_worker import _browser

pytestmark = pytest.mark.integration


def published(database):
    scope = PluginRegistry(database).list()[0]
    value = definition(str(scope["id"]))
    value["scope_revision"] = scope["revision"]
    value["input_schema"]["properties"]["client_note"] = {"type": "string"}
    value["input_schema"]["required"].append("client_note")
    with database.connect() as conn:
        conn.execute("INSERT INTO app.owner_credentials VALUES(true,%s)", ("f" * 64,))
    registry = WorkflowRegistry(database)
    owner = OwnerIdentity("bearer:" + "f" * 64, True)
    registered = registry.register(owner, value)
    evidence = report(registered["definition"])
    registry.validate(owner, value["workflow_id"], value["version"], evidence, digest(evidence))
    return registry.publish(owner, value["workflow_id"], value["version"]), scope


def submit(database, auth, tokens, *, inputs=None, enabled=True):
    current, scope = published(database)
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    credential = auth.issue_mcp(browser)
    body = BusinessTaskInput(
        scope_id=str(scope["id"]),
        instruction="查看目录",
        workflow={
            "workflow_id": current["workflow_id"],
            "version": current["version"],
            "inputs": {"client_note": "new task input"} if inputs is None else inputs,
        },
    )
    with database.connect() as conn:
        value = BusinessControl(database, conditional_workflows_enabled=enabled).submit_employee(
            conn, body, browser, credential["credential_ref"]
        )
    value = database.claim(value["id"], "workflow-entry", seconds=120)
    return value, auth.resolve_credential_ref(value["id"]), current


class ForbiddenPlanner:
    def plan(self, *args, **kwargs):
        pytest.fail("A published matching workflow must not call a primary planner")


@pytest.mark.parametrize("crash_after_publish", [False, True])
def test_explicit_hot_entry_persists_inputs_atomically_and_survives_restart(
    service, tmp_path, crash_after_publish
):
    database, url, auth, tokens, _ = service
    value, token, workflow = submit(database, auth, tokens)
    with MCPConnectionManager(url, auth) as manager:
        client = manager.client(auth.authenticate_mcp_token(token), token)
        initial = harness(database, url, value, client, ForbiddenPlanner(), tmp_path)
        if crash_after_publish:
            publish_plan = initial.repository.publish

            def interrupted(*args, **kwargs):
                publish_plan(*args, **kwargs)
                raise SystemExit("Injected post-publication exit")

            initial.repository.publish = interrupted
            with pytest.raises(SystemExit):
                initial.prepare({})
        else:
            initial.prepare({})
        resumed = harness(database, url, value, client, ForbiddenPlanner(), tmp_path)
        result = drive(resumed)
        assert result["completion"]["valid"]
        assert resumed.repository.context_snapshot()["task_input"] == {
            "instruction": "查看目录",
            "client_note": "new task input",
        }
        instance = WorkflowRegistry(database).instance(client._identity, value["id"])
        assert instance.definition_digest == workflow["definition_digest"]
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_records WHERE task_id=%s", (value["id"],)
            ).fetchone()["n"]
            == 0
        )
        events = conn.execute(
            "SELECT kind,payload FROM app.events WHERE task_id=%s "
            "AND kind='business.workflow_reused'",
            (value["id"],),
        ).fetchall()
        assert len(events) == 1 and events[0]["payload"]["primary_requests"] == 0


@pytest.mark.parametrize("mode", ["missing", "disabled", "instruction_override"])
def test_invalid_workflow_submission_creates_no_task_or_model_request(service, mode):
    database, _, auth, tokens, _ = service
    inputs = {} if mode == "missing" else {"client_note": "new input"}
    if mode == "instruction_override":
        inputs["instruction"] = "replace the employee goal"
    with pytest.raises(ValueError):
        submit(database, auth, tokens, inputs=inputs, enabled=mode != "disabled")
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app.tasks").fetchone()["n"] == 0

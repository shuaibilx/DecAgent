"""Real loopback MCP with explicitly synthetic decisions, never paid model quality.

The scripted gate bypasses calibration only inside this fixture. These regression
reports cannot publish the referenced nonexistent synthetic policies.
"""

from uuid import uuid4

import pytest

from decagent.business.execution import BusinessReadError
from decagent.business.mcp_client import MCPAuthenticationError, MCPConnectionManager
from decagent.business.plans.batching import DecisionBatchRunner
from decagent.business.plans.handlers import BusinessNodeHandlers
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import NodeResult, PlanRuntime
from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.business.workflow_registry import validate_workflow_report
from decagent.config import Settings
from decagent.decisions.contracts import ChoiceAnswer, NoulAnswer, ScoreAnswer, digest, evaluation
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import ReplyInput
from decagent.runtime.config import RuntimeConfig
from evals.deterministic.test_business_workflow_templates import TEMPLATES, definition
from evals.integration.test_business_mcp_client import service as service
from evals.integration.test_business_mcp_worker import _browser

pytestmark = pytest.mark.integration


class SyntheticAdapter:
    """Local label oracle for branch testing; no network, usage or quality claim."""

    def __init__(self, *, score=0, explicit=True, no_match=False):
        self.score, self.explicit, self.no_match = score, explicit, no_match
        self.calls = []

    def evaluate(self, request):
        self.calls.append(request)
        answers = {}
        for question in request.questions:
            if question.type == "choice":
                choice = "no_match" if self.no_match else next(iter(question.criteria))
                answers[question.id] = ChoiceAnswer(
                    choice, {key: float(key == choice) for key in question.criteria}, 1.0
                )
            elif question.type == "noul":
                answers[question.id] = NoulAnswer(float(self.explicit))
            else:
                answers[question.id] = ScoreAnswer(
                    self.score,
                    {str(i): float(i == self.score) for i in range(3)},
                    1.0,
                    {str(i): text for i, text in enumerate(question.criteria)},
                )
        return evaluation(
            request,
            "synthetic",
            "local-oracle",
            actual_model="synthetic-no-provider",
            answers=answers,
        )


def scripted_gate(node, raw, bound, batch):
    kind = node["question"]["type"]
    selected = (
        raw["choice"]
        if kind == "choice"
        else raw["noul"] >= 0.5
        if kind == "noul"
        else (raw["score"])
    )
    return NodeResult(
        {
            "kind": "decide",
            "accepted": True,
            "value": {"selected_value": selected},
            "employee_id": bound.employee_id,
            "scope_revision": bound.scope_revision,
            "account_revision": bound.account_revision,
            "synthetic": True,
            "grants_write_authority": False,
        },
        no_match=selected == "no_match",
    )


def setup(
    service, tmp_path, manager, template_id, *, score=0, explicit=True, no_match=False,
    workflow_identity=None, instruction_override=None,
):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["supervisor"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    client = manager.client(actor, tokens["supervisor"][0])
    inputs = {"instruction": "请创建工单", "title": "Current title", "description": "Current text"}
    if template_id != "create-ticket":
        operation = client.prepare(
            str(uuid4()),
            "create",
            {
                "title": "Current target " + uuid4().hex[:8],
                "category_id": category,
            },
        )
        created = client.commit(operation["operation_id"])
        assert created["state"] == "succeeded"
        ticket_id = created["result"]["ticket"]["id"]
        if template_id == "directory-assign":
            person = client.read("directory", {})["people"][0]
            instruction = f"请分派工单给 {person['name']} {person['team']} {person['id']}"
        else:
            instruction = "请查看详情，按需调整优先级" if score else "请查看详情"
        inputs = {"instruction": instruction, "ticket_id": ticket_id}
    if instruction_override is not None:
        inputs["instruction"] = instruction_override
    scope = PluginRegistry(database).list()[0]
    value = definition(template_id, scope_id=str(scope["id"]), scope_revision=scope["revision"])
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": inputs["instruction"],
            "plugin_digest": scope["manifest_digest"],
            **({"workflow": {"workflow_id": workflow_identity}} if workflow_identity else {}),
        },
        graph_version="business-3",
        employee_id=actor.employee_id,
        credential_ref=issued["credential_ref"],
    )
    task = database.claim(task["id"], "workflow-template-test", seconds=120)
    repo = PlanRepository(database, task["id"], task["lease_owner"], task["lease_generation"])
    repo.publish(value["plan"], input_schema=value["input_schema"])
    services = BusinessServices(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(tickets_mcp_url=url),
        task,
        task["lease_owner"],
        task["lease_generation"],
        mcp_client=client,
    )
    adapter = SyntheticAdapter(score=score, explicit=explicit, no_match=no_match)
    runner = DecisionBatchRunner(repo, adapter, scripted_gate)
    handlers = BusinessNodeHandlers(repo, services, decide=runner.consume)
    runtime = PlanRuntime(repo, handlers.execute, task_input=inputs, prime_decisions=runner.prime)
    return runtime, repo, adapter, services, browser, value


@pytest.mark.parametrize("registered", [True, False])
def test_full_conditional_priority_goal_uses_fresh_read_without_fake_write(
    service, tmp_path, registered,
):
    # Server-pinned identity is synthetic here, not a published quality policy.
    # Only the deterministic goal proof and real MCP effects are under test.
    database, url, auth, _, _ = service
    with MCPConnectionManager(url, auth) as manager:
        runtime, repo, adapter, _, _, _ = setup(
            service, tmp_path, manager, "review-priority", score=0,
            workflow_identity="review-priority" if registered else None,
            instruction_override="请查看详情，按需调整优先级",
        )
        result = drain(runtime)
        assert result["status"] == "complete", result
        completion = result["completion"]
        assert completion["valid"] is registered
        assert completion.get("satisfied_without_write", []) == (
            ["set_priority"] if registered else []
        )
        assert completion.get("actions", []) == []
        assert adapter.calls
        with database.connect() as conn:
            assert conn.execute(
                "SELECT count(*) AS n FROM app.actions WHERE task_id=%s",
                (repo.task_id,),
            ).fetchone()["n"] == 0
        assert_zero_primary(database, repo)


def drain(runtime):
    for _ in range(24):
        result = runtime.step()
        if result["status"] != "advanced":
            return result
    raise AssertionError("Finite workflow did not settle")


def approve(service, repo, browser):
    database = service[0]
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.tasks SET state='awaiting_input',lease_owner=NULL,lease_until=NULL "
            "WHERE id=%s",
            (repo.task_id,),
        )
        task = conn.execute(
            "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE",
            (repo.task_id,),
        ).fetchone()
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE task_id=%s FOR UPDATE",
            (repo.task_id,),
        ).fetchone()
        BusinessControl(database).reply_employee(
            conn,
            task,
            approval,
            ReplyInput(expected_digest=approval["digest"], decision="approve"),
            browser,
            "synthetic-test-employee-approval",
        )
    task = database.claim(repo.task_id, repo.owner, seconds=120)
    repo.generation = task["lease_generation"]


def observed(repo):
    nodes, edges = set(), set()
    # Test-owner inspection remains possible after the credential denial case;
    # execution itself must continue to fail the repository's normal auth guard.
    with repo.database.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM app.business_plan_nodes WHERE task_id=%s",
            (repo.task_id,),
        ).fetchall()
    for row in rows:
        nodes.add(row["node_id"])
        targets = (row["output_snapshot"] or {}).get("control", {}).get("targets", [])
        edges.update((row["node_id"], target) for target in targets)
    return nodes, edges


def assert_zero_primary(database, repo, result=None):
    if result and result["status"] == "complete":
        assert result["completion"]["model_requests"] == 0
        assert result["completion"]["summary"]
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM app.usage_records "
                "WHERE task_id=%s AND model_family='primary'",
                (repo.task_id,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("template_id", TEMPLATES)
def test_wire_matrix_reports_only_executed_node_and_edge_coverage(service, tmp_path, template_id):
    database, url, auth, _, _ = service
    covered_nodes, covered_edges, cases = set(), set(), []
    variants = [("success", {})]
    if template_id == "review-priority":
        variants += [("branch", {"score": 1}), ("branch", {"score": 2})]
    elif template_id == "directory-assign":
        variants += [("missing_input", {"explicit": False}), ("no_match", {"no_match": True})]
    else:
        variants += [("no_match", {"no_match": True})]
    with MCPConnectionManager(url, auth) as manager:
        for index, (category, options) in enumerate(variants):
            runtime, repo, adapter, services, browser, value = setup(
                service, tmp_path, manager, template_id, **options
            )
            result = drain(runtime)
            if result["status"] == "waiting":
                assert result["request"]["kind"] == "action"
                assert result["request"]["operation"]["state"] == "awaiting_approval"
                approve(service, repo, browser)
                # The executor uses the newly claimed lease generation too.
                services.executor.generation = repo.generation
                result = drain(runtime)
            if category in {"success", "branch"}:
                assert result["status"] == "complete" and result["completion"]["valid"], result
            else:
                assert result == {"status": "stopped", "reason": category}
            nodes, edges = observed(repo)
            covered_nodes.update(nodes)
            covered_edges.update(edges)
            assert adapter.calls
            assert_zero_primary(database, repo, result)
            cases.append({"case_id": f"executed-{index}", "category": category, "passed": True})

        for failure in ("missing_input", "permission_denied", "tool_failure"):
            runtime, repo, _, services, _, value = setup(service, tmp_path, manager, template_id)
            if failure == "missing_input":
                runtime.task_input.pop("title" if template_id == "create-ticket" else "ticket_id")
                assert drain(runtime) == {"status": "stopped", "reason": "missing_input"}
            elif failure == "permission_denied":
                with database.connect() as conn:
                    conn.execute(
                        "UPDATE business.mcp_credentials SET revoked_at=now() WHERE id=%s",
                        (repo.guard()["credential_ref"],),
                    )
                with pytest.raises(MCPAuthenticationError):
                    runtime.step()
            else:

                def failed_read(command, params):
                    raise BusinessReadError("not_found")

                services.executor.read = failed_read
                assert drain(runtime) == {"status": "stopped", "reason": "error"}
            nodes, edges = observed(repo)
            covered_nodes.update(nodes)
            covered_edges.update(edges)
            assert_zero_primary(database, repo)
            cases.append({"case_id": failure, "category": failure, "passed": True})
    # Coverage is extracted only from persisted executions above, including both
    # priority routes; no declared node/edge list is copied into the report.
    report = {
        "format": "decagent.workflow-regression.v1",
        "report_id": "synthetic-" + template_id,
        "reference": "fixture://real-mcp-scripted-decisions/" + template_id,
        "definition_digest": digest(value),
        "synthetic": True,
        "quality_claim": False,
        "cases": cases,
        "covered_nodes": sorted(covered_nodes),
        "covered_edges": [list(edge) for edge in sorted(covered_edges)],
    }
    assert validate_workflow_report(report, digest(report), value) == report

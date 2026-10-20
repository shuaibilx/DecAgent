"""Controlled directory routes use PostgreSQL facts and actual loopback MCP.

This read-only fixture adapter is intentionally limited to a pristine disposable
database. It is not a public benchmark adapter or a reset implementation for
arbitrary write workloads. Planner/ReAct replies are scripted, not paid calls.
"""

import json
from contextlib import contextmanager
from decimal import Decimal

import pytest

from decagent.business.execution import BusinessExecutor
from decagent.business.mcp_client import MCPConnectionManager
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import NodeFailure
from decagent.business.registry import PluginRegistry
from decagent.business.tickets.store import TicketStore
from decagent.decisions.contracts import digest
from decagent.evaluation.business.comparison import ComparisonTrial, Route, compare_full_workflow
from decagent.evaluation.business.routes import (
    ControlledRoutes,
    ModelReply,
    TaskScore,
    TrialBoundary,
)
from evals.deterministic.test_business_plan_compiler import read_plan
from evals.integration.test_business_mcp_client import service as service

pytestmark = pytest.mark.integration


class DirectorySession:
    """Concrete real application adapter for a directory-only integration case."""

    def __init__(self, service, isolation_id):
        self.database, self.url, self.auth, self.tokens, _ = service
        self.isolation_id = isolation_id
        self.scope = PluginRegistry(self.database).list()[0]
        self.spec = PluginRegistry(self.database).get(self.scope["id"])
        self.task_input = {"instruction": json.dumps({"command": "directory", "params": {}})}
        self.skill = "Read the current authorized directory and finish after observing it."
        self.manager = MCPConnectionManager(self.url, self.auth)
        token, self.credential_ref = self.tokens["employee-it"]
        self.identity = self.auth.authenticate_mcp_token(token)
        self.client = self.manager.client(self.identity, token)
        self.gateway = self
        self.repository, self.executor, self.task_id = None, None, None
        self.calls = []

    def business_snapshot(self):
        with self.database.connect() as conn:
            return {
                table: conn.execute(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),"
                    "'[]'::jsonb) AS state FROM business." + table + " t"
                ).fetchone()["state"]
                for table in (
                    "people",
                    "categories",
                    "tickets",
                    "ticket_history",
                    "operations",
                    "operation_receipts",
                )
            }

    def reset(self):
        # The service fixture created this fresh isolated DB. No write route is
        # admitted here; verify its pristine snapshot rather than deleting data.
        snapshot = self.business_snapshot()
        return self.repository is None and all(
            not snapshot[name]
            for name in ("tickets", "ticket_history", "operations", "operation_receipts")
        )

    def boundary(self):
        with self.database.connect() as conn:
            account = conn.execute(
                "SELECT a.id::text,a.person_id::text,a.login,a.role,a.active,a.credential_revision,"
                "p.active AS person_active,p.team FROM business.employee_accounts a "
                "JOIN business.people p ON p.id=a.person_id WHERE a.id=%s",
                (self.identity.employee_id,),
            ).fetchone()
            scope = conn.execute(
                "SELECT enabled,revision,manifest_digest FROM app.business_scopes WHERE id=%s",
                (self.scope["id"],),
            ).fetchone()
        return TrialBoundary(
            initial_state_digest=digest(self.business_snapshot()),
            goal_digest=digest(self.task_input),
            actor_profile_digest=digest(account),
            permission_digest=digest({"account": account, "scope": scope}),
            tool_adapter_digest=digest(
                {
                    "manifest": self.spec.manifest,
                    "protocol": self.client.protocol_version,
                    "tools": sorted(self.client.list_tools(), key=lambda tool: tool["name"]),
                }
            ),
        )

    def publish_plan(self, plan, task_input):
        task = self.database.create_business_task(
            self.scope["id"],
            {"instruction": task_input["instruction"], "plugin_digest": self.spec.digest},
            graph_version="business-3",
            employee_id=self.identity.employee_id,
            credential_ref=self.credential_ref,
        )
        task = self.database.claim(task["id"], self.isolation_id, seconds=120)
        self.task_id = task["id"]
        self.repository = PlanRepository(
            self.database, task["id"], self.isolation_id, task["lease_generation"]
        )
        self.repository.publish(plan.to_mapping(), task_input=task_input)
        self.executor = BusinessExecutor(
            self.database,
            self.spec,
            task,
            self.isolation_id,
            task["lease_generation"],
            mcp_client=self.client,
        )
        return self.repository

    def call(self, command, params):
        assert command == "directory", "This integration adapter is read-only"
        self.calls.append((command, params))
        # ReAct also gets an isolated leased application task and the exact same
        # production executor; it does not publish or execute a conditional plan.
        if self.executor is None:
            task = self.database.create_business_task(
                self.scope["id"],
                {"instruction": self.task_input["instruction"], "plugin_digest": self.spec.digest},
                graph_version="business-3",
                employee_id=self.identity.employee_id,
                credential_ref=self.credential_ref,
            )
            task = self.database.claim(task["id"], self.isolation_id, seconds=120)
            self.task_id = task["id"]
            self.executor = BusinessExecutor(
                self.database,
                self.spec,
                task,
                self.isolation_id,
                task["lease_generation"],
                mcp_client=self.client,
            )
        return self.executor.read(command, params)

    def handle_node(self, *args):
        raise NodeFailure("Directory-only integration fixture has no write handler")

    def score(self):
        snapshot = self.business_snapshot()
        forbidden = sum(
            len(snapshot[name])
            for name in ("tickets", "ticket_history", "operations", "operation_receipts")
        )
        success = bool(self.calls)
        if self.repository is not None:
            rows = {row["node_id"]: row for row in self.repository.nodes()}
            with self.database.connect() as conn:
                expected = TicketStore(self.database.url)._read(conn, "directory", {})
            success = (
                rows.get("directory", {}).get("output_snapshot", {}).get("result", {}).get("data")
                == expected
            )
        return TaskScore(success=success, forbidden_side_effects=forbidden)

    def close(self):
        self.client.close()
        self.manager.close()


def setup(service):
    sample = DirectorySession(service, "preflight-only")
    try:
        identity = sample.boundary()
    finally:
        sample.close()
    trial = ComparisonTrial(
        dataset="decagent-directory-integration",
        dataset_version="1",
        dataset_checksum=digest("directory-integration-v1"),
        case_id="directory-loopback",
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        **vars(identity),
    )
    sessions = []

    @contextmanager
    def factory(trial, isolation_id):
        current = DirectorySession(service, isolation_id)
        sessions.append(current)
        try:
            yield current
        finally:
            current.close()

    return ControlledRoutes(factory), trial, sessions


def test_once_plan_persists_real_repository_and_observes_loopback_mcp(service):
    runner, trial, sessions = setup(service)
    result = runner.run(
        Route.PLAN_MCP,
        trial,
        planner=lambda request: ModelReply(
            read_plan(), tokens=19, cost_usd=Decimal("0"), model="scripted-plan"
        ),
    )
    assert result.status == "complete", result.error
    assert result.run.task_success and result.run.primary_requests == 1
    assert result.terminal["completion"]["valid"] and len(result.tool_calls) == 1
    repository = sessions[0].repository
    rows = {row["node_id"]: row for row in repository.nodes()}
    assert rows["directory"]["status"] == rows["done"]["status"] == "succeeded"
    assert rows["directory"]["dispatch_attempts"] == 1
    assert rows["directory"]["output_snapshot"]["result"]["data"]["categories"]
    assert sessions[0].manager.closed and repository.counters()["node_attempts"] == 2


def test_react_and_one_plan_have_same_real_snapshot_identity_gateway_and_private_scorer(service):
    runner, trial, sessions = setup(service)

    def react(request):
        value = (
            {"finish": True} if request["observations"] else {"tool": "directory", "arguments": {}}
        )
        return ModelReply(value, tokens=11, cost_usd=Decimal("0"), model="scripted-primary")

    left = runner.run(Route.REACT_SKILL_MCP, trial, react=react)
    right = runner.run(
        Route.PLAN_MCP,
        trial,
        planner=lambda request: ModelReply(
            read_plan(), tokens=19, cost_usd=Decimal("0"), model="scripted-primary"
        ),
    )
    assert left.status == right.status == "complete", (left.error, right.error)
    assert left.run.task_success and right.run.task_success
    assert compare_full_workflow(left.run, right.run).case_id == trial.case_id
    assert left.run.primary_requests == 2 and right.run.primary_requests == 1
    assert sessions[0].task_id != sessions[1].task_id
    assert sessions[0].manager.closed and sessions[1].manager.closed

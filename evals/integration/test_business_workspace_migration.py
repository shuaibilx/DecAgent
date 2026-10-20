"""The workspace ownership migration keeps legacy recovery identities intact."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from decagent.business.tickets.store import TicketStore
from decagent.storage import database as storage
from evals.integration.database_support import temporary_database

pytestmark = pytest.mark.integration

LEGACY_WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def pre_workspace_database(monkeypatch):
    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    version = storage.SCHEMA_VERSION
    with temporary_database(url, prefix="case_workspace_") as database:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "MIGRATIONS", storage.MIGRATIONS[:-1])
            patch.setattr(storage, "SCHEMA_VERSION", version - 1)
            database.migrate()
        yield database


def _legacy_rows(database):
    now = datetime.now(UTC)
    person_id, employee_id, scope_id = uuid4(), uuid4(), uuid4()
    category_id, ticket_id = uuid4(), uuid4()
    task_id, approval_id, memory_id = uuid4(), uuid4(), uuid4()
    operation_id = f"legacy-operation-{uuid4()}"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO business.people(id,name,team) VALUES(%s,'Legacy employee','Ops')",
            (person_id,),
        )
        connection.execute(
            "INSERT INTO business.categories(id,name) VALUES(%s,'Legacy category')",
            (category_id,),
        )
        connection.execute(
            "INSERT INTO business.employee_accounts"
            "(id,login,person_id,role,password_hash) VALUES(%s,%s,%s,'employee',%s)",
            (employee_id, f"legacy-{employee_id.hex[:12]}", person_id, "x" * 32),
        )
        connection.execute(
            "INSERT INTO app.business_scopes"
            "(id,plugin_id,plugin_version,manifest_digest,manifest) VALUES(%s,'legacy-plugin',"
            "'1.0.0',%s,%s)",
            (scope_id, "a" * 64, Jsonb({})),
        )
        connection.execute(
            "INSERT INTO business.tickets(id,title,category_id,employee_id) "
            "VALUES(%s,'Legacy ticket',%s,%s)",
            (ticket_id, category_id, employee_id),
        )
        connection.execute(
            "INSERT INTO business.ticket_history(id,ticket_id,revision,command,details,employee_id,"
            "operation_id,source) VALUES(%s,%s,1,'create',%s,%s,%s,'external_mcp')",
            (uuid4(), ticket_id, Jsonb({"title": "Legacy ticket"}), employee_id, operation_id),
        )
        connection.execute(
            "INSERT INTO business.operations"
            "(id,employee_id,request_id,scope_id,command,params,effective_params,input_hash,"
            "contract_digest,source,state,confirmation_required) "
            "VALUES(%s,%s,%s,%s,'create',%s,%s,%s,%s,'external_mcp','unknown',false)",
            (
                operation_id,
                employee_id,
                f"request-{uuid4()}",
                scope_id,
                Jsonb({"ticket_id": str(ticket_id)}),
                Jsonb({"ticket_id": str(ticket_id)}),
                "b" * 64,
                "c" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO business.operation_receipts"
            "(operation_id,input_hash,command,resource_id,revision,result,employee_id,contract_digest)"
            " VALUES(%s,%s,'create',%s,1,%s,%s,%s)",
            (
                operation_id,
                "b" * 64,
                ticket_id,
                Jsonb({"ticket_id": str(ticket_id)}),
                employee_id,
                "c" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO app.tasks"
            "(id,kind,business_scope_id,employee_id,authorization_snapshot,graph_version) "
            "VALUES(%s,'business',%s,%s,%s,'business-2')",
            (task_id, scope_id, employee_id, Jsonb({"instruction": "legacy recovery"})),
        )
        connection.execute(
            "INSERT INTO app.approvals(id,task_id,digest,kind) VALUES(%s,%s,%s,'decision')",
            (approval_id, task_id, "d" * 64),
        )
        connection.execute(
            "INSERT INTO app.business_memories"
            "(id,company_scope,employee_id,kind,subject,content,evidence_refs,scope_id,"
            "scope_revision,account_revision,contract_fingerprint,question_fingerprint,"
            "plan_fingerprint,status,expires_at,content_digest,created_at,updated_at) "
            "VALUES(%s,'local-company',%s,'confirmed_preference','response_language',%s,%s,%s,"
            "1,1,%s,%s,%s,'pending_confirmation',%s,%s,%s,%s)",
            (
                memory_id,
                employee_id,
                Jsonb({"language": "zh-CN"}),
                Jsonb([{"kind": "employee_confirmation"}]),
                scope_id,
                "e" * 64,
                "f" * 64,
                "1" * 64,
                now + timedelta(days=30),
                "2" * 64,
                now,
                now,
            ),
        )
    with database.checkpointer() as saver:
        checkpoint_config = saver.put(
            {"configurable": {"thread_id": str(task_id), "checkpoint_ns": ""}},
            empty_checkpoint(),
            {"source": "input", "step": 0},
            {},
        )
    return {
        "person_id": person_id,
        "employee_id": employee_id,
        "scope_id": scope_id,
        "category_id": category_id,
        "ticket_id": ticket_id,
        "task_id": task_id,
        "approval_id": approval_id,
        "memory_id": memory_id,
        "operation_id": operation_id,
        "checkpoint_config": checkpoint_config,
    }


def test_legacy_business_rows_and_checkpoint_are_backfilled_without_identity_changes(
    pre_workspace_database,
):
    database = pre_workspace_database
    ids = _legacy_rows(database)
    with database.connect() as connection:
        before = {
            "employee": connection.execute(
                "SELECT id,login,person_id,role FROM business.employee_accounts WHERE id=%s",
                (ids["employee_id"],),
            ).fetchone(),
            "task": connection.execute(
                "SELECT id,employee_id,business_scope_id,graph_version,state,lease_generation "
                "FROM app.tasks WHERE id=%s",
                (ids["task_id"],),
            ).fetchone(),
            "approval": connection.execute(
                "SELECT id,task_id,digest,status FROM app.approvals WHERE id=%s",
                (ids["approval_id"],),
            ).fetchone(),
            "memory": connection.execute(
                "SELECT id,employee_id,subject,content,status FROM app.business_memories "
                "WHERE id=%s",
                (ids["memory_id"],),
            ).fetchone(),
            "operation": connection.execute(
                "SELECT id,employee_id,request_id,state FROM business.operations WHERE id=%s",
                (ids["operation_id"],),
            ).fetchone(),
            "receipt": connection.execute(
                "SELECT operation_id,input_hash,resource_id,result "
                "FROM business.operation_receipts "
                "WHERE operation_id=%s",
                (ids["operation_id"],),
            ).fetchone(),
        }

    database.migrate()
    database.migrate()
    directory = TicketStore(database.url).directory({})["data"]
    assert all("workspace_id" not in row for row in directory["people"] + directory["categories"])

    with database.connect() as connection:
        legacy_workspace = connection.execute(
            "SELECT id,slug FROM business.workspaces WHERE id=%s", (LEGACY_WORKSPACE_ID,)
        ).fetchone()
        assert str(legacy_workspace["id"]) == LEGACY_WORKSPACE_ID
        assert legacy_workspace["slug"] == "legacy"
        after = {
            "employee": connection.execute(
                "SELECT id,login,person_id,role FROM business.employee_accounts WHERE id=%s",
                (ids["employee_id"],),
            ).fetchone(),
            "task": connection.execute(
                "SELECT id,employee_id,business_scope_id,graph_version,state,lease_generation "
                "FROM app.tasks WHERE id=%s",
                (ids["task_id"],),
            ).fetchone(),
            "approval": connection.execute(
                "SELECT id,task_id,digest,status FROM app.approvals WHERE id=%s",
                (ids["approval_id"],),
            ).fetchone(),
            "memory": connection.execute(
                "SELECT id,employee_id,subject,content,status FROM app.business_memories "
                "WHERE id=%s",
                (ids["memory_id"],),
            ).fetchone(),
            "operation": connection.execute(
                "SELECT id,employee_id,request_id,state FROM business.operations WHERE id=%s",
                (ids["operation_id"],),
            ).fetchone(),
            "receipt": connection.execute(
                "SELECT operation_id,input_hash,resource_id,result "
                "FROM business.operation_receipts "
                "WHERE operation_id=%s",
                (ids["operation_id"],),
            ).fetchone(),
        }
        assert after == before
        owned = (
            "business.people",
            "business.categories",
            "business.tickets",
            "business.ticket_history",
            "business.operation_receipts",
            "business.employee_accounts",
            "business.employee_sessions",
            "business.mcp_credentials",
            "business.employee_audit",
            "business.operations",
            "business.operation_approvals",
            "business.execution_grants",
            "app.tasks",
            "app.events",
            "app.actions",
            "app.action_attempts",
            "app.approvals",
            "app.verification_runs",
            "app.decision_records",
            "app.usage_records",
            "app.deliveries",
            "app.decision_validation_reports",
            "app.decision_policies",
            "app.task_decision_thresholds",
            "app.task_decision_policy_bindings",
            "app.decision_policy_events",
            "app.business_decision_interventions",
            "app.business_optional_intervention_rules",
            "app.business_memory_rules",
            "app.business_memories",
            "app.business_memory_source_revocations",
            "app.business_memory_audit",
            "app.business_workflows",
            "app.workflow_validation_reports",
            "app.workflow_instances",
            "app.workflow_events",
            "app.business_plan_revisions",
            "app.business_plan_heads",
            "app.business_plan_nodes",
        )
        for table in owned:
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s AND column_name='workspace_id'",
                    tuple(table.split(".")),
                ).fetchone()["n"]
                == 1
            ), table
        for table, identifier_column, identifier in (
            ("business.employee_accounts", "id", ids["employee_id"]),
            ("business.tickets", "id", ids["ticket_id"]),
            ("business.operations", "id", ids["operation_id"]),
            ("business.operation_receipts", "operation_id", ids["operation_id"]),
            ("app.tasks", "id", ids["task_id"]),
            ("app.approvals", "id", ids["approval_id"]),
            ("app.business_memories", "id", ids["memory_id"]),
        ):
            row = connection.execute(
                f"SELECT workspace_id FROM {table} WHERE {identifier_column}=%s", (identifier,)
            ).fetchone()
            assert str(row["workspace_id"]) == LEGACY_WORKSPACE_ID
        with database.checkpointer() as saver:
            checkpoint = saver.get_tuple(ids["checkpoint_config"])
        assert checkpoint.config["configurable"]["thread_id"] == str(ids["task_id"])
        assert (
            checkpoint.checkpoint["id"] == ids["checkpoint_config"]["configurable"]["checkpoint_id"]
        )

        guest_workspace_id, guest_category_id = uuid4(), uuid4()
        connection.execute(
            "INSERT INTO business.workspaces(id,slug,kind) VALUES(%s,'test-guest','guest')",
            (guest_workspace_id,),
        )
        connection.execute(
            "INSERT INTO business.categories(id,name,workspace_id) VALUES(%s,'Legacy category',%s)",
            (guest_category_id, guest_workspace_id),
        )

    with pytest.raises(ForeignKeyViolation):
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO business.tickets(id,title,category_id,workspace_id) "
                "VALUES(%s,'Cross-workspace ticket',%s,%s)",
                (uuid4(), ids["category_id"], guest_workspace_id),
            )

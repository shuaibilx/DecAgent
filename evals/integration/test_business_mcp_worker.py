"""Real MCP worker pauses, resumes and retains the original employee and operation."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from decagent.business.access import AccessError
from decagent.business.execution import BusinessExecutor
from decagent.business.mcp_client import MCPConnectionManager
from decagent.business.registry import PluginRegistry
from decagent.business.services import BusinessServices
from decagent.business.worker import BusinessWorker
from decagent.config import Settings
from decagent.gateway.business import BusinessControl
from decagent.gateway.models import ReplyInput
from decagent.models.primary import ModelTurn, ModelUsage, ToolProposal
from decagent.runtime.config import RuntimeConfig
from decagent.runtime.model_calls import TokenPrice
from evals.integration.test_business_mcp_client import service as service

pytestmark = pytest.mark.integration


def _browser(database, actor):
    token_hash = hashlib.sha256(uuid4().bytes).hexdigest()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.employee_sessions"
            "(token_hash,employee_id,account_revision,csrf_hash,expires_at) "
            "VALUES(%s,%s,1,%s,now()+interval '1 hour')",
            (token_hash, actor.employee_id, "a" * 64),
        )
    return replace(actor, source="employee_session", token_hash=token_hash, credential_ref=None)


def _task(database, actor, credential_ref, command, params):
    scope = PluginRegistry(database).list()[0]
    return database.create_business_task(
        scope["id"],
        {
            "instruction": json.dumps({"command": command, "params": params}),
            "strategy": "rule",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"employee_id": str(actor.employee_id), "authenticated": True},
        },
        employee_id=actor.employee_id,
        credential_ref=credential_ref,
    )


def _worker(database, url, auth, manager, tmp_path):
    return BusinessWorker(
        database,
        Settings(database_url=database.url, data_dir=tmp_path),
        RuntimeConfig(tickets_mcp_url=url),
        employee_auth=auth,
        mcp_manager=manager,
        heartbeat_seconds=1,
        lease_seconds=30,
    )


def test_worker_reuses_process_manager_and_finishes_employee_tasks(service, tmp_path):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    with MCPConnectionManager(url, auth) as manager:
        worker = _worker(database, url, auth, manager, tmp_path)
        for index in range(2):
            task = _task(
                database,
                actor,
                issued["credential_ref"],
                "create",
                {
                    "title": "Worker ticket " + str(index),
                    "category_id": category,
                },
            )
            assert worker.run_once()
            assert database.get_task(task["id"])["state"] == "succeeded"
            assert manager.connection_count == 1
        worker.close()
        assert not manager.closed
    with database.connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 2
        )


def test_scripted_model_worker_smoke_uses_real_mcp_without_provider_credentials(
    service, tmp_path, monkeypatch
):
    import decagent.business.services as business_services

    database, url, auth, tokens, _category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    scope = PluginRegistry(database).list()[0]
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "请查询分类目录。",
            "strategy": "main",
            "plugin_digest": scope["manifest_digest"],
            "provenance": {"employee_id": str(actor.employee_id), "authenticated": True},
        },
        budget={"max_calls": 10, "max_tokens": 100000, "max_active_seconds": 60},
        employee_id=actor.employee_id,
        credential_ref=issued["credential_ref"],
    )
    scripted_calls = []

    class ScriptedModel:
        def __init__(self, protocol):
            self.protocol = protocol

        def respond(self, messages, **_kwargs):
            observations = [
                json.loads(message["content"])
                for message in messages
                if message["role"] == "tool"
            ]
            if not observations:
                name = next(
                    tool_name
                    for tool_name, command in self.protocol.commands.items()
                    if command == "directory"
                )
                params = {}
            else:
                action_ids = [
                    item["action_id"] for item in observations if item.get("action_id")
                ]
                name = "decagent_finish"
                params = {
                    "summary": "Read the current ticket directory.",
                    "completed_actions": action_ids,
                }
            scripted_calls.append(name)
            return ModelTurn(
                "",
                (ToolProposal("offline-smoke-" + str(len(scripted_calls)), name, params),),
                ModelUsage(8, 2, 10),
                "scripted-offline",
            )

    class ScriptedBusinessServices(BusinessServices):
        def __init__(self, *args, mcp_client, **kwargs):
            super().__init__(
                *args,
                mcp_client=mcp_client,
                primary_factory=lambda _settings, protocol: ScriptedModel(protocol),
                **kwargs,
            )

    monkeypatch.setattr(business_services, "BusinessServices", ScriptedBusinessServices)

    settings = Settings(database_url=database.url, data_dir=tmp_path, main_api_key="")
    runtime = RuntimeConfig(
        tickets_mcp_url=url,
        main_price=TokenPrice("scripted-offline-1", Decimal(0), Decimal(0)),
    )
    with MCPConnectionManager(url, auth) as manager:
        worker = BusinessWorker(
            database,
            settings,
            runtime,
            employee_auth=auth,
            mcp_manager=manager,
        )
        assert worker.run_once()

    result = database.get_task(task["id"])
    assert result["state"] == "succeeded", database.events(task["id"])[-1]["payload"]
    assert len(scripted_calls) == 2 and scripted_calls[-1] == "decagent_finish"
    assert settings.main_api_key == ""
    with database.connect() as conn:
        actions = conn.execute(
            "SELECT status,snapshot FROM app.actions WHERE task_id=%s", (task["id"],)
        ).fetchall()
        assert len(actions) == 1
        assert actions[0]["status"] == "succeeded"
        assert actions[0]["snapshot"]["command"] == "directory"
        usage = conn.execute(
            "SELECT status,dispatch_state,actual_cost,actual_tokens,resolved_model "
            "FROM app.usage_records WHERE task_id=%s ORDER BY created_at,id",
            (task["id"],),
        ).fetchall()
    assert len(usage) == 2
    assert all(item["status"] == "settled" for item in usage)
    assert all(item["dispatch_state"] == "responded" for item in usage)
    assert all(item["actual_cost"] == Decimal(0) for item in usage)
    assert all(item["actual_tokens"] == 10 for item in usage)
    assert all(item["resolved_model"] == "scripted-offline" for item in usage)


def test_worker_restart_after_browser_approval_keeps_original_service_operation(service, tmp_path):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    ticket = str(uuid4())
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,requester_id,employee_id,"
            "status,resolution) VALUES(%s,'resolved',%s,%s,%s,'resolved','Actual supplied note')",
            (ticket, category, actor.person_id, actor.employee_id),
        )
    task = _task(
        database,
        actor,
        issued["credential_ref"],
        "close",
        {
            "ticket_id": ticket,
            "expected_revision": 1,
        },
    )
    with MCPConnectionManager(url, auth) as manager:
        first = _worker(database, url, auth, manager, tmp_path)
        assert first.run_once()
        waiting = database.get_task(task["id"])
        assert waiting["state"] == "awaiting_approval" and waiting["lease_owner"] is None
        with database.connect() as conn:
            operation = conn.execute("SELECT * FROM business.operations").fetchone()
            assert operation["state"] == "awaiting_approval"
            assert (
                conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()[
                    "n"
                ]
                == 0
            )
            approval = conn.execute("SELECT * FROM app.approvals FOR UPDATE").fetchone()
            BusinessControl(database).reply_employee(
                conn,
                waiting,
                approval,
                ReplyInput(expected_digest=approval["digest"], decision="approve"),
                browser,
                "employee-reply",
            )
        replacement = _worker(database, url, auth, manager, tmp_path)
        assert replacement.run_once()
        assert database.get_task(task["id"])["state"] == "succeeded"
        with database.connect() as conn:
            completed = conn.execute("SELECT * FROM business.operations").fetchone()
            assert completed["id"] == operation["id"] and completed["state"] == "succeeded"
            assert (
                conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"]
                == 1
            )
            assert (
                conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()[
                    "n"
                ]
                == 1
            )


def test_task_reauthentication_requires_same_employee_and_database_freezes_identity(
    service, tmp_path
):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    task = _task(
        database,
        actor,
        issued["credential_ref"],
        "create",
        {
            "title": "Needs original employee",
            "category_id": category,
        },
    )
    auth.revoke_mcp(browser, issued["credential_ref"])
    with MCPConnectionManager(url, auth) as manager:
        worker = _worker(database, url, auth, manager, tmp_path)
        assert worker.run_once()
        waiting = database.get_task(task["id"])
        assert waiting["state"] == "awaiting_input"
        other = auth.authenticate_mcp_token(tokens["employee-facilities"][0])
        other_browser = _browser(database, other)
        with pytest.raises(AccessError):
            with database.connect() as conn:
                BusinessControl(database).reauthenticate(
                    conn, waiting, other_browser, other.credential_ref
                )
        with pytest.raises(psycopg.IntegrityError):
            with database.connect() as conn:
                conn.execute(
                    "UPDATE app.tasks SET employee_id=%s WHERE id=%s",
                    (other.employee_id, task["id"]),
                )
        renewed = auth.issue_mcp(browser)
        with database.connect() as conn:
            BusinessControl(database).reauthenticate(
                conn, waiting, browser, renewed["credential_ref"]
            )
            conn.execute("UPDATE app.tasks SET state='queued' WHERE id=%s", (task["id"],))
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "succeeded"


def _reply(database, task_id, browser, decision, **fields):
    with database.connect() as conn:
        task = conn.execute("SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (task_id,)).fetchone()
        approval = conn.execute(
            "SELECT * FROM app.approvals WHERE task_id=%s AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (task_id,),
        ).fetchone()
        return BusinessControl(database).reply_employee(
            conn,
            task,
            approval,
            ReplyInput(expected_digest=approval["digest"], decision=decision, **fields),
            browser,
            "reply_" + uuid4().hex,
        )


def test_worker_duplicate_name_waits_for_fact_then_uses_exact_directory_person(service, tmp_path):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["supervisor"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    requester = auth.authenticate_mcp_token(tokens["employee-it"][0])
    ticket = str(uuid4())
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,requester_id,employee_id) "
            "VALUES(%s,'Ambiguous assignee',%s,%s,%s)",
            (ticket, category, requester.person_id, requester.employee_id),
        )
    scope = PluginRegistry(database).list()[0]
    task = database.create_business_task(
        scope["id"],
        {
            "instruction": "分配给Zhang Wei，工单 " + ticket,
            "strategy": "rule",
            "plugin_digest": scope["manifest_digest"],
        },
        employee_id=actor.employee_id,
        credential_ref=issued["credential_ref"],
    )
    with MCPConnectionManager(url, auth) as manager:
        worker = _worker(database, url, auth, manager, tmp_path)
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "awaiting_input"
        with database.connect() as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM business.operations").fetchone()["n"] == 0
            )
        _reply(
            database, task["id"], browser, "confirm", confirmation="分配给 IT Support 的Zhang Wei"
        )
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "succeeded", database.events(task["id"])[
            -1
        ]["payload"]
    with database.connect() as conn:
        assigned = conn.execute(
            "SELECT assignee_id FROM business.tickets WHERE id=%s", (ticket,)
        ).fetchone()
        target = conn.execute(
            "SELECT id FROM business.people WHERE name='Zhang Wei' AND team='IT Support'"
        ).fetchone()
        assert assigned["assignee_id"] == target["id"]


def test_worker_edit_replans_new_operation_and_requires_new_specific_approval(service, tmp_path):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["handler-it"][0])
    browser = _browser(database, actor)
    issued = auth.issue_mcp(browser)
    requester = auth.authenticate_mcp_token(tokens["employee-it"][0])
    ticket = str(uuid4())
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO business.tickets(id,title,category_id,requester_id,employee_id,"
            "assignee_id,status) VALUES(%s,'resolution',%s,%s,%s,%s,'in_progress')",
            (ticket, category, requester.person_id, requester.employee_id, actor.person_id),
        )
    params = {"ticket_id": ticket, "expected_revision": 1, "resolution": "Original employee note"}
    task = _task(database, actor, issued["credential_ref"], "resolve", params)
    with MCPConnectionManager(url, auth) as manager:
        worker = _worker(database, url, auth, manager, tmp_path)
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "awaiting_approval"
        _reply(
            database,
            task["id"],
            browser,
            "edit",
            edited_params={
                **params,
                "resolution": "Corrected employee note",
            },
        )
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "awaiting_approval"
        with database.connect() as conn:
            assert (
                conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()[
                    "n"
                ]
                == 0
            )
            states = conn.execute(
                "SELECT state FROM business.operations ORDER BY created_at"
            ).fetchall()
            assert [row["state"] for row in states] == ["rejected", "awaiting_approval"]
        _reply(database, task["id"], browser, "approve")
        assert worker.run_once()
        assert database.get_task(task["id"])["state"] == "succeeded"
    with database.connect() as conn:
        saved = conn.execute(
            "SELECT resolution,revision FROM business.tickets WHERE id=%s", (ticket,)
        ).fetchone()
        assert saved == {"resolution": "Corrected employee note", "revision": 2}


def test_worker_lease_takeover_after_committed_action_replays_checkpoint_without_rewrite(
    service, tmp_path, monkeypatch
):
    database, url, auth, tokens, category = service
    actor = auth.authenticate_mcp_token(tokens["employee-it"][0])
    issued = auth.issue_mcp(_browser(database, actor))
    task = _task(
        database,
        actor,
        issued["credential_ref"],
        "create",
        {
            "title": "Committed before worker exit",
            "category_id": category,
        },
    )

    class WorkerExit(BaseException):
        pass

    original = BusinessExecutor.execute

    def crashed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        assert result["status"] == "succeeded"
        raise WorkerExit()

    with MCPConnectionManager(url, auth) as manager:
        first = _worker(database, url, auth, manager, tmp_path)
        with monkeypatch.context() as change:
            change.setattr(BusinessExecutor, "execute", crashed)
            with pytest.raises(WorkerExit):
                first.run_once()
        with database.connect() as conn:
            conn.execute(
                "UPDATE app.tasks SET lease_until=now()-interval '1 second' WHERE id=%s",
                (task["id"],),
            )
        replacement = _worker(database, url, auth, manager, tmp_path)
        assert replacement.run_once()
        completed = database.get_task(task["id"])
        assert completed["state"] == "succeeded" and completed["lease_generation"] == 2
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.tickets").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM business.ticket_history").fetchone()["n"] == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM business.operation_receipts").fetchone()["n"]
            == 1
        )

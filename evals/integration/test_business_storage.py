"""Business and repair records share durable infrastructure without invented repositories."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from decagent.config import Settings
from decagent.runtime.worker import Worker
from decagent.storage.backup import export_backup, restore_backup
from decagent.storage.database import Database
from decagent.storage.evidence import EvidenceStore

pytestmark = pytest.mark.integration


def scope(database):
    with database.connect() as conn:
        return conn.execute(
            "INSERT INTO app.business_scopes"
            "(id,plugin_id,plugin_version,manifest_digest,manifest) "
            "VALUES(%s,%s,%s,%s,%s) RETURNING *",
            (uuid4(), "storage-fixture", "1", "a" * 64, Jsonb({"id": "storage-fixture"})),
        ).fetchone()


def test_business_task_does_not_need_repository_and_events_remain_linked(database):
    registered = scope(database)
    task = database.create_business_task(
        registered["id"], {"instruction": "创建工单", "plugin_digest": "a" * 64}
    )
    assert task["kind"] == "business"
    assert task["project_id"] is None and task["base_sha"] is None
    event = database.event(task["id"], "business.created", {"scope": str(registered["id"])})
    assert event["project_id"] is None
    assert database.events(task["id"])[0]["kind"] == "business.created"
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database.connect() as conn:
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
            "VALUES(%s,NULL,1,'bad','{}')",
            (uuid4(),),
        )


def test_legacy_constraints_and_worker_are_preserved(database):
    registered = scope(database)
    business = database.create_business_task(
        registered["id"], {"instruction": "查询", "plugin_digest": "a" * 64}
    )
    project = database.add_project("fixture/compatibility")
    repair = database.create_task(project["id"], "b" * 40, {})
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
            "VALUES(%s,NULL,1,'bad','{}')",
            (repair["id"],),
        )
    assert database.event(repair["id"], "repair.created", {})["project_id"] == project["id"]
    worker = Worker(database, Settings(database_url=database.url))
    assert worker.claim()["id"] == repair["id"]
    assert database.get_task(business["id"])["state"] == "queued"
    database.check_version("1")
    database.check_version("business-2", task_kind="business")
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute(
            "INSERT INTO app.tasks(id,kind,project_id,base_sha,authorization_snapshot) "
            "VALUES(%s,'repository_repair',NULL,NULL,'{}')",
            (uuid4(),),
        )


def test_registration_change_blocks_new_task_and_migration_is_repeatable(database):
    registered = scope(database)
    with pytest.raises(ValueError, match="contract"):
        database.create_business_task(
            registered["id"], {"instruction": "查询", "plugin_digest": "b" * 64}
        )
    with database.connect() as conn:
        conn.execute(
            "UPDATE app.business_scopes SET enabled=false WHERE id=%s", (registered["id"],)
        )
    with pytest.raises(ValueError, match="disabled"):
        database.create_business_task(
            registered["id"], {"instruction": "查询", "plugin_digest": "a" * 64}
        )
    database.migrate()


def test_backup_restores_business_scopes_tasks_and_events(database, tmp_path):
    registered = scope(database)
    task = database.create_business_task(
        registered["id"], {"instruction": "创建工单", "plugin_digest": "a" * 64}
    )
    database.event(task["id"], "business.created", {})
    export_backup(database, EvidenceStore(tmp_path / "source-evidence"), tmp_path / "backup")
    name = "business_restore_" + uuid4().hex
    with database.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    restored = Database(database.url.rsplit("/", 1)[0] + "/" + name)
    restore_backup(restored, tmp_path / "backup", tmp_path / "restored-evidence")
    assert restored.get_task(task["id"])["business_scope_id"] == registered["id"]
    assert restored.events(task["id"])[0]["kind"] == "business.created"

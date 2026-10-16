"""Append-only plan storage migrates existing business data without retagging tasks."""

import pytest

from evals.integration.conditional_support import mixed_business_tasks as mixed_business_tasks

pytestmark = pytest.mark.integration


def test_plan_tables_exist_and_migration_preserves_existing_evidence(
    database, mixed_business_tasks
):
    sample = mixed_business_tasks
    before = database.get_task(sample["tasks"]["business-2"]["id"])
    with database.connect() as conn:
        previous = conn.execute(
            "SELECT version,checksum FROM app.schema_migrations WHERE version<=11 ORDER BY version"
        ).fetchall()
        for table in ("business_plan_revisions", "business_plan_nodes", "business_plan_heads"):
            assert conn.execute("SELECT to_regclass(%s) AS name", ("app." + table,)).fetchone()[
                "name"
            ]
    database.migrate()
    after = database.get_task(before["id"])
    assert after["graph_version"] == before["graph_version"]
    assert after["employee_id"] == before["employee_id"]
    with database.connect() as conn:
        assert (
            conn.execute(
                "SELECT version,checksum FROM app.schema_migrations "
                "WHERE version<=11 ORDER BY version"
            ).fetchall()
            == previous
        )
        assert conn.execute(
            "SELECT status,server_operation_id FROM app.actions WHERE id=%s",
            (sample["action"]["id"],),
        ).fetchone() == {
            "status": "unknown",
            "server_operation_id": sample["operation"]["operation_id"],
        }


def test_existing_schema_11_tasks_really_upgrade_without_changed_ids(database, monkeypatch):
    from uuid import uuid4

    from psycopg import sql

    from decagent.storage import database as module
    from evals.integration.conditional_support import create_mixed_business_tasks

    name = "case_v11_" + uuid4().hex
    with database.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    legacy = module.Database(database.url.rsplit("/", 1)[0] + "/" + name)
    with monkeypatch.context() as old:
        old.setattr(module, "MIGRATIONS", module.MIGRATIONS[:11])
        old.setattr(module, "SCHEMA_VERSION", 11)
        legacy.migrate()
        sample = create_mixed_business_tasks(legacy)
        assert legacy.get_task(sample["tasks"]["business-2"]["id"])["graph_version"] == "business-2"
    legacy.migrate()
    with legacy.connect() as conn:
        assert (
            conn.execute("SELECT max(version) AS n FROM app.schema_migrations").fetchone()["n"]
            >= 12
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM app.business_plan_revisions").fetchone()["n"]
            == 0
        )
        saved = conn.execute(
            "SELECT status,server_operation_id FROM app.actions WHERE id=%s",
            (sample["action"]["id"],),
        ).fetchone()
        assert saved["status"] == "unknown"
        assert saved["server_operation_id"] == sample["operation"]["operation_id"]
    assert legacy.get_task(sample["tasks"]["business-3"]["id"])["graph_version"] == "business-3"


def test_published_revision_cannot_be_overwritten(database, mixed_business_tasks):
    import psycopg
    from psycopg.types.json import Jsonb

    from decagent.business.plans import Plan
    from evals.deterministic.test_business_plan_compiler import read_plan

    plan = Plan.from_mapping(read_plan())
    task_id = mixed_business_tasks["tasks"]["business-3"]["id"]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.business_plan_revisions(task_id,revision,plan_id,schema_version,"
            "content_digest,registry_fingerprint,definition) VALUES(%s,1,%s,'1',%s,%s,%s)",
            (
                task_id,
                plan.plan_id,
                plan.digest,
                plan.registry_fingerprint,
                Jsonb(plan.to_mapping()),
            ),
        )
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute(
            "UPDATE app.business_plan_revisions SET definition='{}' WHERE task_id=%s", (task_id,)
        )


def test_revision_parent_digest_is_required(database, mixed_business_tasks):
    import psycopg
    from psycopg.types.json import Jsonb

    from decagent.business.plans import Plan
    from evals.deterministic.test_business_plan_compiler import read_plan

    plan = Plan.from_mapping(read_plan())
    task_id = mixed_business_tasks["tasks"]["business-3"]["id"]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO app.business_plan_revisions(task_id,revision,plan_id,schema_version,"
            "content_digest,registry_fingerprint,definition) VALUES(%s,1,%s,'1',%s,%s,%s)",
            (
                task_id,
                plan.plan_id,
                plan.digest,
                plan.registry_fingerprint,
                Jsonb(plan.to_mapping()),
            ),
        )
    with pytest.raises(psycopg.errors.CheckViolation), database.connect() as conn:
        conn.execute(
            "INSERT INTO app.business_plan_revisions(task_id,revision,plan_id,schema_version,"
            "content_digest,registry_fingerprint,parent_revision,parent_digest,definition) "
            "VALUES(%s,2,%s,'1',%s,%s,1,NULL,%s)",
            (task_id, plan.plan_id, "b" * 64, plan.registry_fingerprint, Jsonb(plan.to_mapping())),
        )

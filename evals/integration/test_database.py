import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    from uuid import uuid4

    from psycopg import sql

    from decagent.storage.database import Database

    url = os.getenv("DECAGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set DECAGENT_TEST_DATABASE_URL to a disposable dedicated database")
    administrator = Database(url)
    name = "case_" + uuid4().hex
    with administrator.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    db = Database(url.rsplit("/", 1)[0] + "/" + name)
    db.migrate()
    return db


def test_migrations_are_repeatable_and_preserve_data(database):
    project = database.add_project("fixture/repeatable")
    database.migrate()
    assert database.get_project(project["id"])["repository"] == "fixture/repeatable"
    with database.connect() as conn:
        from decagent.storage.database import SCHEMA_VERSION

        assert (
            conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"]
            == SCHEMA_VERSION
        )
        assert conn.execute("SELECT to_regclass('checkpoint.checkpoints') AS t").fetchone()["t"]


def test_cross_project_constraints_and_logical_duplicate(database):
    import psycopg

    project = database.add_project("fixture/constraints")
    other = database.add_project("fixture/other")
    task = database.create_task(project["id"], "a" * 40, {"issue": "bug"})
    first = database.plan_action(task["id"], "step:1", "digest", {"tool": "read"})
    assert database.plan_action(task["id"], "step:1", "digest", {"tool": "read"}) == first
    with pytest.raises(ValueError, match="different"):
        database.plan_action(task["id"], "step:1", "other", {"tool": "write"})
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database.connect() as conn:
        conn.execute(
            "INSERT INTO app.events(task_id, project_id, seq, kind, payload) VALUES(%s,%s,1,%s,%s)",
            (task["id"], other["id"], "wrong", "{}"),
        )


def test_only_one_worker_claims_and_stale_generation_cannot_act(database):
    project = database.add_project("fixture/lease")
    task = database.create_task(project["id"], "a" * 40, {"issue": "bug"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda owner: database.claim(task["id"], owner), ["one", "two"]))
    claimed = [c for c in claims if c]
    assert len(claimed) == 1
    with pytest.raises(ValueError, match="lease"):
        database.assert_lease(task["id"], "impostor", claimed[0]["lease_generation"])


def test_events_are_monotonic_and_approval_is_once_only(database):
    project = database.add_project("fixture/events")
    task = database.create_task(project["id"], "a" * 40, {"issue": "bug"})
    action = database.plan_action(task["id"], "step:1", "digest", {"tool": "read"})
    approval = database.ensure_approval(task["id"], action["id"], "digest", "action")
    assert database.ensure_approval(task["id"], action["id"], "digest", "action") == approval
    with pytest.raises(ValueError, match="stale"):
        database.reply_approval(approval["id"], "wrong", "approve")
    database.reply_approval(approval["id"], "digest", "reject")
    with pytest.raises(ValueError, match="replied"):
        database.reply_approval(approval["id"], "digest", "approve")
    with ThreadPoolExecutor(max_workers=4) as pool:
        seqs = list(
            pool.map(lambda n: database.event(task["id"], "test", {"n": n})["seq"], range(8))
        )
    assert sorted(seqs) == list(range(1, 9))


def test_backup_restores_checkpoint_approvals_and_evidence(database, tmp_path):
    from uuid import uuid4

    from langgraph.checkpoint.base import empty_checkpoint
    from psycopg import sql

    from decagent.storage.backup import export_backup, restore_backup
    from decagent.storage.database import Database
    from decagent.storage.evidence import EvidenceStore

    store = EvidenceStore(tmp_path / "evidence")
    ref = store.write("diagnostic token=SECRET", "fixture")
    database.register_evidence(ref)
    project = database.add_project("fixture/backup")
    task = database.create_task(project["id"], "a" * 40, {"issue": "bug"})
    action = database.plan_action(task["id"], "step:1", "backup-digest", {"tool": "read"})
    approval = database.ensure_approval(task["id"], action["id"], "backup-digest", "action")
    with database.checkpointer() as saver:
        config = {"configurable": {"thread_id": str(task["id"]), "checkpoint_ns": ""}}
        saved = saver.put(config, empty_checkpoint(), {"source": "input", "step": 0}, {})
    # New disposable database in this test-owned PostgreSQL container; no DROP or reset.
    database_name = "restore_" + uuid4().hex
    with database.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    url = database.url.rsplit("/", 1)[0] + "/" + database_name
    restored = Database(url)
    backup = tmp_path / "backup"
    export_backup(database, store, backup)
    restore_backup(restored, backup, tmp_path / "restored-evidence")
    assert restored.get_task(task["id"])["authorization_snapshot"] == {"issue": "bug"}
    with restored.connect() as conn:
        assert (
            conn.execute(
                "SELECT digest FROM app.approvals WHERE id=%s", (approval["id"],)
            ).fetchone()["digest"]
            == "backup-digest"
        )
    with restored.checkpointer() as saver:
        assert saver.get_tuple(saved).checkpoint["id"]
    assert EvidenceStore(tmp_path / "restored-evidence").read(ref) == "diagnostic [REDACTED]"
    with pytest.raises(ValueError, match="new"):
        restore_backup(restored, backup, tmp_path / "restored-evidence")


def test_usage_survives_restart_and_retries_are_separate(database):
    from decimal import Decimal

    from decagent.storage.budget import BudgetExceeded
    from decagent.storage.usage import UsageLedger

    project = database.add_project("fixture/budget")
    task = database.create_task(
        project["id"], "a" * 40, {}, {"max_calls": 3, "max_tokens": 100, "max_cost": "1"}
    )
    first = UsageLedger(database, task["id"]).reserve("request", 0, "jev", 10, Decimal("0.6"))
    UsageLedger(database, task["id"]).settle(first, None, None)
    with pytest.raises(BudgetExceeded, match="Cost"):
        UsageLedger(database, task["id"]).reserve("request", 1, "jev", 10, Decimal("0.6"))
    UsageLedger(database, task["id"]).settle(first, 4, Decimal("0.1"))
    second = UsageLedger(database, task["id"]).reserve("request", 1, "jev", 10, Decimal("0.6"))
    assert first != second
    assert (
        UsageLedger(database, task["id"]).reserve("request", 1, "jev", 10, Decimal("0.6")) == second
    )


def test_mismatched_schema_and_graph_are_diagnosed_without_reset(database):
    project = database.add_project("fixture/graph-version")
    task = database.create_task(project["id"], "a" * 40, {}, graph_version="future")
    with pytest.raises(ValueError, match="graph version"):
        database.check_version()
    with database.connect() as conn:
        conn.execute("UPDATE app.tasks SET state='cancelled' WHERE id=%s", (task["id"],))
    database.check_version()


def test_time_waits_and_unknown_price_are_not_silently_free(database):
    from decagent.storage.budget import BudgetExceeded
    from decagent.storage.usage import UsageLedger

    project = database.add_project("fixture/time")
    task = database.create_task(project["id"], "a" * 40, {}, {"max_active_seconds": 3})
    ledger = UsageLedger(database, task["id"])
    assert ledger.charge_active(5) is None  # Queued/human wait time is excluded.
    database.claim(task["id"], "time-worker")
    assert ledger.charge_active(3) == 3
    with pytest.raises(BudgetExceeded, match="price"):
        ledger.reserve("unpriced", 0, "main", 1, None)
    from decimal import Decimal

    with pytest.raises(BudgetExceeded, match="time"):
        ledger.reserve("timed-out", 0, "main", 1, Decimal("0.01"))


def test_documented_backup_cli_in_new_instance(database, tmp_path):
    import subprocess
    import sys
    from uuid import uuid4

    from psycopg import sql

    from decagent.storage.evidence import EvidenceStore

    evidence = EvidenceStore(tmp_path / "source-data" / "evidence")
    ref = evidence.write("diagnostic token=SECRET", "fixture")
    database.register_evidence(ref)
    backup_path = tmp_path / "cli-backup"
    environment = dict(
        os.environ,
        DECAGENT_DATABASE_URL=database.url,
        DECAGENT_DATA_DIR=str(tmp_path / "source-data"),
    )
    subprocess.run(
        [sys.executable, "-m", "decagent", "backup", str(backup_path)],
        env=environment,
        check=True,
        capture_output=True,
    )
    new_name = "cli_restore_" + uuid4().hex
    with database.connect(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(new_name)))
    environment["DECAGENT_DATABASE_URL"] = database.url.rsplit("/", 1)[0] + "/" + new_name
    environment["DECAGENT_DATA_DIR"] = str(tmp_path / "target-data")
    subprocess.run(
        [sys.executable, "-m", "decagent", "restore", str(backup_path)],
        env=environment,
        check=True,
        capture_output=True,
    )
    assert EvidenceStore(tmp_path / "target-data" / "evidence").read(ref)


def test_corrupt_backup_manifest_is_rejected_before_writes(database, tmp_path):
    import json

    from decagent.storage.backup import export_backup, restore_backup
    from decagent.storage.evidence import EvidenceStore

    evidence = EvidenceStore(tmp_path / "evidence")
    database.register_evidence(evidence.write("evidence", "fixture"))
    backup = tmp_path / "backup"
    export_backup(database, evidence, backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest differs"):
        restore_backup(database, backup, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_partial_durable_usage_still_counts_known_actuals(database):
    from decimal import Decimal

    from decagent.storage.budget import BudgetExceeded
    from decagent.storage.usage import UsageLedger

    project = database.add_project("fixture/partial-usage")
    task = database.create_task(
        project["id"], "a" * 40, {}, {"max_calls": 3, "max_tokens": 50, "max_cost": "1"}
    )
    ledger = UsageLedger(database, task["id"])
    receipt = ledger.reserve("partial", 0, "jev", 10, Decimal("0.1"))
    ledger.settle(receipt, tokens=50, cost=None)
    with pytest.raises(BudgetExceeded, match="Token"):
        UsageLedger(database, task["id"]).reserve("next", 0, "jev", 40, Decimal("0.1"))

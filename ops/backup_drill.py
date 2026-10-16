"""Restore the authorized live-test snapshot into a new disposable database, without keys."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def control(source_run, run_id):
    import psycopg
    from psycopg import sql

    from decagent.storage.backup import export_backup, restore_backup
    from decagent.storage.database import Database
    from decagent.storage.evidence import EvidenceStore

    for value in (source_run, run_id):
        if str(UUID(value)) != value:
            raise ValueError("Canonical drill identities required")
    original = Path("/tmp") / ("decagent-live-repository-" + source_run)
    source_receipt = json.loads((original / "control-receipt.json").read_text(encoding="utf-8"))
    if source_receipt.get("status") != "verified":
        raise ValueError("Only a completed authorized live-test snapshot can be restored")
    root_url = os.environ["DECAGENT_TEST_DATABASE_URL"].rsplit("/", 1)[0]
    if "@decagent-executor-pg" not in root_url:
        raise ValueError("Only dedicated test-owned PostgreSQL may be used")
    source = Database(root_url + "/" + source_receipt["database"])
    original_task = source.get_task(source_receipt["task_id"])
    source.migrate()
    assert source.get_task(source_receipt["task_id"]) == original_task
    destination_name = "decagent_backup_drill_" + run_id.replace("-", "")
    destination = Database(root_url + "/" + destination_name)
    directory = Path("/tmp") / ("decagent-backup-drill-" + run_id)
    directory.mkdir(mode=0o700, exist_ok=True)
    saved = directory / "receipt.json"
    if saved.exists():
        print(saved.read_text(encoding="utf-8"))
        return
    backup_path = directory / "snapshot"
    if backup_path.exists():
        manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
    else:
        manifest = export_backup(
            source, EvidenceStore(original / "application/evidence"), backup_path
        )
    with psycopg.connect(root_url + "/postgres", autocommit=True) as conn:
        found = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (destination_name,)
        ).fetchone()
        if found:
            raise ValueError(
                "Existing drill database preserved; inspect partial restore before retry"
            )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(destination_name)))
    restored = restore_backup(destination, backup_path, directory / "evidence")
    task_id = source_receipt["task_id"]
    assert restored["counts"] == manifest["counts"]
    assert destination.get_task(task_id) == source.get_task(task_id)
    with destination.connect() as conn:
        checkpoints = conn.execute(
            "SELECT count(*) AS n FROM checkpoint.checkpoints WHERE thread_id=%s", (task_id,)
        ).fetchone()["n"]
    assert checkpoints == source_receipt["checkpoints"] and checkpoints > 0
    assert manifest["counts"]["app.approvals"] > 0
    assert manifest["counts"]["app.actions"] > 0
    assert manifest["counts"]["app.evidence"] > 0
    # A future schema marker exists only in the newly restored test database.
    with destination.connect() as conn:
        conn.execute("INSERT INTO app.schema_migrations VALUES(999,'future-drill')")
    try:
        destination.migrate()
    except ValueError as error:
        assert "newer" in str(error)
    else:
        raise AssertionError("A future schema must block the older application")
    with destination.connect() as conn:
        conn.execute(
            "DELETE FROM app.schema_migrations WHERE version=999 AND checksum='future-drill'"
        )
    destination.check_version()
    receipt = {
        "status": "verified",
        "run_id": run_id,
        "source_run": source_run,
        "database": destination_name,
        "task_id": task_id,
        "snapshot_sha256": manifest["snapshot_sha256"],
        "counts": manifest["counts"],
        "checkpoints": checkpoints,
        "future_schema_blocked": True,
        "original_task_unchanged": True,
        "paid_requests": 0,
    }
    saved.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


def host(root, intent_name="backup-drill-intent.json"):
    source = json.loads(
        (root / ".decagent/live-model-repair-intent.json").read_text(encoding="utf-8")
    )
    if source.get("status") != "verified":
        raise ValueError("A verified live-model test receipt is required")
    if not re.fullmatch(r"backup-drill-[a-z0-9-]{1,48}\.json", intent_name):
        raise ValueError("A bounded backup-drill intent filename is required")
    path = root / ".decagent" / intent_name
    if path.exists():
        intent = json.loads(path.read_text(encoding="utf-8"))
    else:
        intent = {
            "run_id": str(uuid4()),
            "source_run": source["run_id"],
            "status": "intent_recorded",
        }
        path.write_text(json.dumps(intent, indent=2), encoding="utf-8")
    if intent["source_run"] != source["run_id"]:
        raise ValueError("Backup intent belongs to a different source")

    def docker(*args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, encoding="utf-8"
        )

    docker(
        "cp",
        str(root / "ops/backup_drill.py"),
        "decagent-executor-tests:/opt/decagent/ops/backup_drill.py",
    )
    output = docker(
        "exec",
        "decagent-executor-tests",
        "python",
        "/opt/decagent/ops/backup_drill.py",
        "--control",
        "--source-run",
        intent["source_run"],
        "--run-id",
        intent["run_id"],
    ).stdout
    receipt = json.loads(output)
    if not intent.get("image_rollback"):
        # Exercise immutable images against the restored copy, never the source task.
        original_image = docker(
            "image", "inspect", "decagent-control:local", "--format", "{{.Id}}"
        ).stdout.strip()
        base_tag = "decagent-drill-base:" + intent["run_id"]
        incompatible_tag = "decagent-drill-incompatible:" + intent["run_id"]
        docker("image", "tag", original_image, base_tag)
        build_root = root / ".decagent/backup-drill" / intent["run_id"]
        build_root.mkdir(parents=True, exist_ok=True)
        (build_root / "Dockerfile").write_text(
            "FROM " + base_tag + '\nRUN python -c "from pathlib import Path; '
            "p=Path('/opt/decagent/decagent/runtime/graph.py'); "
            "p.write_text(p.read_text().replace('GRAPH_VERSION = \\\"1\\\"', "
            '\'GRAPH_VERSION = \\"future-drill\\"\'))"\n',
            encoding="utf-8",
        )
        docker("build", "-t", incompatible_tag, str(build_root))
        probe = (
            "import os; from decagent.storage.database import Database; "
            "from decagent.runtime.graph import GRAPH_VERSION; "
            "d=Database(os.environ['DECAGENT_TEST_DATABASE_URL']); "
            "d.check_version(GRAPH_VERSION); print('compatible')"
        )
        fixture = json.loads(
            docker("inspect", "decagent-executor-tests", "--format", "{{json .Config.Env}}").stdout
        )
        base_url = next(
            v.split("=", 1)[1] for v in fixture if v.startswith("DECAGENT_TEST_DATABASE_URL=")
        )
        restored_url = base_url.rsplit("/", 1)[0] + "/" + receipt["database"]
        task_id = receipt["task_id"]
        docker(
            "exec",
            "decagent-executor-tests",
            "python",
            "-c",
            "import os,psycopg; u=os.environ['DECAGENT_TEST_DATABASE_URL'].rsplit('/',1)[0]+"
            + repr("/" + receipt["database"])
            + "; "
            "c=psycopg.connect(u); c.execute("
            "\"UPDATE app.tasks SET state='recovering' WHERE id=%s\", "
            + repr((task_id,))
            + "); c.commit()",
        )
        networks = json.loads(
            docker(
                "inspect", "decagent-executor-pg", "--format", "{{json .NetworkSettings.Networks}}"
            ).stdout
        )
        if len(networks) != 1:
            raise ValueError("Dedicated fixture network must be unambiguous")
        network = next(iter(networks))
        arguments = [
            "run",
            "--rm",
            "--network",
            network,
            "--entrypoint",
            "python",
            "--env",
            "DECAGENT_TEST_DATABASE_URL=" + restored_url,
        ]
        incompatible = subprocess.run(
            ["docker", *arguments, incompatible_tag, "-c", probe],
            capture_output=True,
            text=True,
        )
        if incompatible.returncode == 0 or "incompatible graph version" not in incompatible.stderr:
            raise AssertionError("The incompatible image did not block the active restored task")
        rollback = docker(*arguments, original_image, "-c", probe)
        assert rollback.stdout.strip() == "compatible"
        intent["image_rollback"] = {
            "status": "verified",
            "original_image": original_image,
            "incompatible_image": docker(
                "image", "inspect", incompatible_tag, "--format", "{{.Id}}"
            ).stdout.strip(),
            "incompatible_exit": incompatible.returncode,
            "rollback_exit": rollback.returncode,
            "restored_database_only": True,
            "paid_requests": 0,
        }
    intent.update(status="verified", receipt=receipt)
    path.write_text(json.dumps(intent, indent=2), encoding="utf-8")
    print(json.dumps(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", action="store_true")
    parser.add_argument("--source-run")
    parser.add_argument("--run-id")
    parser.add_argument("--intent-name", default="backup-drill-intent.json")
    args = parser.parse_args()
    if args.control:
        control(args.source_run, args.run_id)
    else:
        root = Path(__file__).resolve().parents[1]
        if Path.cwd().resolve() != root:
            raise ValueError("Run from the DecAgent checkout")
        host(root, args.intent_name)


if __name__ == "__main__":
    main()

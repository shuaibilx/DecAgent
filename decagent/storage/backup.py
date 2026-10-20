"""Consistent database snapshot plus immutable evidence; restore only into a new instance."""

import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from decagent.storage.evidence import EvidenceRef, EvidenceStore


def _encode(value):
    if isinstance(value, (bytes, memoryview)):
        return {"type": "bytes", "value": base64.b64encode(value).decode()}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, UUID):
        return {"type": "uuid", "value": str(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, (dict, list)):
        return {"type": "json", "value": value}
    return {"type": "scalar", "value": value}


def _decode(cell):
    value = cell["value"]
    transforms = {
        "bytes": base64.b64decode,
        "datetime": datetime.fromisoformat,
        "uuid": UUID,
        "decimal": Decimal,
        "json": Jsonb,
        "scalar": lambda v: v,
    }
    if cell["type"] not in transforms:
        raise ValueError("Unsupported backup value type")
    return transforms[cell["type"]](value)


def _table_names(conn):
    return [
        (r["table_schema"], r["table_name"])
        for r in conn.execute(
            "SELECT table_schema,table_name FROM information_schema.tables "
            "WHERE table_schema IN ('app','checkpoint','business') AND table_type='BASE TABLE' "
            "ORDER BY table_schema,table_name"
        ).fetchall()
    ]


def export_backup(database, evidence: EvidenceStore, destination: Path):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Backup destination must be new; existing files are preserved")
    snapshot = {"format": 1, "tables": []}
    with database.connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for schema, table in _table_names(conn):
            rows = conn.execute(
                sql.SQL("SELECT * FROM {}.{}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                )
            ).fetchall()
            snapshot["tables"].append(
                {
                    "schema": schema,
                    "table": table,
                    "rows": [{k: _encode(v) for k, v in row.items()} for row in rows],
                }
            )
        refs = conn.execute("SELECT sha256,size,source FROM app.evidence").fetchall()
    # Immutable evidence: copy only files referenced by the consistent database snapshot.
    destination.mkdir(parents=True)
    export_evidence = EvidenceStore(destination / "evidence", redact=lambda text: text)
    for ref in refs:
        reference = EvidenceRef(**ref)
        copied = export_evidence.write(evidence.read(reference), reference.source)
        if copied.sha256 != reference.sha256:
            raise ValueError("Backup evidence hash mismatch")
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    (destination / "snapshot.json").write_bytes(payload)
    manifest = {
        "format": 1,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "evidence": refs,
        "counts": {f"{t['schema']}.{t['table']}": len(t["rows"]) for t in snapshot["tables"]},
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def restore_backup(database, source: Path, evidence_destination: Path):
    source = Path(source)
    target = Path(evidence_destination)
    if target.exists():
        raise ValueError("Restore evidence volume must be new; existing data is preserved")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    payload = (source / "snapshot.json").read_bytes()
    if (
        manifest["format"] != 1
        or hashlib.sha256(payload).hexdigest() != manifest["snapshot_sha256"]
    ):
        raise ValueError("Backup snapshot integrity mismatch")
    snapshot = json.loads(payload)
    if snapshot.get("format") != 1:
        raise ValueError("Unsupported backup snapshot format")
    actual_counts = {f"{t['schema']}.{t['table']}": len(t["rows"]) for t in snapshot["tables"]}
    if actual_counts != manifest["counts"]:
        raise ValueError("Backup manifest row counts differ from snapshot")
    evidence_rows = next(
        t["rows"] for t in snapshot["tables"] if t["schema"] == "app" and t["table"] == "evidence"
    )
    snapshot_refs = [
        {"sha256": r["sha256"]["value"], "size": r["size"]["value"], "source": r["source"]["value"]}
        for r in evidence_rows
    ]
    if sorted(snapshot_refs, key=lambda r: r["sha256"]) != sorted(
        manifest["evidence"], key=lambda r: r["sha256"]
    ):
        raise ValueError("Backup evidence manifest differs from snapshot")
    incoming = EvidenceStore(source / "evidence")
    refs = [EvidenceRef(**r) for r in manifest["evidence"]]
    for ref in refs:
        incoming.read(ref)
    database.migrate()
    with database.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7731401)")
        conn.execute("SET CONSTRAINTS ALL DEFERRED")
        allowed = set(_table_names(conn))
        incoming_names = {(t["schema"], t["table"]) for t in snapshot["tables"]}
        if allowed != incoming_names or len(incoming_names) != len(snapshot["tables"]):
            raise ValueError("Backup schema differs from this application")
        seeded_legacy_workspace = False
        for schema, table in allowed:
            rows = conn.execute(
                sql.SQL("SELECT * FROM {}.{}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                )
            ).fetchall()
            if table in {"schema_migrations", "checkpoint_migrations"}:
                exported = next(
                    t for t in snapshot["tables"] if t["schema"] == schema and t["table"] == table
                )
                actual = [{k: _encode(v) for k, v in row.items()} for row in rows]
                if actual != exported["rows"]:
                    raise ValueError("Backup migration versions are incompatible")
            elif (schema, table) == ("business", "workspaces") and len(rows) == 1:
                row = rows[0]
                if (
                    str(row["id"]) == "00000000-0000-0000-0000-000000000001"
                    and row["slug"] == "legacy"
                    and row["kind"] == "legacy"
                    and row["active"] is True
                ):
                    seeded_legacy_workspace = True
                else:
                    raise ValueError("Restore database must contain only the migration baseline")
            elif rows:
                raise ValueError("Restore database must be empty; existing data is preserved")
        if seeded_legacy_workspace:
            conn.execute(
                "DELETE FROM business.workspaces WHERE id=%s",
                (UUID("00000000-0000-0000-0000-000000000001"),),
            )
        destination = EvidenceStore(target, redact=lambda text: text)
        for ref in refs:
            destination.write(incoming.read(ref), ref.source)
        # Immediate foreign keys still require parents first; preserve all original IDs.
        order = [
            "projects",
            "tasks",
            "evidence",
            "events",
            "actions",
            "action_attempts",
            "approvals",
            "verification_runs",
            "decision_records",
            "usage_records",
            "memory_candidates",
            "memory_versions",
            "deliveries",
            "owner_credentials",
            "owner_sessions",
            "idempotency_keys",
        ]
        tables = sorted(
            snapshot["tables"],
            key=lambda t: (
                order.index(t["table"]) if t["table"] in order else len(order),
                t["table"],
            ),
        )
        for table in tables:
            if table["table"] in {"schema_migrations", "checkpoint_migrations"}:
                continue
            rows = table["rows"]
            if table["table"] == "memory_versions":
                rows = sorted(rows, key=lambda r: r["valid_from"]["value"])
            for row in rows:
                columns = list(row)
                conn.execute(
                    sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
                        sql.Identifier(table["schema"]),
                        sql.Identifier(table["table"]),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    ),
                    [_decode(row[c]) for c in columns],
                )
    return manifest

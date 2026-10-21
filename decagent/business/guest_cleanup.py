"""Fail-closed cleanup for expired or explicitly cleared guest workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import sql

from decagent.business.operations import OperationService

_WORKSPACE_LOCK_SALT = 46127
_TASK_LOCK_SALT = 46126
_OPERATION_LOCK_SALT = 46125


@dataclass(frozen=True)
class GuestCleanupResult:
    workspace_id: UUID
    status: str
    reason: str | None = None


class GuestWorkspaceCleaner:
    """Delete guest data only after leases, workers, and write outcomes are safe."""

    def __init__(self, database):
        self.database = database
        self.operations = OperationService(database)

    def run_once(self, *, limit=10):
        """Expire idle spaces, then clean a bounded number of inactive workspaces."""
        if not 1 <= limit <= 100:
            raise ValueError("Cleanup batch limit must be between 1 and 100")
        with self.database.connect() as conn:
            expired = conn.execute(
                "SELECT w.id FROM business.workspaces w WHERE w.kind='guest' AND w.active "
                "AND NOT EXISTS (SELECT 1 FROM business.guest_sessions s "
                "WHERE s.workspace_id=w.id AND s.expires_at>now() "
                "AND s.last_activity_at>now()-interval '7 days') "
                "ORDER BY w.created_at,w.id FOR UPDATE OF w SKIP LOCKED LIMIT %s",
                (limit,),
            ).fetchall()
            for workspace in expired:
                self._revoke_workspace(conn, workspace["id"])
            candidates = conn.execute(
                "SELECT id FROM business.workspaces WHERE kind='guest' AND NOT active "
                "AND cleanup_completed_at IS NULL ORDER BY created_at,id LIMIT %s",
                (limit,),
            ).fetchall()
        return [self.clean_workspace(row["id"]) for row in candidates]

    @staticmethod
    def _revoke_workspace(conn, workspace_id):
        conn.execute(
            "UPDATE business.workspaces SET active=false WHERE id=%s AND kind='guest'",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE business.guest_sessions SET revoked_at=coalesce(revoked_at,now()) "
            "WHERE workspace_id=%s",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE business.employee_sessions SET revoked_at=coalesce(revoked_at,now()) "
            "WHERE workspace_id=%s",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE business.mcp_credentials SET revoked_at=coalesce(revoked_at,now()) "
            "WHERE workspace_id=%s",
            (workspace_id,),
        )
        conn.execute(
            "UPDATE business.employee_accounts SET active=false WHERE workspace_id=%s",
            (workspace_id,),
        )

    def clean_workspace(self, workspace_id):
        workspace_id = UUID(str(workspace_id))
        with self.database.connect(autocommit=True) as lock_conn:
            if not self._try_lock(lock_conn, workspace_id, _WORKSPACE_LOCK_SALT):
                return GuestCleanupResult(workspace_id, "pending", "cleanup_in_progress")
            workspace = self._workspace(workspace_id)
            if workspace is None:
                return GuestCleanupResult(workspace_id, "not_found")
            if workspace["kind"] != "guest" or workspace["active"]:
                return GuestCleanupResult(workspace_id, "pending", "workspace_active")
            if workspace["cleanup_completed_at"] is not None:
                return GuestCleanupResult(workspace_id, "complete")

            tasks = self._rows(
                "SELECT id FROM app.tasks WHERE workspace_id=%s ORDER BY id", workspace_id
            )
            operations = self._rows(
                "SELECT id FROM business.operations WHERE workspace_id=%s ORDER BY id", workspace_id
            )
            for task in tasks:
                if not self._try_lock(lock_conn, task["id"], _TASK_LOCK_SALT):
                    return GuestCleanupResult(workspace_id, "pending", "worker_in_flight")
            for operation in operations:
                if not self._try_lock(lock_conn, operation["id"], _OPERATION_LOCK_SALT):
                    return GuestCleanupResult(workspace_id, "pending", "operation_in_flight")

            with self.database.connect() as conn:
                workspace = conn.execute(
                    "SELECT id,kind,active,cleanup_completed_at FROM business.workspaces "
                    "WHERE id=%s FOR UPDATE",
                    (workspace_id,),
                ).fetchone()
                if workspace is None:
                    return GuestCleanupResult(workspace_id, "not_found")
                if workspace["cleanup_completed_at"] is not None:
                    return GuestCleanupResult(workspace_id, "complete")
                if workspace["active"] or workspace["kind"] != "guest":
                    return GuestCleanupResult(workspace_id, "pending", "workspace_active")

                current_tasks = conn.execute(
                    "SELECT id,state,lease_until FROM app.tasks WHERE workspace_id=%s ORDER BY id",
                    (workspace_id,),
                ).fetchall()
                if {row["id"] for row in current_tasks} != {row["id"] for row in tasks}:
                    return GuestCleanupResult(workspace_id, "pending", "task_set_changed")
                if any(
                    row["lease_until"] is not None
                    and conn.execute("SELECT %s>clock_timestamp() AS active", (row["lease_until"],))
                    .fetchone()["active"]
                    for row in current_tasks
                ):
                    return GuestCleanupResult(workspace_id, "pending", "lease_active")

                current_operations = conn.execute(
                    "SELECT * FROM business.operations WHERE workspace_id=%s ORDER BY id",
                    (workspace_id,),
                ).fetchall()
                if {row["id"] for row in current_operations} != {
                    row["id"] for row in operations
                }:
                    return GuestCleanupResult(workspace_id, "pending", "operation_set_changed")
                for operation in current_operations:
                    evidence, _ = self.operations._receipt_evidence(conn, operation)
                    if evidence in {"partial_history", "receipt_mismatch"}:
                        return GuestCleanupResult(
                            workspace_id, "pending", "operation_evidence_ambiguous"
                        )
                    if operation["state"] == "succeeded" and evidence != "verified":
                        return GuestCleanupResult(
                            workspace_id, "pending", "successful_operation_unverified"
                        )
                    if operation["state"] in {"running", "unknown"} and evidence not in {
                        "absent",
                        "verified",
                    }:
                        return GuestCleanupResult(
                            workspace_id, "pending", "operation_evidence_ambiguous"
                        )

                task_ids = [str(row["id"]) for row in current_tasks]
                conn.execute(
                    "UPDATE app.tasks SET state='cancelled',phase='finalize',"
                    "lease_owner=NULL,lease_until=NULL,lease_generation=lease_generation+1 "
                    "WHERE workspace_id=%s AND state NOT IN ('succeeded','failed','cancelled')",
                    (workspace_id,),
                )
                self._delete_checkpoints(conn, task_ids)
                self._delete_workspace_rows(conn, workspace_id)
                conn.execute(
                    "UPDATE business.workspaces SET cleanup_completed_at=now(),active=false "
                    "WHERE id=%s",
                    (workspace_id,),
                )
            return GuestCleanupResult(workspace_id, "complete")

    def _workspace(self, workspace_id):
        with self.database.connect() as conn:
            return conn.execute(
                "SELECT id,kind,active,cleanup_completed_at FROM business.workspaces WHERE id=%s",
                (workspace_id,),
            ).fetchone()

    def _rows(self, query, workspace_id):
        with self.database.connect() as conn:
            return conn.execute(query, (workspace_id,)).fetchall()

    @staticmethod
    def _try_lock(conn, key, salt):
        return conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,%s)) AS acquired",
            (str(key), salt),
        ).fetchone()["acquired"]

    @staticmethod
    def _delete_checkpoints(conn, task_ids):
        if not task_ids:
            return
        tables = conn.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema='checkpoint' AND column_name='thread_id' ORDER BY table_name"
        ).fetchall()
        for table in tables:
            conn.execute(
                sql.SQL("DELETE FROM checkpoint.{} WHERE thread_id=ANY(%s::text[])").format(
                    sql.Identifier(table["table_name"])
                ),
                (task_ids,),
            )

    @staticmethod
    def _delete_workspace_rows(conn, workspace_id):
        tables = conn.execute(
            "SELECT table_schema,table_name FROM information_schema.columns "
            "WHERE table_schema IN ('app','business') AND column_name='workspace_id' "
            "AND table_name<>'guest_workspace_tombstones' "
            "ORDER BY table_schema,table_name"
        ).fetchall()
        selected = {(row["table_schema"], row["table_name"]) for row in tables}
        edges = conn.execute(
            "SELECT child_ns.nspname AS child_schema,child.relname AS child_table,"
            "parent_ns.nspname AS parent_schema,parent.relname AS parent_table "
            "FROM pg_constraint fk JOIN pg_class child ON child.oid=fk.conrelid "
            "JOIN pg_namespace child_ns ON child_ns.oid=child.relnamespace "
            "JOIN pg_class parent ON parent.oid=fk.confrelid "
            "JOIN pg_namespace parent_ns ON parent_ns.oid=parent.relnamespace "
            "WHERE fk.contype='f' AND NOT fk.condeferrable "
            "AND child_ns.nspname IN ('app','business') "
            "AND parent_ns.nspname IN ('app','business')"
        ).fetchall()
        children = {table: set() for table in selected}
        indegree = dict.fromkeys(selected, 0)
        for edge in edges:
            child = (edge["child_schema"], edge["child_table"])
            parent = (edge["parent_schema"], edge["parent_table"])
            if child in selected and parent in selected and parent not in children[child]:
                children[child].add(parent)
                indegree[parent] += 1
        ready = sorted(table for table, degree in indegree.items() if degree == 0)
        ordered = []
        while ready:
            table = ready.pop(0)
            ordered.append(table)
            for parent in sorted(children[table]):
                indegree[parent] -= 1
                if indegree[parent] == 0:
                    ready.append(parent)
                    ready.sort()
        ordered.extend(sorted(selected.difference(ordered)))
        conn.execute("SET CONSTRAINTS ALL DEFERRED")
        for schema, table in ordered:
            conn.execute(
                sql.SQL("DELETE FROM {}.{} WHERE workspace_id=%s").format(
                    sql.Identifier(schema), sql.Identifier(table)
                ),
                (workspace_id,),
            )

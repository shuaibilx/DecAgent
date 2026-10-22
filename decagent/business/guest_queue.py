"""Durable guest trial admission and lease-fenced execution queue."""

from uuid import UUID

from decagent.business.workspaces import parse_workspace_id

_QUEUE_LOCK_ID = 7731403
MAX_ACTIVE_PER_WORKSPACE = 1
MAX_ACTIVE_GUESTS = 2
MAX_WAITING_GUEST_TASKS = 20


class GuestQueueFull(RuntimeError):
    """The bounded public guest waiting queue has no remaining place."""


class GuestQueueUnavailable(RuntimeError):
    """The task is not eligible for the guest trial execution queue."""


class GuestExecutionQueue:
    """Coordinates enqueue and worker claim through PostgreSQL transactions."""

    def __init__(self, database, trial_budget):
        self.database = database
        self.trial_budget = trial_budget

    @staticmethod
    def _ids(workspace_id, task_id):
        try:
            workspace = parse_workspace_id(workspace_id)
            task = UUID(str(task_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise GuestQueueUnavailable("Invalid guest queue identity") from exc
        return workspace, task

    def enqueue(self, conn, *, workspace_id, task_id):
        workspace_id, task_id = self._ids(workspace_id, task_id)
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_QUEUE_LOCK_ID,))
        workspace = conn.execute(
            "SELECT kind,active FROM business.workspaces WHERE id=%s FOR SHARE",
            (workspace_id,),
        ).fetchone()
        if not workspace or workspace["kind"] != "guest" or not workspace["active"]:
            raise GuestQueueUnavailable("Active guest workspace required")

        existing = conn.execute(
            "SELECT workspace_id,queue_state,enqueued_at FROM "
            "business.guest_trial_task_admissions WHERE task_id=%s FOR UPDATE",
            (task_id,),
        ).fetchone()
        if existing:
            if existing["workspace_id"] != workspace_id:
                raise GuestQueueUnavailable("Task is bound to another guest workspace")
            return self.status(conn, workspace_id=workspace_id, task_id=task_id)

        waiting = conn.execute(
            "SELECT count(*) AS total FROM business.guest_trial_task_admissions a "
            "JOIN business.workspaces w ON w.id=a.workspace_id AND w.active "
            "WHERE a.queue_state='waiting'",
        ).fetchone()["total"]
        if waiting >= MAX_WAITING_GUEST_TASKS:
            raise GuestQueueFull("Guest execution queue is full; retry after a task starts")

        # The daily task count, admission row, and queue entry share this transaction.
        self.trial_budget.admit_task(
            workspace_id=workspace_id,
            task_id=task_id,
            connection=conn,
        )
        queued = conn.execute(
            "UPDATE business.guest_trial_task_admissions SET queue_state='waiting',"
            "enqueued_at=clock_timestamp(),last_claimed_at=NULL "
            "WHERE workspace_id=%s AND task_id=%s RETURNING task_id",
            (workspace_id, task_id),
        ).fetchone()
        if not queued:
            raise GuestQueueUnavailable("Guest task admission was not persisted")
        return self.status(conn, workspace_id=workspace_id, task_id=task_id)

    @staticmethod
    def status(conn, *, workspace_id, task_id):
        workspace_id, task_id = GuestExecutionQueue._ids(workspace_id, task_id)
        row = conn.execute(
            "SELECT queue_state,enqueued_at FROM business.guest_trial_task_admissions "
            "WHERE workspace_id=%s AND task_id=%s",
            (workspace_id, task_id),
        ).fetchone()
        if not row:
            return None
        state = row["queue_state"]
        position = None
        reason = None
        if state == "waiting":
            position = conn.execute(
                "SELECT count(*) AS position FROM business.guest_trial_task_admissions a "
                "JOIN business.workspaces w ON w.id=a.workspace_id AND w.active "
                "WHERE a.queue_state='waiting' AND "
                "(a.enqueued_at<%s OR (a.enqueued_at=%s AND a.task_id<=%s))",
                (row["enqueued_at"], row["enqueued_at"], task_id),
            ).fetchone()["position"]
            reason = "guest_execution_capacity"
        return {
            "state": state,
            "position": position,
            "reason": reason,
            "active_per_workspace_limit": MAX_ACTIVE_PER_WORKSPACE,
            "active_global_limit": MAX_ACTIVE_GUESTS,
            "waiting_global_limit": MAX_WAITING_GUEST_TASKS,
        }

    def claim(self, owner, *, lease_seconds, guest_enabled=True):
        with self.database.connect() as conn:
            return self.claim_in_transaction(
                conn,
                owner=owner,
                lease_seconds=lease_seconds,
                guest_enabled=guest_enabled,
            )

    @staticmethod
    def claim_in_transaction(conn, *, owner, lease_seconds, guest_enabled=True):
        """Claim the oldest eligible task while serializing guest slot accounting."""
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_QUEUE_LOCK_ID,))
        active_guests = conn.execute(
            "SELECT count(*) AS total FROM app.tasks t "
            "JOIN business.workspaces w ON w.id=t.workspace_id AND w.kind='guest' "
            "WHERE t.kind='business' AND t.state='running' "
            "AND t.lease_until>clock_timestamp()",
        ).fetchone()["total"]
        return conn.execute(
            "WITH candidate AS ("
            "SELECT t.id FROM app.tasks t "
            "JOIN business.workspaces w ON w.id=t.workspace_id AND w.active "
            "LEFT JOIN business.guest_trial_task_admissions a "
            "ON a.task_id=t.id AND a.workspace_id=t.workspace_id "
            "WHERE t.kind='business' AND t.employee_id IS NOT NULL "
            "AND t.credential_ref IS NOT NULL AND NOT t.business_migration_pending "
            "AND (t.state='queued' OR (t.state='running' AND "
            "(t.lease_until IS NULL OR t.lease_until<=clock_timestamp()))) "
            "AND (w.kind<>'guest' OR ("
            " %s AND a.queue_state IN ('waiting','running') AND %s<%s "
            "AND NOT EXISTS (SELECT 1 FROM app.tasks active "
            "JOIN business.workspaces active_w ON active_w.id=active.workspace_id "
            "AND active_w.kind='guest' WHERE active.kind='business' "
            "AND active.workspace_id=t.workspace_id AND active.id<>t.id "
            "AND active.state='running' AND active.lease_until>clock_timestamp()))) "
            "ORDER BY CASE WHEN w.kind='guest' THEN a.enqueued_at ELSE t.created_at END,"
            "t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1) "
            "UPDATE app.tasks t SET state='running',lease_owner=%s,"
            "lease_generation=lease_generation+1,"
            "lease_until=clock_timestamp()+%s*interval '1 second' "
            "FROM candidate c WHERE t.id=c.id RETURNING t.*",
            (
                guest_enabled,
                active_guests,
                MAX_ACTIVE_GUESTS,
                owner,
                lease_seconds,
            ),
        ).fetchone()

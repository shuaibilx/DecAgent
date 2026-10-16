"""Small explicit SQL repository. Mutations and worker fencing happen in transactions."""

import hashlib
from contextlib import contextmanager
from importlib.resources import files
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from decagent.policy import Redactor
from decagent.storage.safe_record import exact_safe_record

MIGRATIONS = (
    "schema.sql",
    "002_memory_scope.sql",
    "003_delivery_intents.sql",
    "004_model_diagnostics.sql",
    "005_business_tasks.sql",
    "006_service_tickets.sql",
    "007_business_employee_identity.sql",
    "008_business_operations.sql",
    "009_business_grant_confirmation.sql",
    "010_business_scope_revision.sql",
    "011_business_mcp_tasks.sql",
    "012_business_conditional_plans.sql",
    "013_model_request_budgets.sql",
    "014_decision_policies.sql",
    "015_business_decision_interventions.sql",
    "016_business_memory.sql",
    "017_business_workflows.sql",
)
SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    def __init__(self, url: str, redactor=None):
        if not url:
            raise ValueError("DECAGENT_DATABASE_URL is required")
        self.url = url
        self.redactor = redactor or Redactor()

    def connect(self, **kwargs):
        return psycopg.connect(self.url, row_factory=dict_row, **kwargs)

    def migrate(self):
        sources = [
            files("decagent.storage").joinpath(name).read_text(encoding="utf-8")
            for name in MIGRATIONS
        ]
        checksums = [hashlib.sha256(source.encode()).hexdigest() for source in sources]
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7731401)")
            conn.execute("CREATE SCHEMA IF NOT EXISTS app")
            conn.execute("CREATE SCHEMA IF NOT EXISTS checkpoint")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app.schema_migrations "
                "(version integer PRIMARY KEY, checksum text NOT NULL)"
            )
            rows = conn.execute("SELECT * FROM app.schema_migrations ORDER BY version").fetchall()
            if any(row["version"] > SCHEMA_VERSION for row in rows):
                raise ValueError("Database schema is newer than this application; upgrade required")
            if [row["version"] for row in rows] != list(range(1, len(rows) + 1)):
                raise ValueError("Migration history contains a gap; preserve data and investigate")
            for row in rows:
                if row["checksum"] != checksums[row["version"] - 1]:
                    raise ValueError(
                        "Migration checksum differs; add a new migration to preserve data"
                    )
            for index in range(len(rows), SCHEMA_VERSION):
                conn.execute(sources[index])
                conn.execute(
                    "INSERT INTO app.schema_migrations VALUES(%s,%s)", (index + 1, checksums[index])
                )
        # LangGraph owns its independently versioned tables. setup requires autocommit.
        with self.connect(autocommit=True, options="-c search_path=checkpoint") as conn:
            conn.execute("SELECT pg_advisory_lock(7731402)")
            try:
                PostgresSaver(conn).setup()
            finally:
                conn.execute("SELECT pg_advisory_unlock(7731402)")

    def check_version(self, graph_version="1", *, task_kind="repository_repair"):
        if task_kind not in {"repository_repair", "business"}:
            raise ValueError("Unsupported task kind")
        with self.connect() as conn:
            try:
                versions = conn.execute(
                    "SELECT version FROM app.schema_migrations ORDER BY version"
                ).fetchall()
            except psycopg.errors.UndefinedTable as exc:
                raise ValueError("Database is not initialized; run decagent migrate") from exc
            if [r["version"] for r in versions] != list(range(1, SCHEMA_VERSION + 1)):
                raise ValueError("Incompatible database schema; migration/upgrade required")
            incompatible = conn.execute(
                "SELECT id FROM app.tasks WHERE kind=%s AND graph_version<>%s AND state NOT IN "
                "('succeeded','failed','cancelled') "
                "AND NOT (kind='business' AND business_migration_pending) LIMIT 1",
                (task_kind, graph_version),
            ).fetchone()
            if incompatible:
                raise ValueError(
                    "Active task has incompatible graph version; explicit upgrade needed"
                )

    @contextmanager
    def checkpointer(self):
        with self.connect(autocommit=True, options="-c search_path=checkpoint") as conn:
            yield PostgresSaver(conn)

    def add_project(self, repository, base_branch="main"):
        if repository.count("/") != 1 or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/"
            for c in repository
        ):
            raise ValueError("Repository must be owner/name")
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO app.projects(id,repository,base_branch) VALUES(%s,%s,%s) "
                "ON CONFLICT(repository) DO UPDATE SET repository=EXCLUDED.repository RETURNING *",
                (uuid4(), repository, base_branch),
            ).fetchone()

    def get_project(self, project_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM app.projects WHERE id=%s", (project_id,)).fetchone()
            if not row:
                raise KeyError("Project not found")
            return row

    def create_task(self, project_id, base_sha, authorization, budget=None, graph_version="1"):
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO app.tasks"
                "(id,project_id,base_sha,authorization_snapshot,budget,graph_version) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    uuid4(),
                    project_id,
                    base_sha,
                    Jsonb(self.redactor.sanitize(authorization, surface="evidence")),
                    Jsonb(budget or {}),
                    graph_version,
                ),
            ).fetchone()

    def create_business_task(
        self,
        scope_id,
        authorization,
        budget=None,
        graph_version="business-2",
        *,
        employee_id=None,
        credential_ref=None,
    ):
        instruction = authorization.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 65536:
            raise ValueError("A bounded business instruction is required")
        if (employee_id is None) != (credential_ref is None):
            raise ValueError("Employee and business credential must be bound together")
        task_id = uuid4()
        with self.connect() as conn:
            scope = conn.execute(
                "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (scope_id,)
            ).fetchone()
            if not scope or not scope["enabled"]:
                raise ValueError("Business scope is missing or disabled")
            if authorization.get("plugin_digest") != scope["manifest_digest"]:
                raise ValueError("Business contract has changed")
            snapshot = {
                **authorization,
                "task_id": str(task_id),
                "kind": "business",
                "scope_id": str(scope_id),
                "plugin_id": scope["plugin_id"],
                "plugin_version": scope["plugin_version"],
            }
            return conn.execute(
                "INSERT INTO app.tasks"
                "(id,kind,business_scope_id,authorization_snapshot,budget,graph_version,"
                "employee_id,credential_ref) VALUES(%s,'business',%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    task_id,
                    scope_id,
                    Jsonb(exact_safe_record(snapshot, self.redactor)),
                    Jsonb(budget or {}),
                    graph_version,
                    employee_id,
                    credential_ref,
                ),
            ).fetchone()

    def get_task(self, task_id, project_id=None):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM app.tasks WHERE id=%s", (task_id,)).fetchone()
            if not row or (project_id is not None and str(row["project_id"]) != str(project_id)):
                raise KeyError("Task not found in this project")
            return row

    def event(self, task_id, kind, payload):
        with self.connect() as conn:
            task = conn.execute(
                "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s "
                "RETURNING project_id,event_seq",
                (task_id,),
            ).fetchone()
            if not task:
                raise KeyError("Task not found")
            return conn.execute(
                "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (
                    task_id,
                    task["project_id"],
                    task["event_seq"],
                    kind,
                    Jsonb(self.redactor.sanitize(payload, surface="ui")),
                ),
            ).fetchone()

    def events(self, task_id, after=0):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM app.events WHERE task_id=%s AND seq>%s ORDER BY seq",
                (task_id, after),
            ).fetchall()

    def event_once(self, task_id, key, kind, payload):
        """Append an audit event once across node replay, with an exact content binding."""
        safe = self.redactor.sanitize(payload, surface="ui")
        from decagent.policy.canonical import digest

        input_hash = digest("decagent.event.v1", {"kind": kind, "payload": safe})
        scope = "event:" + str(task_id)
        with self.connect() as conn:
            task = conn.execute(
                "SELECT * FROM app.tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not task:
                raise KeyError("Task not found")
            prior = conn.execute(
                "SELECT * FROM app.idempotency_keys WHERE scope=%s AND key=%s", (scope, key)
            ).fetchone()
            if prior:
                if prior["input_hash"] != input_hash:
                    raise ValueError("Event key reused with different content")
                return conn.execute(
                    "SELECT * FROM app.events WHERE task_id=%s AND seq=%s",
                    (task_id, prior["response"]["seq"]),
                ).fetchone()
            row = conn.execute(
                "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s "
                "RETURNING project_id,event_seq",
                (task_id,),
            ).fetchone()
            event = conn.execute(
                "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (task_id, row["project_id"], row["event_seq"], kind, Jsonb(safe)),
            ).fetchone()
            conn.execute(
                "INSERT INTO app.idempotency_keys(scope,key,input_hash,response) "
                "VALUES(%s,%s,%s,%s)",
                (scope, key, input_hash, Jsonb({"seq": event["seq"]})),
            )
            return event

    def plan_action(self, task_id, logical_key, digest, snapshot):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app.actions(id,task_id,logical_key,digest,snapshot) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(task_id,logical_key) DO NOTHING",
                (
                    uuid4(),
                    task_id,
                    logical_key,
                    digest,
                    Jsonb(exact_safe_record(snapshot, self.redactor)),
                ),
            )
            row = conn.execute(
                "SELECT * FROM app.actions WHERE task_id=%s AND logical_key=%s",
                (task_id, logical_key),
            ).fetchone()
            if row["digest"] != digest:
                raise ValueError("Logical action key already has a different digest")
            return row

    def ensure_approval(self, task_id, action_id, digest, kind):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app.approvals(id,task_id,action_id,digest,kind) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(task_id,digest,kind) DO NOTHING",
                (uuid4(), task_id, action_id, digest, kind),
            )
            return conn.execute(
                "SELECT * FROM app.approvals WHERE task_id=%s AND digest=%s AND kind=%s",
                (task_id, digest, kind),
            ).fetchone()

    def reply_approval(self, approval_id, digest, reply, payload=None):
        if reply not in {"approve", "reject", "edit", "confirm"}:
            raise ValueError("Unsupported approval reply")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s FOR UPDATE", (approval_id,)
            ).fetchone()
            if not row or row["digest"] != digest:
                raise ValueError("Approval is stale")
            if row["status"] != "pending":
                raise ValueError("Approval has already been replied to")
            return conn.execute(
                "UPDATE app.approvals SET status='replied',reply=%s,reply_payload=%s,"
                "version=version+1,replied_at=now() WHERE id=%s RETURNING *",
                (
                    reply,
                    Jsonb(self.redactor.sanitize(payload or {}, surface="evidence")),
                    approval_id,
                ),
            ).fetchone()

    def claim(self, task_id, owner, seconds=30):
        if seconds < 1 or seconds > 120:
            raise ValueError("Invalid lease duration")
        with self.connect() as conn:
            return conn.execute(
                "UPDATE app.tasks SET lease_owner=%s,lease_generation=lease_generation+1,"
                "lease_until=now()+%s*interval '1 second',state='running' WHERE id=%s "
                "AND state IN ('queued','running','recovering') "
                "AND (lease_until IS NULL OR lease_until<now()) RETURNING *",
                (owner, seconds, task_id),
            ).fetchone()

    def assert_lease(self, task_id, owner, generation, conn=None):
        if conn is None:
            with self.connect() as current:
                return self.assert_lease(task_id, owner, generation, current)
        row = conn.execute(
            "SELECT * FROM app.tasks WHERE id=%s AND lease_owner=%s AND lease_generation=%s "
            "AND lease_until>now() AND state='running' FOR UPDATE",
            (task_id, owner, generation),
        ).fetchone()
        if not row:
            raise ValueError("Worker lease is stale or task is no longer running")
        return row

    def heartbeat(self, task_id, owner, generation):
        with self.connect() as conn:
            self.assert_lease(task_id, owner, generation, conn)
            conn.execute(
                "UPDATE app.tasks SET lease_until=now()+interval '30 seconds' WHERE id=%s",
                (task_id,),
            )

    def register_evidence(self, reference):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO app.evidence(sha256,size,source) VALUES(%s,%s,%s) "
                "ON CONFLICT(sha256) DO NOTHING",
                (reference.sha256, reference.size, reference.source),
            )
            row = conn.execute(
                "SELECT * FROM app.evidence WHERE sha256=%s", (reference.sha256,)
            ).fetchone()
            if row["size"] != reference.size:
                raise ValueError("Evidence metadata integrity mismatch")

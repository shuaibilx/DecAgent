"""Serial SQL queue consumer with durable LangGraph resumes and generation fencing."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from langgraph.types import Command
from psycopg.types.json import Jsonb

from .config import RuntimeConfig
from .graph import GRAPH_VERSION, build_graph

WAIT_STATES = {
    "action": "awaiting_approval",
    "code_trust": "awaiting_approval",
    "acceptance": "awaiting_input",
    "clarification": "awaiting_input",
    "budget": "paused_budget",
    "reconciliation": "recovering",
}
PHASES = {"prepare", "reproduce", "repair", "verify", "deliver", "finalize"}


class CheckpointIncompatible(ValueError):
    """Preserve the durable graph until the operator explicitly upgrades it."""


class _LeasePulse:
    """Account only the current invocation; no timer survives a human wait."""

    def __init__(self, worker, task):
        self.worker, self.task = worker, task
        self.stop = threading.Event()
        self.lost = False
        self.last = time.monotonic()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="decagent-heartbeat")

    def _run(self):
        while not self.stop.wait(self.worker.heartbeat_seconds):
            with self.lock:
                now = time.monotonic()
                try:
                    with self.worker.database.connect(
                        connect_timeout=5, options="-c statement_timeout=5000"
                    ) as conn:
                        row = conn.execute(
                            "UPDATE app.tasks SET "
                            "lease_until=clock_timestamp()+%s*interval '1 second',"
                            "active_seconds=active_seconds+%s WHERE id=%s AND lease_owner=%s "
                            "AND lease_generation=%s AND state='running' "
                            "AND lease_until>clock_timestamp() RETURNING id",
                            (
                                self.worker.lease_seconds,
                                now - self.last,
                                self.task["id"],
                                self.worker.owner,
                                self.task["lease_generation"],
                            ),
                        ).fetchone()
                    if row is None:
                        self.lost = True
                        return
                    self.last = now
                except Exception:
                    self.lost = True
                    return

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join(7)
        with self.lock:
            return max(0.0, time.monotonic() - self.last)


class Worker:
    def __init__(
        self,
        database,
        settings,
        runtime_config=None,
        *,
        owner=None,
        services_factory=None,
        graph_factory=None,
        heartbeat_seconds=5,
        lease_seconds=30,
    ):
        if heartbeat_seconds <= 0 or lease_seconds <= heartbeat_seconds:
            raise ValueError("Lease must exceed the positive heartbeat interval")
        self.database, self.settings = database, settings
        self.runtime_config = runtime_config or RuntimeConfig.from_env()
        self.owner = owner or "worker-" + uuid4().hex
        self.services_factory = services_factory
        self.graph_factory = graph_factory or build_graph
        self.heartbeat_seconds, self.lease_seconds = heartbeat_seconds, lease_seconds

    def claim(self):
        self.database.check_version(GRAPH_VERSION)
        with self.database.connect() as conn:
            return conn.execute(
                "WITH candidate AS (SELECT id FROM app.tasks WHERE graph_version=%s "
                "AND kind='repository_repair' "
                "AND (state='queued' OR (state='running' AND "
                "(lease_until IS NULL OR lease_until<=clock_timestamp()))) "
                "ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE app.tasks t SET state='running',lease_owner=%s,"
                "lease_generation=lease_generation+1,"
                "lease_until=clock_timestamp()+%s*interval '1 second' "
                "FROM candidate c WHERE t.id=c.id RETURNING t.*",
                (GRAPH_VERSION, self.owner, self.lease_seconds),
            ).fetchone()

    def _transition(self, task, pulse, state, phase, kind, payload):
        elapsed = pulse.finish()
        if pulse.lost:
            return False
        payload = self.database.redactor.sanitize(payload, surface="ui")
        with self.database.connect() as conn:
            row = conn.execute(
                "UPDATE app.tasks SET state=%s,phase=%s,lease_owner=NULL,lease_until=NULL,"
                "active_seconds=active_seconds+%s,event_seq=event_seq+1 WHERE id=%s "
                "AND state='running' AND lease_owner=%s AND lease_generation=%s "
                "AND lease_until>clock_timestamp() RETURNING project_id,event_seq",
                (state, phase, elapsed, task["id"], self.owner, task["lease_generation"]),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
                "VALUES(%s,%s,%s,%s,%s)",
                (task["id"], row["project_id"], row["event_seq"], kind, Jsonb(payload)),
            )
        return True

    @staticmethod
    def _interrupts(snapshot):
        return tuple(item for task in snapshot.tasks for item in task.interrupts)

    def _input(self, graph, config, task):
        snapshot = graph.get_state(config)
        if snapshot.values:
            saved = snapshot.values
            if saved.get("task_id") != str(task["id"]):
                raise ValueError("Checkpoint belongs to another task")
            if saved.get("graph_version", GRAPH_VERSION) != GRAPH_VERSION:
                raise CheckpointIncompatible(
                    "Checkpoint graph version is incompatible; upgrade required"
                )
            pending = self._interrupts(snapshot)
            if pending:
                resumes = {}
                for item in pending:
                    payload = item.value if isinstance(item.value, dict) else {}
                    approval_id = payload.get("approval_id")
                    if approval_id:
                        with self.database.connect() as conn:
                            reply = conn.execute(
                                "SELECT id,status,reply_payload FROM app.approvals "
                                "WHERE id=%s AND task_id=%s",
                                (approval_id, task["id"]),
                            ).fetchone()
                        if (
                            not reply
                            or reply["status"] not in {"replied", "consumed"}
                            or not (reply["reply_payload"] or {}).get("policy_record")
                        ):
                            return None
                        resumes[item.id] = {"approval_id": str(reply["id"])}
                    elif payload.get("kind") in {"budget", "reconciliation"}:
                        with self.database.connect() as conn:
                            request = conn.execute(
                                "SELECT payload FROM app.events WHERE task_id=%s "
                                "AND kind='task.resume_requested' ORDER BY seq DESC LIMIT 1",
                                (task["id"],),
                            ).fetchone()
                        if not request:
                            return None
                        resumes[item.id] = {"kind": payload["kind"], **request["payload"]}
                    else:
                        return None
                return Command(resume=resumes)
            # Continue an unfinished checkpoint; never inject fresh initial input on restart.
            return None
        return {
            "task_id": str(task["id"]),
            "graph_version": GRAPH_VERSION,
            "phase": "prepare",
            "messages": [{"role": "user", "content": task["authorization_snapshot"]["issue"]}],
            "iterations": 0,
            "queue": [],
        }

    def run_once(self):
        task = self.claim()
        if task is None:
            return False
        pulse = _LeasePulse(self, task)
        pulse.start()
        services = None
        try:
            factory = self.services_factory
            if factory is None:
                from .services import RuntimeServices

                factory = RuntimeServices
            services = factory(
                self.database,
                self.settings,
                self.runtime_config,
                task,
                self.owner,
                task["lease_generation"],
            )
            with self.database.checkpointer() as saver:
                graph = self.graph_factory(services, saver)
                config = {"configurable": {"thread_id": str(task["id"])}, "recursion_limit": 1000}
                argument = self._input(graph, config, task)
                snapshot = graph.get_state(config)
                result = (
                    snapshot.values
                    if snapshot.values and not snapshot.next
                    else graph.invoke(argument, config)
                )
                pending = result.get("__interrupt__", ())
                if not pending:
                    pending = self._interrupts(graph.get_state(config))
                phase = result.get("phase", task["phase"])
                phase = phase if phase in PHASES else task["phase"]
                if pending:
                    value = pending[0].value
                    payload = (
                        dict(value) if isinstance(value, dict) else {"reason": "Input required"}
                    )
                    state = WAIT_STATES.get(payload.get("kind"), "awaiting_input")
                    payload.setdefault("reason", "Owner response is required before continuing")
                    payload.setdefault(
                        "question",
                        {
                            "paused_budget": "Which explicit budget should the task continue with?",
                            "recovering": "Confirm the recorded external outcome before resuming.",
                            "awaiting_approval": "Approve, reject or edit this exact action?",
                        }.get(state, "Provide the requested task clarification."),
                    )
                    self._transition(task, pulse, state, phase, "task.waiting", payload)
                else:
                    verification = result.get("verification", {})
                    valid = (
                        verification.get("status") == "verified"
                        and isinstance(verification.get("verification"), dict)
                        and verification["verification"].get("valid") is True
                    )
                    publication = verification.get("publication_status")
                    if publication in {"unknown", "result_unknown", "recovering"}:
                        self._transition(
                            task,
                            pulse,
                            "recovering",
                            phase,
                            "task.waiting",
                            {
                                "kind": "reconciliation",
                                "reason": "Draft publication outcome is unknown",
                                "question": "Reconcile the existing publication before continuing.",
                            },
                        )
                    else:
                        self._transition(
                            task,
                            pulse,
                            "succeeded" if valid else "failed",
                            "finalize",
                            "task.finished",
                            {"verification": verification},
                        )
        except CheckpointIncompatible as error:
            self._transition(
                task,
                pulse,
                "recovering",
                task["phase"],
                "task.waiting",
                {
                    "kind": "version",
                    "reason": str(error),
                    "question": "Upgrade the application and migrate this checkpoint version.",
                },
            )
        except Exception as error:
            self._transition(
                task,
                pulse,
                "failed",
                task["phase"],
                "task.failed",
                {
                    "error_type": type(error).__name__,
                    "reason": str(error),
                },
            )
        finally:
            pulse.finish()
            if services is not None:
                services.close()
        return True

    def observe_merges(self):
        if not self.settings.github_token or not self.runtime_config.author_email:
            return
        from decagent.integrations.github import GitHub
        from decagent.memory import MemoryManager
        from decagent.storage.evidence import EvidenceStore

        from .memory import observe_task_merge

        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT d.task_id,p.repository FROM app.deliveries d JOIN app.tasks t "
                "ON t.id=d.task_id JOIN app.projects p ON p.id=t.project_id "
                "WHERE d.status='draft' OR (d.status='merged' AND EXISTS "
                "(SELECT 1 FROM app.memory_candidates m WHERE m.source_task_id=d.task_id "
                "AND m.status IN ('task_local','pending_merge','pending_review'))) "
                "ORDER BY d.updated_at LIMIT 100"
            ).fetchall()
        evidence = EvidenceStore(
            self.settings.data_dir / "evidence", self.database.redactor.redact_text
        )
        manager = MemoryManager(self.database, evidence)
        for row in rows:
            github = GitHub(row["repository"], self.settings.github_token)
            try:
                observe_task_merge(
                    self.database,
                    evidence,
                    github,
                    manager,
                    str(row["task_id"]),
                    author_name=self.runtime_config.author_name,
                    author_email=self.runtime_config.author_email,
                )
            except Exception:
                self.database.event_once(
                    str(row["task_id"]),
                    "merge-observation-unavailable",
                    "memory.observation_unavailable",
                    {
                        "reason": "GitHub merge evidence could not be confirmed",
                    },
                )
            finally:
                github.client.close()

    def run_forever(self, stop=None, *, idle_seconds=1, maintenance_seconds=60):
        stop = stop or threading.Event()
        next_maintenance = time.monotonic()
        while not stop.is_set():
            if time.monotonic() >= next_maintenance:
                self.observe_merges()
                next_maintenance = time.monotonic() + maintenance_seconds
            if not self.run_once():
                stop.wait(idle_seconds)


def _run_serial_workers(workers, stop=None, *, idle_seconds=1, maintenance_seconds=60):
    """Alternate queue kinds so sustained work in either kind cannot starve the other."""
    if not workers:
        raise ValueError("At least one worker kind is required")
    stop = stop or threading.Event()
    next_maintenance = time.monotonic()
    index, idle = 0, 0
    while not stop.is_set():
        if time.monotonic() >= next_maintenance:
            for worker in workers:
                if (
                    getattr(worker, "kind", "repository_repair") == "repository_repair"
                    and worker.settings.github_token
                ):
                    worker.observe_merges()
            next_maintenance = time.monotonic() + maintenance_seconds
        worked = workers[index].run_once()
        index = (index + 1) % len(workers)
        idle = 0 if worked else idle + 1
        if idle >= len(workers):
            stop.wait(idle_seconds)
            idle = 0


def run_worker(settings, *, kind=None):
    from decagent.storage.database import Database

    if kind not in {None, "business", "repository_repair"}:
        raise ValueError("Unsupported worker kind")
    database = Database(settings.database_url)
    workers = []
    if kind == "business" or kind is None:
        from decagent.business.worker import BusinessWorker

        workers.append(BusinessWorker(database, settings))
    if kind == "repository_repair" or kind is None:
        workers.append(Worker(database, settings))
    try:
        _run_serial_workers(workers)
    finally:
        for worker in workers:
            close = getattr(worker, "close", None)
            if close is not None:
                close()

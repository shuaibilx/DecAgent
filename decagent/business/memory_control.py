"""Employee business memory control and fresh checks for unfinished bindings."""

from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID

from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.context import _execution_projection
from decagent.business.contracts import PluginSpec
from decagent.business.memory import BusinessMemoryStore
from decagent.business.memory_slots import (
    MemorySlotAmbiguous,
    memory_slot_from_binding,
    memory_slot_key,
)
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.contracts import Plan
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.workflow_registry import WorkflowRegistry
from decagent.decisions.contracts import digest
from decagent.storage.safe_record import exact_safe_record


class _ConnectionDatabase:
    """Keep store validation, writes and the HTTP idempotency response atomic."""

    def __init__(self, database, connection):
        self.database, self.connection = database, connection
        self.redactor = database.redactor

    def connect(self):
        return nullcontext(self.connection)

    def __getattr__(self, name):
        return getattr(self.database, name)


def _references(value):
    memories, producers = set(), set()

    def walk(item):
        if isinstance(item, dict):
            source = item.get("source")
            if source == "literal":
                return
            if source == "verified_memory":
                memories.add(memory_slot_from_binding(item))
                return
            elif isinstance(source, str) and source in {"node_output", "accepted_decision"}:
                producers.add(item["node_id"])
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return memories, producers


class BusinessMemoryControl:
    def __init__(self, database):
        self.database = database

    def _store(self, conn):
        return BusinessMemoryStore(_ConnectionDatabase(self.database, conn))

    def _current(self, conn, identity, task_id, *, browser=False):
        store = self._store(conn)
        task, _actor = store.operations.lock_employee_task(
            conn, identity, UUID(str(task_id)), browser_only=browser
        )
        if task is None:
            raise AccessError("not_found", "Original employee memory task unavailable")
        scope = conn.execute(
            "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE",
            (task["business_scope_id"],),
        ).fetchone()
        if (
            task["graph_version"] != "business-3"
            or not scope
            or not scope["enabled"]
            or task["authorization_snapshot"].get("plugin_digest") != scope["manifest_digest"]
        ):
            raise AccessError("contract_changed", "Current conditional memory context unavailable")
        spec = PluginSpec.from_mapping(scope["manifest"])
        if spec.digest != scope["manifest_digest"]:
            raise AccessError("contract_changed", "Memory contract integrity changed")
        registry = QuestionRegistry.from_spec(spec)
        saved = conn.execute(
            "SELECT r.* FROM app.business_plan_heads h JOIN app.business_plan_revisions r "
            "ON r.task_id=h.task_id AND r.revision=h.current_revision WHERE h.task_id=%s "
            "FOR SHARE OF h,r",
            (task["id"],),
        ).fetchone()
        instance = None
        if conn.execute("SELECT to_regclass('app.workflow_instances') AS t").fetchone()["t"]:
            instance = conn.execute(
                "SELECT * FROM app.workflow_instances WHERE task_id=%s FOR SHARE",
                (task["id"],),
            ).fetchone()
        if saved is None and instance is None:
            raise AccessError("context_unavailable", "A current stored plan is required")
        plan = Plan.from_mapping(saved["definition"] if saved else instance["plan"])
        if (
            plan.registry_fingerprint != registry.fingerprint
            or saved
            and (
                plan.digest != saved["content_digest"]
                or plan.revision != saved["revision"]
                or plan.registry_fingerprint != saved["registry_fingerprint"]
                or plan.plan_id != saved["plan_id"]
            )
        ):
            raise AccessError("version_changed", "Stored plan or question catalogue changed")
        plan_fingerprint = plan.digest
        if instance:
            workflows = WorkflowRegistry(_ConnectionDatabase(self.database, conn))
            resolution = workflows.check_instance(identity, task["id"])
            if not resolution.matched:
                raise AccessError(resolution.reason, "Current workflow memory binding unavailable")
            plan_fingerprint = instance["definition_digest"]
        context = store.context(
            identity,
            str(scope["id"]),
            question_fingerprint=registry.fingerprint,
            plan_fingerprint=plan_fingerprint,
        )
        return task, plan, context

    def context(self, identity, task_id):
        """Trusted runtime helper; no fingerprint or epoch is accepted from callers."""
        with self.database.connect() as conn:
            return self._current(conn, identity, task_id)[2]

    def history(self, identity, *, task_id=None, limit=50):
        with self.database.connect() as conn:
            store = self._store(conn)
            if task_id:
                context = self._current(conn, identity, task_id, browser=True)[2]
            else:
                store.operations._identity(conn, identity, lock=True, browser_only=True)
                context = None
            return store.list_own(identity, context=context, limit=limit)

    def get(self, identity, memory_id, *, task_id=None):
        with self.database.connect() as conn:
            store = self._store(conn)
            if task_id:
                context = self._current(conn, identity, task_id, browser=True)[2]
            else:
                store.operations._identity(conn, identity, lock=True, browser_only=True)
                context = None
            return store.get(identity, memory_id, context=context)

    @staticmethod
    def _event(conn, task, kind, payload, redactor):
        seq = conn.execute(
            "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s RETURNING event_seq",
            (task["id"],),
        ).fetchone()["event_seq"]
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) VALUES(%s,NULL,%s,%s,%s)",
            (task["id"], seq, kind, Jsonb(exact_safe_record(payload, redactor))),
        )
        return seq

    def correct(self, identity, memory_id, body):
        with self.database.connect() as conn:
            task, _, context = self._current(conn, identity, body.task_id, browser=True)
            store = self._store(conn)
            old = store._own(conn, identity, memory_id, lock=True)
            if (
                old["content_digest"] != body.expected_digest
                or old["kind"] != "confirmed_preference"
                or old["status"] not in {"active", "pending_confirmation"}
                or old["expires_at"] <= store.clock()
                or not store._matches(old, context)
            ):
                raise AccessError("stale", "Current preference version is required for correction")
            references, _ = store._sources(
                conn, context, old["evidence_refs"], identity.employee_id
            )
            if len(references) >= 16:
                raise ValueError("Correction cannot discard existing evidence to add a new source")
            fingerprint = digest(
                {
                    "memory_id": str(memory_id),
                    "expected_digest": body.expected_digest,
                    "value": body.value,
                    "context": asdict(context),
                }
            )
            prior = conn.execute(
                "SELECT seq FROM app.events WHERE task_id=%s "
                "AND kind='business.memory_correction_proposed' "
                "AND payload->>'correction_digest'=%s ORDER BY seq LIMIT 1",
                (task["id"], fingerprint),
            ).fetchone()
            seq = (
                prior["seq"]
                if prior
                else self._event(
                    conn,
                    task,
                    "business.memory_correction_proposed",
                    {
                        "memory_id": str(memory_id),
                        "employee_id": str(identity.employee_id),
                        "value": body.value,
                        "correction_digest": fingerprint,
                        "classification": "user_confirmed",
                        "pending_preference_confirmation": True,
                    },
                    self.database.redactor,
                )
            )
            return store.propose_preference(
                identity,
                context,
                subject=old["subject"],
                value=body.value,
                evidence_refs=[
                    *references,
                    {"kind": "task_event", "task_id": str(task["id"]), "seq": seq},
                ],
                supersedes=str(memory_id),
            )

    def confirm(self, identity, memory_id, body):
        with self.database.connect() as conn:
            _, _, context = self._current(conn, identity, body.task_id, browser=True)
            return self._store(conn).confirm_preference(
                identity, context, memory_id, body.expected_digest
            )

    def revoke(self, identity, memory_id, body):
        with self.database.connect() as conn:
            _, _, context = self._current(conn, identity, body.task_id, browser=True)
            store = self._store(conn)
            row = store._own(conn, identity, memory_id, lock=True)
            if str(row["scope_id"]) != context.scope_id:
                raise AccessError("not_found", "Memory is outside this task scope")
            return store.revoke(identity, memory_id, body.expected_digest, body.reason)

    def revoke_source(self, identity, body):
        with self.database.connect() as conn:
            _, _, context = self._current(conn, identity, body.task_id, browser=True)
            return self._store(conn).revoke_source(identity, context, body.reference, body.reason)

    def check_dependencies(self, identity, task_id, *, node_ids=None, include_frozen=False):
        """Rebuild eligibility for pending bindings; preserve dispatched/completed evidence.

        The interpreter must call this before using saved memory bindings. This
        query never rewrites node outputs or retries an unknown side effect.
        """
        if type(include_frozen) is not bool:
            raise ValueError("Frozen dependency checking must be an explicit boolean")
        with self.database.connect() as conn:
            task, plan, context = self._current(conn, identity, task_id)
            store = self._store(conn)
            published = conn.execute(
                "SELECT w.status FROM app.workflow_instances i JOIN app.business_workflows w "
                "USING(workflow_id,version) WHERE i.task_id=%s",
                (task["id"],),
            ).fetchone()
            if not published or published["status"] != "published":
                raise AccessError(
                    "workflow_required",
                    "Stable memory slots require a current published workflow",
                )
            rows = {
                row["node_id"]: row
                for row in conn.execute(
                    "SELECT DISTINCT ON(node_id) * FROM app.business_plan_nodes WHERE task_id=%s "
                    "ORDER BY node_id,execution_generation DESC",
                    (task["id"],),
                ).fetchall()
            }
            head = conn.execute(
                "SELECT context_snapshot FROM app.business_plan_heads WHERE task_id=%s",
                (task["id"],),
            ).fetchone()
            if head:
                schema = head["context_snapshot"].get("input_schema")
            else:
                schema = conn.execute(
                    "SELECT w.definition->'input_schema' AS schema FROM app.workflow_instances i "
                    "JOIN app.business_workflows w USING(workflow_id,version) WHERE i.task_id=%s",
                    (task["id"],),
                ).fetchone()["schema"]
            compiled = compile_plan(
                plan,
                PluginSpec.from_mapping(store._scope(conn, context)["manifest"]),
                input_schema=schema,
            )
            rows = {
                name: row
                for name, row in rows.items()
                if name in compiled.order and row["node_digest"] == digest(compiled.node(name))
            }
            inactive = set(_execution_projection(compiled, rows)["inactive_nodes"])
            dependencies, parents = {}, {}
            for node in plan.nodes:
                dependencies[node["id"]], producers = _references(node)
                parents[node["id"]] = (set(node["depends_on"]) | producers) - inactive
                if node["type"] == "complete":
                    for evidence in plan.goal["evidence"]:
                        source = evidence["source"]
                        if source.get("node_id") in inactive:
                            continue
                        goal_memory, goal_producers = _references(evidence)
                        dependencies[node["id"]].update(goal_memory)
                        parents[node["id"]].update(goal_producers - inactive)
            for _ in plan.nodes:
                for name, sources in parents.items():
                    for source in sources:
                        dependencies[name].update(dependencies.get(source, ()))
            selected = set(dependencies) if node_ids is None else set(node_ids)
            if selected - set(dependencies):
                raise ValueError("Unknown dependency-check node")
            selected.difference_update(inactive)
            frozen = {
                name
                for name, row in rows.items()
                if row["status"] in {"succeeded", "skipped", "unknown"}
                or row["dispatch_attempts"] > 0
                or row["action_id"] is not None
            }
            pending = selected if include_frozen else selected - frozen
            memory_slots = set().union(*(dependencies[name] for name in pending))
            current = {}
            slot_errors = {}
            for memory_kind, subject in sorted(memory_slots):
                key = memory_slot_key(memory_kind, subject)
                try:
                    row = store.resolve_slot(
                        identity,
                        context,
                        memory_kind=memory_kind,
                        subject=subject,
                    )
                except MemorySlotAmbiguous:
                    slot_errors[key] = "memory_ambiguous"
                    continue
                except (AccessError, ValueError):
                    continue
                if row and row["active"] and row["verified"] and not row["expired"]:
                    current[key] = row
            invalid = [
                {
                    "node_id": name,
                    "memory_kind": memory_kind,
                    "subject": subject,
                    "reason": slot_errors.get(
                        memory_slot_key(memory_kind, subject), "memory_unavailable"
                    ),
                }
                for name in sorted(pending)
                for memory_kind, subject in sorted(dependencies[name])
                if memory_slot_key(memory_kind, subject) not in current
            ]
            return {
                "valid": not invalid,
                "status": "revalidation_required" if invalid else "current",
                "plan_digest": plan.digest,
                "context": asdict(context),
                "memories": current,
                "invalid_dependencies": invalid,
                "frozen_nodes": sorted(frozen),
                "requires_replan": bool(invalid),
                "grants_write_authority": False,
            }

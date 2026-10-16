"""Immutable plan revisions and replay-safe node records under the task lease."""

from psycopg.types.json import Jsonb

from decagent.business.registry import PluginRegistry
from decagent.decisions.contracts import digest
from decagent.storage.budget import BudgetExceeded
from decagent.storage.safe_record import exact_safe_record

from .compiler import compile_plan, default_input_schema
from .contracts import Plan

GRAPH_VERSION = "business-3"


class PlanConflict(ValueError):
    """The parent revision or an immutable executed node has changed."""


class PlanPublicationStale(PlanConflict):
    """The planner's original permission boundary changed before publication."""


class PlanRepository:
    def __init__(self, database, task_id, owner, generation):
        self.database, self.task_id = database, task_id
        self.owner, self.generation = owner, generation
        task = database.get_task(task_id)
        if task["kind"] != "business" or task["graph_version"] != GRAPH_VERSION:
            raise ValueError("Conditional business task required")
        if task["employee_id"] is None or task["credential_ref"] is None:
            raise ValueError("Original employee and credential required")
        self.spec = PluginRegistry(database).get(
            task["business_scope_id"], task["authorization_snapshot"]["plugin_digest"]
        )
        self._cache = {}

    def guard(self, conn=None):
        if conn is None:
            with self.database.connect() as current:
                return self.guard(current)
        task = self.database.assert_lease(self.task_id, self.owner, self.generation, conn)
        scope = conn.execute(
            "SELECT enabled,manifest_digest,revision FROM app.business_scopes "
            "WHERE id=%s FOR SHARE",
            (task["business_scope_id"],),
        ).fetchone()
        if not scope or not scope["enabled"] or scope["manifest_digest"] != self.spec.digest:
            raise ValueError("Registered business scope changed")
        credential = conn.execute(
            "SELECT a.credential_revision FROM business.mcp_credentials c "
            "JOIN business.employee_accounts a ON a.id=c.employee_id "
            "JOIN business.people p ON p.id=a.person_id WHERE c.id=%s AND c.employee_id=%s "
            "AND c.revoked_at IS NULL AND c.expires_at>clock_timestamp() AND a.active AND p.active "
            "AND c.account_revision=a.credential_revision",
            (task["credential_ref"], task["employee_id"]),
        ).fetchone()
        if not credential:
            from decagent.business.mcp_client import MCPAuthenticationError

            raise MCPAuthenticationError("Original employee credential unavailable")
        return {
            **task,
            "scope_revision": scope["revision"],
            "authorization_revision": credential["credential_revision"],
        }

    def _safe(self, value):
        return exact_safe_record(value, self.database.redactor)

    def _event(self, conn, kind, payload):
        current = conn.execute(
            "UPDATE app.tasks SET event_seq=event_seq+1 WHERE id=%s RETURNING event_seq",
            (self.task_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO app.events(task_id,project_id,seq,kind,payload) VALUES(%s,NULL,%s,%s,%s)",
            (self.task_id, current["event_seq"], kind, Jsonb(self._safe(payload))),
        )

    def _load(self, conn):
        row = conn.execute(
            "SELECT r.* FROM app.business_plan_heads h JOIN app.business_plan_revisions r "
            "ON r.task_id=h.task_id AND r.revision=h.current_revision WHERE h.task_id=%s",
            (self.task_id,),
        ).fetchone()
        if row is None:
            return None
        plan = self._cache.get(row["content_digest"])
        if plan is None:
            plan = Plan.from_mapping(row["definition"])
        if (
            plan.digest != row["content_digest"]
            or plan.revision != row["revision"]
            or plan.registry_fingerprint != row["registry_fingerprint"]
            or plan.plan_id != row["plan_id"]
        ):
            raise ValueError("Recorded plan integrity changed")
        self._cache[plan.digest] = plan
        return plan

    def current(self):
        with self.database.connect() as conn:
            self.guard(conn)
            return self._load(conn)

    def _corrections(self, conn):
        return [
            row["payload"]
            for row in conn.execute(
                "SELECT payload FROM app.events WHERE task_id=%s "
                "AND kind='business.node_superseded' ORDER BY seq",
                (self.task_id,),
            ).fetchall()
        ]

    def instruction(self, conn=None):
        from .corrections import effective_instruction

        if conn is None:
            with self.database.connect() as current:
                return self.instruction(current)
        task = self.guard(conn)
        return effective_instruction(
            task["authorization_snapshot"]["instruction"], self._corrections(conn)
        )

    def apply_correction(self, approval_id):
        """Replace only authenticated draft parameters; never erase its old operation."""
        from .corrections import correction_fact

        with self.database.connect() as conn:
            task = self.guard(conn)
            prior = self._load(conn)
            if prior is None:
                raise PlanConflict("Cannot correct an unpublished plan")
            if any(fact["approval_id"] == str(approval_id) for fact in self._corrections(conn)):
                return prior
            fact = correction_fact(self, conn, task, prior, approval_id)
            value = prior.to_mapping()
            value.pop("content_digest", None)
            value["revision"] += 1
            value["nodes"] = [
                fact["node"] if n["id"] == fact["node_id"] else n for n in value["nodes"]
            ]
        return self.publish(value, parent_digest=prior.digest, correction_approval_id=approval_id)

    def publish(
        self,
        value,
        *,
        parent_digest=None,
        input_schema=None,
        correction_approval_id=None,
        expected_boundary=None,
        task_input=None,
    ):
        with self.database.connect() as conn:
            task = self.guard(conn)
            if expected_boundary is not None and expected_boundary != {
                "employee_id": str(task["employee_id"]),
                "scope_id": str(task["business_scope_id"]),
                "scope_revision": task["scope_revision"],
                "account_revision": task["authorization_revision"],
                "plugin_digest": task["authorization_snapshot"]["plugin_digest"],
            }:
                raise PlanPublicationStale("Planner permission boundary changed before publication")
            prior = self._load(conn)
            if prior:
                saved = conn.execute(
                    "SELECT context_snapshot FROM app.business_plan_heads WHERE task_id=%s",
                    (self.task_id,),
                ).fetchone()["context_snapshot"]
                original_schema = saved.get("input_schema", default_input_schema())
                if input_schema is not None and digest(input_schema) != digest(original_schema):
                    raise PlanConflict("Published task input schema cannot change in a revision")
                input_schema = original_schema
            elif input_schema is None:
                input_schema = default_input_schema()
            compiled = compile_plan(value, self.spec, input_schema=input_schema)
            if task_input is not None:
                from decagent.business.contracts import validate_schema

                task_input = self._safe(task_input)
                validate_schema(input_schema, task_input)
                if task_input.get("instruction") != task["authorization_snapshot"]["instruction"]:
                    raise PlanConflict("Task input cannot replace the original instruction")
                if prior and saved.get("task_input") != task_input:
                    raise PlanConflict("Published task input cannot change during recovery")
            plan = compiled.plan
            definition = self._safe(plan.to_mapping())
            if prior and prior.digest == plan.digest:
                return prior
            if prior is None:
                if plan.revision != 1 or parent_digest is not None:
                    raise PlanConflict("Initial plan revision must be one without a parent")
            elif (
                parent_digest != prior.digest
                or plan.revision != prior.revision + 1
                or plan.plan_id != prior.plan_id
            ):
                raise PlanConflict("Plan parent or revision changed")
            correction = None
            if correction_approval_id is not None:
                from .corrections import correction_fact

                if prior is None:
                    raise PlanConflict("A correction needs an existing prepared plan")
                if any(
                    f["approval_id"] == str(correction_approval_id) for f in self._corrections(conn)
                ):
                    raise PlanConflict("Employee correction was already applied")
                correction = correction_fact(self, conn, task, prior, correction_approval_id)
            maximum = task["budget"].get("max_plan_revisions", 4)
            if plan.revision > maximum:
                raise BudgetExceeded("Plan revision budget exhausted")
            frozen = conn.execute(
                "SELECT node_id,node_digest,execution_generation FROM app.business_plan_nodes "
                "WHERE task_id=%s AND (status IN "
                "('succeeded','waiting','unknown','failed','skipped') "
                "OR action_id IS NOT NULL OR dispatch_attempts>0)",
                (self.task_id,),
            ).fetchall()
            superseded = {
                (f["node_id"], f["generation"], f["old_digest"]) for f in self._corrections(conn)
            }
            frozen = [
                row
                for row in frozen
                if (row["node_id"], row["execution_generation"], row["node_digest"])
                not in superseded
            ]
            nodes = {node["id"]: node for node in plan.nodes}
            prior_nodes = {node["id"]: node for node in prior.nodes} if prior else {}
            protected_edits = {f["node_id"]: f for f in self._corrections(conn)}
            for name, fact in protected_edits.items():
                expected = (
                    correction["new_digest"]
                    if correction and correction["node_id"] == name
                    else fact["new_digest"]
                )
                if name not in nodes or digest(nodes[name]) != expected:
                    raise PlanConflict("Cannot rewrite or revive a superseded corrected node")
            if (
                prior
                and plan.entry != prior.entry
                and (
                    any(row["node_id"] == prior.entry for row in frozen)
                    or prior.entry in protected_edits
                )
            ):
                raise PlanConflict("Cannot move the entry ahead of a frozen executed prefix")
            frozen_writes = {}
            for row in frozen:
                if correction and row["node_id"] == correction["node_id"]:
                    if (
                        row["execution_generation"] != correction["generation"]
                        or row["node_digest"] != correction["old_digest"]
                        or row["node_id"] not in nodes
                        or digest(nodes[row["node_id"]]) != correction["new_digest"]
                    ):
                        raise PlanConflict(
                            "Correction can only change authenticated draft parameters"
                        )
                    continue
                if (
                    row["node_id"] not in nodes
                    or digest(nodes[row["node_id"]]) != row["node_digest"]
                ):
                    raise PlanConflict("Cannot rewrite frozen executed or prepared node")
                original = prior_nodes[row["node_id"]]
                if original["type"] == "governed_write":
                    signature = digest(
                        {"command": original["command"], "inputs": original["inputs"]}
                    )
                    frozen_writes.setdefault(signature, set()).add(original["id"])
            for node in nodes.values():
                if node["type"] != "governed_write":
                    continue
                signature = digest({"command": node["command"], "inputs": node["inputs"]})
                if signature in frozen_writes and node["id"] not in frozen_writes[signature]:
                    raise PlanConflict("Cannot repeat a frozen write under another node ID")
            conn.execute(
                "INSERT INTO app.business_plan_revisions"
                "(task_id,revision,plan_id,schema_version,content_digest,registry_fingerprint,"
                "parent_revision,parent_digest,definition) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.task_id,
                    plan.revision,
                    plan.plan_id,
                    plan.schema_version,
                    plan.digest,
                    plan.registry_fingerprint,
                    prior.revision if prior else None,
                    prior.digest if prior else None,
                    Jsonb(definition),
                ),
            )
            if prior is None:
                conn.execute(
                    "INSERT INTO app.business_plan_heads"
                    "(task_id,current_revision,context_snapshot) "
                    "VALUES(%s,%s,%s)",
                    (
                        self.task_id,
                        plan.revision,
                        Jsonb(
                            {
                                **({"input_schema": input_schema} if input_schema else {}),
                                **({"task_input": task_input} if task_input is not None else {}),
                            }
                        ),
                    ),
                )
            else:
                conn.execute(
                    "UPDATE app.business_plan_heads SET current_revision=%s,"
                    "updated_at=clock_timestamp() WHERE task_id=%s",
                    (plan.revision, self.task_id),
                )
            self._event(
                conn,
                "business.plan_published",
                {
                    "revision": plan.revision,
                    "plan_id": plan.plan_id,
                    "digest": plan.digest,
                    "parent_digest": prior.digest if prior else None,
                },
            )
            if prior is None:
                instance = conn.execute(
                    "SELECT * FROM app.workflow_instances WHERE task_id=%s",
                    (self.task_id,),
                ).fetchone()
                if instance and instance["plan"].get("content_digest") == plan.digest:
                    primary = conn.execute(
                        "SELECT count(*) AS n FROM app.usage_records WHERE task_id=%s "
                        "AND model_family='primary' AND dispatch_state<>'not_sent'",
                        (self.task_id,),
                    ).fetchone()["n"]
                    self._event(
                        conn,
                        "business.workflow_reused",
                        {
                            "workflow_id": instance["workflow_id"],
                            "version": instance["version"],
                            "instance_id": str(instance["instance_id"]),
                            "definition_digest": instance["definition_digest"],
                            "report_digest": instance["report_digest"],
                            "plan_digest": plan.digest,
                            "primary_requests": primary,
                        },
                    )
            if correction:
                self._event(
                    conn,
                    "business.node_superseded",
                    {key: val for key, val in correction.items() if key != "node"},
                )
                conn.execute(
                    "UPDATE app.approvals SET status='consumed' WHERE id=%s",
                    (correction_approval_id,),
                )
        self._cache[plan.digest] = plan
        return plan

    def counters(self):
        with self.database.connect() as conn:
            self.guard(conn)
            return conn.execute(
                "SELECT node_attempts,graph_transitions,current_revision "
                "FROM app.business_plan_heads WHERE task_id=%s",
                (self.task_id,),
            ).fetchone()

    def context_snapshot(self):
        with self.database.connect() as conn:
            self.guard(conn)
            row = conn.execute(
                "SELECT context_snapshot FROM app.business_plan_heads WHERE task_id=%s",
                (self.task_id,),
            ).fetchone()
            return row["context_snapshot"] if row else {}

    def _batch(self, conn, batch_id):
        row = conn.execute(
            "SELECT payload FROM app.events WHERE task_id=%s "
            "AND kind='business.decision_batch' AND payload->>'batch_id'=%s "
            "ORDER BY seq DESC LIMIT 1",
            (self.task_id, batch_id),
        ).fetchone()
        return row["payload"] if row else None

    def batch(self, batch_id):
        with self.database.connect() as conn:
            self.guard(conn)
            return self._batch(conn, batch_id)

    def start_batch(self, batch_id, metadata, nodes):
        """Fence every question and the request marker in one transaction."""
        safe = self._safe(metadata)
        with self.database.connect() as conn:
            self.guard(conn)
            existing = self._batch(conn, batch_id)
            if existing:
                return existing, False
            for name, saved in nodes.items():
                row = self._node(conn, name, saved["execution_generation"])
                if (
                    row["status"] != "running"
                    or row["dispatch_attempts"] != 0
                    or row["input_digest"] != saved["input_digest"]
                ):
                    raise PlanConflict("Question has already been dispatched or changed")
                conn.execute(
                    "UPDATE app.business_plan_nodes SET dispatch_attempts=1,output_snapshot=%s "
                    "WHERE task_id=%s AND node_id=%s AND execution_generation=%s",
                    (
                        Jsonb({"request_batch_id": batch_id}),
                        self.task_id,
                        name,
                        saved["execution_generation"],
                    ),
                )
            record = {**safe, "batch_id": batch_id, "phase": "dispatched"}
            self._event(conn, "business.decision_batch", record)
            return record, True

    def finish_batch(self, batch_id, result, nodes):
        """Save the response and all raw-answer pointers before consuming any."""
        safe = self._safe(result)
        with self.database.connect() as conn:
            self.guard(conn)
            prior = self._batch(conn, batch_id)
            if not prior:
                raise PlanConflict("Decision batch was not dispatched")
            if prior["phase"] == "complete":
                if digest(prior["result"]) != digest(safe):
                    raise PlanConflict("Completed decision response is immutable")
                return prior
            if set(nodes) != set(prior["node_ids"]):
                raise PlanConflict("Batch question identities changed")
            for name, saved in nodes.items():
                row = self._node(conn, name, saved["execution_generation"])
                if (
                    row["status"] != "running"
                    or row["dispatch_attempts"] != 1
                    or row["input_digest"] != saved["input_digest"]
                ):
                    raise PlanConflict("Question changed before its response was saved")
                conn.execute(
                    "UPDATE app.business_plan_nodes SET output_snapshot=%s,"
                    "updated_at=clock_timestamp() WHERE task_id=%s AND node_id=%s "
                    "AND execution_generation=%s",
                    (
                        Jsonb({"request_batch_id": batch_id, "raw_batch_id": batch_id}),
                        self.task_id,
                        name,
                        saved["execution_generation"],
                    ),
                )
            record = {**prior, "phase": "complete", "result": safe}
            self._event(conn, "business.decision_batch", record)
            return record

    def _node(self, conn, node_id, generation=1):
        row = conn.execute(
            "SELECT * FROM app.business_plan_nodes WHERE task_id=%s AND node_id=%s "
            "AND execution_generation=%s FOR UPDATE",
            (self.task_id, node_id, generation),
        ).fetchone()
        if row is None:
            raise KeyError("Node execution not started")
        return row

    def _attempt(self, conn, task):
        row = conn.execute(
            "UPDATE app.business_plan_heads SET node_attempts=node_attempts+1,"
            "updated_at=clock_timestamp() WHERE task_id=%s AND node_attempts<%s "
            "RETURNING node_attempts",
            (self.task_id, task["budget"].get("max_node_attempts", 128)),
        ).fetchone()
        if row is None:
            raise BudgetExceeded("Node attempt budget exhausted")

    def begin(self, node_id, values, provenance, *, generation=None):
        snapshot = self._safe({"values": values, "provenance": provenance})
        input_digest = digest(snapshot)
        with self.database.connect() as conn:
            task = self.guard(conn)
            plan = self._load(conn)
            if plan is None:
                raise ValueError("Publish a plan before executing nodes")
            node = next((item for item in plan.nodes if item["id"] == node_id), None)
            if node is None:
                raise ValueError("Node is not in the current plan")
            latest = conn.execute(
                "SELECT * FROM app.business_plan_nodes WHERE task_id=%s AND node_id=%s "
                "ORDER BY execution_generation DESC LIMIT 1 FOR UPDATE",
                (self.task_id, node_id),
            ).fetchone()
            expected_generation = latest["execution_generation"] if latest else 1
            if latest and latest["node_digest"] != digest(node):
                if not any(
                    f["node_id"] == node_id
                    and f["generation"] == latest["execution_generation"]
                    and f["old_digest"] == latest["node_digest"]
                    and f["new_digest"] == digest(node)
                    for f in self._corrections(conn)
                ):
                    raise PlanConflict("Node successor requires authenticated invalidation")
                expected_generation += 1
            if generation is not None and generation != expected_generation:
                raise PlanConflict("Execution generation cannot bypass the original node")
            generation = expected_generation
            row = conn.execute(
                "SELECT * FROM app.business_plan_nodes WHERE task_id=%s AND node_id=%s "
                "AND execution_generation=%s FOR UPDATE",
                (self.task_id, node_id, generation),
            ).fetchone()
            if row:
                if row["input_digest"] != input_digest or row["node_digest"] != digest(node):
                    raise PlanConflict("Node input or definition changed after execution began")
                return row
            self._attempt(conn, task)
            row = conn.execute(
                "INSERT INTO app.business_plan_nodes"
                "(task_id,node_id,execution_generation,plan_revision,node_digest,status,"
                "input_digest,input_snapshot) VALUES(%s,%s,%s,%s,%s,'running',%s,%s) RETURNING *",
                (
                    self.task_id,
                    node_id,
                    generation,
                    plan.revision,
                    digest(node),
                    input_digest,
                    Jsonb(snapshot),
                ),
            ).fetchone()
            self._event(
                conn,
                "business.node_started",
                {
                    "revision": plan.revision,
                    "node_id": node_id,
                    "generation": generation,
                    "input_digest": input_digest,
                },
            )
            return row

    def dispatch(self, node_id, *, max_attempts=1, generation=1):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 24:
            raise ValueError("Invalid bounded dispatch attempts")
        with self.database.connect() as conn:
            task = self.guard(conn)
            row = self._node(conn, node_id, generation)
            if row["status"] != "running" or row["dispatch_attempts"] >= max_attempts:
                raise ValueError("Node dispatch attempt limit reached")
            if row["dispatch_attempts"] > 0:
                self._attempt(conn, task)
            return conn.execute(
                "UPDATE app.business_plan_nodes SET dispatch_attempts=dispatch_attempts+1,"
                "updated_at=clock_timestamp() WHERE task_id=%s AND node_id=%s "
                "AND execution_generation=%s RETURNING *",
                (self.task_id, node_id, generation),
            ).fetchone()

    def resume_read(self, node_id, input_digest, *, generation=1):
        """Only a failed read transport may re-enter; preserve every attempt."""
        with self.database.connect() as conn:
            self.guard(conn)
            row = self._node(conn, node_id, generation)
            plan = self._load(conn)
            node = next(item for item in plan.nodes if item["id"] == node_id)
            if (
                node["type"] != "read_tool"
                or row["status"] != "waiting"
                or row["action_id"] is not None
                or row["input_digest"] != input_digest
                or row["dispatch_attempts"] >= 2
            ):
                raise PlanConflict("Only the same bounded waiting read can resume")
            conn.execute(
                "UPDATE app.business_plan_nodes SET status='running',"
                "updated_at=clock_timestamp() WHERE task_id=%s AND node_id=%s "
                "AND execution_generation=%s",
                (self.task_id, node_id, generation),
            )

    def finish(self, node_id, status, output=None, *, generation=1):
        if status not in {"succeeded", "skipped", "waiting", "failed", "unknown"}:
            raise ValueError("Invalid node completion status")
        safe = self._safe(output) if output is not None else None
        if status == "succeeded" and safe is None:
            raise ValueError("Succeeded node requires saved output")
        with self.database.connect() as conn:
            self.guard(conn)
            row = self._node(conn, node_id, generation)
            if row["status"] in {"succeeded", "skipped"}:
                if row["status"] != status or row["output_snapshot"] != safe:
                    raise PlanConflict("Terminal node output is immutable")
                return row
            result = conn.execute(
                "UPDATE app.business_plan_nodes SET status=%s,output_snapshot=%s,"
                "updated_at=clock_timestamp() WHERE task_id=%s AND node_id=%s "
                "AND execution_generation=%s RETURNING *",
                (
                    status,
                    Jsonb(safe) if safe is not None else None,
                    self.task_id,
                    node_id,
                    generation,
                ),
            ).fetchone()
            self._event(
                conn,
                "business.node_saved",
                {
                    "node_id": node_id,
                    "status": status,
                    "revision": row["plan_revision"],
                    "generation": generation,
                },
            )
            return result

    def bind_action(self, node_id, action_id, *, generation=1):
        with self.database.connect() as conn:
            self.guard(conn)
            row = self._node(conn, node_id, generation)
            action = conn.execute(
                "SELECT * FROM app.actions WHERE id=%s AND task_id=%s FOR UPDATE",
                (action_id, self.task_id),
            ).fetchone()
            if action is None or action["snapshot"]["params"] != row["input_snapshot"]["values"]:
                raise ValueError("Action does not match node inputs")
            if row["action_id"] not in {None, action["id"]}:
                raise PlanConflict("Node is already bound to another action")
            conn.execute(
                "UPDATE app.business_plan_nodes SET action_id=%s,updated_at=clock_timestamp() "
                "WHERE task_id=%s AND node_id=%s AND execution_generation=%s",
                (action_id, self.task_id, node_id, generation),
            )

    def nodes(self):
        with self.database.connect() as conn:
            self.guard(conn)
            return conn.execute(
                "SELECT * FROM app.business_plan_nodes WHERE task_id=%s "
                "ORDER BY created_at,node_id,execution_generation",
                (self.task_id,),
            ).fetchall()

    def transition(self):
        with self.database.connect() as conn:
            task = self.guard(conn)
            row = conn.execute(
                "UPDATE app.business_plan_heads SET graph_transitions=graph_transitions+1,"
                "updated_at=clock_timestamp() WHERE task_id=%s AND graph_transitions<%s "
                "RETURNING graph_transitions",
                (self.task_id, task["budget"].get("max_graph_transitions", 1024)),
            ).fetchone()
            if row is None:
                raise BudgetExceeded("Conditional graph transition budget exhausted")
            return row["graph_transitions"]

    def save_context(self, snapshot):
        safe = self._safe(snapshot)
        with self.database.connect() as conn:
            self.guard(conn)
            conn.execute(
                "UPDATE app.business_plan_heads SET context_snapshot=context_snapshot||%s,"
                "updated_at=clock_timestamp() WHERE task_id=%s",
                (Jsonb(safe), self.task_id),
            )

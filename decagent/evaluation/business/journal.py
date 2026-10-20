"""Persistent, per-trial SQLite journal for isolated benchmark execution.

The journal implements only the storage port consumed by production
``PlanRuntime``. It is not registered in the application database or exposed
through a production route. A new benchmark run must create a fresh file; an
explicit resume validates the frozen trial, catalog and actor before opening it.
"""

import json
import sqlite3
from math import isfinite
from pathlib import Path
from uuid import uuid4

from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.contracts import Plan
from decagent.business.plans.evaluation_catalog import EvaluationCatalog
from decagent.decisions.contracts import canonical_json, digest

_STATUSES = frozenset({"running", "waiting", "unknown", "succeeded", "failed", "skipped"})


class EvaluationJournalRepository:
    """SQLite-backed plan repository scoped to one frozen benchmark trial."""

    def __init__(self, path, catalog, *, connection, task_id, actor_id, instruction):
        if type(catalog) is not EvaluationCatalog:
            raise TypeError("EvaluationJournalRepository requires EvaluationCatalog")
        catalog.assert_active()
        self.path = Path(path)
        self.spec = catalog
        self.database = None
        self.task_id = task_id
        self.actor_id = actor_id
        self._instruction = instruction
        self._connection = connection
        self._closed = False

    @classmethod
    def create(cls, path, catalog, plan=None, *, actor_id, task_input, input_schema=None):
        if type(catalog) is not EvaluationCatalog:
            raise TypeError("EvaluationJournalRepository requires EvaluationCatalog")
        catalog.assert_active()
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 160:
            raise ValueError("A bounded evaluator actor identity is required")
        if not isinstance(task_input, dict) or not isinstance(task_input.get("instruction"), str):
            raise ValueError("A public benchmark instruction is required")
        task_input = json.loads(canonical_json(task_input))
        if plan is not None:
            plan = plan if isinstance(plan, Plan) else Plan.from_mapping(plan)
            compile_plan(plan, catalog, input_schema=input_schema)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        connection = sqlite3.connect(target, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            cls._initialize(connection)
            task_id = "evaluation-" + uuid4().hex
            repository = cls(
                target,
                catalog,
                connection=connection,
                task_id=task_id,
                actor_id=actor_id,
                instruction=task_input["instruction"],
            )
            with repository._transaction():
                metadata = {
                    "task_id": task_id,
                    "actor_id": actor_id,
                    "instruction": task_input["instruction"],
                    "benchmark": catalog.benchmark,
                    "benchmark_version": catalog.benchmark_version,
                    "case_id": catalog.case_id,
                    "catalog_digest": catalog.digest,
                    "scope_key": repository._scope_key(catalog, actor_id),
                }
                connection.executemany(
                    "INSERT INTO metadata(key,value) VALUES(?,?)",
                    [(key, canonical_json(value)) for key, value in metadata.items()],
                )
                context = {"task_input": task_input}
                if input_schema is not None:
                    context["input_schema"] = json.loads(canonical_json(input_schema))
                connection.execute(
                    "INSERT INTO context_snapshot(id,value) VALUES(1,?)",
                    (canonical_json(context),),
                )
                if plan is not None:
                    repository._insert_plan(plan)
                    repository._event("evaluation.plan_published", {"plan_digest": plan.digest})
                else:
                    repository._event("evaluation.session_created", {})
            return repository
        except BaseException:
            connection.close()
            target.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            raise

    @classmethod
    def resume(cls, path, catalog, *, actor_id):
        if type(catalog) is not EvaluationCatalog:
            raise TypeError("EvaluationJournalRepository requires EvaluationCatalog")
        catalog.assert_active()
        target = Path(path)
        if target.is_symlink() or not target.is_file():
            raise ValueError("Evaluation journal must be an existing regular file")
        connection = sqlite3.connect(target, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            cls._initialize(connection, create=False)
            metadata = {
                row["key"]: json.loads(row["value"])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            expected = cls._scope_key(catalog, actor_id)
            if (
                metadata.get("scope_key") != expected
                or metadata.get("catalog_digest") != catalog.digest
                or metadata.get("benchmark") != catalog.benchmark
                or metadata.get("benchmark_version") != catalog.benchmark_version
                or metadata.get("case_id") != catalog.case_id
                or metadata.get("actor_id") != actor_id
            ):
                raise ValueError("Journal trial, actor or tool catalog does not match")
            repository = cls(
                target,
                catalog,
                connection=connection,
                task_id=metadata["task_id"],
                actor_id=actor_id,
                instruction=metadata["instruction"],
            )
            repository.current()
            return repository
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _scope_key(catalog, actor_id):
        return digest(
            {
                "benchmark": catalog.benchmark,
                "benchmark_version": catalog.benchmark_version,
                "case_id": catalog.case_id,
                "catalog_digest": catalog.digest,
                "actor_id": actor_id,
            }
        )

    @staticmethod
    def _initialize(connection, *, create=True):
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE context_snapshot(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    value TEXT NOT NULL
                );
                CREATE TABLE plan_revisions(
                    revision INTEGER PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL UNIQUE,
                    registry_fingerprint TEXT NOT NULL,
                    definition TEXT NOT NULL
                );
                CREATE TABLE node_runs(
                    node_id TEXT NOT NULL,
                    execution_generation INTEGER NOT NULL,
                    plan_revision INTEGER NOT NULL,
                    node_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    inputs TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    dispatch_attempts INTEGER NOT NULL DEFAULT 0,
                    output_snapshot TEXT NOT NULL,
                    action_id TEXT,
                    PRIMARY KEY(node_id,execution_generation)
                );
                CREATE TABLE external_tool_calls(
                    call_id TEXT PRIMARY KEY,
                    command TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_digest TEXT,
                    result_snapshot TEXT
                );
                CREATE TABLE simulation_trajectory(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    termination_reason TEXT NOT NULL,
                    duration REAL NOT NULL,
                    messages TEXT NOT NULL,
                    content_digest TEXT NOT NULL
                );
                CREATE TABLE events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
            return
        names = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if (
            not {
                "metadata",
                "context_snapshot",
                "plan_revisions",
                "node_runs",
                "external_tool_calls",
                "events",
            }
            <= names
        ):
            raise ValueError("Evaluation journal schema is incomplete")
        call_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(external_tool_calls)")
        }
        if "result_snapshot" not in call_columns:
            raise ValueError("Evaluation journal observation schema is incompatible")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS simulation_trajectory("
            "id INTEGER PRIMARY KEY CHECK(id=1),"
            "termination_reason TEXT NOT NULL,duration REAL NOT NULL,"
            "messages TEXT NOT NULL,content_digest TEXT NOT NULL)"
        )

    def _active(self):
        if self._closed:
            raise ValueError("Evaluation journal is closed")
        self.spec.assert_active()

    def _transaction(self):
        self._active()
        return _Transaction(self._connection)

    def _meta(self, name):
        row = self._connection.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
        if row is None:
            raise ValueError("Evaluation journal metadata is incomplete")
        return json.loads(row["value"])

    def _insert_plan(self, plan):
        self._connection.execute(
            "INSERT INTO plan_revisions(revision,plan_id,content_digest,"
            "registry_fingerprint,definition) "
            "VALUES(?,?,?,?,?)",
            (
                plan.revision,
                plan.plan_id,
                plan.digest,
                plan.registry_fingerprint,
                canonical_json(plan.to_mapping()),
            ),
        )

    def _event(self, kind, payload):
        self._connection.execute(
            "INSERT INTO events(kind,payload) VALUES(?,?)",
            (kind, canonical_json(payload)),
        )

    def record_private_scoring_evidence(self, evidence):
        """Persist bounded digest/count-only EnterpriseOps audit evidence."""
        self._active()
        required = {
            "version",
            "initial_state_fingerprint",
            "post_state_fingerprint",
            "scoring_status",
            "verifier_count",
            "passed_count",
            "verifier_results_digest",
            "model_response_digest",
            "forbidden_side_effects",
            "failure_type",
            "judge_usage",
        }
        if type(evidence) is not dict or set(evidence) != required:
            raise ValueError("Private scoring evidence schema is invalid")
        if (
            evidence["version"] != "enterpriseops-private-score-v1"
            or evidence["scoring_status"] not in {"scored", "scoring_failure"}
            or evidence["forbidden_side_effects"] != "not_measured"
            or type(evidence["verifier_count"]) is not int
            or evidence["verifier_count"] < 0
            or type(evidence["passed_count"]) is not int
            or not 0 <= evidence["passed_count"] <= evidence["verifier_count"]
        ):
            raise ValueError("Private scoring evidence values are invalid")
        for name in ("initial_state_fingerprint", "post_state_fingerprint"):
            fingerprint = evidence[name]
            if fingerprint is None and evidence["scoring_status"] == "scored":
                raise ValueError("Successful scoring needs bounded state fingerprints")
            if fingerprint is not None and (
                type(fingerprint) is not dict
                or set(fingerprint) != {"digest", "table_count", "row_count"}
                or type(fingerprint["digest"]) is not str
                or len(fingerprint["digest"]) != 64
                or any(character not in "0123456789abcdef" for character in fingerprint["digest"])
                or type(fingerprint["table_count"]) is not int
                or not 1 <= fingerprint["table_count"] <= 128
                or type(fingerprint["row_count"]) is not int
                or not 0 <= fingerprint["row_count"] <= 1_280_000
            ):
                raise ValueError("Private scoring state fingerprint is invalid")
        for name in ("verifier_results_digest", "model_response_digest"):
            value = evidence[name]
            if value is not None and (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError("Private scoring digest is invalid")
        if evidence["failure_type"] is not None and (
            type(evidence["failure_type"]) is not str
            or len(evidence["failure_type"]) > 80
        ):
            raise ValueError("Private scoring failure classification is invalid")
        usage = evidence["judge_usage"]
        if (
            type(usage) is not dict
            or set(usage) != {"requests", "tokens", "cost_usd", "seconds", "complete"}
            or type(usage["requests"]) is not int or usage["requests"] < 0
            or type(usage["complete"]) is not bool
            or type(usage["seconds"]) not in {int, float}
            or not isfinite(usage["seconds"]) or usage["seconds"] < 0
        ):
            raise ValueError("Private scoring judge usage is invalid")
        from decimal import Decimal, InvalidOperation

        try:
            cost = Decimal(usage["cost_usd"]) if type(usage["cost_usd"]) is str else None
        except InvalidOperation as exc:
            raise ValueError("Private scoring judge cost is invalid") from exc
        if usage["complete"]:
            if (
                type(usage["tokens"]) is not int or usage["tokens"] < 0
                or cost is None or not cost.is_finite() or cost < 0
            ):
                raise ValueError("Private scoring judge usage is incomplete")
        elif usage["tokens"] is not None or usage["cost_usd"] is not None:
            raise ValueError("Unknown judge usage cannot contain invented totals")
        with self._transaction():
            self._event("evaluation.enterpriseops_private_score", evidence)

    def private_scoring_evidence(self):
        """Return private audit projections; never included in route reports."""
        self._active()
        rows = self._connection.execute(
            "SELECT payload FROM events WHERE kind='evaluation.enterpriseops_private_score' "
            "ORDER BY seq"
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def guard(self, _conn=None):
        self._active()
        return {
            "id": self.task_id,
            "task_id": self.task_id,
            "employee_id": self.actor_id,
            "scope_revision": 1,
            "authorization_revision": 1,
            "authorization_snapshot": {"instruction": self._instruction},
        }

    def instruction(self, _conn=None):
        self._active()
        return self._instruction

    def _safe(self, value):
        self._active()
        return json.loads(canonical_json(value))

    def context_snapshot(self):
        self._active()
        row = self._connection.execute("SELECT value FROM context_snapshot WHERE id=1").fetchone()
        return json.loads(row["value"])

    def save_context(self, snapshot):
        self._active()
        if not isinstance(snapshot, dict):
            raise ValueError("Evaluation context update must be an object")
        with self._transaction():
            current = self.context_snapshot()
            current.update(self._safe(snapshot))
            self._connection.execute(
                "UPDATE context_snapshot SET value=? WHERE id=1", (canonical_json(current),)
            )
            self._event("evaluation.context_saved", {"keys": sorted(snapshot)})

    def current(self):
        self._active()
        row = self._connection.execute(
            "SELECT * FROM plan_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        plan = Plan.from_mapping(json.loads(row["definition"]))
        if (
            plan.digest != row["content_digest"]
            or plan.revision != row["revision"]
            or plan.plan_id != row["plan_id"]
            or plan.registry_fingerprint != row["registry_fingerprint"]
        ):
            raise ValueError("Evaluation journal plan integrity changed")
        return plan

    def publish_plan(self, plan, task_input):
        self._active()
        plan = plan if isinstance(plan, Plan) else Plan.from_mapping(plan)
        if not isinstance(task_input, dict) or digest(task_input) != digest(
            self.context_snapshot().get("task_input")
        ):
            raise ValueError("Evaluation plan cannot replace the frozen task input")
        compile_plan(plan, self.spec, input_schema=self.context_snapshot().get("input_schema"))
        if self.current() is not None:
            raise ValueError("An evaluation trial can publish only one frozen plan")
        with self._transaction():
            self._insert_plan(plan)
            self._event("evaluation.plan_published", {"plan_digest": plan.digest})
        return plan

    def begin_external_call(self, command, inputs):
        self._active()
        self.spec.validate(command, inputs)
        command_spec = self.spec.commands[command]
        if command_spec.effect == "write" and self.has_unknown_external_write():
            raise ValueError("Unknown upstream write cannot be replayed in this trial")
        call_id = str(uuid4())
        with self._transaction():
            self._connection.execute(
                "INSERT INTO external_tool_calls(call_id,command,effect,input_digest,status) "
                "VALUES(?,?,?,?, 'dispatching')",
                (call_id, command, command_spec.effect, digest(inputs)),
            )
            self._event(
                "evaluation.external_tool_dispatch",
                {"call_id": call_id, "command": command, "effect": command_spec.effect},
            )
        return call_id

    def finish_external_call(self, call_id, status, result=None):
        self._active()
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid external tool call status")
        with self._transaction():
            current = self._connection.execute(
                "SELECT command,status FROM external_tool_calls WHERE call_id=?", (call_id,)
            ).fetchone()
            if current is None or current["status"] != "dispatching":
                raise ValueError("External tool call has no active dispatch")
            self._connection.execute(
                "UPDATE external_tool_calls SET status=?,result_digest=?,result_snapshot=? "
                "WHERE call_id=?",
                (
                    status,
                    digest(result) if result is not None else None,
                    canonical_json(result) if result is not None else None,
                    call_id,
                ),
            )
            self._event(
                "evaluation.external_tool_finished",
                {"call_id": call_id, "command": current["command"], "status": status},
            )

    def external_calls(self):
        self._active()
        rows = self._connection.execute(
            "SELECT * FROM external_tool_calls ORDER BY rowid"
        ).fetchall()
        return [
            {
                **dict(row),
                "result_snapshot": (
                    json.loads(row["result_snapshot"])
                    if row["result_snapshot"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def save_simulation_trajectory(self, messages, *, termination_reason, duration):
        """Persist the exact bounded transcript supplied to the original τ scorer."""
        self._active()
        if type(messages) is not list or len(messages) > 2048:
            raise ValueError("Simulation trajectory exceeds its message bound")
        if type(termination_reason) is not str or not termination_reason.strip() or len(
            termination_reason
        ) > 64:
            raise ValueError("Simulation termination reason is invalid")
        if (
            type(duration) not in {int, float}
            or not isfinite(duration)
            or not 0 <= duration <= 1800
        ):
            raise ValueError("Simulation duration is outside the trial bound")
        payload = canonical_json(messages)
        if len(payload.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("Simulation trajectory exceeds its byte bound")
        content_digest = digest(
            {
                "messages": messages,
                "termination_reason": termination_reason,
                "duration": float(duration),
            }
        )
        with self._transaction():
            existing = self._connection.execute(
                "SELECT content_digest FROM simulation_trajectory WHERE id=1"
            ).fetchone()
            if existing is not None:
                if existing["content_digest"] != content_digest:
                    raise ValueError("Simulation trajectory is immutable")
                return
            self._connection.execute(
                "INSERT INTO simulation_trajectory"
                "(id,termination_reason,duration,messages,content_digest) VALUES(1,?,?,?,?)",
                (termination_reason, float(duration), payload, content_digest),
            )
            self._event(
                "evaluation.simulation_trajectory_saved",
                {"content_digest": content_digest, "message_count": len(messages)},
            )

    def simulation_trajectory(self):
        self._active()
        row = self._connection.execute(
            "SELECT termination_reason,duration,messages FROM simulation_trajectory WHERE id=1"
        ).fetchone()
        if row is None:
            return None
        return {
            "messages": json.loads(row["messages"]),
            "termination_reason": row["termination_reason"],
            "duration": row["duration"],
        }

    def has_unknown_external_write(self):
        self._active()
        return (
            self._connection.execute(
                "SELECT 1 FROM external_tool_calls WHERE effect='write' "
                "AND status IN ('dispatching','unknown') LIMIT 1"
            ).fetchone()
            is not None
        )

    def nodes(self):
        self._active()
        rows = self._connection.execute(
            "SELECT * FROM node_runs ORDER BY node_id,execution_generation"
        ).fetchall()
        return [
            {
                **dict(row),
                "inputs": json.loads(row["inputs"]),
                "provenance": json.loads(row["provenance"]),
                "output_snapshot": json.loads(row["output_snapshot"]),
            }
            for row in rows
        ]

    def transition(self):
        self._active()
        with self._transaction():
            self._event("evaluation.runtime_step", {})

    def begin(self, node_id, inputs, provenance, *, generation=None):
        self._active()
        compiled = compile_plan(
            self.current(), self.spec, input_schema=self.context_snapshot().get("input_schema")
        )
        node = compiled.node(node_id)
        current = self._connection.execute(
            "SELECT * FROM node_runs WHERE node_id=? ORDER BY execution_generation DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        if current is not None:
            existing = self._row(current)
            expected_node_digest = digest(compiled.node(node_id))
            if (
                existing["node_digest"] != expected_node_digest
                or existing["input_digest"] != digest(inputs)
                or existing["inputs"] != self._safe(inputs)
                or existing["provenance"] != self._safe(provenance)
                or existing["plan_revision"] != self.current().revision
                or existing["execution_generation"] != (1 if generation is None else generation)
            ):
                raise ValueError("Resumed evaluation node inputs or provenance changed")
            return existing
        chosen = 1 if generation is None else generation
        if type(chosen) is not int or chosen != 1:
            raise ValueError("Isolated evaluation plans do not permit implicit node generations")
        plan = self.current()
        row = {
            "node_id": node_id,
            "execution_generation": chosen,
            "plan_revision": plan.revision,
            "node_digest": digest(node),
            "status": "running",
            "input_digest": digest(inputs),
            "inputs": self._safe(inputs),
            "provenance": self._safe(provenance),
            "dispatch_attempts": 0,
            "output_snapshot": {},
            "action_id": None,
        }
        with self._transaction():
            self._connection.execute(
                "INSERT INTO node_runs(node_id,execution_generation,plan_revision,"
                "node_digest,status,"
                "input_digest,inputs,provenance,dispatch_attempts,output_snapshot,action_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["node_id"],
                    row["execution_generation"],
                    row["plan_revision"],
                    row["node_digest"],
                    row["status"],
                    row["input_digest"],
                    canonical_json(row["inputs"]),
                    canonical_json(row["provenance"]),
                    row["dispatch_attempts"],
                    canonical_json(row["output_snapshot"]),
                    None,
                ),
            )
            self._event(
                "evaluation.node_started", {"node_id": node_id, "input_digest": row["input_digest"]}
            )
        return row

    @staticmethod
    def _row(row):
        return {
            **dict(row),
            "inputs": json.loads(row["inputs"]),
            "provenance": json.loads(row["provenance"]),
            "output_snapshot": json.loads(row["output_snapshot"]),
        }

    def dispatch(self, node_id, *, max_attempts=1, generation=1):
        self._active()
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("A positive dispatch limit is required")
        compiled = compile_plan(
            self.current(), self.spec, input_schema=self.context_snapshot().get("input_schema")
        )
        node = compiled.node(node_id)
        row = self._connection.execute(
            "SELECT * FROM node_runs WHERE node_id=? AND execution_generation=?",
            (node_id, generation),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ValueError("Only a newly running evaluation node can be dispatched")
        if node["type"] == "governed_write" and row["dispatch_attempts"]:
            raise ValueError("Unknown or prior write dispatch cannot be replayed")
        if row["dispatch_attempts"] >= max_attempts:
            raise ValueError("Evaluation tool dispatch limit reached")
        with self._transaction():
            self._connection.execute(
                "UPDATE node_runs SET dispatch_attempts=dispatch_attempts+1 "
                "WHERE node_id=? AND execution_generation=?",
                (node_id, generation),
            )
            self._event(
                "evaluation.tool_dispatch", {"node_id": node_id, "command": self._command(node)}
            )

    @staticmethod
    def _command(node):
        field = "tool" if node["type"] == "read_tool" else "command"
        return node.get(field, {}).get("command")

    def finish(self, node_id, status, output=None, *, generation=1):
        self._active()
        if status not in _STATUSES:
            raise ValueError("Invalid evaluation node status")
        encoded = canonical_json(output or {})
        with self._transaction():
            current = self._connection.execute(
                "SELECT status FROM node_runs WHERE node_id=? AND execution_generation=?",
                (node_id, generation),
            ).fetchone()
            if current is None or current["status"] != "running":
                raise ValueError("Evaluation node finish requires a running node")
            self._connection.execute(
                "UPDATE node_runs SET status=?,output_snapshot=? "
                "WHERE node_id=? AND execution_generation=?",
                (status, encoded, node_id, generation),
            )
            self._event("evaluation.node_finished", {"node_id": node_id, "status": status})

    def close(self):
        if self._closed:
            return
        self._connection.close()
        self._closed = True


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exception_type, exception, traceback):
        self.connection.execute("ROLLBACK" if exception_type else "COMMIT")
        return False

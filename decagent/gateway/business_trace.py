"""Durable employee-only plan inspection and allowlisted, correlated trace exports.

ReadOnlyPlanSource intentionally does not inherit the execution repository. A
browser can inspect a stopped task, but never acquire a worker lease or publish
state through this adapter. Every response is rebuilt in one read-only snapshot.
"""

from contextlib import nullcontext
from decimal import Decimal
from uuid import UUID

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from decagent.business.access import AccessError
from decagent.business.context import TaskWorkingMemory
from decagent.business.contracts import PluginSpec
from decagent.business.operations import OperationService
from decagent.business.plans.contracts import Plan
from decagent.storage.usage import CONDITIONAL_MODEL_DEFAULTS, UsageLedger


class _ReadDatabase:
    def __init__(self, database, connection):
        self.redactor, self.connection = database.redactor, connection

    def connect(self):
        return nullcontext(self.connection)


class ReadOnlyPlanSource:
    """The minimal read protocol consumed by TaskWorkingMemory.rebuild(False)."""

    def __init__(self, database, task_id, identity):
        self.database, self.task_id, self.identity = database, task_id, identity
        with database.connect() as conn:
            task = self.guard(conn)
            scope = conn.execute(
                "SELECT manifest FROM app.business_scopes WHERE id=%s",
                (task["business_scope_id"],),
            ).fetchone()
            self.spec = PluginSpec.from_mapping(scope["manifest"])
            if self.spec.digest != task["authorization_snapshot"].get("plugin_digest"):
                raise AccessError("contract_changed", "Current registered contract required")

    def guard(self, conn):
        actor = OperationService(self.database)._identity(conn, self.identity, browser_only=True)
        task = conn.execute(
            "SELECT t.*,s.revision AS scope_revision,s.manifest_digest,s.enabled "
            "FROM app.tasks t JOIN app.business_scopes s ON s.id=t.business_scope_id "
            "WHERE t.id=%s AND t.kind='business' AND t.employee_id=%s",
            (self.task_id, self.identity.employee_id),
        ).fetchone()
        if task is None:
            raise AccessError("not_found", "Original employee task unavailable")
        if not task["enabled"] or task["manifest_digest"] != task["authorization_snapshot"].get(
            "plugin_digest"
        ):
            raise AccessError("contract_changed", "Current registered business scope required")
        return {**task, "authorization_revision": actor["credential_revision"]}

    def _load(self, conn):
        saved = conn.execute(
            "SELECT r.* FROM app.business_plan_heads h JOIN app.business_plan_revisions r "
            "ON r.task_id=h.task_id AND r.revision=h.current_revision WHERE h.task_id=%s",
            (self.task_id,),
        ).fetchone()
        if saved is None:
            return None
        plan = Plan.from_mapping(saved["definition"])
        if any(
            (
                plan.digest != saved["content_digest"],
                plan.revision != saved["revision"],
                plan.plan_id != saved["plan_id"],
                plan.registry_fingerprint != saved["registry_fingerprint"],
            )
        ):
            raise AccessError("version_changed", "Stored plan integrity changed")
        return plan


def _pick(value, keys):
    """Copy named DTO fields only; never recursively copy provider/event payloads."""
    return {name: value[name] for name in keys if name in value}


def _usage(rows, task):
    requests = []
    for row in rows:
        sent = row["dispatch_state"] in {"dispatching", "responded", "unknown"}
        held_tokens, held_cost = UsageLedger._held_usage([row])
        requests.append(
            {
                **_pick(
                    row,
                    (
                        "id",
                        "provider",
                        "model_family",
                        "purpose",
                        "plan_revision",
                        "node_id",
                        "node_ids",
                        "question_count",
                        "exception_reason",
                        "status",
                        "dispatch_state",
                        "error_code",
                        "model_budget_version",
                        "requested_tokens",
                        "actual_tokens",
                        "created_at",
                    ),
                ),
                "id": str(row["id"]),
                "actual_request": sent,
                "actual_tokens": row["actual_tokens"],
                "actual_cost": str(row["actual_cost"]) if row["actual_cost"] is not None else None,
                "reserved_cost": str(row["reserved_cost"])
                if row["reserved_cost"] is not None
                else None,
                "held_tokens": held_tokens,
                "held_cost": str(held_cost)
                if row["reserved_cost"] is not None or row["actual_cost"] is not None
                else None,
                "unknown_tokens": row["actual_tokens"] is None,
                "unknown_cost": row["actual_cost"] is None,
            }
        )

    def total(selected):
        unknown_tokens = any(row["unknown_tokens"] for row in selected)
        unknown_cost = any(row["unknown_cost"] for row in selected)
        return {
            "actual_requests": sum(row["actual_request"] for row in selected),
            "reservations": len(selected),
            "unknown_requests": sum(
                row["dispatch_state"] in {"dispatching", "unknown"} for row in selected
            ),
            "actual_tokens": None
            if unknown_tokens
            else sum(row["actual_tokens"] for row in selected),
            "actual_cost": None
            if unknown_cost
            else str(sum((Decimal(row["actual_cost"]) for row in selected), Decimal("0"))),
            "known_tokens": sum(row["actual_tokens"] or 0 for row in selected),
            "known_cost": str(
                sum(
                    (
                        Decimal(row["actual_cost"])
                        for row in selected
                        if row["actual_cost"] is not None
                    ),
                    Decimal("0"),
                )
            ),
            "held_tokens": sum(row["held_tokens"] for row in selected),
            "held_cost": None
            if any(row["held_cost"] is None for row in selected)
            else str(sum((Decimal(row["held_cost"]) for row in selected), Decimal("0"))),
            "unknown_tokens": unknown_tokens,
            "unknown_cost": unknown_cost,
        }

    limits = (
        {**CONDITIONAL_MODEL_DEFAULTS, **task["budget"]}
        if task["graph_version"] == "business-3"
        else task["budget"]
    )
    return {
        **total(requests),
        "limits": limits,
        "families": {
            family: total([row for row in requests if row["model_family"] == family])
            for family in ("primary", "jev")
        },
        "remaining_primary_requests": max(
            0,
            limits.get("max_primary_calls", 0)
            - sum(
                row["model_family"] == "primary" and row["dispatch_state"] != "not_sent"
                for row in requests
            ),
        ),
        "requests": requests,
    }


_LINKS = (
    "plan_id",
    "plan_digest",
    "parent_digest",
    "parent_revision",
    "node_id",
    "node_ids",
    "batch_id",
    "approval_id",
    "action_id",
    "operation_id",
    "request_id",
    "request_key",
    "policy_id",
    "policy_version",
    "binding_digest",
    "input_digest",
    "context_digest",
)
_DETAILS = (
    "phase",
    "status",
    "outcome",
    "purpose",
    "reason",
    "category",
    "error_code",
    "error_type",
    "repaired",
    "generation",
    "execution_generation",
    "effective_threshold",
    "threshold",
    "effective_revision",
    "effective_digest",
    "evidence_nodes",
)


def _event_item(row, task_id):
    payload = row["payload"]

    # A familiar metadata key is not permission to export a nested transport body.
    def metadata(keys):
        return {
            key: value
            for key, value in _pick(payload, keys).items()
            if value is None
            or type(value) in (str, int, float, bool)
            or key in {"node_ids", "evidence_nodes"}
            and isinstance(value, list)
            and all(type(item) is str for item in value)
        }

    links = {"task_id": str(task_id), **metadata(_LINKS)}
    if "target_revision" in payload or "plan_revision" in payload or "revision" in payload:
        revision = payload.get(
            "target_revision", payload.get("plan_revision", payload.get("revision"))
        )
        if type(revision) is int:
            links["plan_revision"] = revision
    # Planner journals bind the exact reservation ids. No plan/model body is copied.
    if isinstance(payload.get("usage"), list):
        links["model_request_ids"] = [
            item["id"]
            for item in payload["usage"]
            if isinstance(item, dict) and type(item.get("id")) is str
        ]
    return {
        "id": "event:" + str(row["seq"]),
        "kind": row["kind"],
        "created_at": row["created_at"],
        "seq": row["seq"],
        "links": links,
        "details": metadata(_DETAILS),
    }


class BusinessTraceControl:
    def __init__(self, database):
        self.database = database

    def _read(self, identity, task_id, *, timeline=False):
        with self.database.connect() as conn:
            # A query cannot mutate even if a future projection accidentally writes.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            source = ReadOnlyPlanSource(
                _ReadDatabase(self.database, conn), UUID(str(task_id)), identity
            )
            task = source.guard(conn)
            legacy = task["graph_version"] != "business-3"
            state = (
                {
                    "task_id": str(task_id),
                    "plan": None,
                    "nodes": [],
                    "decisions": {},
                    "actions": [],
                    "approvals": [],
                    "through_event_seq": task["event_seq"],
                }
                if legacy
                else TaskWorkingMemory(source).rebuild(persist=False).to_mapping()
            )
            rows = conn.execute(
                "SELECT * FROM app.usage_records WHERE task_id=%s ORDER BY created_at,id",
                (task_id,),
            ).fetchall()
            state.update(
                legacy=legacy,
                task=_pick(
                    task, ("id", "graph_version", "state", "phase", "event_seq", "created_at")
                ),
                usage=_usage(rows, task),
                plan_revisions=conn.execute(
                    "SELECT revision,plan_id,content_digest,parent_revision,"
                    "parent_digest,created_at "
                    "FROM app.business_plan_revisions WHERE task_id=%s ORDER BY revision",
                    (task_id,),
                ).fetchall(),
                threshold_explanation={
                    "certainty": "Jev 对本次答案的把握程度，不代表判断已被证明正确。",
                    "accuracy": "在独立验证数据集上观察到的实际正确比例。",
                    "confidence_interval": (
                        "验证集实测正确率的统计不确定范围，不代表单次答案把握度。"
                    ),
                },
            )
            state["policies"] = self._policies(conn, task_id)
            state["waiting"] = self._waiting(conn, task, state)
            entries = self._timeline(conn, task, state) if timeline else []
            return state, entries

    @staticmethod
    def _policies(conn, task_id):
        # Only task-bound policy versions and their report references are visible.
        rows = conn.execute(
            "SELECT p.policy_id,p.policy_version,p.status,p.minimum_threshold,p.validation_record,"
            "b.effective_revision,b.effective_digest,b.configuration "
            "FROM app.task_decision_policy_bindings b JOIN app.decision_policies p "
            "ON p.policy_id=b.policy_id AND p.policy_version=b.policy_version WHERE b.task_id=%s",
            (task_id,),
        ).fetchall()
        return [
            {
                **_pick(
                    row,
                    (
                        "policy_id",
                        "policy_version",
                        "status",
                        "minimum_threshold",
                        "validation_record",
                        "effective_revision",
                        "effective_digest",
                    ),
                ),
                **_pick(
                    row["configuration"],
                    (
                        "effective_threshold",
                        "task_threshold",
                        "override_revision",
                        "reason",
                        "allowed_thresholds",
                    ),
                ),
            }
            for row in rows
        ]

    @staticmethod
    def _waiting(conn, task, state):
        if task["state"] not in {"awaiting_input", "awaiting_approval", "paused_budget"}:
            return None
        row = conn.execute(
            "SELECT seq,kind,payload,created_at FROM app.events WHERE task_id=%s "
            "AND (kind LIKE '%%wait%%' OR kind LIKE '%%interrupt%%' OR kind LIKE '%%pause%%') "
            "ORDER BY seq DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        return _event_item(row, task["id"]) if row else None

    @staticmethod
    def _timeline(conn, task, state):
        task_id = task["id"]
        entries = [
            _event_item(row, task_id)
            for row in conn.execute(
                "SELECT seq,kind,payload,created_at FROM app.events WHERE task_id=%s ORDER BY seq",
                (task_id,),
            ).fetchall()
        ]
        definitions = {node["id"]: node for node in (state["plan"] or {}).get("nodes", [])}
        requests = state["usage"]["requests"]
        for entry in entries:
            links = entry["links"]
            if entry["kind"] == "business.decision_batch":
                links["model_request_ids"] = [
                    request["id"]
                    for request in requests
                    if request["model_family"] == "jev"
                    and request["plan_revision"] == links.get("plan_revision")
                    and set(request.get("node_ids") or []) == set(links.get("node_ids") or [])
                ]
        for revision in state["plan_revisions"]:
            entries.append(
                {
                    "id": "plan:" + str(revision["revision"]),
                    "kind": "plan.revision",
                    "created_at": revision["created_at"],
                    "links": {
                        "task_id": str(task_id),
                        "plan_revision": revision["revision"],
                        **_pick(
                            revision,
                            ("plan_id", "content_digest", "parent_revision", "parent_digest"),
                        ),
                    },
                    "details": {},
                }
            )
        for request in state["usage"]["requests"]:
            entries.append(
                {
                    "id": "model:" + request["id"],
                    "kind": "model.request",
                    "created_at": request["created_at"],
                    "links": {
                        "task_id": str(task_id),
                        "model_request_ids": [request["id"]],
                        **_pick(request, ("plan_revision", "node_id", "node_ids")),
                    },
                    "details": _pick(
                        request,
                        (
                            "model_family",
                            "purpose",
                            "status",
                            "dispatch_state",
                            "actual_request",
                            "actual_tokens",
                            "actual_cost",
                            "held_tokens",
                            "held_cost",
                            "unknown_tokens",
                            "unknown_cost",
                            "exception_reason",
                            "error_code",
                        ),
                    ),
                }
            )
        # Saved node status and action journals determine completion, never log prose.
        projected = {(row["node_id"], row["generation"]): row for row in state["nodes"]}
        for row in conn.execute(
            "SELECT node_id,execution_generation,plan_revision,status,action_id,updated_at "
            "FROM app.business_plan_nodes WHERE task_id=%s ORDER BY updated_at,node_id",
            (task_id,),
        ).fetchall():
            fact = projected.get((row["node_id"], row["execution_generation"]), {})
            policy = (
                definitions.get(row["node_id"], {}).get("policy", {}) if fact.get("active") else {}
            )
            entries.append(
                {
                    "id": f"node:{row['node_id']}:{row['execution_generation']}",
                    "kind": "node.state",
                    "created_at": row["updated_at"],
                    "links": {
                        "task_id": str(task_id),
                        **_pick(
                            row, ("node_id", "execution_generation", "plan_revision", "action_id")
                        ),
                        "policy_id": policy.get("policy_id"),
                        "policy_version": policy.get("version"),
                    },
                    "details": {
                        "status": row["status"],
                        **_pick(fact, ("active", "activated", "superseded", "source_ref")),
                    },
                }
            )
        facts = {row["action_id"]: row for row in state["actions"]}
        for row in conn.execute(
            "SELECT id,status,server_operation_id,created_at FROM app.actions "
            "WHERE task_id=%s ORDER BY created_at,id",
            (task_id,),
        ).fetchall():
            fact = facts.get(str(row["id"]), {})
            entries.append(
                {
                    "id": "action:" + str(row["id"]),
                    "kind": "action.state",
                    "created_at": row["created_at"],
                    "links": {
                        "task_id": str(task_id),
                        "action_id": str(row["id"]),
                        "operation_id": row["server_operation_id"],
                        **_pick(fact, ("request_id", "receipt_ref")),
                    },
                    "details": {"status": row["status"], "verified": fact.get("verified", False)},
                }
            )
        for row in conn.execute(
            "SELECT id,request_id,state,input_hash,supersedes,agent_action_id,updated_at "
            "FROM business.operations WHERE employee_id=%s AND (agent_task_id=%s OR id IN "
            "(SELECT server_operation_id FROM app.actions WHERE task_id=%s)) "
            "ORDER BY updated_at,id",
            (task["employee_id"], task_id, task_id),
        ).fetchall():
            entries.append(
                {
                    "id": "operation:" + row["id"],
                    "kind": "operation.state",
                    "created_at": row["updated_at"],
                    "links": {
                        "task_id": str(task_id),
                        "operation_id": row["id"],
                        "action_id": row["agent_action_id"],
                        **_pick(row, ("request_id", "input_hash", "supersedes")),
                    },
                    "details": {
                        "state": row["state"],
                        "requires_verification": row["state"] == "unknown",
                    },
                }
            )
        for row in conn.execute(
            "SELECT a.id,a.action_id,a.kind,a.status,a.reply,a.created_at,a.replied_at,"
            "i.node_id,i.plan_revision,i.plan_digest,i.binding_digest,i.execution_generation "
            "FROM app.approvals a LEFT JOIN app.business_decision_interventions i "
            "ON i.approval_id=a.id AND i.task_id=a.task_id "
            "WHERE a.task_id=%s ORDER BY a.created_at,a.id",
            (task_id,),
        ).fetchall():
            entries.append(
                {
                    "id": "approval:" + str(row["id"]),
                    "kind": "approval.state",
                    "created_at": row["replied_at"] or row["created_at"],
                    "links": {
                        "task_id": str(task_id),
                        "approval_id": str(row["id"]),
                        "action_id": row["action_id"],
                        **_pick(
                            row,
                            (
                                "node_id",
                                "plan_revision",
                                "plan_digest",
                                "binding_digest",
                                "execution_generation",
                            ),
                        ),
                    },
                    "details": _pick(row, ("kind", "status", "reply")),
                }
            )
        for row in conn.execute(
            "SELECT id,kind,policy_id,policy_version,payload,created_at "
            "FROM app.decision_policy_events WHERE task_id=%s ORDER BY id",
            (task_id,),
        ).fetchall():
            item = _event_item({**row, "seq": row["id"]}, task_id)
            item["id"] = "policy:" + str(row["id"])
            item["links"].update(_pick(row, ("policy_id", "policy_version")))
            entries.append(item)
        entries.sort(key=lambda item: (item["created_at"], item.get("seq", 0), item["id"]))
        return entries

    def snapshot(self, identity, task_id):
        return self._read(identity, task_id)[0]

    def trace(self, identity, task_id):
        state, timeline = self._read(identity, task_id, timeline=True)
        return {
            "task_id": str(task_id),
            "through_event_seq": state["through_event_seq"],
            "legacy": state["legacy"],
            "timeline": timeline,
            "usage": state["usage"],
        }

    def export(self, identity, task_id):
        state, timeline = self._read(identity, task_id, timeline=True)
        return {
            "format": "decagent.business-trace.v1",
            "task_id": str(task_id),
            "through_event_seq": state["through_event_seq"],
            "snapshot": state,
            "timeline": timeline,
        }


def install_business_trace_routes(app, database, redactor):
    control = BusinessTraceControl(database)
    bounded = type(redactor)(
        known_secrets=redactor.known_secrets, max_text_chars=1_048_576, max_items=1000, max_depth=32
    )

    def safe(value):
        forbidden = {
            "headers",
            "httpheaders",
            "requestheaders",
            "responseheaders",
            "sdkobject",
            "rawapipayload",
            "rawrequest",
            "rawresponse",
            "credentialref",
            "tokenhash",
        }

        def business_fields(item):
            if isinstance(item, dict):
                return {
                    key: business_fields(child)
                    for key, child in item.items()
                    if "".join(char for char in key.lower() if char.isalnum()) not in forbidden
                }
            if isinstance(item, list):
                return [business_fields(child) for child in item]
            return item

        encoded = business_fields(jsonable_encoder(value))

        def check(item, depth=0):
            if depth >= 32 or isinstance(item, (list, dict)) and len(item) > 1000:
                raise AccessError("trace_too_large", "Trace exceeds the protected export bounds")
            for child in (
                item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
            ):
                check(child, depth + 1)

        check(encoded)
        return bounded.sanitize(encoded, surface="ui")

    @app.get("/api/v2/business/tasks/{task_id}/plan")
    def plan(task_id: UUID, request: Request):
        return JSONResponse(
            safe(control.snapshot(request.state.employee, task_id)),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v2/business/tasks/{task_id}/trace")
    def trace(task_id: UUID, request: Request):
        return JSONResponse(
            safe(control.trace(request.state.employee, task_id)),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v2/business/tasks/{task_id}/export")
    def export(task_id: UUID, request: Request):
        return JSONResponse(
            safe(control.export(request.state.employee, task_id)),
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="business-trace-{task_id}.json"',
            },
        )

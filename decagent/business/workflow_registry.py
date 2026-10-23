"""Owner-published, immutable conditional workflows with fresh employee bindings.

Regression reports attest executable branch coverage, never model quality. Jev
quality remains the responsibility of separately published decision policies.
Nothing in this module executes a tool, approves a write, or publishes on success.
"""

import json
import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from decagent.business import access
from decagent.business.access import AccessError
from decagent.business.contracts import PluginSpec, _schema_check, validate_schema
from decagent.business.mcp_contract import FORBIDDEN_INPUT_KEYS
from decagent.business.memory_slots import memory_slot_from_binding
from decagent.business.operations import OperationService
from decagent.business.plans.bindings import BindingContext
from decagent.business.plans.compiler import compile_plan
from decagent.business.plans.contracts import Plan
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.policy_store import DecisionPolicyStore
from decagent.decisions.contracts import canonical_json, digest

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_AUTHORITY = FORBIDDEN_INPUT_KEYS | {
    "operationid",
    "actionid",
    "approvalid",
    "approvaltoken",
    "confirmationtoken",
    "requestid",
    "requestkey",
    "inputhash",
    "evidenceid",
    "memoryid",
}
_FIELDS = {
    "workflow_id",
    "version",
    "goal_id",
    "scope_id",
    "scope_revision",
    "plan",
    "input_schema",
    "applicability",
    "policy_dependencies",
}
_DERIVED = {"tool_fingerprint", "question_fingerprint"}


def _fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("Unexpected workflow fields")


def _text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _no_history(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(char for char in key.lower() if char.isalnum())
            if normalized in _AUTHORITY:
                raise ValueError("Workflow cannot contain historical authority or identity")
            if key in {"default", "examples"}:
                raise ValueError("Workflow input schema cannot embed historical defaults")
            _no_history(child)
    elif isinstance(value, list):
        for child in value:
            _no_history(child)
    elif isinstance(value, str) and _UUID.search(value):
        raise ValueError("Workflow cannot contain historical resource or user identifiers")


def _validate_memory_slots(value):
    if isinstance(value, dict):
        if value.get("source") == "verified_memory":
            try:
                memory_slot_from_binding(value)
            except (KeyError, ValueError) as exc:
                message = "Workflow memory binding requires a registered stable slot"
                raise ValueError(message) from exc
            return
        for child in value.values():
            _validate_memory_slots(child)
    elif isinstance(value, list):
        for child in value:
            _validate_memory_slots(child)


def validate_definition(value, spec):
    """Compile exactly the runtime Plan schema, deriving pinned registry fingerprints."""
    if type(value) is not dict or set(value) not in (_FIELDS, _FIELDS | _DERIVED):
        raise ValueError("Unexpected workflow definition fields")
    # Canonical JSON rejects nonfinite/non-JSON objects and snapshots caller data.
    result = json.loads(canonical_json(value))
    if len(canonical_json(result).encode()) > 196608:
        raise ValueError("Workflow definition exceeds its bounded metadata size")
    if (
        not all(
            isinstance(result[key], str) and _ID.fullmatch(result[key])
            for key in ("workflow_id", "goal_id")
        )
        or not _text(result["version"])
        or not isinstance(result["scope_id"], str)
        or str(UUID(result["scope_id"])) != result["scope_id"]
        or type(result["scope_revision"]) is not int
        or result["scope_revision"] < 1
    ):
        raise ValueError("Workflow identity and current scope revision are required")
    for key in ("plan", "input_schema", "applicability"):
        _no_history(result[key])
    schema = result["input_schema"]
    _schema_check(schema, closed=True)
    if schema.get("type") != "object":
        raise ValueError("Workflow input schema must be a closed structured object")
    applicability = result["applicability"]
    _fields(applicability, {"roles", "input_equals"})
    roles = applicability["roles"]
    if (
        not isinstance(roles, list)
        or not roles
        or len(roles) != len(set(roles))
        or set(roles) - {"employee", "handler", "supervisor"}
        or type(applicability["input_equals"]) is not dict
        or set(applicability["input_equals"]) - set(schema.get("properties", {}))
    ):
        raise ValueError("Workflow applicability requires explicit registered roles and inputs")
    plan = Plan.from_mapping(result["plan"])
    _validate_memory_slots(plan.to_mapping())
    for node in plan.nodes:
        if node["type"] in {"read_tool", "governed_write"}:
            for name, binding in node.get("inputs", {}).items():
                if binding["source"] == "literal" and (
                    name.endswith("_id")
                    or name in {"expected_revision", "title", "body", "description", "resolution"}
                ):
                    raise ValueError("Business values require fresh parameter slots")
                if name == "expected_revision" and binding["source"] != "node_output":
                    raise ValueError("Resource revision requires a fresh read parameter slot")
    compiled = compile_plan(plan, spec, input_schema=schema)
    policies = result["policy_dependencies"]
    if not isinstance(policies, list) or len(policies) > 32:
        raise ValueError("Workflow requires bounded policy dependencies")
    seen = set()
    for policy in policies:
        _fields(policy, {"policy_id", "policy_version", "definition_digest"})
        key = policy["policy_id"], policy["policy_version"]
        if (
            not all(_text(item) for item in key)
            or key in seen
            or not isinstance(policy["definition_digest"], str)
            or not _HASH.fullmatch(policy["definition_digest"])
        ):
            raise ValueError("Policy dependencies must be unique pinned versions")
        seen.add(key)
    required = {
        (node["policy"]["policy_id"], node["policy"]["version"])
        for node in compiled.plan.nodes
        if node["type"] == "decide"
    }
    if required != seen:
        raise ValueError("Every Jev node requires its exact policy dependency")
    result["plan"] = compiled.plan.to_mapping()
    derived = {
        "tool_fingerprint": spec.digest,
        "question_fingerprint": QuestionRegistry.from_spec(spec).fingerprint,
    }
    if any(key in result and result[key] != item for key, item in derived.items()):
        raise ValueError("Workflow registry fingerprint changed")
    return {**result, **derived}


def validate_workflow_report(report, expected_sha256, definition):
    """Check bounded owner-attested regression coverage without making quality claims."""
    _fields(
        report,
        {
            "format",
            "report_id",
            "reference",
            "definition_digest",
            "synthetic",
            "quality_claim",
            "cases",
            "covered_nodes",
            "covered_edges",
        },
    )
    if (
        not isinstance(expected_sha256, str)
        or not _HASH.fullmatch(expected_sha256)
        or digest(report) != expected_sha256
        or report["format"] != "decagent.workflow-regression.v1"
        or not _text(report["report_id"])
        or not _text(report["reference"], 512)
        or report["definition_digest"] != digest(definition)
        or type(report["synthetic"]) is not bool
        or report["quality_claim"] is not False
    ):
        raise ValueError("Workflow regression report digest, binding or quality claim is invalid")
    cases = report["cases"]
    if not isinstance(cases, list) or not 4 <= len(cases) <= 512:
        raise ValueError("Workflow validation requires representative success and failure cases")
    names, categories = set(), set()
    for case in cases:
        _fields(case, {"case_id", "category", "passed"})
        if not _text(case["case_id"]) or case["case_id"] in names or case["passed"] is not True:
            raise ValueError("Workflow regression cases must be unique and passing")
        if case["category"] not in {
            "success",
            "missing_input",
            "permission_denied",
            "tool_failure",
            "branch",
            "no_match",
            "unknown_receipt",
        }:
            raise ValueError("Unknown workflow regression category")
        names.add(case["case_id"])
        categories.add(case["category"])
    if not {"success", "missing_input", "permission_denied", "tool_failure"} <= categories:
        raise ValueError("Workflow regression report lacks required failure coverage")
    nodes = definition["plan"]["nodes"]
    edges = set()
    for node in nodes:
        targets = list(node.get("next", []))
        targets += [target for case in node.get("cases", []) for target in case["next"]]
        targets += [
            node[key]["node_id"]
            for key in ("on_missing", "on_error", "on_no_match")
            if node.get(key, {}).get("node_id")
        ]
        edges.update((node["id"], target) for target in targets)
    covered_nodes, covered_edges = report["covered_nodes"], report["covered_edges"]
    if (
        not isinstance(covered_nodes, list)
        or any(not isinstance(item, str) for item in covered_nodes)
        or len(covered_nodes) != len(set(covered_nodes))
        or set(covered_nodes) != {node["id"] for node in nodes}
        or not isinstance(covered_edges, list)
        or any(
            not isinstance(edge, list)
            or len(edge) != 2
            or any(not isinstance(item, str) for item in edge)
            for edge in covered_edges
        )
        or len(covered_edges) != len(set(map(tuple, covered_edges)))
        or set(map(tuple, covered_edges)) != edges
    ):
        raise ValueError("Workflow validation requires complete declared node and branch coverage")
    result = json.loads(canonical_json(report))
    if len(canonical_json(result).encode()) > 262144:
        raise ValueError("Workflow validation report is too large")
    return result


@dataclass(frozen=True)
class WorkflowResolution:
    reason: str
    candidates: tuple = ()
    missing_fields: tuple = ()
    workflow: dict | None = None

    @property
    def matched(self):
        return self.reason == "matched" and self.workflow is not None

    def to_dict(self):
        return {
            "reason": self.reason,
            "matched": self.matched,
            "candidates": list(self.candidates),
            "missing_fields": list(self.missing_fields),
            "workflow": self.workflow,
        }


@dataclass(frozen=True)
class WorkflowInstance:
    task_id: str
    instance_id: str
    workflow_id: str
    version: str
    definition_digest: str
    report_digest: str
    plan: Plan
    employee_id: str
    scope_revision: int
    account_revision: int
    _task_input: str

    @property
    def task_input(self):
        return json.loads(self._task_input)

    @property
    def binding_context(self):
        # Every caller starts with its own empty runtime sources. The interpreter
        # must perform new reads and accept new decisions under its normal guards.
        return BindingContext(
            self.task_input,
            {},
            {},
            {},
            self.employee_id,
            self.scope_revision,
            self.account_revision,
        )


def _instance(row):
    return WorkflowInstance(
        str(row["task_id"]),
        str(row["instance_id"]),
        row["workflow_id"],
        row["version"],
        row["definition_digest"],
        row["report_digest"],
        Plan.from_mapping(row["plan"]),
        str(row["employee_id"]),
        row["scope_revision"],
        row["account_revision"],
        canonical_json(row["task_input"]),
    )


class WorkflowRegistry:
    def __init__(self, database):
        self.database = database
        self.policies = DecisionPolicyStore(database)
        self.operations = OperationService(database)

    def _scope(self, conn, scope_id):
        scope = conn.execute(
            "SELECT * FROM app.business_scopes WHERE id=%s FOR SHARE", (scope_id,)
        ).fetchone()
        if not scope or not scope["enabled"]:
            raise AccessError("scope_unavailable", "Business workflow scope unavailable")
        spec = PluginSpec.from_mapping(scope["manifest"])
        if (
            spec.digest != scope["manifest_digest"]
            or spec.id != scope["plugin_id"]
            or spec.version != scope["plugin_version"]
        ):
            raise AccessError("scope_changed", "Business workflow contract integrity changed")
        return scope, spec

    def _row(self, conn, workflow_id, version, *, lock="SHARE"):
        return conn.execute(
            "SELECT * FROM app.business_workflows WHERE workflow_id=%s AND version=%s FOR " + lock,
            (workflow_id, version),
        ).fetchone()

    def _event(self, conn, row, kind, actor_kind, actor_id, payload, task_id=None):
        conn.execute(
            "INSERT INTO app.workflow_events"
            "(workflow_id,version,task_id,kind,actor_kind,actor_id,payload) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                row["workflow_id"],
                row["version"],
                task_id,
                kind,
                actor_kind,
                str(actor_id),
                Jsonb(self.database.redactor.sanitize(payload, surface="ui")),
            ),
        )

    def register(self, owner, definition):
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            scope, spec = self._scope(conn, definition.get("scope_id"))
            value = validate_definition(definition, spec)
            if value["scope_revision"] != scope["revision"]:
                raise ValueError("Workflow scope revision changed")
            fingerprint = digest(value)
            row = conn.execute(
                "INSERT INTO app.business_workflows(workflow_id,version,goal_id,scope_id,"
                "scope_revision,definition,definition_digest,registered_by) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workflow_id,version) "
                "DO NOTHING RETURNING *",
                (
                    value["workflow_id"],
                    value["version"],
                    value["goal_id"],
                    value["scope_id"],
                    value["scope_revision"],
                    Jsonb(value),
                    fingerprint,
                    actor,
                ),
            ).fetchone()
            if row:
                self._event(
                    conn,
                    row,
                    "workflow.draft_registered",
                    "owner",
                    actor,
                    {"definition_digest": fingerprint},
                )
                return row
            row = self._row(conn, value["workflow_id"], value["version"])
            if row["definition_digest"] != fingerprint:
                raise ValueError("Workflow version already has different immutable content")
            return row

    def _dependencies(self, conn, definition, spec):
        definitions = {}
        for dependency in definition["policy_dependencies"]:
            row = self.policies._row(conn, dependency["policy_id"], dependency["policy_version"])
            if (
                not row
                or row["definition_digest"] != dependency["definition_digest"]
                or self.policies._available(conn, row) != "policy_available"
            ):
                return "policy_changed"
            definitions[dependency["policy_id"], dependency["policy_version"]] = row["definition"]
        questions = QuestionRegistry.from_spec(spec)
        for node in definition["plan"]["nodes"]:
            if node["type"] != "decide":
                continue
            policy = definitions[node["policy"]["policy_id"], node["policy"]["version"]]
            question = questions.validate_reference(node["question"])
            expected_source = question["candidate_source"] or "workflow." + question["question_id"]
            if (
                policy["question_id"] != question["question_id"]
                or policy["question_version"] != question["version"]
                or policy["primitive"] != question["type"]
                or policy["context"]["plugin_digest"] != spec.digest
                or policy["context"]["workflow_version"] != spec.manifest["workflow"]["version"]
                or policy["context"]["candidate_source"] != expected_source
            ):
                return "policy_changed"
        return None

    def _current(self, conn, row, scope, spec, *, publication=False):
        if row is None or (not publication and row["status"] in {"draft", "validated"}):
            return "workflow_unavailable"
        if row["status"] == "revoked":
            return "workflow_revoked"
        definition = row["definition"]
        if (
            row["scope_id"] != scope["id"]
            or row["scope_revision"] != scope["revision"]
            or definition["tool_fingerprint"] != spec.digest
        ):
            return "scope_changed"
        if definition["question_fingerprint"] != QuestionRegistry.from_spec(spec).fingerprint:
            return "question_changed"
        if digest(definition) != row["definition_digest"]:
            return "definition_changed"
        return self._dependencies(conn, definition, spec)

    def validate(self, owner, workflow_id, version, report, expected_sha256):
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            row = self._row(conn, workflow_id, version, lock="UPDATE")
            if not row or row["status"] not in {"draft", "validated"}:
                raise ValueError("Only a draft workflow can be validated")
            scope, spec = self._scope(conn, row["scope_id"])
            if reason := self._current(conn, row, scope, spec, publication=True):
                raise ValueError("Workflow dependency unavailable: " + reason)
            value = validate_definition(row["definition"], spec)
            evidence = validate_workflow_report(report, expected_sha256, value)
            saved = conn.execute(
                "INSERT INTO app.workflow_validation_reports"
                "(report_id,report_digest,definition_digest,report,registered_by) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(report_id) DO NOTHING RETURNING *",
                (
                    evidence["report_id"],
                    expected_sha256,
                    row["definition_digest"],
                    Jsonb(evidence),
                    actor,
                ),
            ).fetchone()
            if saved is None:
                saved = conn.execute(
                    "SELECT * FROM app.workflow_validation_reports WHERE report_id=%s FOR SHARE",
                    (evidence["report_id"],),
                ).fetchone()
                if saved["report_digest"] != expected_sha256:
                    raise ValueError("Workflow report already has different immutable content")
            if row["status"] == "validated":
                if row["validation_report_id"] != evidence["report_id"]:
                    raise ValueError("Workflow validation report binding is immutable")
                return row
            row = conn.execute(
                "UPDATE app.business_workflows SET status='validated',validation_report_id=%s,"
                "validated_at=clock_timestamp() WHERE workflow_id=%s AND version=%s RETURNING *",
                (evidence["report_id"], workflow_id, version),
            ).fetchone()
            self._event(
                conn,
                row,
                "workflow.validated",
                "owner",
                actor,
                {
                    "report_id": evidence["report_id"],
                    "report_digest": expected_sha256,
                    "synthetic": evidence["synthetic"],
                    "quality_claim": False,
                },
            )
            return row

    def publish(self, owner, workflow_id, version):
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            row = self._row(conn, workflow_id, version, lock="UPDATE")
            if not row or row["status"] not in {"validated", "published"}:
                raise ValueError("A validated, nonrevoked workflow version is required")
            scope, spec = self._scope(conn, row["scope_id"])
            if reason := self._current(conn, row, scope, spec, publication=True):
                raise ValueError("Workflow dependency unavailable: " + reason)
            evidence = conn.execute(
                "SELECT * FROM app.workflow_validation_reports WHERE report_id=%s FOR SHARE",
                (row["validation_report_id"],),
            ).fetchone()
            validate_workflow_report(
                evidence["report"],
                evidence["report_digest"],
                validate_definition(row["definition"], spec),
            )
            if row["status"] != "published":
                row = conn.execute(
                    "UPDATE app.business_workflows SET status='published',"
                    "published_at=clock_timestamp() WHERE workflow_id=%s "
                    "AND version=%s RETURNING *",
                    (workflow_id, version),
                ).fetchone()
                self._event(
                    conn,
                    row,
                    "workflow.published",
                    "owner",
                    actor,
                    {
                        "definition_digest": row["definition_digest"],
                        "report_digest": evidence["report_digest"],
                    },
                )
            return row

    def revoke(self, owner, workflow_id, version, reason):
        if not _text(reason, 512):
            raise ValueError("A bounded workflow revocation reason is required")
        with self.database.connect() as conn:
            actor = self.policies._owner(conn, owner)
            row = self._row(conn, workflow_id, version, lock="UPDATE")
            if not row:
                raise ValueError("Workflow version unavailable")
            if row["status"] != "revoked":
                row = conn.execute(
                    "UPDATE app.business_workflows SET status='revoked',"
                    "revoked_at=clock_timestamp() WHERE workflow_id=%s "
                    "AND version=%s RETURNING *",
                    (workflow_id, version),
                ).fetchone()
                self._event(conn, row, "workflow.revoked", "owner", actor, {"reason": reason})
            return row

    @staticmethod
    def _input_check(definition, inputs):
        if type(inputs) is not dict:
            return "invalid_input", ()
        try:
            if len(canonical_json(inputs).encode()) > 65536:
                return "invalid_input", ()
        except (ValueError, TypeError, OverflowError):
            return "invalid_input", ()
        missing = set()
        for error in Draft202012Validator(definition["input_schema"]).iter_errors(inputs):
            if error.validator == "required":
                prefix = ".".join(map(str, error.absolute_path))
                missing.update(
                    (prefix + "." if prefix else "") + key
                    for key in error.validator_value
                    if key not in error.instance
                )
        if missing:
            return "missing_input", tuple(sorted(missing))
        try:
            validate_schema(definition["input_schema"], inputs)
        except ValueError:
            return "invalid_input", ()
        for name in ("ticket_id", "person_id", "category_id"):
            if name in inputs:
                try:
                    if not isinstance(inputs[name], str) or str(UUID(inputs[name])) != inputs[name]:
                        return "invalid_input", ()
                except ValueError:
                    return "invalid_input", ()
        if any(
            inputs.get(key) != value
            for key, value in definition["applicability"]["input_equals"].items()
        ):
            return "not_applicable", ()
        return None, ()

    def _applicable(self, conn, row, actor, inputs):
        definition = row["definition"]
        if actor["role"] not in definition["applicability"]["roles"]:
            return "not_applicable", ()
        commands = {
            node["tool" if node["type"] == "read_tool" else "command"]["command"]
            for node in definition["plan"]["nodes"]
            if node["type"] in {"read_tool", "governed_write"}
        }
        if commands - access.allowed_commands(conn, actor["id"]):
            return "permission_denied", ()
        reason, missing = self._input_check(definition, inputs)
        if reason:
            return reason, missing
        if "ticket_id" in inputs:
            try:
                access.get_ticket(conn, actor["id"], inputs["ticket_id"])
            except AccessError:
                return "resource_unavailable", ()
        for name, table in (("category_id", "categories"), ("person_id", "people")):
            if name in inputs:
                suffix = " AND active" if name == "person_id" else ""
                if not conn.execute(
                    "SELECT id FROM business." + table + " WHERE id=%s" + suffix, (inputs[name],)
                ).fetchone():
                    return "resource_unavailable", ()
        return None, ()

    def _match(
        self, conn, identity, scope_id, inputs, workflow_id=None, version=None, goal_id=None
    ):
        actor = self.operations._identity(conn, identity, lock=True)
        try:
            scope, spec = self._scope(conn, scope_id)
        except AccessError as exc:
            return WorkflowResolution(exc.code)
        rows = conn.execute(
            "SELECT * FROM app.business_workflows WHERE scope_id=%s "
            "AND (%s::text IS NULL OR workflow_id=%s) AND (%s::text IS NULL OR version=%s) "
            "AND (%s::text IS NULL OR goal_id=%s) ORDER BY workflow_id,version FOR SHARE",
            (scope_id, workflow_id, workflow_id, version, version, goal_id, goal_id),
        ).fetchall()
        if not workflow_id and not goal_id:
            return WorkflowResolution("clarification_required")
        matches, reasons, missing = [], [], set()
        for row in rows:
            reason = self._current(conn, row, scope, spec)
            if not reason:
                reason, absent = self._applicable(conn, row, actor, inputs)
                missing.update(absent)
            if reason:
                reasons.append(reason)
            else:
                matches.append(row)
        candidates = tuple(
            {key: row[key] for key in ("workflow_id", "version", "goal_id", "definition_digest")}
            for row in matches
        )
        if len(matches) == 1:
            return WorkflowResolution("matched", candidates, workflow=matches[0])
        if len(matches) > 1:
            return WorkflowResolution("ambiguous", candidates)
        priority = (
            "missing_input",
            "invalid_input",
            "policy_changed",
            "scope_changed",
            "question_changed",
            "workflow_revoked",
            "permission_denied",
            "resource_unavailable",
            "not_applicable",
            "definition_changed",
        )
        reason = next((item for item in priority if item in reasons), "workflow_unavailable")
        return WorkflowResolution(reason, missing_fields=tuple(sorted(missing)))

    def match(self, identity, scope_id, inputs, *, workflow_id=None, version=None, goal_id=None):
        """Match an explicit registered target; never use similarity or silently pick a version."""
        with self.database.connect() as conn:
            return self._match(conn, identity, scope_id, inputs, workflow_id, version, goal_id)

    def _task(self, conn, identity, task_id, *, lock=True):
        read_task = (
            self.operations.lock_employee_task if lock else self.operations.read_employee_task
        )
        task, actor = read_task(conn, identity, task_id)
        if not task:
            raise AccessError("not_found", "Original employee workflow task unavailable")
        return task, actor

    def instantiate(self, identity, task_id, workflow_id, version, inputs):
        """Bind only new task inputs; return a fresh empty context for runtime reads."""
        with self.database.connect() as conn:
            task, actor = self._task(conn, identity, task_id)
            resolution = self._match(
                conn, identity, task["business_scope_id"], inputs, workflow_id, version
            )
            if not resolution.matched:
                raise AccessError(
                    resolution.reason, "Workflow cannot be reused: " + resolution.reason
                )
            row = resolution.workflow
            if "instruction" in row["definition"]["input_schema"].get(
                "properties", {}
            ) and inputs.get("instruction") != task["authorization_snapshot"].get("instruction"):
                raise AccessError(
                    "invalid_input", "Workflow instruction differs from original task"
                )
            if (
                task["graph_version"] != "business-3"
                or task["authorization_snapshot"].get("plugin_digest")
                != row["definition"]["tool_fingerprint"]
            ):
                raise AccessError("scope_changed", "Task workflow contract changed")
            previous = conn.execute(
                "SELECT * FROM app.workflow_instances WHERE task_id=%s", (task_id,)
            ).fetchone()
            if previous:
                if previous["account_revision"] != actor["credential_revision"]:
                    raise AccessError("permission_changed", "Workflow account revision changed")
                if reason := self._snapshot_reason(conn, previous, row):
                    raise AccessError(reason, "Workflow instance binding changed: " + reason)
                if (
                    previous["workflow_id"] != workflow_id
                    or previous["version"] != version
                    or previous["input_digest"] != digest(inputs)
                ):
                    raise ValueError("Task workflow binding is immutable")
                return _instance(previous)
            if conn.execute(
                "SELECT task_id FROM app.business_plan_heads WHERE task_id=%s", (task_id,)
            ).fetchone():
                raise ValueError("Existing task plan cannot be replaced by workflow reuse")
            value = dict(row["definition"]["plan"])
            value.pop("content_digest", None)
            instance_id = uuid4()
            value.update(plan_id="workflow-" + instance_id.hex, revision=1)
            _, spec = self._scope(conn, task["business_scope_id"])
            compiled = compile_plan(value, spec, input_schema=row["definition"]["input_schema"])
            evidence = conn.execute(
                "SELECT report_digest FROM app.workflow_validation_reports WHERE report_id=%s",
                (row["validation_report_id"],),
            ).fetchone()
            saved = conn.execute(
                "INSERT INTO app.workflow_instances(task_id,instance_id,workflow_id,version,"
                "definition_digest,report_digest,employee_id,scope_revision,account_revision,"
                "input_digest,task_input,plan) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING *",
                (
                    task_id,
                    instance_id,
                    workflow_id,
                    version,
                    row["definition_digest"],
                    evidence["report_digest"],
                    actor["id"],
                    row["scope_revision"],
                    actor["credential_revision"],
                    digest(inputs),
                    Jsonb(inputs),
                    Jsonb(compiled.plan.to_mapping()),
                ),
            ).fetchone()
            self._event(
                conn,
                row,
                "workflow.instantiated",
                "employee",
                actor["id"],
                {
                    "instance_id": str(instance_id),
                    "definition_digest": row["definition_digest"],
                    "plan_digest": compiled.plan.digest,
                    "input_digest": digest(inputs),
                },
                task_id,
            )
            return _instance(saved)

    @staticmethod
    def _snapshot_reason(conn, instance, workflow):
        if instance["scope_revision"] != workflow["scope_revision"]:
            return "scope_changed"
        if instance["definition_digest"] != workflow["definition_digest"]:
            return "definition_changed"
        report = conn.execute(
            "SELECT * FROM app.workflow_validation_reports WHERE report_id=%s FOR SHARE",
            (workflow["validation_report_id"],),
        ).fetchone()
        if (
            not report
            or report["report_digest"] != instance["report_digest"]
            or digest(report["report"]) != report["report_digest"]
            or report["definition_digest"] != instance["definition_digest"]
        ):
            return "report_changed"
        if digest(instance["task_input"]) != instance["input_digest"]:
            return "input_changed"
        return None

    def instance(self, identity, task_id):
        """Read original instance evidence even after revocation; this never replays it."""
        with self.database.connect() as conn:
            self._task(conn, identity, task_id)
            row = conn.execute(
                "SELECT * FROM app.workflow_instances WHERE task_id=%s", (task_id,)
            ).fetchone()
            if not row:
                raise AccessError("not_found", "Workflow instance unavailable")
            return _instance(row)

    def check_instance(self, identity, task_id, *, lock_task=True):
        """Signal applicability at a safe boundary; preserve independent operation queries."""
        with self.database.connect() as conn:
            task, actor = self._task(conn, identity, task_id, lock=lock_task)
            instance = conn.execute(
                "SELECT * FROM app.workflow_instances WHERE task_id=%s", (task_id,)
            ).fetchone()
            if not instance:
                return WorkflowResolution("workflow_unavailable")
            if instance["account_revision"] != actor["credential_revision"]:
                return WorkflowResolution("permission_changed")
            resolution = self._match(
                conn,
                identity,
                task["business_scope_id"],
                instance["task_input"],
                instance["workflow_id"],
                instance["version"],
            )
            if resolution.matched:
                if reason := self._snapshot_reason(conn, instance, resolution.workflow):
                    return WorkflowResolution(reason)
            return resolution

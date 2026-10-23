"""Production business-3 harness: one planner, durable execution and semantic waits.

The task remains the authority for employee identity and budgets. Plan/model text
and human semantic choices never authorize an MCP side effect.
"""

from dataclasses import replace
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from decagent.business.access import AccessError
from decagent.business.context import ContextBudgetError, ContextBuilder, TaskWorkingMemory
from decagent.business.contracts import validate_schema
from decagent.business.decision_policy import DecisionGate, PolicyContext, gate_decision
from decagent.business.employee_auth import EmployeeIdentity
from decagent.business.execution import BusinessReadError
from decagent.business.guest_jev_pricing import select_jev_profile
from decagent.business.interventions import SemanticInterventions, decision_evidence
from decagent.business.mcp_client import MCPToolError, MCPTransportError
from decagent.business.memory_control import BusinessMemoryControl
from decagent.business.plans.batching import DecisionBatchRunner
from decagent.business.plans.bindings import resolve_inputs
from decagent.business.plans.handlers import BusinessNodeHandlers
from decagent.business.plans.patches import PlanPatchService
from decagent.business.plans.planner import PlanProposalInvalid, budgeted_planner
from decagent.business.plans.planner_journal import PlannerProposalJournal
from decagent.business.plans.repository import PlanRepository
from decagent.business.plans.runtime import (
    TERMINAL,
    NodeFailure,
    NodePending,
    NodeResult,
    PlanRuntime,
)
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.policy_store import DecisionPolicyStore
from decagent.business.services import BusinessServices
from decagent.business.workflow_registry import WorkflowRegistry
from decagent.decisions import JevAdapter
from decagent.decisions.contracts import digest, json_value
from decagent.runtime.model_calls import LedgerAttemptBudget
from decagent.storage.safe_record import exact_safe_record


class _EmployeeSupplement(Exception):
    def __init__(self, node, saved, record):
        self.node, self.saved, self.record = node, saved, record


class _BoundaryDecisionAdapter:
    """Check the active inputs at the adapter's actual model dispatch boundary."""

    def __init__(self, adapter, before_evaluation):
        self.adapter, self.before_evaluation = adapter, before_evaluation

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def evaluate(self, request):
        self.before_evaluation(request)
        return self.adapter.evaluate(request)


class ConditionalBusinessServices:
    def __init__(
        self,
        database,
        settings,
        runtime_config,
        task,
        owner,
        generation,
        *,
        mcp_client,
        planner=None,
        decision_adapter=None,
    ):
        self.database, self.settings, self.config, self.task = (
            database,
            settings,
            runtime_config,
            task,
        )
        self.owner, self.generation = owner, generation
        self.repository = PlanRepository(database, task["id"], owner, generation)
        self.legacy = BusinessServices(
            database, settings, runtime_config, task, owner, generation, mcp_client=mcp_client
        )
        self.executor, self.spec = self.legacy.executor, self.repository.spec
        self.ledger = self.legacy.ledger
        self.planner, self.adapter = planner, decision_adapter
        self.memory = TaskWorkingMemory(self.repository)
        self.policies = DecisionPolicyStore(database)
        self.interventions = SemanticInterventions(database, refresh=self._refresh)
        self.memory_control = BusinessMemoryControl(database)
        self._decision_boundary = None
        self.handlers = BusinessNodeHandlers(
            self.repository,
            self.legacy,
            decide=self._decide,
            ask_user=self._ask,
            before_dispatch=self._before_node,
        )
        self.runner = None

    def close(self):
        close = getattr(self.adapter, "close", None)
        if close:
            close()
        self.legacy.close()

    def guard(self):
        # New model/tool calls check their own overall budgets. This identity/
        # lease guard also permits the existing receipt-only reconciliation path.
        return self.repository.guard()

    def identity(self):
        client = self.executor.client
        identity = getattr(client, "identity", None) or getattr(client, "_identity", None)
        if (
            not isinstance(identity, EmployeeIdentity)
            or identity.employee_id != self.task["employee_id"]
        ):
            raise ValueError("Original employee MCP identity required")
        return identity

    def prepare(self, data):
        if self.repository.current() is not None:
            return
        if self.task["authorization_snapshot"].get("workflow") is not None:
            self._prepare_workflow()
            return
        try:
            context = (
                ContextBuilder(self.memory.rebuild(persist=False), system_prefix="")
                .planning()
                .to_mapping()
            )
            if self.planner is None:
                self.planner = budgeted_planner(
                    self.settings,
                    self.config,
                    self.spec,
                    self.ledger,
                    before_dispatch=self._model_guard,
                )
            journal = PlannerProposalJournal(self.repository)
            proposal = journal.propose(self.planner, context)
            journal.publish(proposal)
        except (PlanProposalInvalid, ContextBudgetError) as exc:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "plan_invalid",
                    "reason": str(exc),
                    "question": "请检查规划失败原因后明确恢复。",
                }
            ) from exc

    def _prepare_workflow(self):
        from decagent.business.workflow_registry import WorkflowRegistry

        selection = self.task["authorization_snapshot"]["workflow"]
        registry = WorkflowRegistry(self.database)
        resolution = registry.match(
            self.identity(),
            self.task["business_scope_id"],
            selection["inputs"],
            workflow_id=selection["workflow_id"],
            version=selection["version"],
        )
        if not resolution.matched or (
            resolution.workflow["definition_digest"] != selection["definition_digest"]
        ):
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "workflow_unavailable",
                    "reason": resolution.reason if not resolution.matched else "definition_changed",
                    "workflow_id": selection["workflow_id"],
                    "version": selection["version"],
                }
            )
        instance = registry.instantiate(
            self.identity(),
            self.task["id"],
            selection["workflow_id"],
            selection["version"],
            selection["inputs"],
        )
        if instance.report_digest != selection["report_digest"]:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "workflow_unavailable",
                    "reason": "report_changed",
                }
            )
        self.repository.publish(
            instance.plan,
            input_schema=resolution.workflow["definition"]["input_schema"],
            task_input=instance.task_input,
        )

    def _runtime(self):
        return PlanRuntime(
            self.repository,
            self.handlers.execute,
            prime_decisions=self._prime,
            before_node=self._before_node,
            recover_original=self._recover_original,
        )

    def _workflow_boundary(self):
        with self.database.connect() as conn:
            self.repository.guard(conn)
            installed = conn.execute(
                "SELECT to_regclass('app.workflow_instances') AS t"
            ).fetchone()["t"]
            instance = (
                installed
                and conn.execute(
                    "SELECT task_id FROM app.workflow_instances WHERE task_id=%s",
                    (self.task["id"],),
                ).fetchone()
            )
        if instance:
            resolution = WorkflowRegistry(self.database).check_instance(
                self.identity(), self.task["id"]
            )
            if not resolution.matched:
                raise NodePending(
                    {
                        "kind": "reconciliation",
                        "category": "workflow_changed",
                        "reason": resolution.reason,
                        "requires_replan": True,
                        "grants_write_authority": False,
                    }
                )

    @staticmethod
    def _uses_memory(value):
        if isinstance(value, dict):
            if value.get("source") == "literal":
                return False
            return value.get("source") == "verified_memory" or any(
                ConditionalBusinessServices._uses_memory(item) for item in value.values()
            )
        return isinstance(value, (list, tuple)) and any(
            ConditionalBusinessServices._uses_memory(item) for item in value
        )

    def _before_node(self, node, bound, saved=None):
        task = self.repository.guard()
        bound.memories = {}  # Cached active JSON is never evidence for a new dispatch.
        if (
            bound.employee_id != str(task["employee_id"])
            or bound.scope_revision != task["scope_revision"]
            or bound.account_revision != task["authorization_revision"]
            or self.identity().account_revision != task["authorization_revision"]
        ):
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "permission_changed",
                    "node_id": node["id"],
                    "reason": "Execution permission epoch changed",
                    "requires_replan": True,
                    "grants_write_authority": False,
                }
            )
        self._workflow_boundary()
        plan = self.repository.current()
        if not self._uses_memory([plan.nodes, plan.goal]):
            return
        try:
            checked = self.memory_control.check_dependencies(
                self.identity(),
                self.task["id"],
                node_ids=[node["id"]],
                include_frozen=True,
            )
        except AccessError as exc:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "dependency_changed",
                    "node_id": node["id"],
                    "reason": exc.code,
                    "requires_replan": True,
                    "grants_write_authority": False,
                }
            ) from exc
        if not checked["valid"]:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "category": "dependency_changed",
                    "node_id": node["id"],
                    "reason": "Current memory dependency is unavailable",
                    "invalid_dependencies": checked["invalid_dependencies"],
                    "requires_replan": True,
                    "grants_write_authority": False,
                }
            )
        bound.memories = checked["memories"]

    def _recover_original(self, node, bound, saved, request):
        original = (saved["output_snapshot"] or {}).get("request", {})
        request = {
            **request,
            "node_id": node["id"],
            "generation": saved["execution_generation"],
            "input_digest": saved["input_digest"],
            "grants_write_authority": False,
            **{key: original[key] for key in ("action_id", "operation_id") if original.get(key)},
            **(
                {
                    "original_approval_id": original.get("approval_id")
                    or original.get("original_approval_id")
                }
                if original.get("approval_id") or original.get("original_approval_id")
                else {}
            ),
        }
        if node["type"] != "governed_write":
            return request
        with self.database.connect() as conn:
            self.repository.guard(conn)
            action = conn.execute(
                "SELECT * FROM app.actions WHERE task_id=%s AND logical_key=%s",
                (self.task["id"], f"plan:{node['id']}:{saved['execution_generation']}"),
            ).fetchone()
        if action is None:
            return request
        if (
            action["snapshot"]["command"] != node["command"]["command"]
            or action["snapshot"]["params"] != saved["input_snapshot"]["values"]
            or digest(saved["input_snapshot"]) != saved["input_digest"]
            or saved.get("action_id") not in {None, action["id"]}
        ):
            raise ValueError("Original operation does not match the frozen node inputs")
        verification = self.executor.verify_original(
            action["id"],
            action["digest"],
            expected_inputs=saved["input_snapshot"]["values"],
        )
        return {
            **request,
            "action_id": str(action["id"]),
            "operation_id": action["server_operation_id"],
            "original_verification": verification,
        }

    def _model_guard(self):
        task = self.guard()
        self._workflow_boundary()
        if self._decision_boundary:
            compiled, bound, names = self._decision_boundary
            rows = {row["node_id"]: row for row in self.repository.nodes()}
            memories = {}
            for name in names:
                self._before_node(compiled.node(name), bound, rows.get(name))
                memories.update(bound.memories)
            bound.memories = memories
        return task

    def _before_evaluation(self, request):
        if self._decision_boundary is None:
            raise ValueError("Current decision dispatch boundary is required")
        compiled, bound, _ = self._decision_boundary
        self._decision_boundary = (
            compiled,
            bound,
            tuple(question.id for question in request.questions),
        )
        self._model_guard()

    def _before_batch(self, compiled, names, bound):
        self._decision_boundary = (compiled, bound, tuple(names))
        self._model_guard()

    def step(self, data):
        pending = self.repository.context_snapshot().get("pending_replan")
        if pending:
            return self._replan_pending(pending)
        try:
            runtime = self._runtime()
            result = self._scope_clarification_step(runtime)
            if result is None:
                result = runtime.step()
        except _EmployeeSupplement as fact:
            # Retire the unresolved inference, not its history. This durable
            # marker blocks the old remainder until a bounded patch is accepted.
            # A crash at either side of publication continues this same marker.
            with self.database.connect() as conn:
                self.repository.guard(conn)
                plan = self.repository._load(conn)
                row = self.repository._node(
                    conn, fact.node["id"], fact.saved["execution_generation"]
                )
                payload = {
                    **(row["output_snapshot"] or {}),
                    "result": {
                        "kind": "decide",
                        "accepted": False,
                        "selection_origin": "employee_session",
                        "semantic_record": fact.record,
                        "grants_write_authority": False,
                    },
                    "control": {"targets": fact.node["next"]},
                }
                conn.execute(
                    "UPDATE app.business_plan_nodes SET status='skipped',output_snapshot=%s "
                    "WHERE task_id=%s AND node_id=%s AND execution_generation=%s",
                    (
                        Jsonb(self.repository._safe(payload)),
                        self.task["id"],
                        fact.node["id"],
                        fact.saved["execution_generation"],
                    ),
                )
                pending = {
                    "node_id": fact.node["id"],
                    "parent_digest": plan.digest,
                    "parent_revision": plan.revision,
                    "approval_id": fact.record["approval_id"],
                }
                conn.execute(
                    "UPDATE app.business_plan_heads SET context_snapshot=context_snapshot||%s "
                    "WHERE task_id=%s",
                    (Jsonb({"pending_replan": pending}), self.task["id"]),
                )
                self.repository._event(conn, "business.supplement_recorded", pending)
            return self._replan_pending(pending)
        self.memory.rebuild()
        return result

    def _scope_clarification_step(self, runtime):
        """Resolve the employee's scope control without relabeling its old reads.

        A permission change can invalidate the directory that caused a scope
        wait. The reply is only a request for a bounded patch; it cannot consume
        that directory, accept a Jev answer or authorize a business action.
        """
        if not any(
            (row["output_snapshot"] or {}).get("request", {}).get("category") == "scope_limit"
            for row in self.repository.nodes()
        ):
            return None
        compiled, rows, bound = runtime._state()
        if any(
            row["status"] in TERMINAL
            and (row["output_snapshot"] or {}).get("control", {}).get("stop")
            for row in rows.values()
        ):
            return None
        ready = runtime._frontier(compiled, rows)
        ready.sort(key=lambda name: compiled.node(name)["type"] == "complete")
        if not ready:
            return None
        node = compiled.node(ready[0])
        saved = rows.get(node["id"])
        request = ((saved or {}).get("output_snapshot") or {}).get("request", {})
        if (
            node["type"] != "decide"
            or request.get("kind") != "clarification"
            or request.get("category") != "scope_limit"
        ):
            return None
        try:
            self._workflow_boundary()
        except NodePending as pending:
            return {"status": "waiting", "node_id": node["id"], "request": pending.request}
        self.repository.transition()
        try:
            self._decide(node, {}, bound, saved)
        except NodePending as pending:
            self.repository.finish(
                node["id"],
                "unknown" if pending.unknown else "waiting",
                {**(saved["output_snapshot"] or {}), "request": pending.request},
                generation=saved["execution_generation"],
            )
            return {"status": "waiting", "node_id": node["id"], "request": pending.request}
        except NodeFailure as exc:
            return runtime._save(
                node, "failed", {"reason": str(exc)}, runtime._exit(node["on_error"]), saved
            )
        raise ValueError("A scope clarification must wait, reject or request a local patch")

    def _replan_pending(self, pending):
        current = self.repository.current()
        try:
            self._workflow_boundary()
            if current.digest == pending["parent_digest"]:
                if self.planner is None:
                    self.planner = budgeted_planner(
                        self.settings,
                        self.config,
                        self.spec,
                        self.ledger,
                        before_dispatch=self._model_guard,
                    )
                PlanPatchService(self.repository, self.planner).replan(
                    "new_observation", evidence_nodes=[pending["node_id"]]
                )
            else:
                with self.database.connect() as conn:
                    self.repository.guard(conn)
                    parent = conn.execute(
                        "SELECT parent_digest FROM app.business_plan_revisions "
                        "WHERE task_id=%s AND revision=%s",
                        (self.task["id"], current.revision),
                    ).fetchone()
                    if not parent or parent["parent_digest"] != pending["parent_digest"]:
                        raise ValueError(
                            "Supplement patch parent changed; explicit review required"
                        )
            self.repository.save_context({"pending_replan": None})
            self.memory.rebuild()
            return {"status": "advanced", "node_id": pending["node_id"]}
        except NodePending as pending_request:
            return {"status": "waiting", "request": pending_request.request}
        except (PlanProposalInvalid, ContextBudgetError) as exc:
            return {
                "status": "waiting",
                "request": {
                    "kind": "reconciliation",
                    "category": "plan_invalid",
                    "reason": str(exc),
                    "required_tokens": getattr(exc, "required_tokens", None),
                    "max_tokens": getattr(exc, "max_tokens", None),
                },
            }

    def _batch_runner(self):
        if self.runner is None:
            if self.adapter is None:
                if not self.settings.jev_api_key:
                    raise NodePending(
                        {
                            "kind": "reconciliation",
                            "category": "configuration",
                            "reason": "Jev credential is unavailable; no primary fallback",
                        }
                    )
                jev_profile = select_jev_profile(
                    is_guest=self.ledger.trial_budget is not None,
                    model=self.settings.jev_model,
                    provider=self.settings.jev_provider,
                    reservation_usd=self.config.jev_reservation,
                )
                if jev_profile is None:
                    raise NodePending(
                        {
                            "kind": "reconciliation",
                            "category": "configuration",
                            "reason": "Guest Jev requires the OpenRouter pricing profile",
                        }
                    )
                jev_model, jev_provider, jev_reservation = jev_profile
                self.adapter = JevAdapter(
                    api_key=self.settings.jev_api_key,
                    model=jev_model,
                    provider=jev_provider,
                    cache_ttl=0,
                    max_retries=0,
                    budget=LedgerAttemptBudget(
                        self.ledger,
                        reservation_usd=jev_reservation,
                        provider=jev_provider,
                        requested_model=jev_model,
                        before_dispatch=self._model_guard,
                    ),
                )
            self.runner = DecisionBatchRunner(
                self.repository,
                _BoundaryDecisionAdapter(self.adapter, self._before_evaluation),
                self._gate,
                scope_request=self._scope_wait,
                before_batch=self._before_batch,
            )
        return self.runner

    def _prime(self, compiled, ready_ids, bound):
        rows = {row["node_id"]: row for row in self.repository.nodes()}
        selected = rows.get(ready_ids[0], {})
        if (selected.get("output_snapshot") or {}).get("request", {}).get("kind") in {
            "decision",
            "clarification",
        }:
            return  # The original raw batch and authenticated human wait own this node.
        self._decision_boundary = (compiled, bound, tuple(ready_ids))
        try:
            memories = {}
            for name in ready_ids:
                self._before_node(compiled.node(name), bound, rows.get(name))
                memories.update(bound.memories)
            bound.memories = memories
            self._batch_runner().prime(compiled, ready_ids, bound)
        finally:
            self._decision_boundary = None

    def _scope_wait(self, node_id, reason):
        compiled, _, bound = self._runtime()._state()
        node = compiled.node(node_id)
        inputs, provenance = resolve_inputs(node, bound)
        saved = self.repository.begin(node_id, inputs, provenance)
        request = self._clarification_request(node, bound, saved, scope_reason=reason)
        self.repository.finish(
            node_id,
            "waiting",
            {**(saved["output_snapshot"] or {}), "request": request},
            generation=saved["execution_generation"],
        )
        return request

    def _clarification_binding(self, node, bound, saved):
        task = self.repository.guard()
        identity = self.identity()
        if (
            bound.employee_id != str(task["employee_id"])
            or bound.scope_revision != task["scope_revision"]
            or bound.account_revision != task["authorization_revision"]
            or identity.account_revision != task["authorization_revision"]
        ):
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "reason": "Clarification requires the current employee permission epoch",
                }
            )
        return {
            "kind": "conditional_clarification",
            "employee_id": bound.employee_id,
            "scope_id": str(task["business_scope_id"]),
            "scope_revision": bound.scope_revision,
            "account_revision": bound.account_revision,
            "plugin_digest": self.spec.digest,
            "node_id": node["id"],
            "node_digest": digest(node),
            "generation": saved["execution_generation"],
            "plan_revision": saved["plan_revision"],
            "input_digest": saved["input_digest"],
        }

    def _clarification_request(self, node, bound, saved, *, scope_reason=None):
        binding = self._clarification_binding(node, bound, saved)
        request = self.legacy.human_request(
            {
                "instruction": self.repository.instruction(),
                "human_kind": "clarification",
                "question": node["prompt"]
                if scope_reason is None
                else "请缩小业务候选范围或补充具体目标：" + scope_reason,
                "action_index": binding["plan_revision"] * 128
                + list(self.repository.current().nodes).index(node),
                "conditional_binding": binding,
            }
        )
        return {
            **request,
            "node_id": node["id"],
            **(
                {"category": "scope_limit", "scope_reason": scope_reason}
                if scope_reason is not None
                else {}
            ),
        }

    def _clarification_reply(self, node, bound, saved, request):
        approval = self._approval(request["approval_id"], kind="clarification")
        expected = self._clarification_binding(node, bound, saved)
        payload = approval["reply_payload"] or {}
        if payload.get("conditional_binding") != expected:
            # Keep the original reply and its epoch in the approval journal. A
            # new question is required; old text never gains the current epoch.
            raise NodePending(
                self._clarification_request(
                    node,
                    bound,
                    saved,
                    scope_reason=request.get("scope_reason", "Current scope requires clarification")
                    if node["type"] == "decide"
                    else None,
                )
            )
        if approval["status"] not in {"replied", "consumed"}:
            raise NodePending(request)
        record = payload.get("policy_record", {})
        if (
            record.get("source") != "employee_session"
            or record.get("employee_id") != bound.employee_id
        ):
            raise ValueError("Original employee clarification required")
        return approval, record, expected

    def _state_evidence(self, node, state, *, context, task=None, identity=None):
        task = task or self.repository.guard()
        identity = identity or self.identity()
        return decision_evidence(
            self.spec,
            task,
            node,
            state,
            context=context,
            scope_version=task["scope_revision"],
            account_version=identity.account_revision,
        )

    def _fresh_state(self, state, read):
        fresh = json_value(state)
        fresh["directory"] = read("directory", {})
        ticket = fresh.get("ticket")
        if ticket and ticket.get("id"):
            fresh["ticket"] = read("get", {"ticket_id": ticket["id"]})["ticket"]
        return fresh

    def _refresh(self, conn, task, row, identity):
        from decagent.business.decision_policy import PolicyContext

        # This callback already holds the task row. Do not enter another executor
        # connection/lease guard: use the employee MCP reader and this transaction.
        if identity.source != "employee_session":
            self.ledger.assert_action_available(connection=conn)

        def read(command, params):
            self.spec.validate(command, params)
            result = self.executor.client.read(command, params)
            validate_schema(self.spec.commands[command].result_schema, result)
            return exact_safe_record(result, self.database.redactor)

        context = PolicyContext(**row["binding"]["snapshot"]["context"])
        return self._state_evidence(
            row["binding"]["node"],
            self._fresh_state(row["binding"]["evidence"], read),
            context=context,
            task=task,
            identity=identity,
        )

    def _gate(self, node, raw, bound, batch):
        values, _ = resolve_inputs(node, bound)
        definition = QuestionRegistry.from_spec(self.spec).validate_reference(node["question"])
        resolution = self.policies.resolve(
            self.identity(), self.task["id"], node["policy"]["policy_id"], node["policy"]["version"]
        )
        context = (
            resolution.policy.context
            if resolution.policy
            else PolicyContext(
                self.spec.digest,
                self.spec.manifest["workflow"]["version"],
                "unvalidated",
                "semantic",
                definition["candidate_source"] or "workflow." + definition["question_id"],
            )
        )
        state = {
            "instruction": values["instruction"],
            "directory": values.get("directory", {}),
            "ticket": values.get("ticket"),
            "sources": values.get("sources", {}),
        }
        original = self._state_evidence(node, state, context=context)
        try:
            current = self._state_evidence(
                node, self._fresh_state(state, self.executor.read), context=context
            )
        except (MCPTransportError, BusinessReadError, MCPToolError) as exc:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "reason": "Fresh evidence read failed; retain original model result",
                }
            ) from exc
        gate = gate_decision(
            replace(original.question, id=definition["question_id"]),
            raw,
            actual_model=batch["result"]["evaluation"]["actual_model"],
            snapshot=original.snapshot,
            current_snapshot=current.snapshot,
            policy=resolution.policy,
            now=datetime.now(UTC),
        )
        if gate.may_consume:
            return self._accepted(
                node,
                {**raw, "selected_value": gate.answer.suggested_value},
                raw,
                bound,
                origin="jev",
                gate=gate.to_dict(),
            )
        if original.snapshot != current.snapshot:
            gate = DecisionGate(
                "manual",
                "stale_decision",
                None,
                gate.actual_model,
                current.snapshot,
                current.snapshot,
                gate.policy_id,
                gate.policy_version,
                gate.threshold,
            )
        saved = next(
            row for row in reversed(self.repository.nodes()) if row["node_id"] == node["id"]
        )
        request = self.interventions.request(
            self.task["id"],
            node["id"],
            self.repository.current().revision,
            current,
            gate,
            identity=self.identity(),
            lease_owner=self.owner,
            lease_generation=self.generation,
            generation=saved["execution_generation"],
        )
        raise NodePending(request)

    def _accepted(self, node, value, raw, bound, *, origin, gate=None, semantic_record=None):
        return NodeResult(
            {
                "kind": "decide",
                "accepted": True,
                "value": value,
                "raw_model_answer": raw,
                "selection_origin": origin,
                "gate": gate,
                "semantic_record": semantic_record,
                "employee_id": bound.employee_id,
                "scope_revision": bound.scope_revision,
                "account_revision": bound.account_revision,
                "grants_write_authority": False,
            },
            no_match=value.get("selected_value") == "no_match",
        )

    def _decide(self, node, inputs, bound, saved):
        request = (saved["output_snapshot"] or {}).get("request", {})
        if request.get("kind") == "clarification":
            approval, record, binding = self._clarification_reply(node, bound, saved, request)
            if record.get("status") == "rejected":
                raise NodeFailure("Employee rejected scope clarification")
            text = record.get("confirmation")
            if not isinstance(text, str) or not text.strip():
                raise NodeFailure("Concrete scope clarification required")
            raise _EmployeeSupplement(
                node,
                saved,
                {
                    "kind": "semantic_scope",
                    "status": "supplement",
                    "source": "employee_session",
                    "actor_id": bound.employee_id,
                    "approval_id": str(approval["id"]),
                    "text": text,
                    "binding_digest": digest(binding),
                    "grants_write_authority": False,
                },
            )
        if request.get("kind") != "decision":
            return self._batch_runner().consume(node, inputs, bound, saved)
        try:
            result = self.interventions.consume(
                request["approval_id"],
                identity=self.identity(),
                lease_owner=self.owner,
                lease_generation=self.generation,
                consumer_id=f"{node['id']}:{saved['execution_generation']}",
            )
        except (MCPTransportError, BusinessReadError, MCPToolError) as exc:
            raise NodePending(
                {**request, "reason": "Current evidence unavailable; response remains pending"}
            ) from exc
        if not result.get("may_resume"):
            if result.get("request"):
                raise NodePending(result["request"])
            if result.get("refresh_required"):
                marker = (saved["output_snapshot"] or {}).get("raw_batch_id")
                batch = self.repository.batch(marker) if marker else None
                if batch and batch["phase"] == "complete":
                    raw = batch["result"]["raw_answers"].get(node["id"], {}).get("value")
                    if raw is not None:
                        return self._gate(node, raw, bound, batch)
            raise NodePending(request)
        if result["status"] == "rejected":
            raise NodeFailure("Original employee rejected the semantic decision")
        if result["status"] == "supplement":
            raise _EmployeeSupplement(node, saved, result)
        value = {"selected_value": result.get("selected_value")}
        if node["question"]["type"] in {"choice", "score"} and result["status"] == "selected":
            value[node["question"]["type"]] = result["selected_value"]
        raw = (result.get("raw_model_answer") or {}).get("raw_answer", {})
        marker = (saved["output_snapshot"] or {}).get("raw_batch_id")
        if marker:
            batch = self.repository.batch(marker)
            if batch and batch["phase"] == "complete":
                raw = batch["result"]["raw_answers"].get(node["id"], {}).get("value", raw)
        accepted = self._accepted(
            node, value, raw, bound, origin="employee_session", semantic_record=result
        )
        return NodeResult(accepted.record, no_match=result["status"] == "no_match")

    def _approval(self, approval_id, *, kind=None):
        with self.database.connect() as conn:
            self.repository.guard(conn)
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s AND task_id=%s",
                (approval_id, self.task["id"]),
            ).fetchone()
            if not approval or (kind and approval["kind"] != kind):
                raise ValueError("Original task approval is unavailable")
            return approval

    def resume(self, data, reply):
        request = data["pending"]
        if request.get("approval_id"):
            approval = self._approval(reply["approval_id"], kind=request["kind"])
            if (
                approval["status"] not in {"replied", "consumed"}
                or approval["digest"] != request["digest"]
            ):
                raise ValueError("No authenticated response for the current question")
            if request["kind"] == "decision":
                if not (approval["reply_payload"] or {}).get("semantic_record"):
                    raise ValueError("Semantic response is unavailable")
                return  # consume rechecks current facts in the original node.
            record = (approval["reply_payload"] or {}).get("policy_record", {})
            if record.get("source") != "employee_session" or record.get("employee_id") != str(
                self.task["employee_id"]
            ):
                raise ValueError("Original employee response required")
            if record.get("status") == "edited":
                self.repository.apply_correction(approval["id"])
            return
        # Generic recovery is authorized by the gateway's durable resume event,
        # not by arbitrary fields in a LangGraph Command payload.
        with self.database.connect() as conn:
            self.repository.guard(conn)
            row = conn.execute(
                "SELECT payload FROM app.events WHERE task_id=%s "
                "AND kind='task.resume_requested' ORDER BY seq DESC LIMIT 1",
                (self.task["id"],),
            ).fetchone()
            if not row or row["payload"].get("employee_id") != str(self.task["employee_id"]):
                raise ValueError("Authenticated task recovery required")

    def _ask(self, node, inputs, bound, saved):
        request = (saved["output_snapshot"] or {}).get("request", {})
        if not request.get("approval_id"):
            request = self._clarification_request(node, bound, saved)
        approval, record, _ = self._clarification_reply(node, bound, saved, request)
        if record.get("status") == "rejected":
            raise NodeFailure("Employee rejected the missing-input question")
        value = record.get("confirmation")
        try:
            if node["answer"]["value_type"] != "string":
                from decagent.business.codec import strict_json

                value = strict_json(value)
            schema = {
                "type": (
                    [node["answer"]["value_type"], "null"]
                    if node["answer"]["nullable"]
                    else node["answer"]["value_type"]
                )
            }
            validate_schema(schema, value)
        except (TypeError, ValueError) as exc:
            raise NodeFailure("Employee answer does not match the declared input type") from exc
        return NodeResult(
            {
                "kind": "ask_user",
                "verified": True,
                "data": value,
                "employee_id": bound.employee_id,
                "scope_revision": bound.scope_revision,
                "account_revision": bound.account_revision,
                "grants_write_authority": False,
            }
        )

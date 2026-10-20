"""Interpret a finite compiled plan from durable node facts and selected edges.

This module never calls a planner. Effectful handlers own their dispatch journal;
the interpreter owns dependency resolution, pure control, and evidence completion.
"""

from dataclasses import dataclass

from decagent.business.contracts import validate_schema
from decagent.decisions.contracts import digest
from decagent.storage.budget import BudgetExceeded

from .bindings import (
    BindingContext,
    BindingInvalid,
    BindingMissing,
    evaluate_condition,
    resolve_inputs,
    run_transform,
)
from .compiler import compile_plan
from .completion import verify_completion
from .evaluation_catalog import EvaluationCatalog

TERMINAL = frozenset({"succeeded", "failed", "skipped"})


@dataclass(frozen=True)
class NodeResult:
    record: dict
    no_match: bool = False


class NodePending(Exception):
    """A handler persisted its own stable approval or original uncertain operation."""

    def __init__(self, request, *, unknown=False):
        if not isinstance(request, dict) or not isinstance(request.get("kind"), str):
            raise ValueError("Node wait requires a structured request")
        self.request, self.unknown = request, unknown
        super().__init__(request.get("reason", "Node requires an authenticated response"))


class NodeFailure(Exception):
    """A known execution failure may follow only the plan's explicit error edge."""


class PlanRuntime:
    def __init__(
        self,
        repository,
        execute,
        *,
        task_input=None,
        memories=None,
        after_save=None,
        prime_decisions=None,
        before_node=None,
        recover_original=None,
        completion_verifier=None,
    ):
        self.repository, self.execute = repository, execute
        self.after_save = after_save
        self.prime_decisions = prime_decisions
        self.before_node, self.recover_original = before_node, recover_original
        self.completion_verifier = completion_verifier
        if type(repository.spec) is EvaluationCatalog:
            repository.spec.assert_active()
            if not callable(completion_verifier):
                raise ValueError("Isolated evaluation requires an execution-finished verifier")
        elif completion_verifier is not None:
            raise ValueError("Custom completion verifiers are limited to isolated evaluation")
        self.memories = memories or {}
        snapshot = repository.context_snapshot()
        task = repository.guard()
        instruction = task["authorization_snapshot"]["instruction"]
        saved = snapshot.get("task_input")
        proposed = task_input if task_input is not None else saved or {"instruction": instruction}
        if proposed.get("instruction") != instruction:
            raise ValueError("Task input cannot replace the authorized instruction")
        if saved is not None and digest(saved) != digest(proposed):
            raise ValueError("Recorded task input cannot change during recovery")
        self.input_schema = snapshot.get("input_schema")
        if self.input_schema:
            validate_schema(self.input_schema, proposed)
        self.task_input = repository._safe(proposed)
        if saved is None:
            repository.save_context({"task_input": self.task_input})

    def _state(self):
        task = self.repository.guard()
        plan = self.repository.current()
        if plan is None:
            raise ValueError("A validated published plan is required")
        compiled = compile_plan(plan, self.repository.spec, input_schema=self.input_schema)
        rows = {}
        for row in self.repository.nodes():
            # Highest generation is the only current execution of a stable node.
            if row["node_id"] not in rows or (
                rows[row["node_id"]]["execution_generation"] < row["execution_generation"]
            ):
                rows[row["node_id"]] = row
        ids = set(compiled.order)
        rows = {
            key: row
            for key, row in rows.items()
            if key in ids and row["node_digest"] == digest(compiled.node(key))
        }
        context = BindingContext(
            self.task_input,
            {},
            {},
            self.memories,
            str(task["employee_id"]),
            task["scope_revision"],
            task["authorization_revision"],
            workflow_id=(task["authorization_snapshot"].get("workflow") or {}).get("workflow_id"),
        )
        for name, row in rows.items():
            if row["status"] != "succeeded":
                continue
            output = (row["output_snapshot"] or {}).get("result", {})
            if output.get("kind") == "decide":
                context.accepted_decisions[name] = output
            elif output.get("verified") is True:
                context.node_outputs[name] = output
        return compiled, rows, context

    @staticmethod
    def _frontier(compiled, rows):
        active, settled, ready = {}, {}, []
        for name in compiled.order:
            parents = compiled.predecessors(name)
            active[name] = name == compiled.plan.entry or any(
                name
                in (rows.get(parent, {}).get("output_snapshot") or {})
                .get("control", {})
                .get("targets", [])
                for parent in parents
            )
            closed = all(settled[parent] for parent in parents)
            row = rows.get(name)
            settled[name] = (not active[name] and closed) or (
                active[name] and row is not None and row["status"] in TERMINAL
            )
            if active[name] and closed and not settled[name]:
                dependencies = compiled.node(name)["depends_on"]
                if all(settled[dep] for dep in dependencies):
                    ready.append(name)
        return ready

    def _record(self, kind, data, context, inputs):
        return {
            "kind": kind,
            "verified": True,
            "data": data,
            "inputs": inputs,
            "employee_id": context.employee_id,
            "scope_revision": context.scope_revision,
            "account_revision": context.account_revision,
        }

    def _perform(self, compiled, node, inputs, context, saved):
        kind = node["type"]
        if kind == "branch":
            for index, case in enumerate(node["cases"]):
                if evaluate_condition(case["condition"], context):
                    return {"case": index}, {"targets": case["next"]}
            return {"case": None}, self._exit(node["on_no_match"])
        if kind == "join":
            return {"joined": True}, {"targets": node["next"]}
        if kind == "transform":
            ref = node["transform"]
            data = run_transform(ref["transform_id"], ref["version"], inputs)
            return self._record(kind, data, context, inputs), {"targets": node["next"]}
        if kind == "complete":
            if self.completion_verifier is not None:
                result = self.completion_verifier(compiled, node, context)
                if (
                    not isinstance(result, dict)
                    or result.get("kind") != "evaluation_execution_finished"
                    or result.get("valid") is not True
                    or result.get("business_goal_verified") is not False
                ):
                    raise ValueError("Evaluation completion may only attest execution finished")
                return {"completion": result}, {"targets": []}
            with self.repository.database.connect() as conn:
                self.repository.guard(conn)
                actions = conn.execute(
                    "SELECT * FROM app.actions WHERE task_id=%s",
                    (self.repository.task_id,),
                ).fetchall()
            result = verify_completion(
                compiled,
                node["id"],
                context,
                instruction=self.repository.instruction(),
                spec=self.repository.spec,
                actions=actions,
            )
            return {"completion": result}, {"targets": []}
        result = self.execute(node, inputs, context, saved)
        if not isinstance(result, NodeResult):
            raise ValueError("Node handler must return a typed saved result")
        route = self._exit(node["on_no_match"]) if result.no_match else {"targets": node["next"]}
        return result.record, route

    @staticmethod
    def _exit(value):
        return (
            {"targets": [value["node_id"]]}
            if "node_id" in value
            else {
                "targets": [],
                "stop": value["stop"],
            }
        )

    def step(self):
        """Advance at most one ready node; all facts survive checkpoint replay."""
        try:
            return self._step()
        except BudgetExceeded as exc:
            return {
                "status": "waiting",
                "request": {
                    "kind": "budget",
                    "reason": str(exc),
                    "code": exc.code,
                },
            }

    def _step(self):
        compiled, rows, context = self._state()
        # Global stop conditions take precedence over independent pending siblings.
        for row in rows.values():
            snapshot = row["output_snapshot"] or {}
            if row["status"] in TERMINAL and snapshot.get("control", {}).get("stop"):
                return {"status": "stopped", "reason": snapshot["control"]["stop"]}
        ready = self._frontier(compiled, rows)
        if not ready:
            completed = [
                row
                for row in rows.values()
                if row["status"] == "succeeded"
                and "completion" in (row["output_snapshot"] or {}).get("result", {})
            ]
            if completed:
                return {
                    "status": "complete",
                    "completion": completed[-1]["output_snapshot"]["result"]["completion"],
                }
            return {"status": "stopped", "reason": "No reachable completion or ready node"}
        # Verify completion after every other activated path has settled. A terminal
        # on a short fan-out path must not predate a pending write on its sibling.
        ready.sort(key=lambda name: compiled.node(name)["type"] == "complete")
        self.repository.transition()
        node = compiled.node(ready[0])
        try:
            if self.before_node:
                self.before_node(node, context, rows.get(node["id"]))
            inputs, provenance = resolve_inputs(node, context, self.repository.spec)
            if node["type"] == "decide" and self.prime_decisions is not None:
                self.prime_decisions(
                    compiled,
                    [name for name in ready if compiled.node(name)["type"] == "decide"],
                    context,
                )
        except NodePending as pending:
            return self._boundary_wait(node, context, rows.get(node["id"]), pending.request)
        except (BindingMissing, BindingInvalid) as exc:
            existing = rows.get(node["id"])
            if existing is not None:
                # A changed source cannot replace the immutable inputs of an
                # already started operation, erase its wait or follow a failure
                # edge past a write whose outcome is still unresolved.
                original = (existing["output_snapshot"] or {}).get("request", {})
                return self._boundary_wait(
                    node,
                    context,
                    existing,
                    {
                        "kind": "reconciliation",
                        "category": "binding_source_changed",
                        "node_id": node["id"],
                        "generation": existing["execution_generation"],
                        "input_digest": existing["input_digest"],
                        "reason": str(exc),
                        **{
                            key: original[key]
                            for key in ("action_id", "operation_id")
                            if original.get(key) is not None
                        },
                        **(
                            {"original_approval_id": original["approval_id"]}
                            if original.get("approval_id") is not None
                            else {}
                        ),
                        **(
                            {"action_id": str(existing["action_id"])}
                            if existing.get("action_id") is not None
                            else {}
                        ),
                    },
                )
            route = node["on_missing" if isinstance(exc, BindingMissing) else "on_error"]
            saved = self.repository.begin(node["id"], {}, {"binding_failure": type(exc).__name__})
            return self._save(node, "failed", {"reason": str(exc)}, self._exit(route), saved)
        saved = self.repository.begin(node["id"], inputs, provenance)
        try:
            result, control = self._perform(compiled, node, inputs, context, saved)
        except NodePending as pending:
            if pending.request.get("category") in {
                "dependency_changed",
                "workflow_changed",
                "permission_changed",
            }:
                return self._boundary_wait(node, context, saved, pending.request)
            self.repository.finish(
                node["id"],
                "unknown" if pending.unknown else "waiting",
                {**(saved["output_snapshot"] or {}), "request": pending.request},
                generation=saved["execution_generation"],
            )
            return {
                "status": "waiting",
                "node_id": node["id"],
                "request": pending.request,
                **({"unknown": True} if pending.unknown else {}),
            }
        except NodeFailure as exc:
            return self._save(
                node, "failed", {"reason": str(exc)}, self._exit(node["on_error"]), saved
            )
        except (BindingMissing, BindingInvalid) as exc:
            exit_name = "on_missing" if isinstance(exc, BindingMissing) else "on_error"
            return self._save(
                node, "failed", {"reason": str(exc)}, self._exit(node[exit_name]), saved
            )
        return self._save(node, "succeeded", result, control, saved)

    def _boundary_wait(self, node, context, saved, request):
        """Pause without changing the original dispatch, approval or result identity."""
        if saved is not None and self.recover_original is not None:
            request = self.recover_original(node, context, saved, request)
        if (
            (self.before_node or self.recover_original)
            and saved is not None
            and saved["status"] == "running"
            and not saved["dispatch_attempts"]
        ):
            self.repository.finish(
                node["id"],
                "waiting",
                {**(saved["output_snapshot"] or {}), "request": request},
                generation=saved["execution_generation"],
            )
        return {"status": "waiting", "node_id": node["id"], "request": request}

    def _save(self, node, status, result, control, saved):
        self.repository.finish(
            node["id"],
            status,
            {"result": result, "control": control},
            generation=saved["execution_generation"],
        )
        if self.after_save:
            self.after_save(node["id"])
        return {"status": "advanced", "node_id": node["id"]}

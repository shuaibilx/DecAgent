"""Bounded Jev fan-out over one current state; raw answers never grant acceptance."""

from dataclasses import dataclass

from decagent.business.questions import BusinessDecisionContext
from decagent.decisions.contracts import (
    DecisionRequest,
    canonical_json,
    digest,
    json_value,
    validate_answers,
)

from .bindings import BindingInvalid, BindingMissing, resolve_inputs
from .runtime import NodePending
from .templates import QuestionRegistry

MAX_QUESTIONS = 8
MAX_CANDIDATES = 256
MAX_INPUT_TOKENS = 8000
FRAMING_TOKEN_RESERVE = 1024


class BatchScopeRequired(ValueError):
    """No legal full candidate/state request fits; ask for a narrower scope."""


@dataclass(frozen=True)
class PreparedBatch:
    request: DecisionRequest
    node_ids: tuple[str, ...]
    plan_revision: int
    plan_digest: str
    provenance_json: str

    @property
    def identity(self):
        return digest(
            {
                "plan": self.plan_digest,
                "nodes": self.node_ids,
                "input": self.request.input_hash,
                "context": vars(self.request.context),
                "questions": [
                    {"id": q.id, "version": q.version, **q.to_wire()}
                    for q in self.request.questions
                ],
            }
        )


def _references(value):
    if isinstance(value, dict):
        if value.get("source") in {"accepted_decision", "node_output"}:
            yield value["node_id"]
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def build_batches(
    compiled,
    ready_ids,
    bound,
    *,
    spec,
    task_id,
    scope_id,
    dataset_version="unvalidated",
    count_tokens=None,
):
    """The caller supplies the activated frontier; speculation is not supported.

    UTF-8 byte length is the conservative default token estimate. A configured
    tokenizer may replace it; provider usage is still settled independently.
    """
    if len(set(ready_ids)) != len(ready_ids):
        raise ValueError("Duplicate ready question")
    selected = [compiled.node(name) for name in ready_ids]
    if any(node["type"] != "decide" for node in selected):
        raise ValueError("Only ready decision nodes may be batched")
    if any(set(_references(node["inputs"])) & set(ready_ids) for node in selected):
        raise ValueError("Same-batch answer or output dependency is forbidden")
    registry = QuestionRegistry.from_spec(spec)
    if registry.fingerprint != compiled.plan.registry_fingerprint:
        raise ValueError("Registered question catalogue changed")
    groups = {}
    for node in selected:
        values, provenance = resolve_inputs(node, bound)
        state = {
            "instruction": values.get("instruction"),
            "directory": values.get("directory", {}),
            "ticket": values.get("ticket"),
            "sources": values.get("sources", {}),
        }
        if not isinstance(state["instruction"], str) or not state["instruction"].strip():
            raise BindingMissing("Current user instruction is missing")
        if type(state["directory"]) is not dict or type(state["sources"]) is not dict:
            raise BindingInvalid("Question evidence must use structured current records")
        definition = registry.get(node["question"]["question_id"], node["question"]["version"])
        candidate_source = definition["candidate_source"]
        if candidate_source:
            rows = state["directory"].get(candidate_source.split(".")[1], [])
            if type(rows) is not list:
                raise BindingInvalid("Candidate directory must be an actual array")
            if len(rows) > 254:
                raise BatchScopeRequired("Candidate scope exceeds the SDK full request limit")
            if not any(row.get("active", True) is True for row in rows):
                raise BindingMissing("No current candidate exists for the requested decision")
        try:
            question = registry.render(
                node["question"]["question_id"],
                state,
                node_id=node["id"],
                version=node["question"]["version"],
            )
        except (ValueError, TypeError) as exc:
            raise BindingInvalid(
                "Current evidence cannot instantiate its trusted template"
            ) from exc
        if len(question.criteria) > MAX_CANDIDATES:
            raise BatchScopeRequired("Candidate scope exceeds the full request limit")
        # Each state also binds the actual employee authorization epoch. It cannot
        # reuse predictions after a credential/role revision or a refreshed read.
        state["sources"] = {
            **state["sources"],
            "scope_revision": bound.scope_revision,
            "account_revision": bound.account_revision,
            "directory_digest": digest(state["directory"]),
            "ticket_digest": digest(state["ticket"]),
        }
        key = digest(state)
        groups.setdefault(key, []).append((node, question, state, provenance))

    batches = []

    def prepare(items):
        state = items[0][2]
        evidence = digest(
            {
                "state": state,
                "employee": bound.employee_id,
                "scope_revision": bound.scope_revision,
                "account_revision": bound.account_revision,
            }
        )
        context = BusinessDecisionContext(
            str(task_id),
            str(scope_id),
            registry.spec.digest,
            registry.spec.manifest["workflow"]["version"],
            "plan-questions-1",
            digest([item[0]["policy"] for item in items]),
            dataset_version,
            evidence,
        )
        request = DecisionRequest(context, tuple(item[1] for item in items), state)
        wire = canonical_json(request.to_wire("x" * 128))
        estimate = count_tokens(wire) if count_tokens else len(wire.encode("utf-8"))
        if type(estimate) is not int or estimate < 0:
            raise ValueError("Input token estimator must return a nonnegative integer")
        if estimate + FRAMING_TOKEN_RESERVE > MAX_INPUT_TOKENS:
            raise BatchScopeRequired("Full question input exceeds token scope limit")
        return PreparedBatch(
            request,
            tuple(item[0]["id"] for item in items),
            compiled.plan.revision,
            compiled.plan.digest,
            canonical_json({item[0]["id"]: item[3] for item in items}),
        )

    for items in groups.values():
        pending = []
        for item in items:
            if len(pending) >= MAX_QUESTIONS:
                batches.append(prepare(pending))
                pending = []
            try:
                prepare([*pending, item])
            except BatchScopeRequired:
                if not pending:
                    raise
                batches.append(prepare(pending))
                pending = [item]
                prepare(pending)  # A single full question must fit; never truncate.
            else:
                pending.append(item)
        if pending:
            batches.append(prepare(pending))
    return tuple(batches)


def evaluate_batch(adapter, prepared):
    result = adapter.evaluate(prepared.request)
    if (
        result.input_hash != prepared.request.input_hash
        or vars(result.context) != vars(prepared.request.context)
        or dict(result.question_versions) != prepared.request.question_versions
    ):
        raise ValueError("Evaluation does not match the prepared batch")
    record = result.to_dict()
    if result.available:
        validate_answers(record["answers"], prepared.request)
    return {
        "batch_id": prepared.identity,
        "evaluation": record,
        "raw_answers": {
            name: {"accepted": False, "value": value}
            for name, value in json_value(record["answers"]).items()
        },
        "actual_requests": len(result.attempts),
        "submitted_questions": len(result.attempts) * len(prepared.node_ids),
        "answered_questions": len(result.answers),
    }


class DecisionBatchRunner:
    """Pre-fetch ready questions, persist raw results, then gate only consumption.

    The adapter must use the shared task usage ledger. The gate is a trusted
    policy service; this class never turns provider confidence into acceptance.
    """

    def __init__(
        self,
        repository,
        adapter,
        gate,
        *,
        after_evaluation=None,
        scope_request=None,
        before_batch=None,
    ):
        self.repository, self.adapter, self.gate = repository, adapter, gate
        self.after_evaluation = after_evaluation
        self.scope_request = scope_request
        self.before_batch = before_batch

    def _uncertain(self, record):
        raise NodePending(
            {
                "kind": "reconciliation",
                "batch_id": record["batch_id"],
                "reason": "Original model request is uncertain; verification required",
            },
            unknown=True,
        )

    def prime(self, compiled, ready_ids, bound):
        rows = {row["node_id"]: row for row in self.repository.nodes()}
        pending = []
        for name in ready_ids:
            saved = rows.get(name, {}).get("output_snapshot") or {}
            marker = saved.get("raw_batch_id") or saved.get("request_batch_id")
            if marker:
                batch = self.repository.batch(marker)
                if not batch or batch["phase"] != "complete":
                    self._uncertain(batch or {"batch_id": marker})
            else:
                pending.append(name)
        task = self.repository.guard()
        # A sibling with missing data must take its own declared exit when selected,
        # rather than make a valid current node fail as collateral to eager batching.
        eligible = []
        for name in pending:
            try:
                build_batches(
                    compiled,
                    [name],
                    bound,
                    spec=self.repository.spec,
                    task_id=self.repository.task_id,
                    scope_id=task["business_scope_id"],
                )
            except (BindingMissing, BindingInvalid):
                if name == ready_ids[0]:
                    raise
            except BatchScopeRequired as exc:
                if name == ready_ids[0]:
                    request = (
                        self.scope_request(name, str(exc))
                        if self.scope_request
                        else {
                            "kind": "clarification",
                            "node_id": name,
                            "reason": str(exc),
                            "question": "请缩小业务范围或候选范围后继续。",
                        }
                    )
                    raise NodePending(request) from exc
            else:
                eligible.append(name)
        batches = build_batches(
            compiled,
            eligible,
            bound,
            spec=self.repository.spec,
            task_id=self.repository.task_id,
            scope_id=task["business_scope_id"],
        )
        for prepared in batches:
            if self.before_batch:
                self.before_batch(compiled, prepared.node_ids, bound)
            saved = {}
            for name in prepared.node_ids:
                node = compiled.node(name)
                values, provenance = resolve_inputs(node, bound)
                saved[name] = self.repository.begin(name, values, provenance)
            metadata = {
                "node_ids": list(prepared.node_ids),
                "plan_revision": prepared.plan_revision,
                "plan_digest": prepared.plan_digest,
                "input_hash": prepared.request.input_hash,
                "employee_id": bound.employee_id,
                "scope_revision": bound.scope_revision,
                "account_revision": bound.account_revision,
                "question_versions": prepared.request.question_versions,
            }
            existing = self.repository.batch(prepared.identity)
            if existing and existing["phase"] == "dispatched":
                self._uncertain(existing)
            record, created = self.repository.start_batch(prepared.identity, metadata, saved)
            if not created:
                if record["phase"] != "complete":
                    self._uncertain(record)
                continue
            bind = getattr(getattr(self.adapter, "budget", None), "bind_request_context", None)
            if bind:
                bind(plan_revision=prepared.plan_revision, node_ids=prepared.node_ids)
            result = evaluate_batch(self.adapter, prepared)
            if self.after_evaluation:
                self.after_evaluation(prepared)
            self.repository.finish_batch(prepared.identity, result, saved)

    def consume(self, node, inputs, bound, saved):
        marker = (saved["output_snapshot"] or {}).get("raw_batch_id")
        batch = self.repository.batch(marker) if marker else None
        if not batch or batch["phase"] != "complete":
            self._uncertain(batch or {"batch_id": marker})
        if (
            batch["employee_id"] != bound.employee_id
            or batch["scope_revision"] != bound.scope_revision
            or batch["account_revision"] != bound.account_revision
        ):
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "node_id": node["id"],
                    "reason": "Decision observation is stale for current permissions",
                }
            )
        raw = batch["result"]["raw_answers"].get(node["id"])
        if not raw:
            raise NodePending(
                {
                    "kind": "reconciliation",
                    "batch_id": marker,
                    "reason": "Decision unavailable; inspect original usage and result",
                }
            )
        return self.gate(node, raw["value"], bound, batch)

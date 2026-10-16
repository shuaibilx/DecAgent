"""Business runtime services: lazy planning, common governance and durable receipts."""

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.business.actions import (
    assess_action,
    explicit_command,
    literal_request,
    matches_literal,
)
from decagent.business.calibration import BusinessCalibration
from decagent.business.codec import strict_json
from decagent.business.execution import BusinessExecutor, BusinessReadError
from decagent.business.planner import PlannerProtocol
from decagent.business.questions import (
    BusinessQuestionsUnavailable,
    build_business_request,
    priority_for_score,
)
from decagent.business.registry import PluginRegistry
from decagent.business.rules import CREATE_PARAMETER_MARKERS, simple_read_params
from decagent.business.rules import proposal as rule_proposal
from decagent.business.tickets import builtin_manifest
from decagent.decisions import JevAdapter
from decagent.decisions.contracts import digest, evaluation, json_value, validate_answers
from decagent.models.primary import (
    ModelUsage,
    PrimaryModel,
    PrimaryRequestNotDispatched,
    PrimaryResponseRejected,
)
from decagent.runtime.model_calls import BudgetedPrimary, LedgerAttemptBudget
from decagent.storage.budget import BudgetExceeded
from decagent.storage.safe_record import exact_safe_record
from decagent.storage.usage import UsageLedger

UUID_PATTERN = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"


class BusinessServices:
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
        primary_factory=None,
    ):
        self.database, self.settings, self.config, self.task = (
            database,
            settings,
            runtime_config,
            task,
        )
        self.spec = PluginRegistry(database).get(
            task["business_scope_id"], task["authorization_snapshot"]["plugin_digest"]
        )
        self.executor = BusinessExecutor(
            database, self.spec, task, owner, generation, mcp_client=mcp_client
        )
        self.ledger = UsageLedger(database, task["id"])
        self.protocol = PlannerProtocol(self.spec)
        self.primary_factory, self._primary, self._jev = primary_factory, None, None
        self.profile, self.profile_error = None, None
        path = Path(os.getenv("DECAGENT_BUSINESS_CALIBRATION_FILE", ""))
        if str(path) != ".":
            try:
                self.profile = BusinessCalibration.from_dict(
                    strict_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError):
                self.profile_error = "invalid_calibration"

    def close(self):
        if self._jev is not None:
            self._jev.close()

    def guard(self):
        row = self.executor.guard()
        if row["active_seconds"] >= row["budget"].get("max_active_seconds", 1800):
            raise BudgetExceeded("Active task time exhausted")
        return row

    def reconciliation_guard(self):
        """Existing receipt checks retain lease/identity guards without a new call budget."""
        return self.executor.guard()

    def run_phase(self, phase, data):
        if phase in {"verify", "finalize"}:
            self.reconciliation_guard()
        else:
            self.guard()
        method = getattr(self, "_" + phase, None)
        if phase not in {"prepare", "decide", "plan", "validate", "execute", "verify", "finalize"}:
            raise ValueError("Unknown business phase")
        with self.database.connect() as conn:
            self.executor.guard(conn)
            task = conn.execute(
                "UPDATE app.tasks SET phase=%s,event_seq=event_seq+1 WHERE id=%s "
                "RETURNING event_seq",
                (phase, self.task["id"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO app.events(task_id,project_id,seq,kind,payload) "
                "VALUES(%s,NULL,%s,'business.phase',%s)",
                (
                    self.task["id"],
                    task["event_seq"],
                    Jsonb({"phase": phase, "step": data.get("steps", 0)}),
                ),
            )
        return {"phase": phase, **method(data)}

    @staticmethod
    def _literal(instruction):
        return literal_request(instruction)

    def _evidence(self, data, proposal=None):
        evidence = dict(data.get("evidence", {}))
        sources = dict(evidence.get("sources", {}))
        if "directory" in self.spec.commands:
            evidence["directory"] = self.executor.read("directory", {})
            sources["directory"] = digest(evidence["directory"])
        literal = proposal or self._literal(data["instruction"])
        ticket_id = (literal or {}).get("params", {}).get("ticket_id")
        if not ticket_id and literal is None:
            directory_ids = {
                item["id"]
                for kind in ("people", "categories")
                for item in evidence.get("directory", {}).get(kind, [])
            }
            matches = [
                item
                for item in re.findall(UUID_PATTERN, data["instruction"])
                if item not in directory_ids
            ]
            ticket_id = matches[0] if len(matches) == 1 else None
        if ticket_id and "get" in self.spec.commands:
            try:
                fetched = self.executor.read("get", {"ticket_id": ticket_id})
            except BusinessReadError as error:
                if error.code != "not_found":
                    raise
                evidence["ticket"] = None
                evidence["missing_ticket"] = ticket_id
            else:
                evidence.pop("missing_ticket", None)
                evidence["ticket"] = fetched["ticket"]
                sources["ticket"] = digest(fetched)
        evidence["sources"] = sources
        return evidence

    def _prepare(self, data):
        evidence = self._evidence(data)
        if evidence.get("missing_ticket"):
            return {
                "evidence": evidence,
                "route": "human",
                "human_kind": "clarification",
                "question": "未找到指定工单，请提供存在的工单 ID。",
            }
        return {"evidence": evidence, "route": "decide"}

    def _decide(self, data):
        if data.get("queue"):
            return {"route": "validate"}
        if data.get("planning_mode") == "literal" and data.get("observations"):
            return {
                "completion": {"valid": True, "summary": "Requested operation verified"},
                "route": "finalize",
            }
        literal = self._literal(data["instruction"])
        if literal and not data.get("observations") and data.get("strategy", "hybrid") != "main":
            return {"queue": [literal], "planning_mode": "literal", "route": "validate"}
        if data.get("planning_mode") == "primary" or self.spec.level == "basic":
            return {"route": "plan"}
        if data.get("strategy") == "rule":
            proposal = rule_proposal(self.spec, data["instruction"], data["evidence"])
            if proposal:
                return {"queue": [proposal], "planning_mode": "literal", "route": "validate"}
            return {
                "question": "请提供明确的命令与参数，或改用 main/hybrid 策略处理自然语言。",
                "human_kind": "clarification",
                "route": "human",
            }
        if data.get("strategy", "hybrid") == "main":
            return {"route": "plan"}
        proposal = rule_proposal(self.spec, data["instruction"], data["evidence"])
        if proposal and proposal["command"] in {"list", "directory", "get"}:
            return {"queue": [proposal], "planning_mode": "literal", "route": "validate"}
        return self._typed_route(data)

    def _typed_route(self, data):
        try:
            request = build_business_request(
                self.spec,
                str(self.task["id"]),
                str(self.task["business_scope_id"]),
                data["instruction"],
                data["evidence"],
                dataset_version=self.profile.dataset_version if self.profile else "uncalibrated",
            )
        except BusinessQuestionsUnavailable as unavailable:
            reason = str(unavailable)
            snapshot = {
                "request": {
                    "context": vars(unavailable.context),
                    "state": json_value(unavailable.state),
                    "questions": [],
                },
                "evaluation": {"answers": {}, "actual_model": None, "error": reason},
                "accepted_questions": [],
                "route_reason": reason,
            }
            key = digest(snapshot["request"])
            with self.database.connect() as conn:
                self.executor.guard(conn)
                prior = conn.execute(
                    "SELECT id FROM app.decision_records WHERE task_id=%s AND input_hash=%s",
                    (self.task["id"], key),
                ).fetchone()
                if not prior:
                    conn.execute(
                        "INSERT INTO app.decision_records(id,task_id,input_hash,question_version,"
                        "policy_version,requested_model,resolved_model,result,final_route) "
                        "VALUES(%s,%s,%s,%s,%s,%s,NULL,%s,'plan')",
                        (
                            uuid4(),
                            self.task["id"],
                            key,
                            unavailable.context.question_version,
                            unavailable.context.policy_version,
                            self.settings.jev_model,
                            Jsonb(exact_safe_record(snapshot, self.database.redactor)),
                        ),
                    )
            return {"route": "plan", "reason": reason}
        key = request.cache_key(self.settings.jev_model)
        with self.database.connect() as conn:
            prior = conn.execute(
                "SELECT * FROM app.decision_records WHERE task_id=%s "
                "AND input_hash=%s ORDER BY created_at DESC LIMIT 1",
                (self.task["id"], key),
            ).fetchone()
        if prior:
            saved = prior["result"]["evaluation"]
            answers = validate_answers(saved["answers"], request) if saved["answers"] else {}
        else:
            if not self.profile or not self.profile.compatible(request, self.settings.jev_model):
                unavailable = self.profile_error or "missing_compatible_calibration_or_credentials"
            elif not any(
                self.profile.thresholds.get(question.id) is not None
                for question in request.questions
            ):
                unavailable = "no_enabled_calibrated_questions"
            elif not self.settings.jev_api_key:
                unavailable = "missing_compatible_calibration_or_credentials"
            else:
                unavailable = None
            if unavailable:
                result = evaluation(
                    request,
                    "hybrid",
                    self.settings.jev_model,
                    error=unavailable,
                )
            else:
                if self._jev is None:
                    self._jev = JevAdapter(
                        api_key=self.settings.jev_api_key,
                        model=self.settings.jev_model,
                        provider=self.settings.jev_provider,
                        cache_ttl=0,
                        max_retries=0,
                        budget=LedgerAttemptBudget(
                            self.ledger,
                            reservation_usd=self.config.jev_reservation,
                            before_dispatch=self.guard,
                        ),
                    )
                result = self._jev.evaluate(request)
            saved, answers = result.to_dict(), dict(result.answers)
        accepted = {
            name: answer
            for name, answer in answers.items()
            if self.profile and self.profile.accepts(request, saved["actual_model"], name, answer)
        }
        update = (
            {"route": "plan", "reason": "no_enabled_calibrated_questions"}
            if saved.get("error") == "no_enabled_calibrated_questions"
            else self._fast_proposal(data, accepted)
        )
        route = update["route"]
        if not prior:
            snapshot = {
                "request": {
                    "context": vars(request.context),
                    "state": json_value(request.state),
                    "questions": [
                        {"id": q.id, "version": q.version, **q.to_wire()} for q in request.questions
                    ],
                },
                "evaluation": saved,
                "accepted_questions": sorted(accepted),
                "route_reason": update.get("reason", "typed_candidate_validated_next"),
            }
            with self.database.connect() as conn:
                self.executor.guard(conn)
                conn.execute(
                    "INSERT INTO app.decision_records(id,task_id,input_hash,question_version,"
                    "policy_version,requested_model,resolved_model,result,final_route) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        uuid4(),
                        self.task["id"],
                        key,
                        request.context.question_version,
                        request.context.policy_version,
                        self.settings.jev_model,
                        saved["actual_model"],
                        Jsonb(exact_safe_record(snapshot, self.database.redactor)),
                        route,
                    ),
                )
        return update

    def _fast_proposal(self, data, answers):
        fallback = {"route": "plan", "reason": "No compatible certain typed fast path"}
        builtin = builtin_manifest()
        if (
            self.spec.id != builtin["id"]
            or self.spec.manifest.get("workflow", {}).get("questions")
            != builtin["workflow"]["questions"]
        ):
            return fallback
        if "operation" not in answers or answers["operation"].choice == "no_match":
            return fallback
        command = answers["operation"].choice
        if (
            command not in builtin["commands"]
            or command not in self.spec.commands
            or self.spec.commands[command].input_schema
            != builtin["commands"][command]["input_schema"]
        ):
            return fallback
        ticket = data["evidence"].get("ticket")
        params = {}
        if command in {"list", "directory"}:
            params = simple_read_params(command, data["instruction"])
            if params is None:
                return fallback
        elif command == "get" and ticket:
            params = {"ticket_id": ticket["id"]}
        elif command in {"start", "close", "reopen"} and ticket:
            params = {"ticket_id": ticket["id"], "expected_revision": ticket["revision"]}
        elif command == "assign" and ticket and "person" in answers:
            if answers["person"].choice == "no_match":
                return {
                    "route": "human",
                    "human_kind": "clarification",
                    "question": "请指定处理人的部门或目录中的准确 ID。",
                }
            params = {
                "ticket_id": ticket["id"],
                "person_id": answers["person"].choice,
                "expected_revision": ticket["revision"],
            }
        elif command == "create" and all(
            key in answers
            for key in (
                "category",
                "work_blockage",
                "explicit_assignee",
            )
        ):
            if answers["explicit_assignee"].probability > 0.5 or any(
                word in data["instruction"].casefold() for word in CREATE_PARAMETER_MARKERS
            ):
                return fallback  # Compound create+assign plans retain the primary planner.
            if answers["category"].choice == "no_match":
                return {
                    "route": "human",
                    "human_kind": "clarification",
                    "question": "请从实际业务目录中指定工单分类。",
                }
            maximum = self.spec.commands["create"].input_schema["properties"]["title"]["maxLength"]
            params = {
                "title": data["instruction"][:maximum],
                "description": data["instruction"],
                "category_id": answers["category"].choice,
                "priority": priority_for_score(answers["work_blockage"].score),
            }
        else:
            return fallback
        return {
            "queue": [{"command": command, "params": params}],
            "planning_mode": "literal",
            "route": "validate",
        }

    def _model(self):
        if self._primary is None:
            model = (
                self.primary_factory(self.settings, self.protocol)
                if self.primary_factory
                else PrimaryModel.from_settings(
                    self.settings,
                    tool_definitions=self.protocol.definitions,
                    validator=self.protocol.validate,
                )
            )
            self._primary = BudgetedPrimary(
                model,
                self.ledger,
                price=self.config.main_price,
                unknown_cost_reservation=self.config.unknown_main_reservation,
                before_dispatch=self.guard,
            )
        return self._primary

    def _plan(self, data):
        messages = data.get("messages") or [
            {"role": "system", "content": self.protocol.instructions()},
            {
                "role": "user",
                "content": data["instruction"]
                + "\nActual business evidence:\n"
                + json.dumps(data.get("evidence", {}), ensure_ascii=False)
                + "\nPreviously verified operations (reuse, never repeat):\n"
                + json.dumps(data.get("observations", []), ensure_ascii=False),
            },
        ]
        key = digest({"messages": messages, "plugin": self.spec.digest})
        with self.database.connect() as conn:
            prior = conn.execute(
                "SELECT result FROM app.decision_records WHERE task_id=%s "
                "AND input_hash=%s AND question_version='primary-plan-1'",
                (self.task["id"], key),
            ).fetchone()
        if prior:
            record = prior["result"]
        else:
            start = time.monotonic()
            resolved = None
            try:
                turn = self._model().respond(messages)
            except PrimaryRequestNotDispatched as error:
                record = {
                    "error": "planner_response_invalid",
                    "rejection_reason": error.reason,
                    "proposals": [],
                    "usage": vars(ModelUsage(0, 0, 0)),
                }
            except PrimaryResponseRejected as error:
                resolved = error.model
                record = {
                    "error": "planner_response_invalid",
                    "rejection_reason": error.reason,
                    "proposals": [],
                    "usage": vars(error.usage) if error.usage else None,
                }
            except ValueError:
                record = {"error": "planner_response_invalid", "proposals": []}
            else:
                resolved = turn.model
                record = {
                    "assistant": turn.assistant_message(),
                    "proposals": [
                        {
                            "command": self.protocol.command(t.name),
                            "params": json_value(t.params),
                            "tool_call_id": t.id,
                        }
                        for t in turn.tools
                    ],
                    "usage": vars(turn.usage) if turn.usage else None,
                }
            record["elapsed_seconds"] = time.monotonic() - start
            with self.database.connect() as conn:
                self.executor.guard(conn)
                conn.execute(
                    "INSERT INTO app.decision_records(id,task_id,input_hash,question_version,"
                    "policy_version,requested_model,resolved_model,result,final_route) "
                    "VALUES(%s,%s,%s,'primary-plan-1','business-1',%s,%s,%s,'planning')",
                    (
                        uuid4(),
                        self.task["id"],
                        key,
                        self.settings.main_model,
                        resolved,
                        Jsonb(exact_safe_record(record, self.database.redactor)),
                    ),
                )
        if record.get("error"):
            failures = data.get("plan_failures", 0) + 1
            if failures >= 2:
                return {
                    "plan_failures": failures,
                    "route": "human",
                    "human_kind": "clarification",
                    "question": (
                        "主模型两次未返回符合插件契约的操作。请补充明确目标，或检查模型配置后恢复。"
                    ),
                }
            return {
                "plan_failures": failures,
                "planning_mode": "primary",
                "route": "plan",
                "messages": [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Program validation rejected the previous response. "
                            "Return only registered tools with complete valid parameters; "
                            "use decagent_ask_user for missing facts."
                        ),
                    },
                ],
            }
        if not record["proposals"]:
            return {
                "route": "human",
                "human_kind": "clarification",
                "question": "主模型没有提出可执行操作，请补充任务目标或必要参数。",
            }
        return {
            "messages": [*messages, record["assistant"]],
            "queue": record["proposals"],
            "planning_mode": "primary",
            "plan_failures": 0,
            "route": "validate",
        }

    def _validate(self, data):
        proposal = data["queue"][0]
        command, params = proposal["command"], proposal["params"]
        if command == "decagent_ask_user":
            self.protocol.validate(command, params)
            return {"question": params["question"], "human_kind": "clarification", "route": "human"}
        if command == "decagent_finish":
            self.protocol.validate(command, params)
            observations = data.get("observations", [])
            observed = {o["action_id"] for o in observations}
            commands = self.spec.commands
            writes = {
                o["action_id"] for o in observations if commands[o["command"]].effect == "write"
            }
            claimed = set(params["completed_actions"])
            valid = bool(claimed) and writes <= claimed <= observed
            return {
                "completion": {"valid": valid, "summary": params["summary"]},
                "route": "finalize",
            }
        evidence = self._evidence(data, proposal)
        assessment = assess_action(
            self.spec,
            str(self.task["id"]),
            str(self.task["business_scope_id"]),
            data["instruction"],
            command,
            params,
            evidence,
        )
        if data.get("force_confirmation") and assessment["outcome"] == "allow":
            assessment = {
                **assessment,
                "outcome": "confirm",
                "reason": "Edited parameters require a new concrete confirmation",
            }
        key = "business:" + str(data.get("action_index", 0)) + ":" + assessment["digest"]
        action = self.executor.plan(key, assessment)
        update = {"evidence": evidence, "action_id": str(action["id"]), "assessment": assessment}
        if assessment["outcome"] == "deny":
            with self.database.connect() as conn:
                self.executor.guard(conn)
                conn.execute("UPDATE app.actions SET status='denied' WHERE id=%s", (action["id"],))
            return {
                **update,
                "completion": {"valid": False, "summary": assessment["reason"]},
                "route": "finalize",
            }
        if assessment["outcome"] == "stale":
            # The revision is a program-owned parameter source. Refresh it, then
            # generate a new proposal and confirmation instead of reusing old authority.
            refreshed = {
                **proposal,
                "params": {**params, "expected_revision": evidence["ticket"]["revision"]},
            }
            return {
                "evidence": evidence,
                "queue": [refreshed, *data["queue"][1:]],
                "approval_id": None,
                "action_index": data.get("action_index", 0) + 1,
                "route": "validate",
            }
        if assessment["outcome"] == "clarify":
            return {
                **update,
                "human_kind": "clarification",
                "question": assessment["reason"],
                "route": "human",
            }
        return {**update, "route": "execute"}

    def _human_digest(self, data):
        kind = data.get("human_kind", "clarification")
        conditional = (
            {"conditional_binding": data["conditional_binding"]}
            if self.task["graph_version"] == "business-3" and "conditional_binding" in data
            else {}
        )
        return (
            data["assessment"]["digest"]
            if kind == "action"
            else digest(
                {
                    "task_id": str(self.task["id"]),
                    "scope": str(self.task["business_scope_id"]),
                    "plugin": self.spec.digest,
                    "instruction": data["instruction"],
                    "question": data["question"],
                    "step": data.get("action_index", 0),
                    **conditional,
                }
            )
        )

    def human_request(self, data):
        self.guard()
        kind, bound = data.get("human_kind", "clarification"), self._human_digest(data)
        subject = {
            "question": data["question"],
            "kind": kind,
            "digest": bound,
            "employee_id": str(self.task["employee_id"]),
        }
        if self.task["graph_version"] == "business-3" and "conditional_binding" in data:
            subject["conditional_binding"] = data["conditional_binding"]
        if kind == "action":
            operation = data["operation"]
            subject.update(
                operation_id=operation["operation_id"], input_hash=operation["input_hash"]
            )
        with self.database.connect() as conn:
            self.executor.guard(conn)
            conn.execute(
                "INSERT INTO app.approvals(id,task_id,action_id,digest,kind,reply_payload) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(task_id,digest,kind) DO NOTHING",
                (
                    uuid4(),
                    self.task["id"],
                    data.get("action_id") if kind == "action" else None,
                    bound,
                    kind,
                    Jsonb(exact_safe_record(subject, self.database.redactor)),
                ),
            )
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE task_id=%s AND digest=%s AND kind=%s",
                (self.task["id"], bound, kind),
            ).fetchone()
        return {
            "kind": kind,
            "approval_id": str(approval["id"]),
            "digest": bound,
            "question": data["question"],
            "reason": data.get("assessment", {}).get("reason"),
            "action": data.get("assessment", {}).get("snapshot") if kind == "action" else None,
            "operation_id": data.get("operation_id") if kind == "action" else None,
            "operation": data.get("operation") if kind == "action" else None,
        }

    def human_reply(self, data, reply):
        self.guard()
        with self.database.connect() as conn:
            self.executor.guard(conn)
            approval = conn.execute(
                "SELECT * FROM app.approvals WHERE id=%s AND task_id=%s FOR UPDATE",
                (reply["approval_id"], self.task["id"]),
            ).fetchone()
            if not approval or approval["status"] not in {"replied", "consumed"}:
                raise ValueError("Business approval has no authenticated response")
            if (
                approval["kind"] != data.get("human_kind", "clarification")
                or approval["digest"] != self._human_digest(data)
                or (
                    approval["kind"] == "action" and str(approval["action_id"]) != data["action_id"]
                )
            ):
                raise ValueError(
                    "Business response does not match the current question or proposal"
                )
            record = (approval["reply_payload"] or {}).get("policy_record", {})
            if (
                record.get("business") is not True
                or record.get("employee_id") != str(self.task["employee_id"])
                or record.get("source") != "employee_session"
            ):
                raise ValueError("Business response provenance is invalid")
            if approval["reply"] == "reject":
                return {
                    "completion": {
                        "valid": False,
                        "summary": "Employee rejected the business action",
                    },
                    "route": "finalize",
                }
            if record.get("status") == "stale":
                conn.execute(
                    "UPDATE app.approvals SET status='consumed' WHERE id=%s", (approval["id"],)
                )
                return {"approval_id": None, "route": "validate"}
            if approval["kind"] == "action" and approval["reply"] in {"approve", "confirm"}:
                if (
                    record.get("action_digest") != approval["digest"]
                    or record.get("status") != "approved"
                    or record.get("operation_id") != data.get("operation_id")
                    or record.get("input_hash") != data["operation"]["input_hash"]
                ):
                    raise ValueError("Business approval does not authorize the current proposal")
                conn.execute(
                    "UPDATE app.approvals SET status='consumed' WHERE id=%s", (approval["id"],)
                )
                return {"approval_id": str(approval["id"]), "route": "execute"}
            conn.execute(
                "UPDATE app.approvals SET status='consumed' WHERE id=%s", (approval["id"],)
            )
        if approval["reply"] == "edit":
            proposal = {**data["queue"][0], "params": record["edited_params"]}
            instruction = data["instruction"]
            if self._literal(instruction):
                instruction = json.dumps(
                    {"command": proposal["command"], "params": proposal["params"]},
                    ensure_ascii=False,
                )
            return {
                "instruction": instruction,
                "queue": [proposal, *data["queue"][1:]],
                "force_confirmation": True,
                "approval_id": None,
                "operation_id": None,
                "operation": None,
                "action_index": data.get("action_index", 0) + 1,
                "route": "validate",
            }
        content = record.get("confirmation")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Clarification requires a concrete employee fact")
        return {
            "instruction": data["instruction"] + "\n用户补充：" + content,
            "messages": [],
            "queue": [],
            "planning_mode": None,
            "plan_failures": 0,
            "action_index": data.get("action_index", 0) + 1,
            "route": "prepare",
        }

    def _execute(self, data):
        # A changed resource may be the result of this very operation. Consult the
        # durable journal before freshness checks that apply only to undispatched work.
        with self.database.connect() as conn:
            self.executor.guard(conn)
            action = conn.execute(
                "SELECT status,digest FROM app.actions WHERE id=%s AND task_id=%s",
                (data["action_id"], self.task["id"]),
            ).fetchone()
        if not action:
            raise ValueError("Business action does not belong to this task")
        if action["status"] in {"succeeded", "running", "unknown"}:
            result = self.executor.execute(data["action_id"], data["assessment"]["digest"])
            return self._execution_route(result)
        proposal = data["queue"][0]
        evidence = self._evidence(data, proposal)
        current = assess_action(
            self.spec,
            str(self.task["id"]),
            str(self.task["business_scope_id"]),
            data["instruction"],
            proposal["command"],
            proposal["params"],
            evidence,
        )
        if data.get("force_confirmation") and current["outcome"] == "allow":
            current = {**current, "outcome": "confirm"}
        if (
            current["digest"] != data["assessment"]["digest"]
            or current["outcome"] not in {"allow", "confirm"}
            or current["outcome"] != data["assessment"]["outcome"]
        ):
            return {
                "evidence": evidence,
                "approval_id": None,
                "action_index": data.get("action_index", 0) + 1,
                "route": "validate",
            }
        result = self.executor.execute(
            data["action_id"], current["digest"], data.get("approval_id")
        )
        return self._execution_route(result)

    @staticmethod
    def _execution_route(result):
        if result.get("status") == "awaiting_approval":
            return {
                "execution": None,
                "operation_id": result["operation_id"],
                "operation": result["operation"],
                "human_kind": "action",
                "question": "请核对服务端保存的具体操作并确认。",
                "route": "human",
            }
        return {"execution": result, "route": "verify"}

    def _verify(self, data):
        result = data.get("execution") or {}
        if data.get("reconciliation") or result.get("status") == "unknown":
            result = self.executor.reconcile(data["action_id"])
        if result.get("status") == "awaiting_approval":
            return self._execution_route(result)
        if result.get("status") == "unknown":
            from decagent.business.errors import BusinessReconciliationRequired

            raise BusinessReconciliationRequired(
                {
                    "action_id": data["action_id"],
                    "reason": result.get("reason", "Write result is unknown"),
                }
            )
        if result.get("status") != "succeeded":
            return {
                "completion": {
                    "valid": False,
                    "summary": result.get("reason", "Business operation failed"),
                },
                "route": "finalize",
            }
        proposal = data["queue"][0]
        observation = {
            "action_id": data["action_id"],
            "command": proposal["command"],
            "params": proposal["params"],
            "data": result["data"],
            "verification": result["verification"],
        }
        messages = list(data.get("messages", []))
        if proposal.get("tool_call_id"):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": proposal["tool_call_id"],
                    "content": json.dumps(observation, ensure_ascii=False),
                }
            )
        return {
            "observations": [*data.get("observations", []), observation],
            "messages": messages,
            "queue": data["queue"][1:],
            "action_id": None,
            "approval_id": None,
            "operation_id": None,
            "operation": None,
            "action_index": data.get("action_index", 0) + 1,
            "execution": None,
            "reconciliation": None,
            "reconciliation_reply": None,
            "force_confirmation": False,
            "route": "decide",
        }

    def _finalize(self, data):
        completion = dict(
            data.get("completion", {"valid": False, "summary": "No verified goal completion"})
        )
        observations = data.get("observations", [])
        registered = self.spec.commands
        required = {name for name in registered if explicit_command(data["instruction"], name)}
        literal = self._literal(data["instruction"])
        if literal:
            required = {literal["command"]} & registered.keys()
        queue = data.get("queue", [])
        prior_failure = not completion.get("valid") and (
            bool(completion.get("reason"))
            or (bool(queue) and queue[0]["command"] != "decagent_finish")
        )
        if not required and not prior_failure:
            completion["valid"] = False
            completion["reason"] = "goal_unproven"
            completion["summary"] = (
                "The requested goal could not be established by the registered contract."
            )
        if completion.get("valid"):
            commands = {o["command"] for o in observations}
            with self.database.connect() as conn:
                durable = conn.execute(
                    "SELECT id,status,receipt FROM app.actions WHERE task_id=%s", (self.task["id"],)
                ).fetchall()
            verified = {
                str(a["id"]) for a in durable if a["status"] == "succeeded" and a["receipt"]
            }
            completion["valid"] = (
                bool(required)
                and bool(observations)
                and required <= commands
                and all(o["action_id"] in verified for o in observations)
            )
            if literal:
                completion["valid"] = completion["valid"] and any(
                    matches_literal(literal, o["command"], o["params"]) for o in observations
                )
            if "create" in required and "assign" in required:
                created = {
                    o["data"].get("ticket", {}).get("id")
                    for o in observations
                    if o["command"] == "create"
                }
                assigned = {
                    o["params"].get("ticket_id") for o in observations if o["command"] == "assign"
                }
                completion["valid"] = completion["valid"] and bool(created & assigned)
        completion["actions"] = [o["action_id"] for o in observations]
        return {"completion": completion, "route": "__end__"}

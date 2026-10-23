"""Durable reservations, explicit attribution and versioned action guards."""

import math
import re
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg.types.json import Jsonb

from decagent.business.guest_jev_pricing import guest_jev_profile_is_bounded
from decagent.storage.budget import BudgetExceeded

MODEL_BUDGET_VERSION = "1"
CONDITIONAL_MODEL_DEFAULTS = {
    "model_budget_version": MODEL_BUDGET_VERSION,
    "primary_target": 3,
    "max_primary_calls": 6,
    "max_jev_calls": 24,
    "max_calls": 30,
    "max_tokens": 100_000,
    "max_active_seconds": 1800,
    "max_actions": 128,
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
SAFE_MODEL_VALUE = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:/~-]{0,254}\Z")


def _safe_model_value(value, name):
    if value is not None and (
        not isinstance(value, str) or SAFE_MODEL_VALUE.fullmatch(value) is None
    ):
        raise ValueError(f"Invalid {name}")
    return value


def _safe_endpoint(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("Invalid endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"})
    ):
        raise ValueError("Invalid endpoint")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


class UsageLedger:
    def __init__(
        self,
        database,
        task_id,
        *,
        trial_budget=None,
        guest_workspace_id=None,
        guest_dispatch_disabled=False,
    ):
        if (trial_budget is None) != (guest_workspace_id is None):
            raise ValueError("Guest workspace and trial budget must be configured together")
        if type(guest_dispatch_disabled) is not bool:
            raise ValueError("Guest dispatch gate must be boolean")
        self.database = database
        self.task_id = task_id
        self.trial_budget = trial_budget
        self.guest_workspace_id = guest_workspace_id
        self.guest_dispatch_disabled = guest_dispatch_disabled

    def _trial_reservation_id(self, conn, usage_record_id):
        row = conn.execute(
            "SELECT id FROM business.guest_trial_usage_reservations "
            "WHERE usage_record_id=%s AND task_id=%s AND workspace_id=%s FOR UPDATE",
            (usage_record_id, self.task_id, self.guest_workspace_id),
        ).fetchone()
        if not row:
            raise BudgetExceeded(
                "Guest trial reservation history is missing", code="guest_trial_ledger_missing"
            )
        return row["id"]

    def charge_active(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Active duration must be nonnegative")
        with self.database.connect() as conn:
            row = conn.execute(
                "UPDATE app.tasks SET active_seconds=active_seconds+%s WHERE id=%s "
                "AND state IN ('running','recovering') RETURNING active_seconds",
                (seconds, self.task_id),
            ).fetchone()
            return row["active_seconds"] if row else None

    @staticmethod
    def _held_usage(rows):
        tokens = sum(
            r["actual_tokens"]
            if r["status"] == "settled"
            else max(r["requested_tokens"], r["actual_tokens"] or 0)
            for r in rows
        )
        cost = sum(
            (
                r["actual_cost"]
                if r["status"] == "settled"
                else max(r["reserved_cost"] or Decimal("0"), r["actual_cost"] or Decimal("0"))
                for r in rows
            ),
            Decimal("0"),
        )
        return tokens, cost

    def _task(self, conn):
        task = conn.execute(
            "SELECT *,lease_until>clock_timestamp() AS budget_lease_valid "
            "FROM app.tasks WHERE id=%s FOR UPDATE",
            (self.task_id,),
        ).fetchone()
        if not task:
            raise KeyError("Task not found")
        return task

    def _rows(self, conn):
        return conn.execute(
            "SELECT * FROM app.usage_records WHERE task_id=%s", (self.task_id,)
        ).fetchall()

    @staticmethod
    def _bounds(task):
        if task["graph_version"] != "business-3":
            return task["budget"]
        bounds = {**CONDITIONAL_MODEL_DEFAULTS, **task["budget"]}
        if bounds["model_budget_version"] != MODEL_BUDGET_VERSION:
            raise ValueError("Unsupported conditional model budget version")
        for name in ("primary_target", "max_primary_calls", "max_jev_calls"):
            if type(bounds[name]) is not int or bounds[name] < 0:
                raise ValueError(f"Invalid budget field: {name}")
        for name in ("max_calls", "max_tokens", "max_active_seconds", "max_actions"):
            if type(bounds[name]) is not int or bounds[name] <= 0:
                raise ValueError(f"Invalid budget field: {name}")
        if bounds["primary_target"] > bounds["max_primary_calls"]:
            raise ValueError("Primary target exceeds its hard limit")
        return bounds

    @staticmethod
    def _counted(rows):
        return [row for row in rows if row.get("dispatch_state") != "not_sent"]

    @staticmethod
    def _unknown_cost_rows(rows):
        return [
            row
            for row in rows
            if row["actual_cost"] is None
            and (
                row["status"] == "unknown"
                or row.get("dispatch_state") in {"dispatching", "unknown"}
            )
        ]

    @staticmethod
    def _has_successful_recovery(row, rows):
        by_id = {str(item["id"]): item for item in rows}
        for candidate in rows:
            if candidate["status"] != "settled" or candidate.get("dispatch_state") != "responded":
                continue
            if candidate["actual_cost"] is None:
                continue
            current = candidate
            visited = set()
            while current.get("recovery_of") is not None:
                parent_id = str(current["recovery_of"])
                if parent_id in visited:
                    break
                visited.add(parent_id)
                if parent_id == str(row["id"]):
                    return True
                current = by_id.get(parent_id)
                if current is None:
                    break
        return False

    @classmethod
    def _valid_recovery(cls, recovery_id, rows, *, recovery_receipt_id=None):
        by_id = {str(row["id"]): row for row in rows}
        candidate = by_id.get(str(recovery_id))
        if candidate is None or candidate["model_family"] != "primary":
            return False
        ancestors = set()
        current = candidate
        retryable_unknown = False
        while current is not None:
            current_id = str(current["id"])
            if current_id in ancestors or current.get("model_family") != "primary":
                return False
            ancestors.add(current_id)
            if (
                current["status"] == "unknown"
                and current.get("dispatch_state") == "unknown"
                and current.get("error_category")
                in {"timeout", "connection", "transient_http"}
            ):
                retryable_unknown = True
            parent_id = current.get("recovery_of")
            current = by_id.get(str(parent_id)) if parent_id is not None else None
        candidate_unknown = (
            candidate["status"] == "unknown"
            and candidate.get("dispatch_state") == "unknown"
            and candidate.get("error_category")
            in {"timeout", "connection", "transient_http"}
        )
        candidate_proven_not_sent = (
            candidate["status"] == "settled"
            and candidate.get("dispatch_state") == "not_sent"
            and bool(candidate.get("error_code"))
        )
        if not candidate_unknown and not candidate_proven_not_sent:
            return False
        if not retryable_unknown:
            return False
        children = [
            row
            for row in rows
            if row.get("recovery_of") is not None
            and str(row["recovery_of"]) == str(recovery_id)
        ]
        if children:
            if len(children) != 1 or recovery_receipt_id is None:
                return False
            child = children[0]
            if (
                str(child["id"]) != str(recovery_receipt_id)
                or child["model_family"] != "primary"
                or child["status"] != "reserved"
            ):
                return False
        unresolved = cls._unknown_cost_rows(rows)
        return bool(unresolved) and all(str(row["id"]) in ancestors for row in unresolved)

    def _overall(
        self, task, rows, bounds, *, before_action=False, allow_unknown_recovery_id=None,
        recovery_receipt_id=None,
    ):
        if task["graph_version"] == "business-3":
            if task["state"] not in {"running", "recovering"}:
                raise BudgetExceeded("Task is not active", code="task_not_active")
            if not task["budget_lease_valid"]:
                raise BudgetExceeded("Task lease expired", code="task_lease_expired")
            unresolved = self._unknown_cost_rows(rows)
            if unresolved and not all(
                self._has_successful_recovery(row, rows) for row in unresolved
            ):
                if allow_unknown_recovery_id is None or not self._valid_recovery(
                    allow_unknown_recovery_id, rows, recovery_receipt_id=recovery_receipt_id
                ):
                    raise BudgetExceeded("Model cost is unresolved", code="unknown_model_cost")
        if task["active_seconds"] >= bounds.get("max_active_seconds", 1800):
            raise BudgetExceeded("Active task time exhausted", code="active_time_budget_exhausted")
        _, cost = self._held_usage(rows)
        limit = bounds.get("max_cost")
        if before_action and limit is not None and cost >= Decimal(str(limit)):
            raise BudgetExceeded(
                "Cost budget exhausted before action", code="cost_budget_exhausted"
            )

    def assert_action_available(self, *, connection=None, current_action=None):
        """Check new actions, keeping the old call/token guard for legacy tasks.

        Existing receipt-only verification retains its separate executor guard.
        Lease ownership and employee binding remain enforced by that executor.
        """
        if connection is None:
            with self.database.connect() as conn:
                return self.assert_action_available(connection=conn, current_action=current_action)
        task = self._task(connection)
        rows = self._rows(connection)
        bounds = self._bounds(task)
        self._overall(task, rows, bounds, before_action=True)
        if task["graph_version"] != "business-3":
            tokens, _ = self._held_usage(rows)
            if len(rows) >= bounds.get("max_calls", 30):
                raise BudgetExceeded("Call budget exhausted before action")
            if tokens >= bounds.get("max_tokens", 100_000):
                raise BudgetExceeded("Token budget exhausted before action")
        else:
            attempts = connection.execute(
                "SELECT count(*) AS n FROM app.action_attempts x "
                "JOIN app.actions a ON a.id=x.action_id WHERE a.task_id=%s",
                (self.task_id,),
            ).fetchone()["n"]
            in_progress = False
            if current_action is not None:
                current = connection.execute(
                    "SELECT a.status,EXISTS(SELECT 1 FROM app.action_attempts x "
                    "WHERE x.action_id=a.id AND x.generation=%s) AS owns_attempt "
                    "FROM app.actions a WHERE a.id=%s AND a.task_id=%s",
                    (task["lease_generation"], current_action, self.task_id),
                ).fetchone()
                if current is None or current["status"] not in {"running", "unknown"}:
                    raise BudgetExceeded(
                        "Current execution is not owned by this lease",
                        code="execution_fence_changed",
                    )
                in_progress = current["status"] == "running" and current["owns_attempt"]
            if attempts > bounds["max_actions"] or (
                attempts == bounds["max_actions"] and not in_progress
            ):
                raise BudgetExceeded(
                    "Execution budget exhausted", code="execution_budget_exhausted"
                )

    def assert_available(self, *, connection=None):
        """Compatibility entry point selected by persisted graph version."""
        return self.assert_action_available(connection=connection)

    @staticmethod
    def _attribution(
        model_family, purpose, plan_revision, node_id, node_ids, question_count, exception_reason
    ):
        if model_family is not None and model_family not in {"primary", "jev"}:
            raise ValueError("Invalid model family")
        if purpose is not None:
            _identifier(purpose, "model purpose")
        if (model_family is None) != (purpose is None):
            raise ValueError("Model family and purpose must be supplied together")
        if plan_revision is not None and (
            type(plan_revision) is not int or not 1 <= plan_revision <= 4
        ):
            raise ValueError("Invalid plan revision")
        if node_id is not None:
            _identifier(node_id, "node id")
        if node_ids is None:
            node_ids = [node_id] if node_id else None
        else:
            if (
                not isinstance(node_ids, (list, tuple))
                or not 1 <= len(node_ids) <= 32
                or any(not isinstance(item, str) for item in node_ids)
                or len(set(node_ids)) != len(node_ids)
            ):
                raise ValueError("Invalid model node ids")
            node_ids = [_identifier(item, "node id") for item in node_ids]
            if node_id is not None and node_id not in node_ids:
                raise ValueError("Node id is not in request node ids")
        if question_count is not None and (
            type(question_count) is not int or not 1 <= question_count <= 8
        ):
            raise ValueError("Invalid question count")
        if exception_reason is not None:
            _identifier(exception_reason, "primary exception reason")
        return {
            "model_family": model_family,
            "purpose": purpose,
            "plan_revision": plan_revision,
            "node_id": node_id,
            "node_ids": node_ids,
            "question_count": question_count,
            "exception_reason": exception_reason,
        }

    def reserve(
        self,
        key,
        attempt,
        provider,
        tokens,
        cost,
        allow_unknown_price=False,
        *,
        model_family=None,
        purpose=None,
        plan_revision=None,
        node_id=None,
        node_ids=None,
        question_count=None,
        exception_reason=None,
        recovery_of=None,
        request_role=None,
        requested_model=None,
        endpoint=None,
        requested_output_tokens=None,
        trial_cost_currency=None,
        trial_cost_native=None,
        trial_price_basis_id=None,
    ):
        if (
            type(tokens) is not int
            or tokens <= 0
            or type(attempt) is not int
            or attempt < 0
            or (cost is not None and (not cost.is_finite() or cost < 0))
        ):
            raise ValueError("Invalid usage reservation")
        attribution = self._attribution(
            model_family,
            purpose,
            plan_revision,
            node_id,
            node_ids,
            question_count,
            exception_reason,
        )
        if request_role is not None:
            _identifier(request_role, "request role")
        requested_model = _safe_model_value(requested_model, "requested model")
        endpoint = _safe_endpoint(endpoint)
        if requested_output_tokens is not None and (
            type(requested_output_tokens) is not int
            or not 1 <= requested_output_tokens <= 8192
            or requested_output_tokens > tokens
        ):
            raise ValueError("Invalid requested output token limit")
        if recovery_of is not None:
            try:
                recovery_of = str(recovery_of)
                from uuid import UUID

                recovery_of = UUID(recovery_of)
            except (TypeError, ValueError, AttributeError):
                raise ValueError("Invalid recovery receipt") from None
        node_ids = attribution["node_ids"]
        with self.database.connect() as conn:
            task = self._task(conn)
            if self.guest_dispatch_disabled:
                raise BudgetExceeded(
                    "Guest trial model dispatch is disabled", code="guest_trial_disabled"
                )
            if (
                self.trial_budget is not None
                and task.get("workspace_id") != self.guest_workspace_id
            ):
                raise BudgetExceeded(
                    "Guest trial ledger does not match task workspace",
                    code="guest_trial_workspace_mismatch",
                )
            if self.trial_budget is not None and model_family == "jev":
                if not guest_jev_profile_is_bounded(
                    provider=provider,
                    endpoint=endpoint,
                    model=requested_model,
                    tokens=tokens,
                    cost_currency=trial_cost_currency,
                    reserved_cost=trial_cost_native,
                    price_basis_id=trial_price_basis_id,
                ):
                    raise BudgetExceeded(
                        "Guest Jev dispatch requires the pinned, fully funded fee profile",
                        code="guest_jev_fee_bound_required",
                    )

            def reserve_trial(receipt_id):
                if self.trial_budget is None:
                    return None
                if (
                    trial_cost_currency is None
                    or trial_cost_native is None
                    or trial_price_basis_id is None
                ):
                    raise BudgetExceeded(
                        "Guest dispatch requires a versioned native-currency price",
                        code="guest_trial_price_required",
                    )
                from decagent.business.trial_budget import (
                    BudgetPolicyError,
                    TrialBudgetExceeded,
                )

                try:
                    return self.trial_budget.reserve_request(
                        workspace_id=self.guest_workspace_id,
                        task_id=self.task_id,
                        request_key=key,
                        attempt=attempt,
                        provider=provider,
                        model=requested_model or provider,
                        cost_currency=trial_cost_currency,
                        reserved_cost=trial_cost_native,
                        reserved_qwen_tokens=(
                            tokens if provider.casefold() in {"qwen", "dashscope"} else 0
                        ),
                        price_basis_id=trial_price_basis_id,
                        usage_record_id=receipt_id,
                        connection=conn,
                    )
                except TrialBudgetExceeded as exc:
                    raise BudgetExceeded(str(exc), code=exc.code) from exc
                except BudgetPolicyError as exc:
                    raise BudgetExceeded(
                        str(exc), code="guest_trial_policy_unavailable"
                    ) from exc

            prior = conn.execute(
                "SELECT * FROM app.usage_records WHERE task_id=%s AND reservation_key=%s "
                "AND attempt=%s",
                (self.task_id, key, attempt),
            ).fetchone()
            if prior:
                if self.trial_budget is not None:
                    prior_trial = reserve_trial(prior["id"])
                    cost = prior_trial["reserved_cost_usd"]
                if (
                    prior["requested_tokens"] != tokens
                    or prior["reserved_cost"] != cost
                    or prior["provider"] != provider
                    or prior["recovery_of"] != recovery_of
                    or prior["request_role"] != request_role
                    or prior["requested_model"] != requested_model
                    or prior["endpoint"] != endpoint
                    or prior["requested_output_tokens"] != requested_output_tokens
                    or any(prior[name] != value for name, value in attribution.items())
                ):
                    raise ValueError("Reservation key reused for different request")
                return prior["id"]
            bounds = self._bounds(task)
            conditional = task["graph_version"] == "business-3"
            if conditional and model_family is None:
                raise ValueError("Conditional requests require explicit model attribution")
            if recovery_of is not None and (not conditional or model_family != "primary"):
                raise BudgetExceeded(
                    "Only a conditional primary request may recover unknown usage",
                    code="unknown_model_cost",
                )
            if cost is None and (
                conditional or not (allow_unknown_price and bounds.get("allow_unknown_price"))
            ):
                raise BudgetExceeded(
                    "Unknown price requires a bounded fee reservation", code="unknown_model_price"
                )
            rows = self._rows(conn)
            if self.trial_budget is not None and self._unknown_cost_rows(rows):
                raise BudgetExceeded(
                    "Guest task has unresolved provider usage", code="unknown_model_cost"
                )
            receipt_id = uuid4()
            trial_reservation = reserve_trial(receipt_id)
            if trial_reservation is not None:
                cost = trial_reservation["reserved_cost_usd"]
            if recovery_of is not None and not self._valid_recovery(recovery_of, rows):
                raise BudgetExceeded(
                    "Unknown request is not eligible for recovery", code="unknown_model_cost"
                )
            self._overall(
                task, rows, bounds, allow_unknown_recovery_id=recovery_of
            )
            counted = self._counted(rows) if conditional else rows
            held_tokens, held_cost = self._held_usage(rows)
            if len(counted) >= bounds.get("max_calls", 30):
                raise BudgetExceeded(
                    "Call budget exhausted before request", code="model_request_budget_exhausted"
                )
            if conditional:
                if any(row["model_family"] is None for row in counted):
                    raise BudgetExceeded(
                        "Historical model classification is unresolved",
                        code="unclassified_model_usage",
                    )
                family_count = sum(row["model_family"] == model_family for row in counted)
                if family_count >= bounds[f"max_{model_family}_calls"]:
                    raise BudgetExceeded(
                        "Model family budget exhausted", code=f"{model_family}_budget_exhausted"
                    )
                if (
                    model_family == "primary"
                    and family_count >= bounds["primary_target"]
                    and exception_reason is None
                ):
                    raise BudgetExceeded(
                        "Primary request above target requires an exception reason",
                        code="primary_exception_reason_required",
                    )
            if held_tokens + tokens > bounds.get("max_tokens", 100_000):
                raise BudgetExceeded(
                    "Token budget exhausted before request", code="model_token_budget_exhausted"
                )
            limit = bounds.get("max_cost")
            if limit is not None and cost is not None and held_cost + cost > Decimal(str(limit)):
                raise BudgetExceeded(
                    "Cost budget exhausted before request", code="cost_budget_exhausted"
                )
            return conn.execute(
                "INSERT INTO app.usage_records"
                "(id,task_id,reservation_key,attempt,provider,requested_tokens,reserved_cost,"
                "model_family,purpose,plan_revision,node_id,node_ids,question_count,exception_reason,"
                "dispatch_state,model_budget_version,recovery_of,request_role,requested_model,"
                "endpoint,requested_output_tokens) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    receipt_id,
                    self.task_id,
                    key,
                    attempt,
                    provider,
                    tokens,
                    cost,
                    model_family,
                    purpose,
                    plan_revision,
                    node_id,
                    Jsonb(node_ids) if node_ids else None,
                    question_count,
                    exception_reason,
                    "reserved" if model_family else None,
                    MODEL_BUDGET_VERSION if conditional else None,
                    recovery_of,
                    request_role,
                    requested_model,
                    endpoint,
                    requested_output_tokens,
                ),
            ).fetchone()["id"]

    def reserve_model(self, *args, **kwargs):
        """Classified request protocol used by provider wrappers."""
        return self.reserve(*args, **kwargs)

    def mark_dispatched(self, receipt_id):
        with self.database.connect() as conn:
            task = self._task(conn)
            receipt = conn.execute(
                "SELECT * FROM app.usage_records WHERE id=%s AND task_id=%s",
                (receipt_id, self.task_id),
            ).fetchone()
            if receipt is None:
                raise ValueError("Unknown model reservation")
            if self.guest_dispatch_disabled:
                raise BudgetExceeded(
                    "Guest trial model dispatch is disabled", code="guest_trial_disabled"
                )
            rows = self._rows(conn)
            if self.trial_budget is not None and any(
                str(row["id"]) != str(receipt_id)
                for row in self._unknown_cost_rows(rows)
            ):
                raise BudgetExceeded(
                    "Guest task has unresolved provider usage", code="unknown_model_cost"
                )
            if task["graph_version"] == "business-3":
                bounds = self._bounds(task)
                self._overall(
                    task,
                    rows,
                    bounds,
                    allow_unknown_recovery_id=receipt["recovery_of"],
                    recovery_receipt_id=receipt_id,
                )
                if receipt["recovery_of"] is not None and not self._valid_recovery(
                    receipt["recovery_of"], rows, recovery_receipt_id=receipt_id
                ):
                    raise BudgetExceeded(
                        "Unknown request is not eligible for recovery", code="unknown_model_cost"
                    )
                counted = self._counted(rows)
                tokens, cost = self._held_usage(rows)
                if len(counted) > bounds["max_calls"] or tokens > bounds["max_tokens"]:
                    raise BudgetExceeded(
                        "Reserved request exceeds current model budget",
                        code="model_request_budget_exhausted",
                    )
                for family in ("primary", "jev"):
                    if (
                        sum(row["model_family"] == family for row in counted)
                        > bounds[f"max_{family}_calls"]
                    ):
                        raise BudgetExceeded(
                            "Reserved family exceeds current model budget",
                            code=f"{family}_budget_exhausted",
                        )
                if bounds.get("max_cost") is not None and cost > Decimal(str(bounds["max_cost"])):
                    raise BudgetExceeded(
                        "Reserved cost exceeds current model budget", code="cost_budget_exhausted"
                    )
            if self.trial_budget is not None:
                self.trial_budget.mark_dispatched(
                    self._trial_reservation_id(conn, receipt_id), connection=conn
                )
            row = conn.execute(
                "UPDATE app.usage_records SET dispatch_state='dispatching' "
                "WHERE id=%s AND task_id=%s AND status='reserved' "
                "AND dispatch_state='reserved' RETURNING id",
                (receipt_id, self.task_id),
            ).fetchone()
            if not row:
                raise ValueError("Reservation cannot start another dispatch")

    def mark_not_sent(self, receipt_id, *, reason, after_local_preflight=False):
        """Release only when trusted local code proves no provider request was sent."""
        _identifier(reason, "local dispatch reason")
        if type(after_local_preflight) is not bool:
            raise ValueError("Local preflight proof must be explicit")
        with self.database.connect() as conn:
            self._task(conn)
            row = conn.execute(
                "UPDATE app.usage_records SET dispatch_state='not_sent',status='settled',"
                "actual_tokens=0,actual_cost=0,error_code=%s "
                "WHERE id=%s AND task_id=%s AND status='reserved' "
                "AND (dispatch_state='reserved' OR (%s AND dispatch_state='dispatching')) "
                "RETURNING id",
                (reason, receipt_id, self.task_id, after_local_preflight),
            ).fetchone()
            if not row:
                raise ValueError("Unknown or completed dispatch cannot be released as not sent")
            if self.trial_budget is not None:
                self.trial_budget.mark_not_sent(
                    self._trial_reservation_id(conn, receipt_id),
                    reason=reason,
                    after_local_preflight=after_local_preflight,
                    connection=conn,
                )

    def settle_model(
        self, receipt_id, tokens, cost, *, response_state, error_code=None,
        resolved_model=None, server_request_id=None, latency_ms=None, error_category=None,
        provider_status=None, actual_input_tokens=None, actual_output_tokens=None,
        trial_actual_cost_native=None, trial_actual_cost_basis=None,
        trial_voucher_credit_native=None, trial_cash_paid_native=None,
        trial_billing_evidence_source=None,
    ):
        return self.settle(
            receipt_id,
            tokens,
            cost,
            response_state=response_state,
            error_code=error_code,
            resolved_model=resolved_model,
            server_request_id=server_request_id,
            latency_ms=latency_ms,
            error_category=error_category,
            provider_status=provider_status,
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
            trial_actual_cost_native=trial_actual_cost_native,
            trial_actual_cost_basis=trial_actual_cost_basis,
            trial_voucher_credit_native=trial_voucher_credit_native,
            trial_cash_paid_native=trial_cash_paid_native,
            trial_billing_evidence_source=trial_billing_evidence_source,
        )

    def settle(
        self, receipt_id, tokens, cost, *, response_state=None, error_code=None,
        resolved_model=None, server_request_id=None, latency_ms=None, error_category=None,
        provider_status=None, actual_input_tokens=None, actual_output_tokens=None,
        trial_actual_cost_native=None, trial_actual_cost_basis=None,
        trial_voucher_credit_native=None, trial_cash_paid_native=None,
        trial_billing_evidence_source=None,
    ):
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Usage cannot be negative or noninteger")
        if cost is not None and (not cost.is_finite() or cost < 0):
            raise ValueError("Cost must be nonnegative and finite")
        if response_state is not None and response_state not in {"responded", "unknown"}:
            raise ValueError("Invalid response state")
        if error_code is not None:
            _identifier(error_code, "model error code")
        resolved_model = _safe_model_value(resolved_model, "resolved model")
        server_request_id = _safe_model_value(server_request_id, "server request id")
        if latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0):
            raise ValueError("Invalid provider latency")
        if error_category is not None:
            _identifier(error_category, "provider error category")
        if provider_status is not None and (
            type(provider_status) is not int or not 100 <= provider_status <= 599
        ):
            raise ValueError("Invalid provider HTTP status")
        with self.database.connect() as conn:
            self._task(conn)
            row = conn.execute(
                "SELECT * FROM app.usage_records WHERE id=%s AND task_id=%s FOR UPDATE",
                (receipt_id, self.task_id),
            ).fetchone()
            if not row or row["status"] == "settled":
                raise ValueError("Unknown reservation or already settled")
            if self.trial_budget is not None:
                trial_row = self.trial_budget.settle_request(
                    self._trial_reservation_id(conn, receipt_id),
                    response_state=response_state or "unknown",
                    actual_input_tokens=actual_input_tokens,
                    actual_output_tokens=actual_output_tokens,
                    actual_cost_native=trial_actual_cost_native,
                    actual_cost_basis=trial_actual_cost_basis,
                    voucher_credit_native=trial_voucher_credit_native,
                    cash_paid_native=trial_cash_paid_native,
                    billing_evidence_source=trial_billing_evidence_source,
                    server_request_id=server_request_id,
                    latency_ms=latency_ms,
                    error_category=error_category,
                    provider_status=provider_status,
                    connection=conn,
                )
                cost = trial_row["actual_cost_usd"]
            tokens = (
                max(tokens, row["actual_tokens"] or 0)
                if tokens is not None
                else row["actual_tokens"]
            )
            cost = (
                max(cost, row["actual_cost"] or Decimal("0"))
                if cost is not None
                else row["actual_cost"]
            )
            status = (
                "settled"
                if response_state != "unknown" and tokens is not None and cost is not None
                else "unknown"
            )
            dispatch_state = None
            if row["model_family"] is not None:
                dispatch_state = response_state or (
                    "responded" if status == "settled" else "unknown"
                )
            conn.execute(
                "UPDATE app.usage_records SET actual_tokens=%s,actual_cost=%s,status=%s,"
                "dispatch_state=%s,error_code=COALESCE(%s,error_code),"
                "resolved_model=COALESCE(%s,resolved_model),"
                "server_request_id=COALESCE(%s,server_request_id),"
                "latency_ms=COALESCE(%s,latency_ms),"
                "error_category=COALESCE(%s,error_category),"
                "provider_status=COALESCE(%s,provider_status) WHERE id=%s",
                (
                    tokens, cost, status, dispatch_state, error_code,
                    resolved_model, server_request_id, latency_ms, error_category,
                    provider_status, receipt_id,
                ),
            )

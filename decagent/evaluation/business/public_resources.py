"""Adapters that bind live benchmark callbacks to the durable public ledger."""

from __future__ import annotations

import inspect
from copy import deepcopy
from decimal import Decimal
from math import isfinite
from time import perf_counter

from decagent.decisions.contracts import digest, jev_model_matches
from decagent.models.primary import PrimaryRequestNotDispatched

from .public_diagnostics import safe_exception_type, safe_failure_stage, safe_http_status


class PublicToolOutcomeUnknown(RuntimeError):
    """A public tool was sent but its upstream result could not be confirmed."""


class PublicModelOutcomeUnknown(RuntimeError):
    """A dispatched model request has unknown usage and must not be replayed."""


class PublicModelNotDispatched(RuntimeError):
    """A callback confirmed that its local preflight failed before HTTP dispatch."""


class PublicModelProfileDrift(RuntimeError):
    """A response came from a model identity outside the frozen evaluation profile."""


class PublicModelAdapters(dict):
    """Role callbacks with deterministic, idempotent client-resource cleanup."""

    def __init__(self, adapters, resources=()):
        super().__init__(adapters)
        seen = set()
        self._resources = []
        for resource in resources:
            if id(resource) in seen or not callable(getattr(resource, "close", None)):
                continue
            seen.add(id(resource))
            self._resources.append(resource)
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        failures = []
        for resource in reversed(self._resources):
            try:
                resource.close()
            except Exception as exc:
                failures.append(type(exc).__name__)
        if failures:
            raise RuntimeError(
                "Public model client cleanup failed: " + ",".join(failures)
            ) from None

    def close_suppressing_errors(self):
        try:
            self.close()
        except Exception:
            pass


class LedgeredToolGateway:
    """Reserve and settle each upstream tool attempt before/after dispatch.

    The wrapped gateway remains the original environment/MCP transport. This
    adapter records only the registered command/effect and digests, never tool
    arguments or returned business data. An exception after dispatch is
    conservatively unknown and cannot be replayed.
    """

    def __init__(
        self, gateway, ledger, *, trial_id, route, tool_effects, max_timeout_seconds=60
    ):
        if not callable(getattr(gateway, "call", None)):
            raise TypeError("A call-capable upstream gateway is required")
        try:
            gateway_parameters = inspect.signature(gateway.call).parameters.values()
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "Public tool gateway must expose a timeout-aware call contract"
            ) from exc
        if not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "timeout_seconds"
            and parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            for parameter in gateway_parameters
        ):
            raise TypeError("Public tool gateway must accept timeout_seconds")
        required_ledger_methods = (
            "reserve_tool_call",
            "mark_tool_dispatched",
            "mark_tool_not_sent",
            "settle_tool_call",
            "mark_tool_unknown",
            "route_remaining_seconds",
            "batch_snapshot",
        )
        if any(not callable(getattr(ledger, name, None)) for name in required_ledger_methods):
            raise TypeError("A durable public trial ledger is required")
        route = getattr(route, "value", route)
        if type(trial_id) is not str or type(route) is not str:
            raise ValueError("A frozen trial ID and route are required")
        if type(tool_effects) is not dict or any(
            type(name) is not str
            or not name
            or type(effect) is not str
            or effect not in {"read", "write", "ask_user"}
            for name, effect in tool_effects.items()
        ):
            raise ValueError("Registered tool effects are required")
        if (
            type(max_timeout_seconds) not in {int, float}
            or not isfinite(max_timeout_seconds)
            or not 0 < max_timeout_seconds <= 1_800
        ):
            raise ValueError("Public tool timeout must be finite and at most 1,800 seconds")
        self.gateway = gateway
        self.ledger = ledger
        self.trial_id = trial_id
        self.route = route
        self.tool_effects = dict(tool_effects)
        self.max_timeout_seconds = float(max_timeout_seconds)

    def _deadline_timeout(self):
        route_remaining = self.ledger.route_remaining_seconds(self.trial_id, self.route)
        batch_remaining = self.ledger.batch_snapshot().get("remaining_wall_seconds")
        if (
            type(route_remaining) not in {int, float}
            or type(batch_remaining) not in {int, float}
            or not isfinite(route_remaining)
            or not isfinite(batch_remaining)
            or route_remaining <= 0
            or batch_remaining <= 0
        ):
            raise ValueError("Public tool deadline expired before dispatch")
        return min(
            self.max_timeout_seconds, float(route_remaining), float(batch_remaining)
        )

    def call(self, command, params):
        if type(command) is not str or type(params) is not dict:
            raise ValueError("Public tool call is outside the frozen capability catalog")
        effect = self.tool_effects.get(command)
        if effect is None:
            raise ValueError("Public tool call is outside the frozen capability catalog")
        self._deadline_timeout()
        attempt = self.ledger.reserve_tool_call(
            trial_id=self.trial_id,
            route=self.route,
            command=command,
            effect=effect,
            arguments_digest=digest(params),
        )
        try:
            timeout_seconds = self._deadline_timeout()
        except Exception:
            self.ledger.mark_tool_not_sent(
                attempt,
                reason="deadline_expired",
                verified_not_sent=True,
            )
            raise
        try:
            self.ledger.mark_tool_dispatched(attempt)
        except Exception:
            # No transport was invoked, so this specific reservation is known not sent.
            self.ledger.mark_tool_not_sent(
                attempt,
                reason="dispatch_not_admitted",
                verified_not_sent=True,
            )
            raise
        started = perf_counter()
        try:
            timeout_seconds = self._deadline_timeout()
        except Exception as exc:
            self.ledger.mark_tool_unknown(
                attempt,
                error_category="tool_deadline_error",
                failure_stage="route_deadline",
                exception_type=safe_exception_type(exc),
                http_status=safe_http_status(exc),
            )
            raise PublicToolOutcomeUnknown(
                "Public tool outcome is unknown; automatic replay is disabled"
            ) from exc
        try:
            result = self.gateway.call(
                command, deepcopy(params), timeout_seconds=timeout_seconds
            )
            result_digest = digest(result)
        except Exception as exc:
            self.ledger.mark_tool_unknown(
                attempt,
                error_category="tool_dispatch_error",
                failure_stage="tool_gateway",
                exception_type=safe_exception_type(exc),
                http_status=safe_http_status(exc),
            )
            raise PublicToolOutcomeUnknown(
                "Public tool outcome is unknown; automatic replay is disabled"
            ) from exc
        self.ledger.settle_tool_call(
            attempt,
            duration_seconds=perf_counter() - started,
            result_digest=result_digest,
        )
        return result


class LedgeredModelCallback:
    """One measured model dispatch guarded by the durable public-trial ledger.

    The callback receives exactly one request and must return the routes module's
    measured ModelReply. The caller supplies an input-token upper-bound function
    over the complete model-visible request (including system/tool framing). A
    callback must raise ``PublicModelNotDispatched`` only when it can prove that
    no provider request was sent; every other post-dispatch exception is unknown.
    """

    def __init__(
        self,
        callback,
        ledger,
        *,
        profile,
        trial_id,
        route,
        input_token_upper_bound,
        route_remaining_seconds,
    ):
        if (
            not callable(callback)
            or not callable(input_token_upper_bound)
            or not callable(route_remaining_seconds)
        ):
            raise TypeError("A model callback, input bound and route deadline are required")
        try:
            callback_parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError) as exc:
            raise TypeError("Model callback must expose its timeout-aware call contract") from exc
        if not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == "timeout_seconds"
            and parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            for parameter in callback_parameters
        ):
            raise TypeError("Model callback must accept timeout_seconds")
        if not callable(getattr(ledger, "reserve_model_attempt", None)):
            raise TypeError("A durable public-trial ledger is required")
        if getattr(profile, "role", None) not in {
            "primary", "jev", "user_simulator", "judge"
        }:
            raise ValueError("A frozen public model role is required")
        route = getattr(route, "value", route)
        if type(trial_id) is not str or not trial_id or type(route) is not str or not route:
            raise ValueError("A frozen public trial and route are required")
        self.callback = callback
        self.ledger = ledger
        self.profile = profile
        self.trial_id = trial_id
        self.route = route
        self.input_token_upper_bound = input_token_upper_bound
        self.route_remaining_seconds = route_remaining_seconds

    def __call__(self, request, *, timeout_seconds=None):
        profile = self.profile
        if timeout_seconds is not None and (
            type(timeout_seconds) not in {int, float}
            or not isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("A positive finite caller deadline is required")
        unknown_price_is_authorized = (
            profile.pricing_status == "unknown"
            and profile.provider == "qwen"
            and profile.role in {"primary", "user_simulator", "judge"}
        )
        if profile.pricing_status != "known" and not unknown_price_is_authorized:
            raise ValueError(
                "Live public dispatch without a price is allowed only for token-capped Qwen roles"
            )
        input_bound = self.input_token_upper_bound(deepcopy(request))
        if type(input_bound) is not int or not 1 <= input_bound <= profile.max_input_tokens:
            raise ValueError("Complete model input exceeds the frozen input-token bound")
        output_bound = profile.max_output_tokens
        reserved_cost = None
        reserved_cost_currency = None
        if profile.pricing_status == "known":
            reserved_cost, _basis = profile.calculate_cost(input_bound, output_bound)
            reserved_cost_currency = profile.currency
        attempt_id = self.ledger.reserve_model_attempt(
            trial_id=self.trial_id,
            route=self.route,
            role=profile.role,
            estimated_input_tokens=input_bound,
            max_output_tokens=output_bound,
            reserved_cost_usd=(reserved_cost if reserved_cost_currency == "USD" else None),
            reserved_cost_amount=reserved_cost,
            reserved_cost_currency=reserved_cost_currency,
            profile_digest=profile.fingerprint,
        )
        try:
            route_remaining = self.route_remaining_seconds()
            batch_remaining = self.ledger.batch_snapshot()["remaining_wall_seconds"]
            if (
                type(route_remaining) not in {int, float}
                or route_remaining <= 0
                or type(batch_remaining) not in {int, float}
                or batch_remaining <= 0
            ):
                raise ValueError("Route or batch deadline expired before model dispatch")
            timeout_limits = [
                float(profile.timeout_seconds),
                float(route_remaining),
                float(batch_remaining),
            ]
            if timeout_seconds is not None:
                timeout_limits.append(float(timeout_seconds))
            timeout_seconds = min(timeout_limits)
        except Exception:
            self.ledger.mark_model_not_sent(
                attempt_id,
                reason="deadline_preflight",
                verified_not_sent=True,
            )
            raise
        try:
            self.ledger.mark_model_dispatched(attempt_id)
        except Exception:
            self.ledger.mark_model_not_sent(
                attempt_id,
                reason="dispatch_not_admitted",
                verified_not_sent=True,
            )
            raise

        started = perf_counter()
        try:
            reply = self.callback(deepcopy(request), timeout_seconds=timeout_seconds)
        except PrimaryRequestNotDispatched as exc:
            self.ledger.mark_model_not_sent(
                attempt_id,
                reason="verified_model_preflight_failure",
                verified_not_sent=True,
            )
            raise PublicModelNotDispatched(
                "Model client rejected the request before provider dispatch"
            ) from exc
        except PublicModelNotDispatched as exc:
            self.ledger.mark_model_not_sent(
                attempt_id,
                reason="verified_preflight_failure",
                verified_not_sent=True,
            )
            raise exc
        except BaseException as exc:
            measured = getattr(exc, "reply", None)
            if _is_measured_model_reply(measured):
                self._settle(
                    attempt_id,
                    measured,
                    started,
                    error_category="model_response_error",
                    failure=exc,
                )
            else:
                self.ledger.mark_model_unknown(
                    attempt_id,
                    error_category="model_dispatch_error",
                    attempt_seconds=perf_counter() - started,
                    failure_stage=safe_failure_stage(exc),
                    exception_type=safe_exception_type(exc),
                    http_status=safe_http_status(exc),
                )
                raise PublicModelOutcomeUnknown(
                    "Model request was dispatched but usage cannot be confirmed"
                ) from exc
            raise

        if not _is_measured_model_reply(reply):
            self.ledger.mark_model_unknown(
                attempt_id,
                error_category="invalid_measured_response",
                attempt_seconds=perf_counter() - started,
                failure_stage="model_response_validation",
            )
            raise PublicModelOutcomeUnknown(
                "Model request returned no complete measured usage record"
            )

        self._settle(attempt_id, reply, started)
        model_drift = False
        if reply.model is not None:
            if profile.role == "jev":
                model_drift = not jev_model_matches(profile.model, reply.model)
            else:
                model_drift = reply.model != profile.model
        profile_drift = (
            reply.profile is None
            or reply.profile.fingerprint != profile.fingerprint
            or model_drift
            or (reply.provider is not None and reply.provider != profile.provider)
        )
        if profile_drift:
            stop_batch = getattr(self.ledger, "stop_for_model_profile_drift", None)
            if callable(stop_batch):
                stop_batch()
            raise PublicModelProfileDrift(
                "Model response identity differs from the frozen evaluation profile"
            )
        return reply

    def _settle(self, attempt_id, reply, started, *, error_category=None, failure=None):
        profile = self.profile
        if (
            reply.input_tokens is None
            or reply.output_tokens is None
            or reply.profile is None
            or reply.profile.fingerprint != profile.fingerprint
        ):
            self.ledger.mark_model_unknown(
                attempt_id,
                error_category="model_usage_unknown",
                attempt_seconds=perf_counter() - started,
                failure_stage="model_response_validation",
                http_status=(
                    reply.http_status
                    if type(reply.http_status) is int and 100 <= reply.http_status <= 599
                    else None
                ),
            )
            raise PublicModelOutcomeUnknown(
                "Dispatched model usage or frozen profile identity is unknown"
            )
        calculated_cost = None
        calculated_basis = None
        if profile.pricing_status == "known":
            calculated_cost, calculated_basis = profile.calculate_cost(
                reply.input_tokens,
                reply.output_tokens,
                cached_input_tokens=reply.cached_input_tokens,
            )
        billing = reply.billing_evidence
        if billing is not None:
            actual_cost = billing.gross_amount
            actual_currency = billing.currency
            cost_source = "provider_billed"
            cost_basis = "provider_billed"
        elif reply.cost_source == "provider_billed" and reply.cost_usd is not None:
            actual_cost = reply.cost_usd
            actual_currency = "USD"
            cost_source = "provider_billed"
            cost_basis = "provider_billed"
        elif calculated_cost is not None:
            actual_cost = calculated_cost
            actual_currency = profile.currency
            cost_source = "frozen_profile"
            cost_basis = calculated_basis
        else:
            actual_cost = None
            actual_currency = None
            cost_source = "unknown"
            cost_basis = "unknown"
        actual_cost_usd = actual_cost if actual_currency == "USD" else None
        cost_mismatch = (
            reply.cost_source == "frozen_profile"
            and reply.cost_usd is not None
            and (
                actual_currency != "USD"
                or reply.cost_usd != actual_cost
            )
        )
        if cost_source not in {"provider_billed", "frozen_profile", "unknown"}:
            self.ledger.mark_model_unknown(
                attempt_id,
                error_category="model_cost_unknown",
                attempt_seconds=perf_counter() - started,
                failure_stage="model_response_validation",
                http_status=(
                    reply.http_status
                    if type(reply.http_status) is int and 100 <= reply.http_status <= 599
                    else None
                ),
            )
            raise PublicModelOutcomeUnknown(
                "Model usage is known but cost evidence is unavailable"
            )
        self.ledger.settle_model_attempt(
            attempt_id,
            actual_input_tokens=reply.input_tokens,
            actual_output_tokens=reply.output_tokens,
            cached_input_tokens=reply.cached_input_tokens,
            actual_cost_usd=actual_cost_usd,
            actual_cost_amount=actual_cost,
            actual_cost_currency=actual_currency,
            actual_cost_basis=cost_basis,
            provider_request_id=reply.provider_request_id,
            generation_id=reply.generation_id,
            http_status=reply.http_status,
            attempt_seconds=perf_counter() - started,
            error_category=error_category,
            failure_stage=(
                safe_failure_stage(failure, default="model_response_validation")
                if failure is not None
                else None
            ),
            exception_type=safe_exception_type(failure),
            actual_provider=reply.provider,
            actual_model=reply.model,
            actual_cost_source=cost_source,
            billing_evidence=(
                {
                    "gross_amount": str(billing.gross_amount),
                    "currency": billing.currency,
                    "voucher_credit_amount": (
                        str(billing.voucher_credit_amount)
                        if billing.voucher_credit_amount is not None
                        else None
                    ),
                    "cash_paid_amount": (
                        str(billing.cash_paid_amount)
                        if billing.cash_paid_amount is not None
                        else None
                    ),
                    "source": billing.source,
                    "record_id": billing.record_id,
                }
                if billing is not None
                else None
            ),
        )
        if profile.role == "jev":
            resolved_models = getattr(self.ledger, "resolved_model_identities", None)
            if callable(resolved_models) and len(resolved_models("jev")) > 1:
                stop_batch = getattr(self.ledger, "stop_for_model_profile_drift", None)
                if callable(stop_batch):
                    stop_batch()
                raise PublicModelProfileDrift(
                    "The resolved Jev model version changed within the frozen cohort"
                )
        if cost_mismatch:
            raise PublicModelProfileDrift(
                "Callback fee differs from the frozen token-rate calculation"
            )


class LedgeredModelAdapterFactory:
    """Bind route-specific model callbacks to the public request ledger.

    The supplied callback factories own prompt construction and model clients;
    this factory adds frozen role identity, resource reservation, dispatch
    accounting, measured settlement, and the active route deadline. It returns
    the adapter names consumed by ``ControlledRoutes.model_adapter_factory``.
    It deliberately does not create provider clients or open a live ledger.
    """

    _CALLBACKS = frozenset(
        {"planner", "react", "semantic", "summarizer", "user_simulator", "official_judge"}
    )
    _ROLE_DIGESTS = {
        "primary": "model_profile_digest",
        "jev": "jev_profile_digest",
        "user_simulator": "user_simulator_profile_digest",
        "judge": "judge_profile_digest",
    }

    def __init__(
        self,
        callback_factories,
        input_token_upper_bounds,
        ledger,
        profiles,
        *,
        trial_id_for,
    ):
        if (
            type(callback_factories) is not dict
            or not callback_factories
            or set(callback_factories) - self._CALLBACKS
            or any(not callable(factory) for factory in callback_factories.values())
        ):
            raise ValueError("Public model callback factories are invalid")
        if (
            type(input_token_upper_bounds) is not dict
            or set(input_token_upper_bounds) != set(callback_factories)
            or any(
                bound is not None and not callable(bound)
                for bound in input_token_upper_bounds.values()
            )
        ):
            raise ValueError("Every model callback factory needs a complete input-token bound")
        if not callable(trial_id_for):
            raise TypeError("A frozen manifest trial-ID resolver is required")
        required_roles = ("primary", "user_simulator", "judge")
        if any(not hasattr(profiles, name) for name in required_roles):
            raise TypeError("Frozen evaluation role profiles are required")
        required_ledger = ("reserve_model_attempt", "route_remaining_seconds")
        if any(not callable(getattr(ledger, name, None)) for name in required_ledger):
            raise TypeError("A durable public-trial model ledger is required")
        self.callback_factories = dict(callback_factories)
        self.input_token_upper_bounds = dict(input_token_upper_bounds)
        self.ledger = ledger
        self.profiles = profiles
        self.trial_id_for = trial_id_for

    def __call__(self, session, trial, route):
        route_name = getattr(route, "value", route)
        if type(route_name) is not str:
            raise ValueError("A frozen public route is required")
        trial_id = self.trial_id_for(trial)
        if type(trial_id) is not str or len(trial_id) != 64:
            raise ValueError("The route must resolve to a manifest trial identity")
        profiles = {
            "primary": self.profiles.primary,
            "user_simulator": self.profiles.user_simulator,
            "judge": self.profiles.judge,
        }
        if getattr(self.profiles, "jev", None) is not None:
            profiles["jev"] = self.profiles.jev
        for role, attribute in self._ROLE_DIGESTS.items():
            profile = profiles.get(role)
            if profile is None:
                continue
            if getattr(trial, attribute, None) != profile.fingerprint:
                raise ValueError("Trial model identity differs from the frozen role profile")

        callback_roles = {
            "planner": "primary",
            "react": "primary",
            "summarizer": "primary",
            "semantic": (
                "jev" if route_name == "plan_jev_semantic_mcp" else "primary"
            ),
            "user_simulator": "user_simulator",
            "official_judge": "judge",
        }
        required_names = set()
        if route_name == "react_skill_mcp":
            required_names.add("react")
        elif route_name in {"plan_mcp", "plan_primary_semantic_mcp", "plan_jev_semantic_mcp"}:
            required_names.add("planner")
        if route_name == "plan_jev_semantic_mcp":
            required_names.add("semantic")
            if "jev" not in profiles:
                raise ValueError("A frozen Jev profile is required for Jev routes")
        supports_user_simulation = getattr(session, "supports_user_simulation", False)
        requires_official_judge = getattr(session, "requires_official_judge", False)
        if supports_user_simulation or requires_official_judge:
            required_names.add("official_judge")
        if supports_user_simulation:
            required_names.add("user_simulator")
        if not required_names <= set(self.callback_factories):
            raise ValueError("Required route model callbacks are not configured")

        active_names = set(required_names)
        if "summarizer" in self.callback_factories:
            active_names.add("summarizer")
        adapters, resources = {}, []
        try:
            for name, factory in self.callback_factories.items():
                if name not in active_names:
                    continue
                role = callback_roles[name]
                profile = profiles[role]
                callback = factory(session, trial, route)
                resources.append(callback)
                if not callable(callback):
                    raise TypeError("A public model callback factory returned no callable")
                input_bound = getattr(callback, "input_token_upper_bound", None)
                if not callable(input_bound):
                    input_bound = self.input_token_upper_bounds[name]
                if not callable(input_bound):
                    raise TypeError(
                        "A public model callback must provide its complete input-token bound"
                    )
                adapters[name] = LedgeredModelCallback(
                    callback,
                    self.ledger,
                    profile=profile,
                    trial_id=trial_id,
                    route=route_name,
                    input_token_upper_bound=input_bound,
                    route_remaining_seconds=lambda: self.ledger.route_remaining_seconds(
                        trial_id, route_name
                    ),
                )
        except BaseException:
            PublicModelAdapters({}, resources).close_suppressing_errors()
            raise
        if getattr(session, "supports_user_simulation", False):
            adapters["user_model"] = profiles["user_simulator"].model
        if getattr(session, "supports_user_simulation", False) or getattr(
            session, "requires_official_judge", False
        ):
            adapters["judge_model"] = profiles["judge"].model
        return PublicModelAdapters(adapters, resources)


def _is_measured_model_reply(value):
    if value is None:
        return False
    tokens = getattr(value, "tokens", None)
    input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "output_tokens", None)
    model = getattr(value, "model", None)
    cost = getattr(value, "cost_usd", None)
    cost_source = getattr(value, "cost_source", None)
    cached_input_tokens = getattr(value, "cached_input_tokens", None)
    billing = getattr(value, "billing_evidence", None)
    billing_currency = getattr(billing, "currency", None)
    billing_amount = getattr(billing, "gross_amount", None)
    non_usd_bill = (
        type(billing_currency) is str
        and billing_currency != "USD"
        and isinstance(billing_amount, Decimal)
        and billing_amount.is_finite()
        and billing_amount >= 0
    )
    return (
        type(tokens) is int
        and tokens >= 0
        and type(input_tokens) is int
        and type(output_tokens) is int
        and tokens == input_tokens + output_tokens
        and (
            cached_input_tokens is None
            or (
                type(cached_input_tokens) is int
                and 0 <= cached_input_tokens <= input_tokens
            )
        )
        and (model is None or (type(model) is str and bool(model.strip())))
        and (
            cost is None
            or (
                isinstance(cost, Decimal)
                and cost.is_finite()
                and cost >= 0
            )
        )
        and (
            cost_source == "unknown" or (non_usd_bill and cost_source == "provider_billed")
            if cost is None
            else cost_source in {"provider_billed", "frozen_profile"}
        )
    )

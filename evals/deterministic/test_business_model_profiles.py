import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from decagent.evaluation.business.comparison import (
    ComparisonTrial,
    Route,
    RouteRun,
    compare_full_workflow,
)
from decagent.evaluation.business.model_profiles import (
    EvaluationModelProfiles,
    ModelProfile,
)
from decagent.evaluation.business.routes import ModelReply, ObservedUsage


def profile(
    *, role="primary", endpoint="https://maas.example/v1", price_version="qwen-test-v1"
):
    return ModelProfile(
        role=role,
        provider="qwen",
        model="qwen3.8-flash",
        endpoint=endpoint,
        price_version=price_version,
        price_source="test-only explicit zero-rate fixture; not billing evidence",
        input_price_per_million=Decimal("0"),
        output_price_per_million=Decimal("0"),
        max_input_tokens=65_536,
        max_output_tokens=8_192,
        fallback_allowed=False,
    )


def trial(model_profile_digest):
    return ComparisonTrial(
        dataset="enterpriseops-gym",
        dataset_version="fixed-v1",
        dataset_checksum="a" * 64,
        case_id="case-01",
        branch_kind="structured",
        initial_state_digest="b" * 64,
        goal_digest="c" * 64,
        actor_profile_digest="d" * 64,
        permission_digest="e" * 64,
        tool_adapter_digest="f" * 64,
        memory_mode="disabled",
        workflow_mode="cold",
        model_profile_digest=model_profile_digest,
    )


def run(route, trial_context, actual_profile_digest):
    return RouteRun(
        route=route,
        trial=trial_context,
        task_success=True,
        forbidden_side_effects=0,
        primary_requests=1,
        jev_requests=0,
        primary_tokens=10,
        jev_tokens=0,
        primary_cost_usd=Decimal("0"),
        jev_cost_usd=Decimal("0"),
        machine_seconds=1.0,
        human_wait_seconds=0.0,
        user_simulator_cost_usd=Decimal("0"),
        actual_models=(("primary", "qwen3.8-flash"),),
        actual_profiles=(("primary", actual_profile_digest),),
    )


def test_profile_digest_freezes_provider_endpoint_pricing_and_execution_limits():
    original = profile()

    assert original.fingerprint != profile(endpoint="https://other.example/v1").fingerprint
    assert original.fingerprint != profile(price_version="qwen-test-v2").fingerprint
    assert original.fingerprint == profile().fingerprint
    assert "api_key" not in repr(original).lower()


def test_unknown_pricing_is_explicit_and_round_trips_in_profile_v3():
    unknown = replace(
        profile(),
        price_version="qwen-unverified-billing-20261020",
        price_source="Qwen is billed; voucher offsets account charges; rate not verified",
        input_price_per_million=None,
        output_price_per_million=None,
        pricing_status="unknown",
    )
    profiles = EvaluationModelProfiles(
        primary=unknown,
        user_simulator=replace(profile(role="user_simulator"), pricing_status="unknown",
                               input_price_per_million=None, output_price_per_million=None),
        judge=replace(profile(role="judge"), pricing_status="unknown",
                      input_price_per_million=None, output_price_per_million=None),
    )

    encoded = profiles.to_dict()
    decoded = EvaluationModelProfiles.from_dict(encoded)

    assert encoded["schema_version"] == "evaluation-model-profiles-v3"
    assert encoded["roles"]["primary"]["input_price_per_million"] is None
    assert encoded["roles"]["primary"]["pricing_status"] == "unknown"
    assert decoded.primary.fingerprint == unknown.fingerprint
    assert decoded.fingerprint == profiles.fingerprint


def test_cny_cached_input_prices_round_trip_in_profile_v4_without_rewriting_v3():
    source = "https://platform.qianwenai.com/pricing/api"
    roles = {
        role: replace(
            profile(role=role),
            price_version="qwen3.8-flash-cny-20261021-v1",
            price_source=source,
            input_price_per_million=Decimal("0.8"),
            output_price_per_million=Decimal("2.7"),
            pricing_status="known",
            price_currency="CNY",
            cached_input_price_per_million=Decimal("0.1"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    profiles = EvaluationModelProfiles(**roles)

    encoded = profiles.to_dict()
    decoded = EvaluationModelProfiles.from_dict(encoded)

    assert encoded["schema_version"] == "evaluation-model-profiles-v4"
    assert encoded["roles"]["primary"]["price_currency"] == "CNY"
    assert encoded["roles"]["primary"]["cached_input_price_per_million"] == "0.1"
    assert decoded.primary.fingerprint == profiles.primary.fingerprint
    assert decoded.fingerprint == profiles.fingerprint

    root = Path(__file__).resolve().parents[2]
    legacy_document = json.loads(
        (root / "evals/benchmarks/manifests/evaluation_model_profiles_v3.json").read_text(
            encoding="utf-8"
        )
    )
    legacy = EvaluationModelProfiles.from_dict(legacy_document)
    assert legacy.to_dict() == legacy_document


def test_profile_v4_keeps_jev_identity_and_declares_currency_fields_for_every_role():
    root = Path(__file__).resolve().parents[2]
    legacy_document = json.loads(
        (root / "evals/benchmarks/manifests/evaluation_model_profiles_v3.json").read_text(
            encoding="utf-8"
        )
    )
    legacy = EvaluationModelProfiles.from_dict(legacy_document)
    updates = {
        role: replace(
            getattr(legacy, role),
            price_version="qwen3.8-flash-standard-cny-20261021-v1",
            price_source="https://platform.qianwenai.com/pricing/api",
            input_price_per_million=Decimal("0.8"),
            output_price_per_million=Decimal("2.7"),
            pricing_status="known",
            price_currency="CNY",
            cached_input_price_per_million=Decimal("0.1"),
        )
        for role in ("primary", "user_simulator", "judge")
    }
    profiles = replace(legacy, **updates)

    encoded = profiles.to_dict()
    decoded = EvaluationModelProfiles.from_dict(encoded)

    assert encoded["schema_version"] == "evaluation-model-profiles-v4"
    assert set(encoded["roles"]["jev"]) >= {
        "price_currency",
        "cached_input_price_per_million",
    }
    assert encoded["roles"]["jev"]["price_currency"] is None
    assert encoded["roles"]["jev"]["cached_input_price_per_million"] is None
    assert encoded["roles"]["jev"]["fingerprint"] == legacy_document["roles"]["jev"]["fingerprint"]
    assert decoded.jev.fingerprint == legacy.jev.fingerprint


def test_profile_calculates_cny_cost_with_cache_and_safe_uncached_upper_bound():
    selected = replace(
        profile(),
        price_version="qwen-cny-v1",
        price_source="https://platform.qianwenai.com/pricing/api",
        input_price_per_million=Decimal("0.8"),
        output_price_per_million=Decimal("2.7"),
        price_currency="CNY",
        cached_input_price_per_million=Decimal("0.1"),
    )

    exact_cost, exact_basis = selected.calculate_cost(10, 3, cached_input_tokens=4)
    upper_bound, upper_bound_basis = selected.calculate_cost(10, 3)

    assert exact_cost == Decimal("0.0000133")
    assert exact_basis == "frozen_rate"
    assert upper_bound == Decimal("0.0000161")
    assert upper_bound_basis == "frozen_rate_upper_bound"


def test_unknown_price_requires_both_rates_and_explicit_status():
    with pytest.raises(ValueError, match="Known pricing"):
        replace(profile(), input_price_per_million=None, pricing_status="known")
    with pytest.raises(ValueError, match="[Kk]nown pricing"):
        replace(profile(), output_price_per_million=None)


def test_profile_digest_freezes_request_parameters_tool_contract_timeout_and_routing():
    original = profile()
    exact = replace(
        original,
        temperature=Decimal("0"),
        top_p=Decimal("1"),
        seed=None,
        unsupported_parameters=("seed",),
        response_mode="chat_tool_or_text",
        tool_choice="auto",
        timeout_seconds=60,
        provider_routing="direct_endpoint",
    )

    assert exact.fingerprint != replace(exact, temperature=Decimal("0.2")).fingerprint
    assert exact.fingerprint != replace(exact, tool_choice="none").fingerprint
    assert exact.fingerprint != replace(exact, timeout_seconds=45).fingerprint
    assert exact.fingerprint != replace(exact, provider_routing="openrouter_default").fingerprint


def test_frozen_primary_profile_allows_a_bounded_180_second_timeout():
    selected = replace(profile(), timeout_seconds=180)

    assert selected.timeout_seconds == 180
    with pytest.raises(ValueError, match="between one and 180 seconds"):
        replace(profile(), timeout_seconds=181)


def test_profile_rejects_sending_a_parameter_declared_unsupported():
    with pytest.raises(ValueError, match="unsupported parameter"):
        replace(profile(), seed=42, unsupported_parameters=("seed",))


def test_text_only_profile_cannot_advertise_automatic_tool_selection():
    with pytest.raises(ValueError, match="Text-only profile"):
        replace(profile(), response_mode="chat_text", tool_choice="auto")


def test_primary_model_factory_applies_the_frozen_profile_to_the_provider_client(monkeypatch):
    from decagent.models import primary as primary_module

    captured = {}

    class FakeClient:
        def bind_tools(self, definitions, *, tool_choice):
            captured["tool_definitions"] = definitions
            captured["tool_choice"] = tool_choice
            return self

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(primary_module, "ChatOpenAI", fake_chat_openai)
    selected = replace(
        profile(),
        temperature=Decimal("0.2"),
        top_p=Decimal("0.8"),
        seed=17,
        unsupported_parameters=(),
        timeout_seconds=23,
        tool_choice="none",
    )

    primary_module.PrimaryModel.from_evaluation_profile(
        selected,
        api_key="test-secret",
        tool_definitions=[],
        validator=lambda *_: None,
    )

    assert captured["model"] == selected.model
    assert captured["base_url"] == selected.endpoint
    assert captured["max_tokens"] == selected.max_output_tokens
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.8
    assert captured["seed"] == 17
    assert captured["timeout"] == 23
    assert captured["tool_choice"] == "none"


def test_primary_model_factory_forwards_an_injected_http_client(monkeypatch):
    from decagent.models import primary as primary_module

    captured = {}

    class FakeClient:
        def bind_tools(self, _definitions, *, tool_choice):
            captured["tool_choice"] = tool_choice
            return self

    monkeypatch.setattr(
        primary_module,
        "ChatOpenAI",
        lambda **kwargs: captured.update(kwargs) or FakeClient(),
    )
    selected_http_client = object()

    primary_module.PrimaryModel.from_evaluation_profile(
        profile(),
        api_key="test-secret",
        tool_definitions=[],
        validator=lambda *_: None,
        http_client=selected_http_client,
    )

    assert captured["http_client"] is selected_http_client


def test_primary_model_owns_the_injected_httpx2_client_without_sending_requests():
    import httpx2

    from decagent.business.plans.planner import plan_tool_definitions
    from decagent.models.primary import PrimaryModel

    injected = httpx2.Client(event_hooks={"request": [], "response": []})
    primary = PrimaryModel.from_evaluation_profile(
        profile(),
        api_key="test-secret",
        tool_definitions=plan_tool_definitions(),
        validator=lambda *_: None,
        http_client=injected,
    )
    try:
        bound_model = primary.client.bound
        assert bound_model.http_client is injected
        assert bound_model.root_client._client is injected
    finally:
        primary.close()

    assert injected.is_closed


def test_primary_model_factory_omits_parameters_the_profile_marks_unsupported(monkeypatch):
    from decagent.models import primary as primary_module

    captured = {}

    class FakeClient:
        def bind_tools(self, _definitions, *, tool_choice):
            captured["tool_choice"] = tool_choice
            return self

    def fake_chat_openai(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(primary_module, "ChatOpenAI", fake_chat_openai)
    selected = profile()
    primary_module.PrimaryModel.from_evaluation_profile(
        selected,
        api_key="test-secret",
        tool_definitions=[],
        validator=lambda *_: None,
    )

    assert captured["temperature"] == 0.0
    assert captured["top_p"] == 1.0
    assert "seed" not in captured
    assert captured["timeout"] == selected.timeout_seconds
    assert captured["tool_choice"] == selected.tool_choice


def test_enabled_fallback_requires_its_own_frozen_profile():
    with pytest.raises(ValueError, match="separately frozen profile"):
        replace(profile(), fallback_allowed=True)


def test_observed_usage_records_model_profile_per_role():
    selected = profile()
    usage = ObservedUsage()

    usage.call_reply(
        "primary",
        lambda _request: ModelReply(
            "ok", 4, Decimal("0"), "qwen3.8-flash", 3, 1, selected
        ),
        {"task": "read-only"},
    )

    assert usage.actual_profiles == [("primary", selected.fingerprint)]
    assert usage.actual_models == [("primary", "qwen3.8-flash")]
    assert usage.primary_requests == 1 and usage.primary_tokens == 4


def test_observed_usage_forwards_provider_deadline():
    observed_timeouts = []
    usage = ObservedUsage()

    def callback(_request, *, timeout_seconds):
        observed_timeouts.append(timeout_seconds)
        return ModelReply(
            "ok", 1, Decimal("0"), "qwen3.8-flash", 1, 0, profile()
        )

    usage.call_reply("primary", callback, {"task": "read"}, timeout_seconds=0.4)

    assert observed_timeouts == [0.4]
    assert usage.primary_requests == 1


def test_known_tokens_with_unknown_cost_remain_usable_and_mark_cost_incomplete():
    unknown = replace(
        profile(), input_price_per_million=None, output_price_per_million=None,
        pricing_status="unknown",
    )
    usage = ObservedUsage()

    usage.call_reply(
        "primary",
        lambda _request: ModelReply(
            "ok", 4, None, "qwen3.8-flash", 3, 1, unknown
        ),
        {},
    )

    assert usage.complete is True
    assert usage.primary_tokens == 4
    assert usage.primary_cost_usd is None
    assert usage.cost_complete is False


def test_observed_usage_rejects_profile_role_confusion():
    usage = ObservedUsage()
    judge_profile = profile(role="judge")

    with pytest.raises(ValueError, match="profile role"):
        usage.call_reply(
            "primary",
            lambda _request: ModelReply(
                "ok", 0, Decimal("0"), "qwen3.8-flash", 0, 0, judge_profile
            ),
            {},
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint": "http://maas.example/v1"},
        {"endpoint": "https://user:pass@maas.example/v1"},
        {"input_price_per_million": Decimal("-1")},
        {"max_input_tokens": 65_537},
        {"max_output_tokens": 8_193},
    ],
)
def test_profile_rejects_unsafe_or_unbounded_model_configuration(changes):
    values = {
        "role": "primary",
        "provider": "qwen",
        "model": "qwen3.8-flash",
        "endpoint": "https://maas.example/v1",
        "price_version": "qwen-test-v1",
        "price_source": "owner-confirmed:test free quota",
        "input_price_per_million": Decimal("0"),
        "output_price_per_million": Decimal("0"),
        "max_input_tokens": 65_536,
        "max_output_tokens": 8_192,
        "fallback_allowed": False,
    }
    values.update(changes)

    with pytest.raises(ValueError):
        ModelProfile(**values)


def test_live_comparison_binds_actual_provider_profile_to_frozen_trial():
    selected = profile()
    frozen = trial(selected.fingerprint)

    paired = compare_full_workflow(
        run(Route.REACT_SKILL_MCP, frozen, selected.fingerprint),
        run(Route.PLAN_MCP, frozen, selected.fingerprint),
    )

    assert paired.case_id == "case-01"


def test_comparison_rejects_actual_profile_that_disagrees_with_frozen_trial():
    selected = profile()
    other_endpoint = profile(endpoint="https://other.example/v1")
    frozen = trial(selected.fingerprint)

    with pytest.raises(ValueError, match="(?i:actual model profile)"):
        compare_full_workflow(
            run(Route.REACT_SKILL_MCP, frozen, selected.fingerprint),
            run(Route.PLAN_MCP, frozen, other_endpoint.fingerprint),
        )


def test_comparison_rejects_same_model_name_with_different_role_profile_digest():
    selected = profile()
    changed_pricing = profile(price_version="qwen-test-v2")
    frozen = trial(selected.fingerprint)
    react = run(Route.REACT_SKILL_MCP, frozen, selected.fingerprint)
    planned = run(Route.PLAN_MCP, frozen, selected.fingerprint)
    planned = replace(planned, actual_profiles=(("primary", changed_pricing.fingerprint),))

    with pytest.raises(ValueError, match="(?i:actual model profile)"):
        compare_full_workflow(react, planned)


def test_manifest_freezes_general_model_profiles_per_role():
    from pathlib import Path

    from decagent.evaluation.business.model_profiles import EvaluationModelProfiles
    from evals.benchmarks.build_trial_manifest import build_trial_manifest

    profiles = EvaluationModelProfiles(
        primary=profile(),
        user_simulator=profile(role="user_simulator"),
        judge=profile(role="judge"),
        jev=ModelProfile(
            role="jev",
            provider="openrouter",
            model="~typesafe/jev-latest",
            endpoint="https://openrouter.ai/api/alpha/decisions",
            price_version="user-confirmed-jev-openrouter-v1",
            price_source="owner-confirmed: OpenRouter Jev rate, 2026-10-12",
            input_price_per_million=Decimal("0.042"),
            output_price_per_million=Decimal("0"),
            max_input_tokens=65_536,
            max_output_tokens=8_192,
            max_question_tokens=32_768,
            max_provider_attempts=3,
            temperature=None,
            top_p=None,
            seed=None,
            unsupported_parameters=("temperature", "top_p", "seed"),
            response_mode="jev_decisions",
            tool_choice="not_applicable",
            timeout_seconds=10,
            provider_routing="openrouter_model_alias",
        ),
    )
    root = Path(__file__).resolve().parents[2]

    manifest = build_trial_manifest(root, model_profiles=profiles)

    assert manifest["model_profile_status"] == "frozen"
    assert manifest["model_profiles"]["fingerprint"] == profiles.fingerprint
    assert manifest["full_trials"][0]["model_profile_digest"] == profiles.primary.fingerprint
    assert (
        manifest["full_trials"][0]["user_simulator_profile_digest"]
        == profiles.user_simulator.fingerprint
    )
    assert manifest["full_trials"][0]["judge_profile_digest"] == profiles.judge.fingerprint
    assert manifest["full_trials"][0]["jev_profile_digest"] == profiles.jev.fingerprint

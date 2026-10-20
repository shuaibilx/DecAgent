from dataclasses import replace
from decimal import Decimal

import pytest

from decagent.evaluation.business.comparison import (
    AblationAxis,
    ComparisonTrial,
    PublicComparisonTrial,
    Route,
    RouteRun,
    compare_ablation,
    compare_full_workflow,
    compare_semantic_judgment,
    compare_structured_branch,
)


def trial(*, branch_kind="semantic", memory_mode="disabled", workflow_mode="cold"):
    return ComparisonTrial(
        dataset="decagent-business",
        dataset_version="business-synthetic-reference-v2",
        dataset_checksum="a" * 64,
        case_id="case-001",
        branch_kind=branch_kind,
        initial_state_digest="b" * 64,
        goal_digest="c" * 64,
        actor_profile_digest="d" * 64,
        permission_digest="e" * 64,
        tool_adapter_digest="f" * 64,
        memory_mode=memory_mode,
        workflow_mode=workflow_mode,
    )


def run(route, *, context=None, plan="1" * 64, decision_input="2" * 64, jev=0):
    return RouteRun(
        route=route,
        trial=context or trial(),
        plan_digest=plan,
        decision_input_digest=decision_input,
        task_success=True,
        forbidden_side_effects=0,
        primary_requests=1,
        jev_requests=jev,
        primary_tokens=100,
        jev_tokens=0,
        primary_cost_usd=Decimal("0"),
        jev_cost_usd=Decimal("0"),
        machine_seconds=1.0,
        human_wait_seconds=0.0,
        user_simulator_cost_usd=Decimal("0.03"),
    )


def test_semantic_pair_requires_same_plan_and_same_actual_decision_input():
    jev = run(Route.PLAN_JEV_SEMANTIC, jev=1)
    primary = run(Route.PLAN_PRIMARY_SEMANTIC)

    paired = compare_semantic_judgment(jev, primary)

    assert paired.case_id == "case-001"
    assert paired.plan_digest == "1" * 64
    assert paired.decision_input_digest == "2" * 64

    with pytest.raises(ValueError, match="decision_input_digest"):
        compare_semantic_judgment(jev, run(Route.PLAN_PRIMARY_SEMANTIC, decision_input="3" * 64))


def test_full_workflow_pair_rejects_a_different_initial_snapshot_or_actor_scope():
    react = run(Route.REACT_SKILL_MCP)
    plan = run(Route.PLAN_MCP)

    assert compare_full_workflow(react, plan).case_id == "case-001"
    changed = replace(react.trial, initial_state_digest="9" * 64)
    with pytest.raises(ValueError, match="initial_state_digest"):
        compare_full_workflow(run(Route.REACT_SKILL_MCP, context=changed), plan)


def test_structured_rule_trial_is_never_counted_as_a_jev_trial():
    context = trial(branch_kind="structured")

    pair = compare_structured_branch(
        run(Route.PLAN_MCP, context=context),
        run(Route.CODE_RULE_MCP, context=context),
    )
    assert pair.case_id == "case-001"

    with pytest.raises(ValueError, match="(?i:structured)"):
        compare_semantic_judgment(
            run(Route.PLAN_JEV_SEMANTIC, context=context, jev=1),
            run(Route.PLAN_PRIMARY_SEMANTIC, context=context),
        )


def test_memory_and_reuse_ablations_change_only_the_declared_axis():
    cold = run(Route.PLAN_JEV_SEMANTIC, context=trial(workflow_mode="cold"), jev=1)
    hot = run(Route.PLAN_JEV_SEMANTIC, context=trial(workflow_mode="hot"), plan="1" * 64, jev=1)
    reuse_pair = compare_ablation(cold, hot, axis=AblationAxis.WORKFLOW_REUSE)
    assert reuse_pair.axis is AblationAxis.WORKFLOW_REUSE

    memory_off = run(Route.PLAN_JEV_SEMANTIC, context=trial(memory_mode="disabled"), jev=1)
    memory_on = run(Route.PLAN_JEV_SEMANTIC, context=trial(memory_mode="verified"), jev=1)
    memory_pair = compare_ablation(memory_off, memory_on, axis=AblationAxis.MEMORY)
    assert memory_pair.axis is AblationAxis.MEMORY
    memory_on_hot = run(
        Route.PLAN_JEV_SEMANTIC,
        context=trial(memory_mode="verified", workflow_mode="hot"),
        jev=1,
    )
    with pytest.raises(ValueError, match="memory_mode"):
        compare_ablation(cold, memory_on_hot, axis=AblationAxis.WORKFLOW_REUSE)


def test_user_simulator_cost_stays_separate_from_agent_cost():
    result = run(Route.REACT_SKILL_MCP)
    assert result.agent_cost_usd == Decimal("0")
    assert result.user_simulator_cost_usd == Decimal("0.03")


def test_public_trial_binds_manifest_id_without_changing_fairness_fingerprint():
    original = trial()

    public = PublicComparisonTrial.from_trial(original, "a" * 64)

    assert isinstance(public, ComparisonTrial)
    assert public.trial_id == "a" * 64
    assert public.fingerprint == original.fingerprint
    assert public.case_id == original.case_id


@pytest.mark.parametrize("trial_id", ["", "a" * 63, "A" * 64, "g" * 64])
def test_public_trial_rejects_noncanonical_manifest_identity(trial_id):
    with pytest.raises(ValueError, match="manifest trial ID"):
        PublicComparisonTrial.from_trial(trial(), trial_id)


@pytest.mark.parametrize("other", [Route.PLAN_JEV_SEMANTIC, Route.PLAN_PRIMARY_SEMANTIC])
def test_full_workflow_pairs_all_main_routes_including_jev(other):
    paired = compare_full_workflow(run(Route.REACT_SKILL_MCP), run(other))
    assert paired.right_route is other
    paired = compare_full_workflow(run(Route.PLAN_MCP), run(other))
    assert paired.right_route is other


def test_execution_only_replay_cannot_be_reported_as_cold_full_workflow_cost():
    replay = replace(run(Route.PLAN_MCP), evidence_scope="execution_only")
    with pytest.raises(ValueError, match="[Ff]ull.workflow"):
        compare_full_workflow(run(Route.REACT_SKILL_MCP), replay)


def test_actual_memory_fixture_identity_is_frozen_except_in_explicit_memory_ablation():
    original = replace(trial(memory_mode="verified"), memory_fixture_digest="1" * 64)
    changed = replace(original, memory_fixture_digest="2" * 64)
    with pytest.raises(ValueError, match="memory_fixture_digest"):
        compare_full_workflow(
            run(Route.REACT_SKILL_MCP, context=original), run(Route.PLAN_MCP, context=changed)
        )
    memory_off = run(Route.PLAN_MCP)
    memory_on = run(Route.PLAN_MCP, context=original)
    assert compare_ablation(memory_off, memory_on, axis="memory_mode").axis is AblationAxis.MEMORY


def test_paired_comparisons_require_the_same_exposure_group_and_resource_profile():
    original = trial()
    react = run(Route.REACT_SKILL_MCP, context=original)
    planned = run(Route.PLAN_MCP, context=original)
    assert compare_full_workflow(react, planned).case_id == original.case_id

    changed_exposure = replace(original, exposure_group="development_exposed")
    with pytest.raises(ValueError, match="exposure_group"):
        compare_full_workflow(react, run(Route.PLAN_MCP, context=changed_exposure))

    changed_resources = replace(original, resource_profile_digest="9" * 64)
    with pytest.raises(ValueError, match="resource_profile_digest"):
        compare_full_workflow(react, run(Route.PLAN_MCP, context=changed_resources))


def test_comparisons_reject_mixed_evidence_scope_and_identical_routes():
    with pytest.raises(ValueError, match="routes"):
        compare_full_workflow(run(Route.PLAN_MCP), run(Route.PLAN_MCP))
    with pytest.raises(ValueError, match="scope"):
        compare_semantic_judgment(
            run(Route.PLAN_JEV_SEMANTIC),
            replace(run(Route.PLAN_PRIMARY_SEMANTIC), evidence_scope="execution_only"),
        )


def test_paired_workflows_reject_different_models_reported_by_the_callbacks():
    left = replace(
        run(Route.REACT_SKILL_MCP), actual_models=(("primary", "provider/model-a"),)
    )
    right = replace(
        run(Route.PLAN_MCP), actual_models=(("primary", "provider/model-b"),)
    )

    with pytest.raises(ValueError, match="different actual primary model"):
        compare_full_workflow(left, right)

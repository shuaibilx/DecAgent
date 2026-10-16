"""Optional low certainty skips require an explicit, current owner-published rule."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from decagent.business import interventions
from decagent.business.access import AccessError
from decagent.business.decision_policy import DecisionSnapshot, gate_decision
from decagent.decisions.contracts import QUESTIONS, digest
from evals.integration.test_business_policy_store import sample as sample

pytestmark = pytest.mark.integration


def optional_case(database, sample, *, minimum=2, probability=0.6):
    question = QUESTIONS["memory_relevant"]
    policy = replace(
        sample["policy"],
        question_id=question.id,
        question_version=question.version,
        primitive=question.type,
        min_candidates=minimum,
        max_candidates=minimum,
        context=replace(sample["policy"].context, risk="optional_memory"),
    )
    report = sample["report"]
    report["binding"].update(
        question_id=question.id,
        question_version=question.version,
        primitive=question.type,
        min_candidates=minimum,
        max_candidates=minimum,
        context=dict(vars(policy.context)),
    )
    store, owner = sample["store"], sample["owner"]
    store.register_validation(owner, report, digest(report))
    store.register(owner, policy)
    policy = store.publish(owner, policy.policy_id, policy.policy_version)
    task, actor = sample["task"], sample["backend"].identity
    snapshot = DecisionSnapshot(
        policy.context,
        "a" * 64,
        digest(question.criteria),
        2,
        str(task["business_scope_id"]),
        "1",
        str(actor.employee_id),
        "1",
    )
    gate = gate_decision(
        question,
        {"type": "noul", "noul": probability},
        actual_model=policy.actual_model,
        snapshot=snapshot,
        current_snapshot=snapshot,
        policy=policy,
        now=datetime.now(UTC),
    )
    assert gate.reason == ("undecided_noul" if probability == 0.5 else "below_threshold")
    assert hasattr(interventions, "OptionalMemoryRules"), (
        "Explicit optional-memory rules service is missing"
    )
    rules = interventions.OptionalMemoryRules(database)
    options = {
        "rule_id": "optional-memory",
        "rule_version": "1",
        "question": question,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "context": policy.context,
        "reference": "fixture-reviewed-optional-rule",
        "valid_until": datetime.now(UTC) + timedelta(hours=1),
    }
    return rules, question, gate, options


def evaluate(rules, question, gate, **changes):
    options = {
        "purpose": "optional_memory",
        "active": True,
        "rule_id": "optional-memory",
        "rule_version": "1",
    }
    options.update(changes)
    return rules.evaluate(question, gate, **options)


def test_optional_memory_is_manual_by_default_and_skips_only_after_publication(database, sample):
    rules, question, gate, options = optional_case(database, sample)
    assert evaluate(rules, question, gate)["status"] == "manual"
    rules.publish(sample["owner"], **options)
    result = evaluate(rules, question, gate)
    assert result["status"] == "skipped"
    assert result["semantic_record"]["rule_id"] == "optional-memory"
    assert result["semantic_record"]["low_score_action"] == "skip"
    assert not result["primary_fallback"] and not result["grants_write_authority"]
    assert evaluate(rules, question, gate, purpose="business_decision")["status"] == "manual"
    assert evaluate(rules, question, gate, active=False)["status"] == "inactive"
    assert evaluate(rules, QUESTIONS["memory_write"], gate)["status"] == "manual"


def test_optional_publication_requires_current_owner_and_exact_published_policy(database, sample):
    rules, question, gate, options = optional_case(database, sample)
    for actor in (
        sample["backend"].browser,
        replace(sample["owner"], session_id="bearer:" + "d" * 64),
    ):
        with pytest.raises(AccessError):
            rules.publish(actor, **options)
    with pytest.raises(ValueError):
        rules.publish(sample["owner"], **{**options, "question": QUESTIONS["memory_write"]})
    with pytest.raises(ValueError):
        rules.publish(sample["owner"], **{**options, "policy_version": "missing"})
    rules.publish(sample["owner"], **options)
    with pytest.raises(ValueError):
        rules.publish(sample["owner"], **{**options, "reference": "silently-rewritten"})
    sample["store"].revoke(sample["owner"], options["policy_id"], "1", "withdrawn")
    assert evaluate(rules, question, gate)["status"] == "manual"


def test_optional_rule_rechecks_gate_freshness_expiry_model_and_revocation(database, sample):
    rules, question, gate, options = optional_case(database, sample)
    rules.publish(sample["owner"], **options)
    assert evaluate(rules, question, replace(gate, actual_model="jev-9.9.9"))["status"] == "manual"
    changed = replace(gate.current_snapshot, state_digest="changed")
    assert evaluate(rules, question, replace(gate, current_snapshot=changed))["status"] == "manual"
    assert evaluate(rules, question, replace(gate, answer=None))["status"] == "manual"
    rules.clock = lambda: datetime.now(UTC) + timedelta(days=2)
    assert evaluate(rules, question, gate)["status"] == "manual"
    rules.clock = lambda: datetime.now(UTC)
    rules.revoke(sample["owner"], "optional-memory", "1", "Withdraw optional skip")
    assert evaluate(rules, question, gate)["status"] == "manual"


def test_optional_tie_cannot_publish_a_policy_outside_its_candidate_range(database, sample):
    rules, _, _, options = optional_case(database, sample, minimum=3, probability=0.5)
    with pytest.raises(ValueError, match="policy"):
        rules.publish(sample["owner"], **options)

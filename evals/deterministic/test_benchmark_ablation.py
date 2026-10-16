"""Ablations exchange only a classifier; trusted execution gates remain shared."""

from dataclasses import replace

import httpx
import pytest
from test_policy_boundaries import context

from decagent.decisions import (
    DecisionContext,
    DecisionEngine,
    JevAdapter,
    MainLLMStrategy,
    RuleStrategy,
    build_request,
)
from decagent.integrations.github import PublicationBlocked, VerificationGate
from decagent.policy import Action, Outcome, govern


@pytest.mark.parametrize("name", ["rule", "main-llm", "jev"])
def test_strategy_replacement_preserves_hard_denial_and_publication_gate(name, tmp_path):
    def forbidden_call(*_):
        pytest.fail("A hard-denied action must never call a semantic model")

    with httpx.Client(transport=httpx.MockTransport(forbidden_call)) as client:
        strategies = {
            "rule": RuleStrategy(),
            "main-llm": MainLLMStrategy(forbidden_call, "qwen3.8-flash"),
            "jev": JevAdapter(
                api_key="fixture-only",
                client=client,
                provider="openrouter",
                model="~typesafe/jev-latest",
            ),
        }
        request = build_request(
            ("action_risk",),
            {"action": {"tool": "publish_draft"}, "authorization": {"publish_draft": False}},
            DecisionContext("t", "p", "code", "evidence", "policy", "auth"),
        )
        decision = DecisionEngine(strategies[name]).decide(request, hard_outcome="deny")
        assert decision.governance_outcome == "deny"
        policy = context(tmp_path)
        policy = replace(policy, authorization=replace(policy.authorization, publish_draft=False))
        publication = Action(
            "publish_draft", {"title": "fixture", "body": "fixture", "branch": "decagent/task-1"}
        )
        assert govern(publication, policy, semantic_advice="allow").outcome == Outcome.DENY
        assert (
            govern(
                Action("read_file", {"path": "../other-task/secret"}),
                policy,
                semantic_advice="allow",
            ).outcome
            == Outcome.DENY
        )
        with pytest.raises(PublicationBlocked):
            VerificationGate.check([], "a" * 40, "b" * 64)

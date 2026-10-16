"""Only controller-created frozen checker intents can access the image's verifier."""

from dataclasses import replace

import pytest
from test_policy_boundaries import context

from decagent.policy import Action, Outcome, govern
from decagent.tools.repair.session import validation_command


def test_model_cannot_select_reserved_verifier(tmp_path):
    command = validation_command(
        (("test_bug.py", "def test_bug(): assert True"),), ("test_bug.py",)
    )
    action = Action("run_command", {"argv": command.argv})
    decision = govern(action, context(tmp_path))
    assert decision.outcome == Outcome.DENY and decision.hard_denial


@pytest.mark.parametrize("flags", [("-mdecagent_check",), ("-Pmdecagent_check",)])
def test_combined_module_flags_cannot_select_reserved_verifier(tmp_path, flags):
    decision = govern(
        Action("run_command", {"argv": ("python", *flags, "e30=")}), context(tmp_path)
    )
    assert decision.outcome == Outcome.DENY and decision.hard_denial


def test_trusted_verification_still_obeys_code_trust_and_owner_review(tmp_path):
    command = validation_command(
        (("test_bug.py", "def test_bug(): assert True"),), ("test_bug.py",)
    )
    action = Action("run_command", {"argv": command.argv})
    approved_intent = govern(action, context(tmp_path), verification_intent=command)
    assert approved_intent.outcome == Outcome.REVIEW
    assert not approved_intent.hard_denial
    untrusted = replace(context(tmp_path), code_trust=None)
    assert govern(action, untrusted, verification_intent=command).outcome == (
        Outcome.CODE_TRUST_REQUIRED
    )


@pytest.mark.parametrize(
    "field,value", [("checker_hash", "a" * 64), ("collect_only", True), ("code_source", "baseline")]
)
def test_intent_mismatch_is_hard_denied(tmp_path, field, value):
    command = validation_command(
        (("test_bug.py", "def test_bug(): assert True"),), ("test_bug.py",)
    )
    action = Action("run_command", {"argv": command.argv})
    decision = govern(
        action, context(tmp_path), verification_intent=replace(command, **{field: value})
    )
    assert decision.outcome == Outcome.DENY and decision.hard_denial

"""Memory data and context are evidence, never authority."""

from dataclasses import replace

import pytest

from decagent.memory import Applicability, CandidateInput, ContextBudgetError, ContextBuilder
from decagent.storage.evidence import EvidenceStore


def candidate(tmp_path, **changes):
    evidence = EvidenceStore(tmp_path).write("independent test passed", "verification")
    value = CandidateInput(
        project_id="project",
        source_task_id="task",
        category="fact",
        content="The cache key includes tenant id",
        evidence=evidence,
        applicability=Applicability(key="cache.key", tags=("cache",), versions=("b" * 40,)),
        source_path="src/cache.py",
        source_blob="c" * 40,
        source_pr="https://github.com/o/r/pull/1",
        verification_ids=("verification",),
    )
    return replace(value, **changes)


def test_candidate_fingerprint_deduplicates_normalized_tags_and_binds_evidence(tmp_path):
    first = candidate(
        tmp_path,
        applicability=Applicability(
            key="cache.key", tags=("cache", "tenant", "cache"), versions=("b" * 40,)
        ),
    )
    second = replace(
        first,
        applicability=Applicability(
            key="cache.key", tags=("tenant", "cache"), versions=("b" * 40,)
        ),
    )
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != replace(first, source_blob="d" * 40).fingerprint


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "guess"},
        {"evidence": None},
        {"content": ""},
        {"sensitivity": "unknown"},
        {"speculative": True},
        {"source_path": "../../secret"},
        {"source_blob": "invalid"},
    ],
)
def test_candidates_fail_closed_for_missing_support_and_unknown_sensitivity(tmp_path, changes):
    with pytest.raises(ValueError):
        candidate(tmp_path, **changes)


def test_applicability_never_treats_an_unknown_revision_as_current():
    scope = Applicability(key="cache", versions=("a" * 40,))
    assert scope.matches("a" * 40)
    assert not scope.matches("b" * 40)
    assert not scope.matches("")


def events():
    return [
        {"seq": 1, "kind": "task.goal", "payload": {"content": "Fix cache isolation"}},
        {"seq": 2, "kind": "authorization", "payload": {"content": "owner scope o/r only"}},
        {
            "seq": 3,
            "kind": "message",
            "payload": {"message": {"role": "user", "content": "old log " * 200}},
        },
        {
            "seq": 4,
            "kind": "message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                }
            },
        },
        {
            "seq": 5,
            "kind": "message",
            "payload": {
                "message": {"role": "tool", "tool_call_id": "call-1", "content": "cache evidence"}
            },
        },
        {"seq": 6, "kind": "approval.pending", "payload": {"content": "pending digest ABC"}},
        {"seq": 7, "kind": "verification.failed", "payload": {"content": "failure evidence XYZ"}},
    ]


def test_context_keeps_authority_pending_approval_evidence_and_complete_tool_pairs():
    import copy

    source = events()
    original = copy.deepcopy(source)
    result = ContextBuilder().build(source, max_tokens=1200, output_reserve=100)
    assert result.token_count <= 1100
    assert source == original
    rendered = str(result.messages)
    assert "owner scope o/r only" in rendered
    assert "pending digest ABC" in rendered
    assert "failure evidence XYZ" in rendered
    calls = {call["id"] for message in result.messages for call in message.get("tool_calls", [])}
    outputs = {message["tool_call_id"] for message in result.messages if message["role"] == "tool"}
    assert calls == outputs
    assert result.summary.ranges == ((3, 3),)
    assert result.summary.version == "1"
    assert result.summary.authority == "untrusted_data"


def test_context_fails_when_preserved_records_exceed_budget():
    with pytest.raises(ContextBudgetError, match="preserved"):
        ContextBuilder().build(events(), max_tokens=50, output_reserve=20)


def test_incomplete_tool_call_is_not_sent_as_a_fabricated_result():
    source = events()[:5]
    source.pop()
    result = ContextBuilder().build(source, max_tokens=10000, output_reserve=100)
    assert not any(message.get("tool_calls") for message in result.messages)
    assert not any(message["role"] == "tool" for message in result.messages)


def test_context_can_compact_more_than_one_hundred_optional_audit_events():
    source = [{"seq": 1, "kind": "authorization", "payload": {"content": "owner scope"}}]
    source.extend(
        {"seq": index, "kind": "message", "payload": {"content": "old log " * 50}}
        for index in range(2, 202)
    )
    result = ContextBuilder().build(source, max_tokens=1500, output_reserve=100)
    assert result.token_count <= 1400
    assert 1 in result.selected_event_seqs
    assert result.summary.ranges


def test_tool_result_before_its_call_is_rejected():
    source = events()
    source[3]["seq"], source[4]["seq"] = source[4]["seq"], source[3]["seq"]
    with pytest.raises(ValueError, match="before"):
        ContextBuilder().build(source, max_tokens=10000, output_reserve=100)

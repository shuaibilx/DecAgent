"""Pinned original verifier on synthetic inputs; no tasks, Docker or model network."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.enterprise_environment import UpstreamScorerHook
from decagent.evaluation.business.enterprise_scoring import (
    _PINNED_ROOT,
    BudgetedEnterpriseJudge,
    EnterpriseOfficialScorer,
    EnterpriseScoringError,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_PINNED_ROOT / "benchmark/verifier.py").is_file(),
        reason="Pinned upstream source cache is optional; no replacement verifier is used",
    ),
]


@pytest.mark.parametrize("case", ["pass", "task_failure", "sql_error", "response_check"])
def test_original_verifier_synthetic_database_tool_and_response_checks(monkeypatch, case):
    from decagent.evaluation.business.enterprise_scoring import _load_pinned_verifier_types

    engine, _ = _load_pinned_verifier_types()
    assert engine.__module__ == "benchmark.verifier"
    real_client = httpx.AsyncClient
    requests = []

    def sql(request):
        requests.append(request)
        assert request.url.path == "/api/sql-runner"
        assert request.headers["x-database-id"] == "synthetic-db"
        assert request.headers["x-itsm-user-token"] == "synthetic-actor"
        return (
            httpx.Response(500)
            if case == "sql_error"
            else httpx.Response(
                200,
                json={"success": True, "data": [{"synthetic": 1}], "row_count": 1},
            )
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(sql),
            trust_env=False,
        ),
    )
    state = {"digest": digest("synthetic state"), "table_count": 1, "row_count": 1}
    client = SimpleNamespace(
        base_url="http://127.0.0.1:1",
        database_id="synthetic-db",
        context={"x-itsm-user-token": "synthetic-actor"},
        _get_auth_headers=lambda: {},
    )
    environment = SimpleNamespace(
        scorer_hook=lambda: UpstreamScorerHook(client, "synthetic-gym"),
        state_fingerprint=lambda: deepcopy(state),
    )

    class SyntheticJudge:
        requests = 0

        async def ainvoke(self, messages):
            assert "Actual synthetic answer." in messages[1].content
            self.requests += 1
            return SimpleNamespace(content='{"score":9,"reasoning":"synthetic"}')

        def usage(self):
            return {
                "requests": self.requests,
                "prompt_tokens": 10 * self.requests,
                "completion_tokens": 3 * self.requests,
                "cost_usd": Decimal("0.002") * self.requests,
                "seconds": 0.1 * self.requests,
            }

    judge = SyntheticJudge()
    configs = [
        {
            "verifier_type": "database_state",
            "gym_name": "synthetic-gym",
            "validation_config": {
                "query": "SELECT 1 AS synthetic",
                "expected_value": 0 if case == "task_failure" else 1,
            },
        },
        {
            "verifier_type": "tool_execution",
            "gym_name": "synthetic-gym",
            "validation_config": {"selected_tools": ["lookup_synthetic"], "minimum_tool_calls": 1},
        },
    ]
    if case == "response_check":
        configs.append(
            {
                "verifier_type": "response_check",
                "gym_name": "synthetic-gym",
                "validation_config": {
                    "sql_query": "SELECT 1 AS synthetic",
                    "comparison_prompt": "Check the synthetic value.",
                },
            }
        )
    scorer = EnterpriseOfficialScorer(
        SimpleNamespace(server_name="synthetic-gym", verifiers=tuple(configs)),
        judge_client=BudgetedEnterpriseJudge(judge, judge.usage),
    )
    response = {
        "content": "Actual synthetic answer.",
        "tool_calls": [{"name": "lookup_synthetic", "args": {}}],
    }
    if case == "sql_error":
        with pytest.raises(EnterpriseScoringError):
            scorer.score_with_model_response(environment, response, initial_state_fingerprint=state)
        assert scorer.private_audit_evidence["scoring_status"] == "scoring_failure"
    else:
        score = scorer.score_with_model_response(
            environment,
            response,
            initial_state_fingerprint=state,
        )
        assert score.success is (case != "task_failure")
        assert score.official and score.forbidden_side_effects is None
        assert score.judge_requests == (1 if case == "response_check" else 0)
    assert requests and judge.requests == (1 if case == "response_check" else 0)
    assert "SELECT" not in repr(scorer.private_audit_evidence)
    assert "synthetic-actor" not in repr(scorer.private_audit_evidence)

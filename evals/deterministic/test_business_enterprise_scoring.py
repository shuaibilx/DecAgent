"""Private, offline contracts for the pinned EnterpriseOps verifier adapter."""

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from decagent.decisions.contracts import digest
from decagent.evaluation.business.enterprise_environment import UpstreamScorerHook
from decagent.evaluation.business.enterprise_scoring import (
    BudgetedEnterpriseJudge,
    EnterpriseOfficialScorer,
    EnterpriseScoringError,
)


class FakeEngine:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.judge_client = None

    async def execute_verifier(self, config, response, database_id, **kwargs):
        self.calls.append((config, deepcopy(response), database_id, kwargs))
        if config.verifier_type == "response_check":
            await self.judge_client.llm.ainvoke(
                [SimpleNamespace(type="human", content="synthetic verifier input")]
            )
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeEnvironment:
    def __init__(self, state):
        self.client = SimpleNamespace(
            database_id="private-db-id",
            context={"x-itsm-user-token": "private-context"},
        )
        self.state = state
        # This offline fixture owns no container, socket, or external resource.
        self.cleanup_verified = True

    def scorer_hook(self):
        return UpstreamScorerHook(self.client, "gym-itsm")

    def state_fingerprint(self):
        return deepcopy(self.state)


def spec(verifiers):
    return SimpleNamespace(server_name="gym-itsm", verifiers=tuple(verifiers))


def make_scorer(task_spec, outcomes, *, judge_client=None):
    engine = FakeEngine(outcomes)
    engine_builds = []

    def engine_factory(clients, supplied_judge):
        engine_builds.append((clients, supplied_judge))
        engine.judge_client = supplied_judge
        return engine

    def config_factory(**values):
        values = deepcopy(values)
        return SimpleNamespace(**{"name": None, "description": None, "gym_name": None, **values})

    scorer = EnterpriseOfficialScorer(
        task_spec,
        engine_factory=engine_factory,
        verifier_config_factory=config_factory,
        judge_client=judge_client,
    )
    return scorer, engine, engine_builds


def test_exact_hidden_configs_and_actual_response_go_only_to_original_verifier():
    configs = [
        {
            "verifier_type": "database_state",
            "gym_name": "gym-itsm",
            "name": "private-check",
            "validation_config": {
                "query": "SELECT count(*) FROM ticket",
                "expected_value": 1,
            },
        },
        {
            "verifier_type": "tool_execution",
            "gym_name": "gym-itsm",
            "validation_config": {"selected_tools": ["create_ticket"]},
        },
    ]
    response = {
        "content": "I created ticket INC-17.",
        "tool_calls": [{"name": "create_ticket", "args": {"summary": "Example"}}],
    }
    initial = {"digest": digest("initial private state"), "table_count": 4, "row_count": 12}
    post = {"digest": digest("post private state"), "table_count": 4, "row_count": 13}
    scorer, engine, engine_builds = make_scorer(spec(configs), [{"passed": True}, {"passed": True}])

    score = scorer.score_with_model_response(
        FakeEnvironment(post), response, initial_state_fingerprint=initial
    )

    assert score.success is True and score.official is True
    assert score.benchmark_score == 1.0
    assert score.forbidden_side_effects is None
    clients, supplied_judge = engine_builds[0]
    assert clients["gym-itsm"].database_id == "private-db-id"
    assert supplied_judge is None
    assert [
        {key: value for key, value in call[0].__dict__.items() if key in config}
        for call, config in zip(engine.calls, configs, strict=True)
    ] == configs
    assert [call[1] for call in engine.calls] == [response, response]
    assert all(call[2] == "private-db-id" for call in engine.calls)
    assert all(
        call[3]
        == {
            "context": {"x-itsm-user-token": "private-context"},
            "gym_name": "gym-itsm",
        }
        for call in engine.calls
    )

    evidence = scorer.private_audit_evidence
    assert evidence["initial_state_fingerprint"] == initial
    assert evidence["post_state_fingerprint"] == post
    assert evidence["verifier_count"] == evidence["passed_count"] == 2
    assert evidence["forbidden_side_effects"] == "not_measured"
    assert "SELECT count" not in repr(evidence)
    assert "expected_value" not in repr(evidence)
    assert "private-context" not in repr(score)
    assert "private-check" not in repr(score)


@pytest.mark.parametrize(
    ("outcomes", "success", "score"),
    [
        ([{"passed": True}, {"passed": True}], True, 1.0),
        ([{"passed": True}, {"passed": False}], False, 0.0),
    ],
)
def test_original_verifier_boolean_results_preserve_task_success_semantics(
    outcomes, success, score
):
    task_spec = spec(
        [
            {"verifier_type": "database_state", "validation_config": {"query": "SELECT 1"}},
            {
                "verifier_type": "tool_execution",
                "gym_name": "gym-itsm",
                "validation_config": {"minimum_tool_calls": 1},
            },
        ]
    )
    scorer, _, _ = make_scorer(task_spec, outcomes)

    result = scorer.score_with_model_response(
        FakeEnvironment({"digest": digest("post"), "table_count": 1, "row_count": 1}),
        {"content": "", "tool_calls": [{"name": "lookup", "args": {}}]},
        initial_state_fingerprint={"digest": digest("initial"), "table_count": 1, "row_count": 1},
    )

    assert result.success is success
    assert result.benchmark_score == score
    assert result.forbidden_side_effects is None


@pytest.mark.parametrize(
    "outcomes",
    [
        [RuntimeError("private verifier failure")],
        [{"passed": False, "error": "private SQL details"}],
        [{"actual": 0, "expected": 1}],
        [None],
    ],
)
def test_exception_error_or_invalid_verifier_payload_is_scoring_failure(outcomes):
    scorer, _, _ = make_scorer(
        spec([{"verifier_type": "database_state", "validation_config": {"query": "SELECT 1"}}]),
        outcomes,
    )

    with pytest.raises(EnterpriseScoringError):
        scorer.score_with_model_response(
            FakeEnvironment({"digest": digest("post"), "table_count": 1, "row_count": 1}),
            {"content": "", "tool_calls": []},
            initial_state_fingerprint={
                "digest": digest("initial"),
                "table_count": 1,
                "row_count": 1,
            },
        )

    assert scorer.private_audit_evidence["scoring_status"] == "scoring_failure"
    assert "private SQL details" not in repr(scorer.private_audit_evidence)


def test_response_check_requires_real_final_response_and_injected_judge():
    task_spec = spec(
        [
            {
                "verifier_type": "response_check",
                "gym_name": "gym-itsm",
                "validation_config": {
                    "sql_query": "SELECT note FROM ticket",
                    "comparison_prompt": "Check the actual response.",
                },
            }
        ]
    )
    state = {"digest": digest("post"), "table_count": 1, "row_count": 1}
    initial = {"digest": digest("initial"), "table_count": 1, "row_count": 1}
    scorer, _, _ = make_scorer(task_spec, [{"passed": True}])

    with pytest.raises(EnterpriseScoringError, match="final response"):
        scorer.score_with_model_response(
            FakeEnvironment(state),
            {"content": "", "tool_calls": []},
            initial_state_fingerprint=initial,
        )
    with pytest.raises(EnterpriseScoringError, match="judge"):
        scorer.score_with_model_response(
            FakeEnvironment(state),
            {"content": "Actual answer.", "tool_calls": []},
            initial_state_fingerprint=initial,
        )

    with pytest.raises(EnterpriseScoringError, match="budget-accounted"):
        make_scorer(task_spec, [{"passed": True}], judge_client=object())[
            0
        ].score_with_model_response(
            FakeEnvironment(state),
            {"content": "Actual answer.", "tool_calls": []},
            initial_state_fingerprint=initial,
        )

    class FakeJudge:
        def __init__(self):
            self.usage = {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": "0",
                "seconds": 0.0,
            }
            self.llm = self

        async def ainvoke(self, _messages):
            self.usage = {
                "requests": 1,
                "prompt_tokens": 9,
                "completion_tokens": 6,
                "cost_usd": "0.003",
                "seconds": 0.25,
            }
            return SimpleNamespace(content='{"score": 9, "reasoning": "synthetic"}')

        def usage_snapshot(self):
            return deepcopy(self.usage)

    raw_judge = FakeJudge()
    judge = BudgetedEnterpriseJudge(raw_judge.llm, raw_judge.usage_snapshot)
    scorer, _, engine_builds = make_scorer(task_spec, [{"passed": True}], judge_client=judge)
    result = scorer.score_with_model_response(
        FakeEnvironment(state),
        {"content": "Actual answer.", "tool_calls": []},
        initial_state_fingerprint=initial,
    )
    assert result.success is True
    assert engine_builds[0][1] is judge
    assert result.judge_requests == 1
    assert result.judge_tokens == 15
    assert str(result.judge_cost_usd) == "0.003"
    assert result.judge_seconds == 0.25


def enterprise_session_fixture(tmp_path, *, changed_state=False):
    from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
    from decagent.evaluation.business.comparison import ComparisonTrial
    from evals.deterministic.test_business_evaluation_catalog import tool_catalog

    state = {"digest": digest("fresh state"), "table_count": 1, "row_count": 1}
    environment = FakeEnvironment(state)
    environment.spec = SimpleNamespace(
        task_id="synthetic-enterprise",
        image="example@sha256:" + "a" * 64,
        platform="linux/amd64",
        context={},
        selected_tools=("lookup_case",),
        restricted_tools=(),
        server_name="gym-itsm",
        verifiers=(
            {
                "verifier_type": "tool_execution",
                "validation_config": {
                    "selected_tools": ["lookup_case"],
                    "minimum_tool_calls": 1,
                },
            },
        ),
    )
    environment.model_input = lambda: {
        "system_prompt": "Public policy.",
        "user_prompt": "Find the case.",
        "tools": tool_catalog(),
    }
    environment.calls = []

    def call_tool(name, arguments, *, timeout_seconds=None):
        assert timeout_seconds is None or timeout_seconds > 0
        environment.calls.append((name, deepcopy(arguments)))
        return {
            "content": [{"type": "text", "text": "case found"}],
            "structuredContent": {"case_id": "C-17"},
        }

    environment.call_tool = call_tool
    scope = EvaluationScope("enterpriseops-gym", "pinned", "synthetic-enterprise")
    scope.__enter__()
    catalog = EvaluationCatalog.from_mcp_tools(
        scope,
        tool_catalog(),
        benchmark_version=environment.spec.image,
        result_contract_version="enterprise-mcp-observation-v3",
    )
    trial = ComparisonTrial(
        dataset="enterpriseops-gym",
        dataset_version="pinned",
        dataset_checksum=digest("synthetic source"),
        case_id="synthetic-enterprise",
        branch_kind="structured",
        memory_mode="disabled",
        workflow_mode="cold",
        initial_state_digest=state["digest"],
        goal_digest=digest({"system_prompt": "Public policy.", "user_prompt": "Find the case."}),
        actor_profile_digest=digest({"context_identity": {}}),
        permission_digest=digest(
            {
                "selected_tools": ["lookup_case"],
                "restricted_tools": [],
                "effective_tools": ["lookup_case"],
            }
        ),
        tool_adapter_digest=digest(
            {
                "image": environment.spec.image,
                "platform": "linux/amd64",
                "mcp_protocol": "2024-11-05",
                "catalog_digest": catalog.digest,
            }
        ),
    )
    if changed_state:
        environment.state["digest"] = digest("different restored state")
    scorer, engine, _ = make_scorer(environment.spec, [{"passed": True}])
    return environment, scope, trial, scorer, engine


def test_enterprise_session_accepts_official_scorer_and_persists_private_evidence(tmp_path):
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    environment, scope, trial, scorer, engine = enterprise_session_fixture(tmp_path)
    try:
        session = EvaluationPlanSession.from_enterprise(
            environment,
            scope,
            trial,
            "fresh-isolation",
            journal_root=tmp_path,
            scorer=scorer,
        )
        try:
            session.call("lookup_case", {"user_id": "u-17"})
            session.set_final_response("Case found.")
            result = session.score()
            assert result.success and result.forbidden_side_effects is None
            assert engine.calls[0][1] == {
                "content": "Case found.",
                "tool_calls": [{"name": "lookup_case", "args": {"user_id": "u-17"}}],
            }
            evidence = session.repository.private_scoring_evidence()
            assert evidence == [session.private_score_evidence]
            assert evidence[0]["forbidden_side_effects"] == "not_measured"
            assert "validation_config" not in repr(session.task_input)
            assert "verifiers" not in repr(session.repository.context_snapshot())
        finally:
            session.close()
    finally:
        scope.__exit__(None, None, None)


def test_changed_fresh_database_is_rejected_before_journal_or_verifier(tmp_path):
    from decagent.evaluation.business.public_session import EvaluationPlanSession

    environment, scope, trial, scorer, engine = enterprise_session_fixture(
        tmp_path,
        changed_state=True,
    )
    try:
        with pytest.raises(ValueError, match="Fresh EnterpriseOps state"):
            EvaluationPlanSession.from_enterprise(
                environment,
                scope,
                trial,
                "wrong-state",
                journal_root=tmp_path,
                scorer=scorer,
            )
        assert not engine.calls and not environment.calls
        assert not list(tmp_path.rglob("*.sqlite3"))
    finally:
        scope.__exit__(None, None, None)


def test_judge_unknown_usage_and_partial_call_accounting_never_return_a_score():
    task_spec = spec([{"verifier_type": "response_check", "validation_config": {}}] * 2)
    state = {"digest": digest("state"), "table_count": 1, "row_count": 1}

    class Judge:
        requests = 0

        async def ainvoke(self, _messages):
            self.requests += 1
            return SimpleNamespace(content='{"score":9}')

        def usage(self):
            # Two real dispatches must not be reported as one measured request.
            return {
                "requests": min(self.requests, 1),
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost_usd": Decimal("0"),
                "seconds": 0.1,
            }

    raw = Judge()
    judge = BudgetedEnterpriseJudge(raw, raw.usage)
    scorer, _, _ = make_scorer(task_spec, [{"passed": True}] * 2, judge_client=judge)
    with pytest.raises(EnterpriseScoringError, match="usage"):
        scorer.score_with_model_response(
            FakeEnvironment(state),
            {"content": "Actual answer.", "tool_calls": []},
            initial_state_fingerprint=state,
        )


def test_unknown_judge_price_keeps_official_score_and_known_token_usage():
    task_spec = spec([{"verifier_type": "response_check", "validation_config": {}}])
    state = {"digest": digest("post"), "table_count": 1, "row_count": 1}

    class UnknownPriceJudge:
        requests = 0

        async def ainvoke(self, _messages):
            self.requests += 1
            return SimpleNamespace(content='{"score":9}')

        def usage(self):
            return {
                "requests": self.requests,
                "prompt_tokens": 7 * self.requests,
                "completion_tokens": 3 * self.requests,
                "cost_usd": None,
                "seconds": 0.2 * self.requests,
            }

    judge = UnknownPriceJudge()
    scorer, _, _ = make_scorer(
        task_spec,
        [{"passed": True}],
        judge_client=BudgetedEnterpriseJudge(judge, judge.usage),
    )

    score = scorer.score_with_model_response(
        FakeEnvironment(state),
        {"content": "Actual answer.", "tool_calls": []},
        initial_state_fingerprint=state,
    )

    assert score.official and score.success
    assert score.judge_requests == 1
    assert score.judge_tokens == 10
    assert score.judge_cost_usd is None
    observed = scorer.observed_judge_usage
    assert observed == {
        "requests": 1,
        "tokens": 10,
        "cost_usd": None,
        "seconds": pytest.approx(0.2),
        "complete": True,
    }


def test_controlled_route_binds_enterprise_judge_from_ledgered_adapter(tmp_path):
    from contextlib import contextmanager

    from decagent.evaluation.business.comparison import Route
    from decagent.evaluation.business.public_session import EvaluationPlanSession
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply

    environment, scope, trial, _, _ = enterprise_session_fixture(tmp_path)
    environment.spec.verifiers = ({"verifier_type": "response_check", "validation_config": {}},)
    scorer, engine, _ = make_scorer(environment.spec, [{"passed": True}])

    class Ledger:
        rows = []

        def model_attempts_snapshot(self, _trial_id, _route):
            return list(self.rows)

    class JudgeCallback:
        def __init__(self):
            self.ledger = Ledger()
            self.trial_id = "trial"
            self.route = "react_skill_mcp"

        def __call__(self, request):
            assert request["model"] == "synthetic-judge"
            assert request["messages"] == [
                {"role": "user", "content": "synthetic verifier input"}
            ]
            self.ledger.rows.append(
                {
                    "role": "judge",
                    "status": "settled",
                    "actual_input_tokens": 4,
                    "actual_output_tokens": 2,
                    "actual_cost_usd": "0.001",
                    "attempt_seconds": 0.1,
                }
            )
            return ModelReply(
                {"content": '{"score":9}', "tool_calls": []},
                tokens=6,
                cost_usd=Decimal("0.001"),
                model="synthetic-judge",
                input_tokens=4,
                output_tokens=2,
            )

    judge = JudgeCallback()

    @contextmanager
    def factory(frozen, isolation):
        session = EvaluationPlanSession.from_enterprise(
            environment,
            scope,
            frozen,
            isolation,
            journal_root=tmp_path,
            scorer=scorer,
        )
        try:
            yield session
        finally:
            session.close()

    def adapters(_session, _trial, _route):
        return {
            "react": lambda _request: ModelReply(
                {"finish": True, "message": "Actual final answer."},
                tokens=2,
                cost_usd=Decimal("0"),
                model="synthetic-primary",
            ),
            "official_judge": judge,
            "judge_model": "synthetic-judge",
        }

    try:
        result = ControlledRoutes(factory).run(
            Route.REACT_SKILL_MCP,
            trial,
            model_adapter_factory=adapters,
        )
        assert result.failure_kind is None, result.error
        assert result.score is not None and result.score.official and result.score.success
        assert result.usage.judge_requests == 1
        assert result.usage.judge_tokens == 6
        assert result.usage.judge_cost_usd == Decimal("0.001")
        assert len(engine.calls) == 1
    finally:
        scope.__exit__(None, None, None)


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_judge_dispatch_keeps_known_or_unknown_usage_in_private_audit(unknown):
    task_spec = spec([{"verifier_type": "response_check", "validation_config": {}}])
    state = {"digest": digest("state"), "table_count": 1, "row_count": 1}

    class FailingJudge:
        requests = 0

        async def ainvoke(self, _messages):
            self.requests += 1
            raise TimeoutError("private provider details")

        def usage(self):
            return {
                "requests": self.requests,
                "prompt_tokens": 7 if self.requests else 0,
                "completion_tokens": None if unknown and self.requests else 0,
                "cost_usd": "0.001" if self.requests else "0",
                "seconds": 0.2 if self.requests else 0.0,
            }

    raw = FailingJudge()
    scorer, _, _ = make_scorer(
        task_spec,
        [{"passed": True}],
        judge_client=BudgetedEnterpriseJudge(raw, raw.usage),
    )
    with pytest.raises(EnterpriseScoringError):
        scorer.score_with_model_response(
            FakeEnvironment(state),
            {"content": "Actual answer.", "tool_calls": []},
            initial_state_fingerprint=state,
        )
    observed = scorer.observed_judge_usage
    assert observed["requests"] == 1
    assert observed["complete"] is not unknown
    assert observed["tokens"] == (None if unknown else 7)
    assert observed["cost_usd"] == (None if unknown else Decimal("0.001"))
    assert scorer.private_audit_evidence["judge_usage"]["requests"] == 1
    assert "private provider" not in repr(scorer.private_audit_evidence)


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_official_judge_usage_is_retained_by_route_result(tmp_path, unknown):
    from decagent.evaluation.business.comparison import Route
    from decagent.evaluation.business.public_session import EvaluationPlanSession
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply

    environment, scope, trial, _, _ = enterprise_session_fixture(tmp_path)
    environment.spec.verifiers = ({"verifier_type": "response_check", "validation_config": {}},)

    class Ledger:
        def __init__(self):
            self.rows = []

        def model_attempts_snapshot(self, _trial_id, _route):
            return list(self.rows)

    class Judge:
        def __init__(self):
            self.ledger = Ledger()
            self.trial_id = "isolated-route-trial"
            self.route = Route.REACT_SKILL_MCP.value

        def __call__(self, request, *, timeout_seconds=None):
            assert request["model"] == "synthetic-judge"
            assert timeout_seconds is None
            self.ledger.rows.append(
                {
                    "role": "judge",
                    "status": "unknown" if unknown else "settled",
                    "actual_input_tokens": None if unknown else 4,
                    "actual_output_tokens": None if unknown else 0,
                    "actual_cost_usd": None if unknown else "0.001",
                    "attempt_seconds": 0.1,
                }
            )
            if unknown:
                raise TimeoutError("synthetic provider failure")
            return ModelReply(
                {"content": "synthetic judge result", "tool_calls": []},
                tokens=4,
                cost_usd=Decimal("0.001"),
                model="synthetic-judge",
                input_tokens=4,
                output_tokens=0,
            )

    judge = Judge()
    scorer, _, _ = make_scorer(
        environment.spec,
        [RuntimeError("synthetic verifier failure")],
    )
    audit = []

    @contextmanager
    def factory(frozen, isolation):
        session = EvaluationPlanSession.from_enterprise(
            environment,
            scope,
            frozen,
            isolation,
            journal_root=tmp_path,
            scorer=scorer,
        )
        try:
            yield session
        finally:
            audit.extend(session.repository.private_scoring_evidence())
            session.close()

    try:
        result = ControlledRoutes(factory).run(
            Route.REACT_SKILL_MCP,
            trial,
            official_judge=judge,
            judge_model="synthetic-judge",
            react=lambda _request: ModelReply(
                {"finish": True, "message": "Actual final answer."},
                tokens=2,
                cost_usd=Decimal("0"),
                model="synthetic-primary",
            ),
        )
        assert result.failure_kind == "scoring_failure"
        assert result.run is None and result.score is None
        assert result.usage.judge_requests == 1
        assert result.usage.judge_tokens == (0 if unknown else 4)
        assert result.usage.complete is not unknown
        assert result.usage.judge_cost_usd == (None if unknown else Decimal("0.001"))
        assert audit[0]["judge_usage"]["complete"] is not unknown
        assert "validation_config" not in repr(result)
    finally:
        scope.__exit__(None, None, None)


@pytest.mark.parametrize("mutation", ["changed", "missing", "extra"])
def test_modified_upstream_source_is_rejected_before_import(tmp_path, monkeypatch, mutation):
    import json
    from hashlib import sha256
    from zipfile import ZipFile

    from decagent.evaluation.business import enterprise_scoring as module

    benchmark = tmp_path / "evals/benchmarks/vendor_cache/enterpriseops-gym/benchmark"
    benchmark.mkdir(parents=True)
    archive = tmp_path / "source.zip"
    with ZipFile(archive, "w") as bundle:
        for name in ("__init__.py", "models.py", "verifier.py", "llm_client.py"):
            (benchmark / name).write_text("# pinned source\n", encoding="utf-8")
            bundle.writestr("upstream/benchmark/" + name, b"# pinned source\n")
    manifest = tmp_path / "evals/benchmarks/manifests/source_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "enterpriseops_gym": {
                    "source_repository": {
                        "archive": {
                            "path": "source.zip",
                            "sha256": sha256(archive.read_bytes()).hexdigest(),
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    if mutation == "changed":
        (benchmark / "llm_client.py").write_text("# altered\n", encoding="utf-8")
    elif mutation == "missing":
        (benchmark / "models.py").unlink()
    else:
        (benchmark / "extra.py").write_text("# extra\n", encoding="utf-8")
    monkeypatch.setattr(module, "_PINNED_ROOT", benchmark.parent)
    monkeypatch.setattr(
        module.importlib,
        "import_module",
        lambda _name: pytest.fail("Unverified source must never be imported"),
    )
    with pytest.raises(EnterpriseScoringError, match="source"):
        module._load_pinned_verifier_types()


def test_unaccounted_judge_stops_immediately_and_cannot_be_reused():
    state = {"digest": digest("state"), "table_count": 1, "row_count": 1}

    class UnaccountedJudge:
        dispatched = 0

        async def ainvoke(self, _messages):
            self.dispatched += 1
            return SimpleNamespace(content='{"score":9}')

        def usage(self):
            return {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": "0",
                "seconds": 0.0,
            }

    raw = UnaccountedJudge()
    scorer, _, _ = make_scorer(
        spec([{"verifier_type": "response_check", "validation_config": {}}] * 3),
        [{"passed": True}] * 3,
        judge_client=BudgetedEnterpriseJudge(raw, raw.usage),
    )
    for _ in range(2):
        with pytest.raises(EnterpriseScoringError):
            scorer.score_with_model_response(
                FakeEnvironment(state),
                {"content": "Actual answer.", "tool_calls": []},
                initial_state_fingerprint=state,
            )
        assert raw.dispatched == 1


@pytest.mark.parametrize("position", [0, 1, None])
def test_loading_verifier_preserves_caller_import_paths(tmp_path, monkeypatch, position):
    from decagent.evaluation.business import enterprise_scoring as module

    package = tmp_path / "benchmark"
    package.mkdir()
    for name in ("verifier.py", "models.py"):
        (package / name).write_text("# synthetic", encoding="utf-8")
    monkeypatch.setattr(module, "_PINNED_ROOT", tmp_path)
    monkeypatch.setattr(module, "_verify_pinned_source", lambda _root: None)
    modules = {
        "benchmark.verifier": SimpleNamespace(
            __file__=str(package / "verifier.py"),
            VerifierEngine=object,
        ),
        "benchmark.models": SimpleNamespace(
            __file__=str(package / "models.py"),
            VerifierConfig=object,
        ),
    }
    monkeypatch.setattr(module.importlib, "import_module", modules.__getitem__)
    before = ["caller-first", "caller-second"]
    if position is not None:
        before.insert(position, str(tmp_path))
    monkeypatch.setattr(module.sys, "path", before.copy())
    module._load_pinned_verifier_types()
    assert module.sys.path == before

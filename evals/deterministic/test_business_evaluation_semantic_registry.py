"""Reviewed semantic templates remain inside isolated benchmark capabilities."""

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal

import pytest

from decagent.business.contracts import PluginSpec
from decagent.business.plans.compiler import PlanCompileError, compile_plan
from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope
from decagent.business.plans.templates import QuestionRegistry
from decagent.business.tickets import builtin_manifest
from decagent.decisions.contracts import digest
from decagent.evaluation.business.semantic_templates import CSM_KNOWLEDGE_TEMPLATE_VERSION


def knowledge_tool(*, name="retrieve_knowledge", read_only=None):
    fields = {
        "knowledge_id": {"type": "integer"},
        "kb_number": {"type": "string"},
        "title": {"type": "string"},
        "state": {"type": "string", "enum": ["draft", "published", "retired"]},
        "visibility": {"type": "string", "enum": ["internal", "external"]},
        "product_id": {"type": "integer"},
        "owner_id": {"type": "integer"},
        "sys_created_on": {"type": "string"},
        "body": {"type": "string"},
        "score": {"type": "number"},
    }
    return {
        "name": name,
        "description": "Search published knowledge articles.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "search_text": {"type": "string"},
                "state": {"type": "string", "enum": ["draft", "published", "retired"]},
                "product_id": {"type": "integer"},
                "visibility": {"type": "string", "enum": ["internal", "external"]},
                "limit": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "array",
            "items": {"type": "object", "properties": fields},
        },
        "annotations": None if read_only is None else {"readOnlyHint": read_only},
    }


def catalog(tools=None, *, benchmark="enterpriseops-gym"):
    scope = EvaluationScope(benchmark, "pinned-v1", "csm-task")
    scope.__enter__()
    return scope, EvaluationCatalog.from_mcp_tools(
        scope,
        tools if tools is not None else [knowledge_tool()],
        benchmark_version="pinned-v1",
        result_contract_version="enterprise-mcp-observation-v2",
    )


def ref(source, *, node_id=None, path=(), value_type="string"):
    result = {
        "source": source,
        "path": list(path),
        "value_type": value_type,
        "nullable": False,
        "provenance": "user_input" if source == "task_input" else "tool_result",
    }
    if node_id is not None:
        result["node_id"] = node_id
    return result


def node(node_id, kind, **fields):
    return {
        "id": node_id,
        "type": kind,
        "depends_on": [],
        "on_missing": {"stop": "missing_input"},
        "on_error": {"stop": "error"},
        **fields,
    }


def semantic_plan(spec, registry, *, candidate_tool="retrieve_knowledge"):
    return {
            "schema_version": "1",
            "plan_id": "csm-semantic-choice",
            "revision": 1,
            "registry_fingerprint": registry.fingerprint,
            "entry": "search",
            "goal": {
                "description": "Choose an applicable article from observed knowledge results.",
                "evidence": [
                    {
                        "id": "articles",
                        "kind": "tool_result",
                        "description": "The exact upstream search response.",
                        "source": ref(
                            "node_output", node_id="search", path=["value"], value_type="array"
                        ),
                    }
                ],
            },
            "nodes": [
                node(
                    "search",
                    "read_tool",
                    tool={"plugin_id": spec.id, "command": candidate_tool},
                    inputs={
                        "search_text": {
                            "source": "literal",
                            "value": "network performance heavy load",
                            "value_type": "string",
                            "nullable": False,
                            "provenance": "plan_literal",
                        },
                        "state": {
                            "source": "literal",
                            "value": "published",
                            "value_type": "string",
                            "nullable": False,
                            "provenance": "plan_literal",
                        },
                    },
                    next=["choose"],
                ),
                node(
                    "choose",
                    "decide",
                    depends_on=["search"],
                    question={
                        "question_id": "csm_knowledge_article",
                        "version": CSM_KNOWLEDGE_TEMPLATE_VERSION,
                        "type": "choice",
                    },
                    policy={"policy_id": "benchmark-semantic", "version": "1"},
                    inputs={
                        "instruction": ref("task_input", path=["instruction"]),
                        "articles": ref(
                            "node_output", node_id="search", path=["value"], value_type="array"
                        ),
                    },
                    next=["done"],
                    on_no_match={"stop": "no_match"},
                ),
                node("done", "complete", depends_on=["choose"], evidence=["articles"]),
            ],
        }


def test_csm_template_is_registered_only_for_reviewed_isolated_catalog_and_renders_exact_result():
    scope, spec = catalog()
    try:
        registry = QuestionRegistry.from_spec(spec)
        definition = registry.get("csm_knowledge_article", CSM_KNOWLEDGE_TEMPLATE_VERSION)
        assert definition["candidate_source"] == "tool:retrieve_knowledge"
        assert definition["state_fields"] == ["instruction", "retrieval_arguments", "articles"]

        question = registry.render(
            "csm_knowledge_article",
            {
                "instruction": "For the network product issue, link the directly relevant article.",
                "retrieval_arguments": {
                    "search_text": "network performance heavy load",
                    "state": "published",
                },
                "articles": [
                    {
                        "knowledge_id": 7,
                        "kb_number": "KB000007",
                        "title": "Network performance",
                        "state": "published",
                        "visibility": "external",
                        "product_id": 73,
                        "body": "Troubleshooting steps.",
                        "score": 0.92,
                    }
                ],
            },
            node_id="choose",
            version=CSM_KNOWLEDGE_TEMPLATE_VERSION,
        )

        assert question.id == "choose"
        assert question.type == "choice"
        assert set(question.criteria) == {"article:7", "no_match"}
        assert question.state_fields == ("issue", "articles", "source")
    finally:
        scope.__exit__(None, None, None)


def test_csm_template_compiles_only_when_arguments_and_candidates_share_the_same_read_node():
    scope, spec = catalog()
    try:
        registry = QuestionRegistry.from_spec(spec)
        compiled = compile_plan(semantic_plan(spec, registry), spec)
        assert compiled.order == ("search", "choose", "done")

        invalid = deepcopy(semantic_plan(spec, registry))
        invalid["nodes"][1]["inputs"]["articles"]["node_id"] = "done"
        with pytest.raises((PlanCompileError, ValueError)):
            compile_plan(invalid, spec)

        limited = deepcopy(semantic_plan(spec, registry))
        limited["nodes"][0]["inputs"]["limit"] = {
            "source": "literal",
            "value": 10,
            "value_type": "integer",
            "nullable": False,
            "provenance": "plan_literal",
        }
        # Limited retrieval is valid as a tool call, but cannot feed this choice.
        with pytest.raises((PlanCompileError, ValueError)):
            compile_plan(limited, spec)
    finally:
        scope.__exit__(None, None, None)


@pytest.mark.parametrize(
    "tools",
    [
        [knowledge_tool(name="search_articles")],
        [knowledge_tool(read_only=False)],
        [
            {
                **knowledge_tool(),
                "inputSchema": {
                    **knowledge_tool()["inputSchema"],
                    "required": ["search_text", "state", "limit"],
                },
            }
        ],
    ],
)
def test_template_is_absent_when_the_upstream_capability_is_not_the_reviewed_contract(tools):
    scope, spec = catalog(tools)
    try:
        assert all(
            item["question_id"] != "csm_knowledge_article"
            for item in QuestionRegistry.from_spec(spec).describe()
        )
    finally:
        scope.__exit__(None, None, None)


def test_production_plugin_spec_cannot_register_the_benchmark_semantic_template():
    spec = PluginSpec.from_mapping(deepcopy(builtin_manifest()))

    assert all(
        item["question_id"] != "csm_knowledge_article"
        for item in QuestionRegistry.from_spec(spec).describe()
    )


def test_same_tool_name_in_another_benchmark_does_not_receive_enterpriseops_read_override():
    scope, spec = catalog(benchmark="tau-bench")
    try:
        assert spec.commands["retrieve_knowledge"].effect == "write"
        assert QuestionRegistry.from_spec(spec).describe() == []
    finally:
        scope.__exit__(None, None, None)


def test_controlled_plan_route_sends_same_verified_candidates_and_accepts_only_returned_id(
    tmp_path,
):
    from decagent.evaluation.business.comparison import ComparisonTrial, Route
    from decagent.evaluation.business.public_session import EvaluationPlanSession
    from decagent.evaluation.business.routes import ControlledRoutes, ModelReply, TaskScore

    tools = [knowledge_tool()]
    instruction = "For the network product issue, link the directly relevant article."
    facts = {name: digest(name) for name in (
        "initial_state_digest",
        "goal_digest",
        "actor_profile_digest",
        "permission_digest",
        "tool_adapter_digest",
    )}
    trial = ComparisonTrial(
        dataset="enterpriseops-gym",
        dataset_version="pinned-v1",
        dataset_checksum=digest("csm-dataset"),
        case_id="csm-task",
        branch_kind="semantic",
        memory_mode="disabled",
        workflow_mode="cold",
        **facts,
    )
    article = {
        "knowledge_id": 7,
        "kb_number": "KB000007",
        "title": "Network performance",
        "state": "published",
        "visibility": "external",
        "product_id": 73,
        "owner_id": 3,
        "sys_created_on": "2026-01-01T00:00:00Z",
        "body": "Troubleshooting steps.",
        "score": 0.92,
    }
    retrieval_args = {
        "search_text": "network performance heavy load",
        "state": "published",
    }

    class Environment:
        cleanup_verified = True

        def __init__(self):
            self.calls = []

        def call_tool(self, command, arguments, **_kwargs):
            self.calls.append((command, deepcopy(arguments)))
            return {
                "content": [{"type": "text", "text": "one published article"}],
                "structuredContent": [deepcopy(article)],
            }

    observed = {"sessions": [], "decisions": [], "requests": []}

    class TrackingSession(EvaluationPlanSession):
        def accept_decision(self, *args, **kwargs):
            result = super().accept_decision(*args, **kwargs)
            observed["decisions"].append(deepcopy(result.record))
            return result

    @contextmanager
    def factory(frozen, isolation_id):
        with EvaluationScope(frozen.dataset, frozen.dataset_version, frozen.case_id) as scope:
            public_catalog = EvaluationCatalog.from_mcp_tools(
                scope,
                tools,
                benchmark_version=frozen.dataset_version,
                result_contract_version="enterprise-mcp-observation-v2",
            )
            session = TrackingSession(
                Environment(),
                public_catalog,
                trial=frozen,
                isolation_id=isolation_id,
                journal_root=tmp_path,
                actor_id="eval:csm-task",
                task_input={"instruction": instruction},
                skill="Use exact results from the original benchmark tools.",
                boundary_facts=facts,
                scorer=lambda _environment: TaskScore(False, 0),
            )
            observed["sessions"].append(session)
            try:
                yield session
            finally:
                session.close()

    with EvaluationScope("enterpriseops-gym", "pinned-v1", "csm-task") as scope:
        spec = EvaluationCatalog.from_mcp_tools(
            scope,
            tools,
            benchmark_version="pinned-v1",
            result_contract_version="enterprise-mcp-observation-v2",
        )
        plan = semantic_plan(spec, QuestionRegistry.from_spec(spec))

    def semantic(request, *, timeout_seconds):
        observed.setdefault("semantic_deadlines", []).append(timeout_seconds)
        observed["requests"].append(deepcopy(request))
        probabilities = {key: 0.01 for key in request["question"]["criteria"]}
        probabilities["article:7"] = 0.99
        return ModelReply(
            {
                "type": "choice",
                "choice": "article:7",
                "confidence": 0.99,
                "probabilities": probabilities,
            },
            tokens=20,
            cost_usd=Decimal("0.0001"),
            model="scripted-jev",
        )

    result = ControlledRoutes(factory).run(
        Route.PLAN_JEV_SEMANTIC,
        trial,
        frozen_plan=plan,
        semantic=semantic,
        deadline_provider=lambda: 2.5,
    )

    assert result.status == "complete", result.error
    assert result.cleanup_verified is True
    assert result.usage.jev_requests == 1
    assert observed["semantic_deadlines"] == [2.5]
    assert result.terminal["final_response"]["mode"] == "deterministic"
    assert observed["sessions"][0].environment.calls == [
        ("retrieve_knowledge", retrieval_args)
    ]
    assert observed["requests"][0]["state"]["retrieval_arguments"] == retrieval_args
    assert observed["requests"][0]["state"]["articles"] == [article]
    accepted = observed["decisions"][0]
    assert accepted["kind"] == "decide"
    assert accepted["accepted"] is True
    assert accepted["value"]["knowledge_id"] == 7
    assert accepted["grants_write_authority"] is False

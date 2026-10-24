"""Trusted benchmark catalog stays isolated from the production plugin boundary."""

import json
from copy import deepcopy

import pytest

from decagent.business.contracts import validate_schema
from decagent.business.plans.contracts import Plan
from decagent.business.plans.templates import QuestionRegistry
from decagent.decisions.contracts import digest


def api():
    from decagent.business.plans.evaluation_catalog import EvaluationCatalog, EvaluationScope

    return EvaluationCatalog, EvaluationScope


def tool_catalog():
    return [
        {
            "name": "lookup_case",
            "description": "Look up a case for the supplied benchmark user.",
            "inputSchema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        }
    ]


def reference(source, *, node_id=None, path=(), value_type="string"):
    return {
        "source": source,
        "node_id": node_id,
        "path": list(path),
        "value_type": value_type,
        "nullable": False,
        "provenance": "tool_result" if source == "node_output" else "user_input",
    }


def catalog_plan(catalog):
    registry = QuestionRegistry.from_spec(catalog)
    return Plan.from_mapping(
        {
            "schema_version": "1",
            "plan_id": "public-case",
            "revision": 1,
            "registry_fingerprint": registry.fingerprint,
            "entry": "lookup",
            "goal": {
                "description": "Observe the case lookup result.",
                "evidence": [
                    {
                        "id": "case-id",
                        "kind": "tool_result",
                        "description": "The upstream result contains a case ID.",
                        "source": reference(
                            "node_output",
                            node_id="lookup",
                            path=["value", "case_id"],
                        ),
                    }
                ],
            },
            "nodes": [
                {
                    "id": "lookup",
                    "depends_on": [],
                    "on_missing": {"stop": "missing_input"},
                    "on_error": {"stop": "error"},
                    "type": "read_tool",
                    "tool": {"plugin_id": catalog.id, "command": "lookup_case"},
                    "inputs": {
                        "user_id": {
                            "source": "literal",
                            "value": "u-17",
                            "value_type": "string",
                            "nullable": False,
                            "provenance": "plan_literal",
                        }
                    },
                    "next": ["done"],
                },
                {
                    "id": "done",
                    "depends_on": ["lookup"],
                    "on_missing": {"stop": "missing_input"},
                    "on_error": {"stop": "error"},
                    "type": "complete",
                    "evidence": ["case-id"],
                },
            ],
        }
    )


def test_isolated_catalog_accepts_original_business_user_id_and_compiles_dynamic_result_path():
    EvaluationCatalog, EvaluationScope = api()
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        from decagent.business.plans.compiler import compile_plan

        compiled = compile_plan(catalog_plan(catalog), catalog)
        assert compiled.order == ("lookup", "done")
        catalog.validate("lookup_case", {"user_id": "u-17"})
        assert catalog.commands["lookup_case"].effect == "read"


def test_reviewed_enterprise_read_tool_is_enabled_only_for_its_pinned_schema(
    monkeypatch,
):
    from decagent.business.plans import evaluation_catalog as module

    EvaluationCatalog, EvaluationScope = api()
    image = "docker.io/example/enterpriseops-csm@sha256:" + "a" * 64
    tool = {
        "name": "find_product",
        "description": "Look up a product by exact name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"product_id": {"type": "integer"}},
        },
    }
    monkeypatch.setitem(
        module._REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS,
        (image, "find_product"),
        digest(tool),
    )

    with EvaluationScope("enterpriseops-gym", "csm-v1", "case-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version=image,
            result_contract_version="1",
        )
        assert catalog.commands["find_product"].effect == "read"

        drifted = deepcopy(tool)
        drifted["inputSchema"]["properties"]["name"]["minLength"] = 2
        changed = EvaluationCatalog.from_mcp_tools(
            scope,
            [drifted],
            benchmark_version=image,
            result_contract_version="1",
        )
        assert changed.commands["find_product"].effect == "write"


def test_pinned_csm_find_user_read_classification_requires_exact_image_and_schema():
    EvaluationCatalog, EvaluationScope = api()
    image = (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm"
        "@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189"
    )
    tool = {
        "name": "find_user",
        "description": (
            "Look up a user (portal customer, portal user or internal agent) by full name."
        ),
        "inputSchema": {
            "properties": {
                "name": {
                    "description": "Full name of user, e.g., 'Harry Middleton'.",
                    "type": "string",
                }
            },
            "required": ["name"],
            "type": "object",
        },
        "outputSchema": {
            "properties": {
                "active": {"description": "Whether the user account is active.", "type": "boolean"},
                "email": {"description": "Unique email address of the user.", "type": "string"},
                "first_name": {"description": "User's first name.", "type": "string"},
                "last_name": {"description": "User's last name.", "type": "string"},
                "location_id": {
                    "description": (
                        "Unique identifier of the location associated with this record. "
                        "This value references the primary key of the location in the "
                        "location table."
                    ),
                    "type": "integer",
                },
                "role": {
                    "description": "User role assigned from the predefined list.",
                    "enum": ["admin", "agent", "manager", "customer"],
                    "type": "string",
                },
                "user_id": {"description": "Unique identifier of the user.", "type": "integer"},
            },
            "type": "object",
        },
    }
    assert digest(tool) == "7515a3367bce8c157fa4c8738ea55b24e6983c0fd9787415abeaad8a252242d9"

    with EvaluationScope("enterpriseops-gym", "csm-v1", "case-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version=image,
            result_contract_version="enterprise-mcp-observation-v2",
        )
        assert catalog.commands["find_user"].effect == "read"

        drifted = deepcopy(tool)
        drifted["description"] += " Changed schema."
        changed = EvaluationCatalog.from_mcp_tools(
            scope,
            [drifted],
            benchmark_version=image,
            result_contract_version="enterprise-mcp-observation-v2",
        )
        assert changed.commands["find_user"].effect == "write"

        changed_image = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="docker.io/example/other-csm@sha256:" + "c" * 64,
            result_contract_version="enterprise-mcp-observation-v2",
        )
        assert changed_image.commands["find_user"].effect == "write"


def test_reviewed_enterprise_read_tool_does_not_override_an_explicit_write_hint(
    monkeypatch,
):
    from decagent.business.plans import evaluation_catalog as module

    EvaluationCatalog, EvaluationScope = api()
    image = "docker.io/example/enterpriseops-csm@sha256:" + "b" * 64
    tool = {
        "name": "find_product",
        "description": "Look up a product by exact name.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "outputSchema": None,
        "annotations": {"readOnlyHint": False},
    }
    monkeypatch.setitem(
        module._REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS,
        (image, "find_product"),
        digest(tool),
    )

    with EvaluationScope("enterpriseops-gym", "csm-v1", "case-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version=image,
            result_contract_version="1",
        )
        assert catalog.commands["find_product"].effect == "write"


def test_evaluation_scope_accepts_long_frozen_benchmark_case_ids_with_a_bounded_limit():
    _, EvaluationScope = api()
    long_case_id = "t" * 213
    with EvaluationScope("tau-bench", "96a3ef9", long_case_id) as scope:
        assert scope.case_id == long_case_id

    with pytest.raises(ValueError, match="case_id"):
        EvaluationScope("tau-bench", "96a3ef9", "t" * 257)


def test_evaluation_catalog_fingerprint_is_stable_across_trial_scopes():
    EvaluationCatalog, EvaluationScope = api()
    fingerprints = []
    for case_id in ("retail-17", "retail-18"):
        with EvaluationScope("tau-bench", "96a3ef9", case_id) as scope:
            catalog = EvaluationCatalog.from_mcp_tools(
                scope,
                tool_catalog(),
                benchmark_version="96a3ef9",
                result_contract_version="1",
            )
            fingerprints.append((catalog.digest, catalog.registry_fingerprint))
    assert fingerprints[0] == fingerprints[1]


def test_closed_evaluation_scope_cannot_compile_or_validate_tools():
    EvaluationCatalog, EvaluationScope = api()
    scope = EvaluationScope("tau-bench", "96a3ef9", "retail-17")
    with scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        plan = catalog_plan(catalog)
    from decagent.business.plans.compiler import compile_plan

    with pytest.raises(ValueError, match="scope|active|closed"):
        compile_plan(plan, catalog)
    with pytest.raises(ValueError, match="scope|active|closed"):
        catalog.validate("lookup_case", {"user_id": "u-17"})


def test_dynamic_result_path_is_checked_against_actual_runtime_value_type():
    EvaluationCatalog, EvaluationScope = api()
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        plan = catalog_plan(catalog)
        from decagent.business.plans.compiler import compile_plan

        compile_plan(plan, catalog)
        from decagent.business.plans.bindings import BindingContext, BindingInvalid, resolve_binding

        binding = deepcopy(plan.goal["evidence"][0]["source"])
        context = BindingContext(
            {"instruction": "look up"},
            {
                "lookup": {
                    "kind": "read_tool",
                    "verified": True,
                    "data": {"value": {"case_id": 17}},
                    "employee_id": "eval:retail-17",
                    "scope_revision": 1,
                }
            },
            {},
            {},
            "eval:retail-17",
            1,
        )
        with pytest.raises(BindingInvalid, match="type"):
            resolve_binding(binding, context)


def test_observation_keeps_original_mcp_response_and_derives_structured_value_view():
    EvaluationCatalog, EvaluationScope = api()
    original = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "case summary"}],
            "structuredContent": {"case_id": "C-17", "status": "open"},
        },
    }
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        observed = catalog.observe("lookup_case", original["result"])
        assert observed == {
            "raw": original["result"],
            "value": {"case_id": "C-17", "status": "open"},
            "is_error": False,
            "normalizations": [],
        }
        original["result"]["structuredContent"]["case_id"] = "changed"
        assert observed["raw"]["structuredContent"]["case_id"] == "C-17"


def test_null_structured_content_falls_back_to_json_text_for_output_validation():
    EvaluationCatalog, EvaluationScope = api()
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["case_id", "status"],
    }
    raw = {
        "content": [
            {"type": "text", "text": '{"case_id":"C-17","status":"open"}'}
        ],
        "structuredContent": None,
        "isError": False,
    }

    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )

        assert catalog.observe("lookup_case", raw) == {
            "raw": raw,
            "value": {"case_id": "C-17", "status": "open"},
            "is_error": False,
            "normalizations": [],
        }


@pytest.mark.parametrize(("wire_value", "expected"), [(0, False), (1, True)])
def test_declared_boolean_output_normalizes_database_bit_values(wire_value, expected):
    EvaluationCatalog, EvaluationScope = api()
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "type": "object",
        "properties": {"active": {"type": "boolean"}},
        "required": ["active"],
        "additionalProperties": False,
    }
    raw = {
        "content": [{"type": "text", "text": json.dumps({"active": wire_value})}],
        "isError": False,
    }

    with EvaluationScope("enterpriseops-gym", "csm-v1", "csm-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="csm-v1",
            result_contract_version="enterprise-mcp-observation-v2",
        )

        observed = catalog.observe("lookup_case", raw)
        validate_schema(catalog.commands["lookup_case"].result_schema, observed)

    assert observed["raw"] == raw
    assert observed["value"]["active"] is expected
    assert observed["normalizations"] == ["/active"]


def test_declared_boolean_output_does_not_coerce_other_integers():
    EvaluationCatalog, EvaluationScope = api()
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "type": "object",
        "properties": {"active": {"type": "boolean"}},
        "required": ["active"],
        "additionalProperties": False,
    }
    raw = {
        "content": [{"type": "text", "text": '{"active":2}'}],
        "isError": False,
    }

    with EvaluationScope("enterpriseops-gym", "csm-v1", "csm-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="csm-v1",
            result_contract_version="enterprise-mcp-observation-v2",
        )

        with pytest.raises(ValueError, match="boolean"):
            catalog.observe("lookup_case", raw)


def test_integer_union_is_not_normalized_to_boolean():
    EvaluationCatalog, EvaluationScope = api()
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "type": "object",
        "properties": {"active": {"type": ["boolean", "integer"]}},
        "required": ["active"],
    }
    raw = {
        "content": [{"type": "text", "text": '{"active":1}'}],
        "isError": False,
    }

    with EvaluationScope("enterpriseops-gym", "csm-v1", "csm-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="csm-v1",
            result_contract_version="enterprise-mcp-observation-v2",
        )

        observed = catalog.observe("lookup_case", raw)

    assert type(observed["value"]["active"]) is int
    assert observed["normalizations"] == []


def test_referenced_boolean_output_is_not_implicitly_normalized():
    EvaluationCatalog, EvaluationScope = api()
    tool = tool_catalog()[0]
    tool["outputSchema"] = {
        "$defs": {"active_flag": {"type": "boolean"}},
        "type": "object",
        "properties": {"active": {"$ref": "#/$defs/active_flag"}},
        "required": ["active"],
        "additionalProperties": False,
    }
    raw = {
        "content": [{"type": "text", "text": '{"active":1}'}],
        "isError": False,
    }

    with EvaluationScope("enterpriseops-gym", "csm-v1", "csm-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version="csm-v1",
            result_contract_version="enterprise-mcp-observation-v2",
        )

        with pytest.raises(ValueError, match="boolean"):
            catalog.observe("lookup_case", raw)


def test_evaluation_catalog_rejects_remote_json_schema_references():
    EvaluationCatalog, EvaluationScope = api()
    tools = tool_catalog()
    tools[0]["inputSchema"]["$ref"] = "https://example.com/schema.json"
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        with pytest.raises(ValueError, match="local"):
            EvaluationCatalog.from_mcp_tools(
                scope,
                tools,
                benchmark_version="96a3ef9",
                result_contract_version="1",
            )


def test_tau_result_object_is_preserved_as_json_transport_data():
    EvaluationCatalog, EvaluationScope = api()
    from decagent.evaluation.business.tau_tools import TauToolResult

    tool = {
        "type": "function",
        "function": {
            "name": "lookup_case",
            "description": "Look up a benchmark case.",
            "parameters": {
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
                "additionalProperties": False,
            },
        },
    }
    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope, [tool], benchmark_version="96a3ef9", result_contract_version="1"
        )
        result = TauToolResult('{"status":"open"}', False)
        assert catalog.observe("lookup_case", result) == {
            "raw": {"content": '{"status":"open"}', "is_error": False},
            "value": {"status": "open"},
            "is_error": False,
            "normalizations": [],
        }


def test_production_plugin_factory_still_rejects_non_service_mcp_plugins():
    from decagent.business.contracts import PluginSpec
    from decagent.business.tickets import builtin_manifest

    manifest = deepcopy(builtin_manifest())
    manifest["id"] = "tau-bench"
    manifest["version"] = "1.0.0"
    with pytest.raises(ValueError, match="service-tickets"):
        PluginSpec.from_mapping(manifest)


def test_plan_runtime_requires_separate_execution_finished_verifier_for_isolated_catalog():
    EvaluationCatalog, EvaluationScope = api()
    from decagent.business.plans.runtime import NodeResult, PlanRuntime

    class Journal:
        def __init__(self, catalog, plan):
            self.spec, self.task_id, self.plan = catalog, "eval:retail-17", plan
            self.rows, self.context = {}, {}

        def guard(self, _conn=None):
            return {
                "employee_id": self.task_id,
                "scope_revision": 1,
                "authorization_revision": 1,
                "authorization_snapshot": {"instruction": "Find the benchmark case."},
            }

        def context_snapshot(self):
            return deepcopy(self.context)

        def save_context(self, values):
            self.context.update(deepcopy(values))

        def _safe(self, value):
            return deepcopy(value)

        def current(self):
            return self.plan

        def nodes(self):
            return list(self.rows.values())

        def transition(self):
            pass

        def begin(self, node_id, inputs, _provenance):
            row = {
                "node_id": node_id,
                "node_digest": digest(next(n for n in self.plan.nodes if n["id"] == node_id)),
                "execution_generation": 1,
                "status": "running",
                "output_snapshot": {},
                "input_digest": digest(inputs),
                "dispatch_attempts": 0,
            }
            self.rows[node_id] = row
            return row

        def dispatch(self, node_id, *, max_attempts, generation):
            self.rows[node_id]["dispatch_attempts"] += 1

        def finish(self, node_id, status, output, *, generation):
            self.rows[node_id].update(status=status, output_snapshot=deepcopy(output))

    with EvaluationScope("tau-bench", "96a3ef9", "retail-17") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            tool_catalog(),
            benchmark_version="96a3ef9",
            result_contract_version="1",
        )
        plan = catalog_plan(catalog)
        repository = Journal(catalog, plan)

        def execute(node, inputs, context, _saved):
            return NodeResult(
                {
                    "kind": node["type"],
                    "verified": True,
                    "data": {"value": {"case_id": "C-17"}},
                    "inputs": deepcopy(inputs),
                    "employee_id": context.employee_id,
                    "scope_revision": context.scope_revision,
                    "account_revision": context.account_revision,
                }
            )

        with pytest.raises(ValueError, match="execution|verifier|evaluation"):
            PlanRuntime(repository, execute, task_input={"instruction": "Find the benchmark case."})

        runtime = PlanRuntime(
            repository,
            execute,
            task_input={"instruction": "Find the benchmark case."},
            completion_verifier=lambda _compiled, _node, _context: {
                "kind": "evaluation_execution_finished",
                "valid": True,
                "business_goal_verified": False,
            },
        )
        assert runtime.step()["status"] == "advanced"
        assert runtime.step()["status"] == "advanced"
        assert "done" in repository.rows, repository.rows
        terminal = runtime.step()
        assert terminal["status"] == "complete", terminal
        assert terminal["completion"]["kind"] == "evaluation_execution_finished"
        assert terminal["completion"]["business_goal_verified"] is False

        invalid_repository = Journal(catalog, plan)
        invalid_runtime = PlanRuntime(
            invalid_repository,
            execute,
            task_input={"instruction": "Find the benchmark case."},
            completion_verifier=lambda _compiled, _node, _context: {
                "kind": "evaluation_execution_finished",
                "valid": True,
                "business_goal_verified": True,
            },
        )
        assert invalid_runtime.step()["status"] == "advanced"
        with pytest.raises(ValueError, match="execution finished"):
            invalid_runtime.step()


def test_production_runtime_rejects_evaluation_only_completion_callback():
    from decagent.business.contracts import PluginSpec
    from decagent.business.plans.runtime import PlanRuntime
    from decagent.business.tickets import builtin_manifest

    repository = type("Repository", (), {})()
    repository.spec = PluginSpec.from_mapping(builtin_manifest())
    with pytest.raises(ValueError, match="limited to isolated evaluation"):
        PlanRuntime(
            repository,
            lambda *_args: None,
            completion_verifier=lambda *_args: {
                "kind": "evaluation_execution_finished",
                "valid": True,
                "business_goal_verified": False,
            },
        )


def test_pinned_csm_find_entitlements_preserves_documented_nullable_product_id(monkeypatch):
    from decagent.business.plans import evaluation_catalog as module

    EvaluationCatalog, EvaluationScope = api()
    image = (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm"
        "@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189"
    )
    tool = {
        "name": "find_entitlements",
        "description": "Find entitlements with filters.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entitlement_id": {"type": "integer"},
                    "product_id": {
                        "type": "integer",
                        "description": (
                            "Product ID linked to the entitlement "
                            "(null means all products)."
                        ),
                    },
                },
            },
        },
    }
    monkeypatch.setitem(
        module._REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS,
        (image, "find_entitlements"),
        digest(tool),
    )
    monkeypatch.setitem(
        module._REVIEWED_ENTERPRISE_NULLABLE_OUTPUT_FIELDS,
        (image, "find_entitlements", digest(tool)),
        ("product_id",),
    )
    with EvaluationScope("enterpriseops-gym", "csm-v1", "case-1") as scope:
        catalog = EvaluationCatalog.from_mcp_tools(
            scope,
            [tool],
            benchmark_version=image,
            result_contract_version="enterprise-mcp-observation-v3",
        )
        observed = catalog.observe(
            "find_entitlements",
            [{"entitlement_id": 11, "product_id": None}],
        )

        assert catalog.commands["find_entitlements"].effect == "read"
        assert catalog.commands["find_entitlements"].upstream_output_schema[
            "items"
        ]["properties"]["product_id"]["type"] == "integer"
        assert catalog.commands["find_entitlements"].observation_output_schema[
            "items"
        ]["properties"]["product_id"]["type"] == ["integer", "null"]
        assert observed["raw"] == [{"entitlement_id": 11, "product_id": None}]
        assert observed["value"] == [{"entitlement_id": 11, "product_id": None}]


def test_csm_find_entitlements_compatibility_is_pinned_to_the_audited_tool_digest():
    from decagent.business.plans import evaluation_catalog as module

    image = (
        "docker.io/shivakrishnareddyma225/enterpriseops-gym-mcp-csm"
        "@sha256:eaa456ac9aa85728426e7d3813a0bbca0949d6a8695be30e26f03894e6e6b189"
    )
    source_digest = "cf9941b75174a6cb699916957f63b6fb566a83b520bc173d441e869f3586ff9c"

    assert (
        module._REVIEWED_ENTERPRISE_READ_TOOL_DIGESTS[(image, "find_entitlements")]
        == source_digest
    )
    assert module._REVIEWED_ENTERPRISE_NULLABLE_OUTPUT_FIELDS[
        (image, "find_entitlements", source_digest)
    ] == ("product_id",)
